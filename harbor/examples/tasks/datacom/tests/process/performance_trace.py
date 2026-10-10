"""维度 C-1：性能轨迹 ``performance_trace``（程序化部分，维度内权重 0.6）。

=============================================================================
模块用途
=============================================================================
原体系 §7.4 的 C1 execution_trace + C2 response_quality，改成从 **ATIF 轨迹**
直接计算。扣分制，基准 1.0。

    C1 执行轨迹
      低效行为次数        每次 -0.05，上限 -0.25
      瓶颈严重度          严重 -0.15 / 中等 -0.08
      工具调用错误率      >0.2 时 -0.10
      重复搜索/盲目试错   每类 -0.05，上限 -0.15
    C2 响应质量
      401/403 每次 -0.15（上限 -0.30）／500 每次 -0.10／429 每次 -0.05
      异常响应 -0.10／P95>60s -0.15／重试率>0.3 -0.10

=============================================================================
实现理念
=============================================================================
1. **ATIF 是结构化的过程证据，不需要"重建"。**
   原流程的链路B要花 1-3 小时，因为它从非结构化日志里重建过程。ATIF 已经把
   steps / tool_calls / metrics / timestamps 摆好了，所以这一维度是"读字段 +
   套公式"，秒级完成。这是移植后最省的一块。

2. **凡是有阈值判断的地方都必须留痕。**
   每个扣分项都记录"命中了什么、扣了多少、依据是什么"，写进 reasoning。
   过程分数最容易被质疑（"为什么我的 agent 只有 0.6"），没有留痕就无法解释。

3. **数据源缺失时给 None，不给 0。**
   C2 的数据源（原流程 collector 的 api_calls，含每次请求的状态码与延迟）
   在 ATIF 里**没有对应物** —— ATIF 的 metrics.extra 是各 agent 自定义的。
   所以 C2 只能"尽量挖，挖不到就记 data_gap 并让动态加权忽略它"。
   把不可得当 0 会系统性压低所有模型，正好毁掉这个 task 的目的。

4. **启发式必须标注。**
   "工具调用失败"在 ATIF 里没有标准字段，只能按 observation 内容里的错误
   特征串猜测。命中率不保证准确，所以 reasoning 里明确写 suspected / heuristic，
   并且在跨模型对比时它只能作为**同口径的相对指标**，不能当绝对事实。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from rewardkit import criterion

from _lib import evidence, llm, reward, trajectory

# 扣分上限（原体系 §7.4 的"上限"列）
_CAP_INEFFICIENCY = 0.25
_CAP_BOTTLENECK = 0.15
_CAP_REPEAT = 0.15
_CAP_AUTH = 0.30
_CAP_SERVER = 0.30
_CAP_RATE_LIMIT = 0.15
_CAP_ANOMALY = 0.20
_CAP_SLOW = 0.15
_CAP_RETRY = 0.10

# 瓶颈阈值：两个相邻 step 之间的间隔超过这些秒数即判为瓶颈。
# 取"严重 > 中等 > 忽略"三档，数值按"一次构建/测试通常的量级"设定，按需调整。
_BOTTLENECK_SEVERE_SEC = 600.0
_BOTTLENECK_MEDIUM_SEC = 300.0

# 疑似 API 错误的特征串（用于 C2 的尽力挖掘）。
_AUTH_MARKERS = ("401", "403", "unauthorized", "forbidden", "authentication")
_SERVER_MARKERS = ("500", "502", "503", "internal server error")
_RATE_LIMIT_MARKERS = ("429", "rate limit", "too many requests")
_ANOMALY_MARKERS = ("malformed", "invalid response", "unexpected response")


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _max_step_gap_sec(trajectory_doc: dict[str, Any]) -> float | None:
    """相邻 step 的最大时间间隔 —— 瓶颈的启发式代理。"""
    stamps = [
        stamp
        for stamp in (
            _parse_timestamp(step.get("timestamp"))
            for step in trajectory_doc.get("steps") or []
            if isinstance(step, dict)
        )
        if stamp is not None
    ]
    if len(stamps) < 2:
        return None
    return max(
        (later - earlier).total_seconds()
        for earlier, later in zip(stamps, stamps[1:], strict=False)
    )


def _repeated_call_classes(trajectory_doc: dict[str, Any]) -> int:
    """完全重复的工具调用（同函数 + 同参数）出现的"类"数。

    对应原体系的"重复搜索 / 盲目试错"：agent 反复用同样的输入调同样的工具，
    通常说明它在原地打转而不是在推进。
    """
    seen: dict[str, int] = {}
    for step in trajectory_doc.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for call in step.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            try:
                signature = json.dumps(
                    [call.get("function_name"), call.get("arguments")],
                    sort_keys=True,
                    ensure_ascii=False,
                )
            except (TypeError, ValueError):
                continue
            seen[signature] = seen.get(signature, 0) + 1
    return sum(1 for count in seen.values() if count > 1)


def _count_markers(trajectory_doc: dict[str, Any], markers: tuple[str, ...]) -> int:
    """在轨迹的 metrics.extra 与 observation 文本里数特征串命中次数。

    这是 C2 的"尽力挖掘"路径：ATIF 不保证记录 API 状态码，
    所以命中就计、不命中不猜 —— 命中为 0 时返回 0 而不是 None，
    因为"真的没出现错误"和"数据源不存在"在这里无法区分，
    由上层根据 final_metrics 是否存在来判断数据源可用性。
    """
    haystack: list[str] = []
    for step in trajectory_doc.get("steps") or []:
        if not isinstance(step, dict):
            continue
        metrics = step.get("metrics")
        if isinstance(metrics, dict):
            extra = metrics.get("extra")
            if isinstance(extra, dict):
                haystack.append(json.dumps(extra, ensure_ascii=False).lower())
        observation = step.get("observation")
        if isinstance(observation, dict):
            haystack.append(json.dumps(observation, ensure_ascii=False).lower())
    blob = "\n".join(haystack)
    return sum(blob.count(marker) for marker in markers)


def _execution_trace(
    stats: trajectory.TrajectoryStats, trajectory_doc: dict[str, Any]
) -> tuple[float, list[dict[str, Any]]]:
    deductions: list[dict[str, Any]] = []

    def deduct(name: str, penalty: float, cap: float, detail: str) -> None:
        deductions.append(
            {"item": name, "penalty": round(penalty, 4), "cap": cap, "detail": detail}
        )

    # 低效行为：用"疑似失败的工具调用"近似（启发式，见模块头理念 4）
    failed = stats.n_tool_calls_suspected_failed
    if failed:
        penalty = min(_CAP_INEFFICIENCY, 0.05 * failed)
        deduct(
            "inefficient_actions",
            penalty,
            _CAP_INEFFICIENCY,
            f"{failed} suspected failed tool calls (heuristic: error markers in observations)",
        )

    # 瓶颈
    gap = _max_step_gap_sec(trajectory_doc)
    if gap is not None and gap >= _BOTTLENECK_MEDIUM_SEC:
        penalty = (
            _CAP_BOTTLENECK if gap >= _BOTTLENECK_SEVERE_SEC else 0.08
        )
        severity = "severe" if gap >= _BOTTLENECK_SEVERE_SEC else "medium"
        deduct(
            "bottleneck",
            penalty,
            _CAP_BOTTLENECK,
            f"max gap between steps {gap:.0f}s ({severity})",
        )

    # 工具调用错误率
    error_rate = stats.tool_error_rate
    if error_rate is not None and error_rate > 0.2:
        deduct(
            "tool_error_rate",
            0.10,
            0.10,
            f"tool error rate {error_rate:.2%} > 20%",
        )

    # 重复搜索 / 盲目试错
    repeats = _repeated_call_classes(trajectory_doc)
    if repeats:
        penalty = min(_CAP_REPEAT, 0.05 * repeats)
        deduct(
            "repeated_calls",
            penalty,
            _CAP_REPEAT,
            f"{repeats} repeated (same tool + same arguments) call classes",
        )

    total = sum(item["penalty"] for item in deductions)
    return reward.clamp01(1.0 - total), deductions


def _response_quality(trajectory_doc: dict[str, Any]) -> tuple[float | None, dict[str, Any]]:
    """C2 响应质量：尽力从 ATIF 里挖 API 错误信号。

    数据源可用性以 final_metrics 是否存在为准 —— 这是 ATIF 里唯一有契约保证的
    汇总字段。没有它说明该 agent 根本没记录用量，C2 记 data_gap。
    """
    final = trajectory_doc.get("final_metrics")
    if not isinstance(final, dict):
        return None, {
            "skipped": True,
            "reason": "final_metrics absent: no usage data source in this trajectory",
        }

    deductions: list[dict[str, Any]] = []

    def count_and_deduct(
        name: str, markers: tuple[str, ...], per: float, cap: float
    ) -> None:
        hits = _count_markers(trajectory_doc, markers)
        if hits:
            deductions.append(
                {
                    "item": name,
                    "hits": hits,
                    "penalty": round(min(cap, per * hits), 4),
                    "cap": cap,
                }
            )

    count_and_deduct("auth_errors", _AUTH_MARKERS, 0.15, _CAP_AUTH)
    count_and_deduct("server_errors", _SERVER_MARKERS, 0.10, _CAP_SERVER)
    count_and_deduct("rate_limit_errors", _RATE_LIMIT_MARKERS, 0.05, _CAP_RATE_LIMIT)
    count_and_deduct("anomalous_responses", _ANOMALY_MARKERS, 0.10, _CAP_ANOMALY)

    total = sum(item["penalty"] for item in deductions)
    return reward.clamp01(1.0 - total), {
        "deductions": deductions,
        "note": (
            "ATIF has no standard API status field; signals are mined from "
            "metrics.extra and observation text (heuristic)"
        ),
    }


@criterion
def performance_trace(workspace: Path) -> dict:
    """C1 + C2 动态加权（缺失子维度忽略）。"""
    trajectory_doc = trajectory.load(evidence.TRAJECTORY_PATH)
    stats = trajectory.summarize(trajectory_doc)
    if not stats.available or trajectory_doc is None:
        return reward.criterion_result(
            None,
            reasoning=f"trajectory unavailable: {stats.reason}",
            confidence=0.0,
        )

    execution_trace, execution_detail = _execution_trace(stats, trajectory_doc)
    response_quality, quality_detail = _response_quality(trajectory_doc)

    score, weight_detail = reward.dynamic_weighted_mean(
        {
            "execution_trace": execution_trace,
            "response_quality": response_quality,
        },
        reward.TRACE_WEIGHTS,
    )
    missing = weight_detail["missing"]
    confidence = reward.calibrate_confidence(
        0.85,
        performance_trace_missing=bool(missing),
        evidence_count=stats.n_agent_steps,
    )
    return reward.criterion_result(
        score,
        reasoning=(
            f"steps={stats.n_steps} agent_steps={stats.n_agent_steps} "
            f"llm_calls={stats.n_llm_calls} tool_calls={stats.n_tool_calls} "
            f"tool_error_rate={stats.tool_error_rate} "
            f"cache_hit_rate={stats.cache_hit_rate} missing_subdims={missing}"
        ),
        confidence=confidence,
        execution_trace=execution_detail,
        response_quality=quality_detail,
        trajectory_stats=stats.to_dict(),
    )
