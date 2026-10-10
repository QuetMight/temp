"""ATIF 轨迹解析与统计。

=============================================================================
为什么需要这个模块
=============================================================================
原流程的"过程评判"是链路B（improving-evaluation）：从 HTML 分析报告里挖
耗时、瓶颈、知识消费、幻觉。那套东西有 6 步工作流、动辄 1-3 小时，因为它要
从**非结构化的 agent 日志**里重建过程。

Harbor 已经把过程结构化成了 ATIF（/logs/agent/trajectory.json）。所以这里
不需要"重建"，只需要"读字段"：

    final_metrics.total_prompt_tokens     → token 用量
    final_metrics.total_cached_tokens     → 缓存命中的绝对量
    total_cached / total_prompt           → **缓存命中率**（Harbor 的 /compare 不给这个）
    steps[].llm_call_count 求和           → 真实推理轮次
    steps[].tool_calls                    → 工具使用分布与错误率
    steps[].timestamp                     → 分段耗时

**实现理念：轨迹是"结构化的过程证据"，不是"要分析的日志"。**
凡是 ATIF 里能直接读到的，绝不重新推导；只有 ATIF 不保证的字段
（例如工具调用是否失败）才用启发式，并且明确标注为启发式。

**这个模块被两处复用**：
  1. verifier 的 process 维度判分（打分）
  2. 宿主侧对比插件（跨模型对比轨迹数据）
两者读同一份实现，避免"判分口径"和"对比口径"漂移。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 判定一次工具调用是否失败的启发式标记。
# ATIF 没有标准的"工具失败"字段：provider 各自把错误写进 observation content。
# 这些标记覆盖常见形态；命中即计为一次疑似失败，并在 reasoning 里标注为启发式。
_ERROR_MARKERS: tuple[str, ...] = (
    "error:",
    "traceback (most recent call last)",
    "command not found",
    "no such file or directory",
    "permission denied",
    "failed to",
    '"is_error": true',
    '"status": "error"',
)


@dataclass
class TrajectoryStats:
    """一次 agent 运行的结构化过程统计。"""

    available: bool = False
    reason: str = ""
    n_steps: int = 0
    n_agent_steps: int = 0
    n_user_steps: int = 0
    n_system_steps: int = 0
    n_llm_calls: int = 0
    n_tool_calls: int = 0
    n_tool_calls_suspected_failed: int = 0
    tool_histogram: dict[str, int] = field(default_factory=dict)
    total_prompt_tokens: int | None = None
    total_completion_tokens: int | None = None
    total_cached_tokens: int | None = None
    total_cost_usd: float | None = None
    cache_hit_rate: float | None = None
    schema_version: str | None = None
    agent_name: str | None = None
    model_name: str | None = None

    @property
    def tool_error_rate(self) -> float | None:
        if not self.n_tool_calls:
            return None
        return round(self.n_tool_calls_suspected_failed / self.n_tool_calls, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "schema_version": self.schema_version,
            "agent_name": self.agent_name,
            "model_name": self.model_name,
            "n_steps": self.n_steps,
            "n_agent_steps": self.n_agent_steps,
            "n_user_steps": self.n_user_steps,
            "n_system_steps": self.n_system_steps,
            "n_llm_calls": self.n_llm_calls,
            "n_tool_calls": self.n_tool_calls,
            "n_tool_calls_suspected_failed": self.n_tool_calls_suspected_failed,
            "tool_error_rate": self.tool_error_rate,
            "tool_histogram": dict(sorted(self.tool_histogram.items())),
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_cached_tokens": self.total_cached_tokens,
            "total_cost_usd": self.total_cost_usd,
            "cache_hit_rate": self.cache_hit_rate,
        }


def load(path: Path) -> dict[str, Any] | None:
    """读 ATIF 文档。缺失或不可解析时返回 None（采集是 best-effort）。"""
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _looks_like_error(result: dict[str, Any]) -> bool:
    content = result.get("content")
    if isinstance(content, list):
        content = " ".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict)
        )
    if not isinstance(content, str):
        return False
    lowered = content.lower()
    return any(marker in lowered for marker in _ERROR_MARKERS)


def summarize(trajectory: dict[str, Any] | None) -> TrajectoryStats:
    """把 ATIF 文档压成给判分与对比用的统计量。

    防御式读取：ATIF 的 `extra` 字段是各 agent 的自定义元数据，结构不保证一致；
    `final_metrics` 是可选的，agent 不一定填。缺失一律留 None，不猜 0。
    """
    if not isinstance(trajectory, dict):
        return TrajectoryStats(available=False, reason="trajectory missing or invalid")

    steps = trajectory.get("steps")
    if not isinstance(steps, list) or not steps:
        return TrajectoryStats(available=False, reason="trajectory has no steps")

    agent = trajectory.get("agent") if isinstance(trajectory.get("agent"), dict) else {}
    final = (
        trajectory.get("final_metrics")
        if isinstance(trajectory.get("final_metrics"), dict)
        else {}
    )

    stats = TrajectoryStats(
        available=True,
        schema_version=trajectory.get("schema_version"),
        agent_name=agent.get("name"),
        model_name=agent.get("model_name"),
        n_steps=len(steps),
    )

    for step in steps:
        if not isinstance(step, dict):
            continue
        source = step.get("source")
        if source == "agent":
            stats.n_agent_steps += 1
        elif source == "user":
            stats.n_user_steps += 1
        elif source == "system":
            stats.n_system_steps += 1

        call_count = step.get("llm_call_count")
        if isinstance(call_count, int) and call_count > 0:
            stats.n_llm_calls += call_count
        elif source == "agent":
            # 老版本 ATIF（v1.7 之前）没有 llm_call_count；按"一个 agent step
            # 至少一次推理"下界估计，并在 schema_version 上留痕以便对照。
            stats.n_llm_calls += 1

        for call in step.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            stats.n_tool_calls += 1
            name = str(call.get("function_name") or "unknown")
            stats.tool_histogram[name] = stats.tool_histogram.get(name, 0) + 1

        observation = step.get("observation")
        if isinstance(observation, dict):
            for result in observation.get("results") or []:
                if isinstance(result, dict) and _looks_like_error(result):
                    stats.n_tool_calls_suspected_failed += 1

    prompt = final.get("total_prompt_tokens")
    cached = final.get("total_cached_tokens")
    completion = final.get("total_completion_tokens")
    cost = final.get("total_cost_usd")

    stats.total_prompt_tokens = prompt if isinstance(prompt, int) else None
    stats.total_completion_tokens = completion if isinstance(completion, int) else None
    stats.total_cached_tokens = cached if isinstance(cached, int) else None
    stats.total_cost_usd = float(cost) if isinstance(cost, (int, float)) else None

    # 缓存命中率 = cached / prompt。
    # ATIF 规定 prompt_tokens **包含**缓存命中（不是额外的），所以分母是 prompt，
    # 结果天然落在 [0, 1]。
    if stats.total_prompt_tokens and stats.total_cached_tokens is not None:
        stats.cache_hit_rate = round(
            stats.total_cached_tokens / stats.total_prompt_tokens, 4
        )

    return stats
