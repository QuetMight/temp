#!/usr/bin/env python3
"""模型换代对比报告 —— Harbor JobPlugin。

=============================================================================
这个模块解决什么
=============================================================================
需求：同一份任务、同一套 SDD 技能，**换模型**之后代码生成效果与轨迹数据
（token 用量 / 缓存命中率 / 执行时间）是变好还是变差，并给出 HTML 对比报告。

原流程的做法：同时启动两个子用例跑不同模型，评测后由 report-generator 生成
对比 HTML。移植后拆成两半：

  运行    → Harbor 的 Job。`agents:` 里放两个模型配置 × 一个 dataset，
            `job_plan.py` 的笛卡尔积天然产出"两个模型跑同样的任务"。
  报告    → 本插件。它在 job 结束时拿到**完整的 JobResult**（含每个 trial 的
            TrialResult），聚合成对比报告。

=============================================================================
为什么是 JobPlugin，而不是一个独立脚本
=============================================================================
Harbor 的 JobPlugin 协议恰好提供了这个位置：

    on_job_start(job)         → 能拿到 job.job_dir / job.id
    on_job_end(job_result)    → 能拿到全部 TrialResult（内存对象）

三个关键理由：

1. **数据完整性**。`jobs/<job>/result.json` 落盘时是
   `exclude={"trial_results"}`（job.py 的三处 _write_job_result 都带
   exclude_trial_results=True），所以**从磁盘读不到 trial 明细**。
   只有内存里的 JobResult 有。独立脚本必须去遍历每个 trial 自己的
   result.json，多一层路径假设；插件直接拿到对象。

2. **时机正确**。`finalize_job_plugins()` 在 `job.run()` 返回之后、
   CLI 打印总结之前调用，此时所有 trial 都已结算。

3. **失败不伤人**。`finalize_job_plugins` 对每个插件单独 try/except 并
   只打 warning —— 报告生成失败绝不会让一次跑了几小时的评测作废。
   这个性质很重要：报告是**读取**，不该有**中断**的权限。

=============================================================================
实现理念
=============================================================================
1. **对照必须显式，不能靠顺序。**
   新/旧模型谁是基线不能靠"agents 列表里的先后"推断 —— 那太隐晦，而且
   Harbor 展开顺序还会被 n_attempts 影响。所以基线由 `baseline_model`
   参数显式指定；未指定时取第一个 agent 的模型，并在报告里写明
   "基线 = X（推断，建议显式指定）"。

2. **聚合口径与判分口径共用一份实现。**
   缓存命中率、token 汇总这些指标在 verifier 判分里也算过（process 维度）。
   如果报告另写一套，两个地方会漂移，而漂移在跨模型对比里会被误读成模型差异。
   所以这里 `sys.path` 加上 `tests/`，直接 `from _lib import ...` 复用同一份
   `_lib/trajectory.py`。

3. **缓存命中率必须"先求和再相除"，不能"各自求比再平均"。**
   `Σcache / Σinput` 与 `mean(cache_i / input_i)` 在有失败 trial 或 trial 长度
   差异很大时结果不同。前者是"整体缓存效率"，后者会被短 trial 支配。
   本实验要的是前者。

4. **缺失与零必须区分，且不参与平均。**
   某模型全部 trial 都没记录 token（agent 没填 AgentContext）时，token 列应当
   显示 "n/a"，而不是 0 —— 否则"没记录"会被读成"很省 token"。

5. **失败 trial 不静默丢弃，但也不污染均值。**
   异常 trial 的 reward 是 None，计入 `n_trials` 与 `n_errors`，但不进入
   reward 均值。报告里显式列出错误类型分布，因为"新模型更容易超时"本身
   就是一个重要结论。

6. **自包含 HTML，零外部依赖。**
   内嵌 CSS、无 CDN、无 JS 库。封闭内网环境里外链资源会静默失败，
   留下一份排版错乱的"报告"，比没有报告更糟。

7. **把口径风险写在报告里，而不是留在文档里。**
   跨厂商的缓存语义完全不同（Anthropic 是显式 cache_control，
   OpenAI 系是自动前缀缓存），缓存命中率**不可跨厂商直接比**。
   这类结论级的注意事项必须出现在报告页面底部，而不是埋在 README 里 ——
   看报告的人不会去翻 README。

=============================================================================
用法
=============================================================================
    PYTHONPATH=examples/tasks/datacom/compare \
    uv run harbor run \
        -c examples/tasks/datacom/compare/compare-job.yaml \
        --plugin model_comparison_plugin:ModelComparisonReport \
        --pk baseline_model=<老模型名>

产出（写到 job 目录）：
    comparison.json   机器可读的聚合结果
    comparison.html   人看的对比报告

`baseline_model` 也可以用 `PLUGIN.baseline_model=...` 的写法（多个插件时必需）。
"""

from __future__ import annotations

import html
import json
import statistics
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harbor.models.job.plugin import BaseJobPlugin

if TYPE_CHECKING:
    from harbor.job import Job
    from harbor.models.job.result import JobResult
    from harbor.models.trial.result import TrialResult

# ─────────────────────────────────────────────────────────────────────────────
# 复用判分链的共享实现（同一份源码，见 tests/_lib/__init__.py 的说明）
# ─────────────────────────────────────────────────────────────────────────────
_TESTS_DIR = Path(__file__).resolve().parent.parent / "tests"
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _lib import reward as reward_lib  # noqa: E402
from _lib import trajectory as trajectory_lib  # noqa: E402

# 主分数键：Harbor 读 reward.json 里的这个键。
MAIN_REWARD_KEY = "reward"


# ─────────────────────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TrialMetrics:
    """单个 trial 的可用指标（全部容忍缺失）。"""

    task: str
    trial_name: str
    rewards: dict[str, float] = field(default_factory=dict)
    error_type: str | None = None
    n_input_tokens: int | None = None
    n_cache_tokens: int | None = None
    n_output_tokens: int | None = None
    cost_usd: float | None = None
    agent_seconds: float | None = None
    total_seconds: float | None = None
    verifier_seconds: float | None = None
    # 来自 ATIF 轨迹的补充指标（可能全为 None）
    n_steps: int | None = None
    n_tool_calls: int | None = None
    tool_error_rate: float | None = None
    cache_hit_rate: float | None = None


@dataclass
class ModelGroup:
    """一个 (agent, model) 组合的全部 trial。"""

    label: str
    agent: str
    model: str | None
    trials: list[TrialMetrics] = field(default_factory=list)

    # ── 基础计数 ────────────────────────────────────────────────────────────
    @property
    def n_trials(self) -> int:
        return len(self.trials)

    @property
    def n_errors(self) -> int:
        return sum(1 for t in self.trials if t.error_type)

    def error_histogram(self) -> dict[str, int]:
        histogram: dict[str, int] = {}
        for trial in self.trials:
            if trial.error_type:
                histogram[trial.error_type] = histogram.get(trial.error_type, 0) + 1
        return dict(sorted(histogram.items()))

    # ── reward ──────────────────────────────────────────────────────────────
    def reward_keys(self) -> list[str]:
        keys: set[str] = set()
        for trial in self.trials:
            keys.update(trial.rewards)
        return sorted(keys)

    def reward_values(self, key: str) -> list[float]:
        return [t.rewards[key] for t in self.trials if key in t.rewards]

    def reward_mean(self, key: str) -> float | None:
        values = self.reward_values(key)
        return round(statistics.fmean(values), 4) if values else None

    def reward_stdev(self, key: str) -> float | None:
        values = self.reward_values(key)
        return round(statistics.stdev(values), 4) if len(values) > 1 else None

    def reward_coverage(self, key: str) -> float:
        """有该维度分数的 trial 占比 —— 用来识别"维度大面积缺失"的运行。"""
        if not self.trials:
            return 0.0
        return round(len(self.reward_values(key)) / len(self.trials), 4)

    # ── 通过条件（原体系 §7.1 的 AND 表达式）────────────────────────────────
    def pass_rate(self) -> float | None:
        """reward >= PASS_THRESHOLD 且 feature_completeness >= FLOOR 的占比。

        这正是"通过条件放在报告层"的落地处：看报告的人据此决定能不能上生产，
        而 reward.json 只负责如实报出各维度分数。
        """
        scored = [
            t
            for t in self.trials
            if MAIN_REWARD_KEY in t.rewards or "correctness" in t.rewards
        ]
        if not scored:
            return None
        passed = 0
        for trial in scored:
            main = trial.rewards.get(MAIN_REWARD_KEY)
            completeness = trial.rewards.get("correctness")
            if main is None or main < reward_lib.PASS_THRESHOLD:
                continue
            if completeness is not None and completeness < reward_lib.FEATURE_COMPLETENESS_FLOOR:
                continue
            passed += 1
        return round(passed / len(scored), 4)

    # ── token / 成本 ────────────────────────────────────────────────────────
    def _sum(self, attr: str) -> int | None:
        values = [getattr(t, attr) for t in self.trials if getattr(t, attr) is not None]
        return sum(values) if values else None

    def _mean(self, attr: str) -> float | None:
        values = [getattr(t, attr) for t in self.trials if getattr(t, attr) is not None]
        return round(statistics.fmean(values), 2) if values else None

    def total_input_tokens(self) -> int | None:
        return self._sum("n_input_tokens")

    def total_output_tokens(self) -> int | None:
        return self._sum("n_output_tokens")

    def total_cache_tokens(self) -> int | None:
        return self._sum("n_cache_tokens")

    def cache_hit_rate(self) -> float | None:
        """整体缓存命中率 = Σcache / Σinput。

        刻意不用"每个 trial 的命中率再取平均" —— 见模块头的实现理念 3。
        """
        total_input = self.total_input_tokens()
        total_cache = self.total_cache_tokens()
        if not total_input or total_cache is None:
            return None
        return round(total_cache / total_input, 4)

    def total_cost_usd(self) -> float | None:
        values = [t.cost_usd for t in self.trials if t.cost_usd is not None]
        return round(sum(values), 6) if values else None

    def mean_cost_usd(self) -> float | None:
        return self._mean("cost_usd")

    def mean_tokens_per_trial(self) -> float | None:
        total = self.total_input_tokens()
        if total is None or not self.n_trials:
            return None
        return round(total / self.n_trials, 1)

    # ── 耗时 ────────────────────────────────────────────────────────────────
    def mean_agent_seconds(self) -> float | None:
        return self._mean("agent_seconds")

    def mean_total_seconds(self) -> float | None:
        return self._mean("total_seconds")

    def mean_verifier_seconds(self) -> float | None:
        return self._mean("verifier_seconds")

    # ── 轨迹 ────────────────────────────────────────────────────────────────
    def mean_steps(self) -> float | None:
        return self._mean("n_steps")

    def mean_tool_calls(self) -> float | None:
        return self._mean("n_tool_calls")

    def mean_tool_error_rate(self) -> float | None:
        values = [t.tool_error_rate for t in self.trials if t.tool_error_rate is not None]
        return round(statistics.fmean(values), 4) if values else None


# ─────────────────────────────────────────────────────────────────────────────
# 从 TrialResult 提取指标
# ─────────────────────────────────────────────────────────────────────────────
def _seconds(timing: Any) -> float | None:
    """TimingInfo → 秒。缺任一端返回 None。"""
    started = getattr(timing, "started_at", None)
    finished = getattr(timing, "finished_at", None)
    if started is None or finished is None:
        return None
    try:
        return round((finished - started).total_seconds(), 2)
    except TypeError:
        return None


def _trial_dir(job_dir: Path, trial: TrialResult) -> Path:
    """定位 trial 目录。

    优先用 `job_dir / trial_name`（等于 TrialPaths 的构造方式）；
    拿不到 job_dir 时退回解析 trial_uri。
    """
    candidate = job_dir / trial.trial_name
    if candidate.is_dir():
        return candidate
    uri = getattr(trial, "trial_uri", None)
    if isinstance(uri, str) and uri.startswith("file:"):
        from urllib.parse import unquote, urlparse
        from urllib.request import url2pathname

        return Path(url2pathname(unquote(urlparse(uri).path)))
    return candidate


def _extract_trial_metrics(job_dir: Path, trial: TrialResult) -> TrialMetrics:
    n_input, n_cache, n_output, cost = trial.compute_token_cost_totals()
    metrics = TrialMetrics(
        task=trial.task_name,
        trial_name=trial.trial_name,
        rewards=dict(trial.verifier_result.rewards or {})
        if trial.verifier_result is not None
        else {},
        error_type=(
            trial.exception_info.exception_type if trial.exception_info else None
        ),
        n_input_tokens=n_input,
        n_cache_tokens=n_cache,
        n_output_tokens=n_output,
        cost_usd=cost,
        agent_seconds=_seconds(trial.agent_execution),
        total_seconds=_seconds(trial),
        verifier_seconds=_seconds(trial.verifier),
    )

    # 轨迹补充指标：与 verifier 判分用同一份 _lib/trajectory.py
    stats = trajectory_lib.summarize(
        trajectory_lib.load(_trial_dir(job_dir, trial) / "agent" / "trajectory.json")
    )
    if stats.available:
        metrics.n_steps = stats.n_steps
        metrics.n_tool_calls = stats.n_tool_calls
        metrics.tool_error_rate = stats.tool_error_rate
        metrics.cache_hit_rate = stats.cache_hit_rate
    return metrics


def _group_key(trial: TrialResult) -> tuple[str, str | None]:
    info = trial.agent_info
    model = info.model_info.name if info.model_info else None
    return info.name, model


def build_groups(job_result: JobResult, job_dir: Path) -> list[ModelGroup]:
    """按 (agent, model) 分组。

    Harbor 保证同一 job 里"同一 agent + 同一 model"面对的是同一份 dataset，
    所以这个分组天然是"两个模型跑同样的任务"。
    """
    groups: dict[tuple[str, str | None], ModelGroup] = {}
    for trial in job_result.trial_results:
        key = _group_key(trial)
        agent, model = key
        label = f"{agent}/{model}" if model else agent
        group = groups.setdefault(
            key, ModelGroup(label=label, agent=agent, model=model)
        )
        group.trials.append(_extract_trial_metrics(job_dir, trial))
    return sorted(groups.values(), key=lambda g: g.label)


# ─────────────────────────────────────────────────────────────────────────────
# 报告渲染
# ─────────────────────────────────────────────────────────────────────────────
_CSS = """
:root { color-scheme: light dark; }
body { font: 14px/1.55 -apple-system, "Segoe UI", "Noto Sans CJK SC", sans-serif;
       margin: 0 auto; max-width: 1180px; padding: 32px 20px 80px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 32px 0 10px; padding-bottom: 6px;
     border-bottom: 1px solid #8883; }
.sub { color: #6b7280; margin: 0 0 24px; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 4px; font-size: 13px; }
th, td { border: 1px solid #8883; padding: 6px 9px; text-align: right; }
th:first-child, td:first-child { text-align: left; }
thead th { background: #8881; font-weight: 600; }
.better { color: #067d17; font-weight: 600; }
.worse  { color: #b3261e; font-weight: 600; }
.flat   { color: #6b7280; }
.na     { color: #9ca3af; }
.baseline { background: #8881; }
.note { border-left: 3px solid #d97706; background: #d9770612;
        padding: 10px 14px; margin: 14px 0; border-radius: 0 4px 4px 0; }
.note h3 { margin: 0 0 6px; font-size: 13px; }
code { background: #8882; padding: 1px 5px; border-radius: 3px; font-size: 12px; }
ul { margin: 6px 0; padding-left: 22px; }
"""

# 指标方向：+1 越大越好，-1 越小越好。
# 用于 Δ 列的着色 —— 不做这个映射，"耗时 -12%" 会被误染成红色。
_METRIC_DIRECTION: dict[str, int] = {
    "reward": 1,
    "correctness": 1,
    "quality": 1,
    "process": 1,
    "pass_rate": 1,
    "cache_hit_rate": 1,
    "input_tokens": -1,
    "output_tokens": -1,
    "cost_usd": -1,
    "agent_seconds": -1,
    "total_seconds": -1,
    "steps": -1,
    "tool_calls": -1,
    "tool_error_rate": -1,
}


def _fmt(value: Any, *, digits: int = 3) -> str:
    if value is None:
        return '<span class="na">n/a</span>'
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return html.escape(str(value))


def _delta_cell(baseline: Any, current: Any, direction: int) -> str:
    """相对基线的变化。方向感知着色，并给出好坏标记。"""
    if baseline in (None, 0) or current is None:
        return '<span class="na">—</span>'
    try:
        change = (float(current) - float(baseline)) / abs(float(baseline))
    except (TypeError, ValueError, ZeroDivisionError):
        return '<span class="na">—</span>'
    if abs(change) < 0.005:
        return '<span class="flat">≈ 0.0%</span>'
    good = (change > 0) == (direction > 0)
    css = "better" if good else "worse"
    arrow = "↑" if change > 0 else "↓"
    return f'<span class="{css}">{arrow} {change:+.1%}</span>'


def _metric_rows(groups: list[ModelGroup], baseline: ModelGroup) -> list[tuple]:
    """(指标名, 方向, 取值函数) —— 总览表与 Δ 表共用一份定义，避免错位。"""
    return [
        ("主分数 reward", "reward", lambda g: g.reward_mean(MAIN_REWARD_KEY)),
        ("功能完整性", "correctness", lambda g: g.reward_mean("correctness")),
        ("代码质量", "quality", lambda g: g.reward_mean("quality")),
        ("性能轨迹", "process", lambda g: g.reward_mean("process")),
        ("通过率", "pass_rate", lambda g: g.pass_rate()),
        ("输入 token 合计", "input_tokens", lambda g: g.total_input_tokens()),
        ("输出 token 合计", "output_tokens", lambda g: g.total_output_tokens()),
        ("每 trial 输入 token", "input_tokens", lambda g: g.mean_tokens_per_trial()),
        ("缓存命中率", "cache_hit_rate", lambda g: g.cache_hit_rate()),
        ("成本合计 USD", "cost_usd", lambda g: g.total_cost_usd()),
        ("agent 平均耗时 s", "agent_seconds", lambda g: g.mean_agent_seconds()),
        ("trial 平均总耗时 s", "total_seconds", lambda g: g.mean_total_seconds()),
        ("平均步数", "steps", lambda g: g.mean_steps()),
        ("平均工具调用", "tool_calls", lambda g: g.mean_tool_calls()),
        ("工具疑似错误率", "tool_error_rate", lambda g: g.mean_tool_error_rate()),
    ]


def _render_overview(groups: list[ModelGroup], baseline: ModelGroup) -> str:
    head = "".join(f"<th>{html.escape(g.label)}</th>" for g in groups)
    rows = _metric_rows(groups, baseline)
    body = []
    for label, _direction, getter in rows:
        cells = []
        for group in groups:
            css = ' class="baseline"' if group is baseline else ""
            mark = "（基线）" if group is baseline else ""
            cells.append(f"<td{css}>{_fmt(getter(group))}{mark}</td>")
        body.append(f"<tr><td>{html.escape(label)}</td>{''.join(cells)}</tr>")
    return (
        "<h2>总览</h2><table><thead><tr><th>指标</th>"
        f"{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def _render_delta(groups: list[ModelGroup], baseline: ModelGroup) -> str:
    others = [g for g in groups if g is not baseline]
    if not others:
        return (
            "<h2>相对基线的变化</h2>"
            "<p class='sub'>本次 job 只有一个 agent/model 组合，无法对比。</p>"
        )
    head = "".join(
        f"<th>{html.escape(g.label)}<br><span class='sub'>相对 {html.escape(baseline.label)}</span></th>"
        for g in others
    )
    body = []
    for label, direction, getter in _metric_rows(groups, baseline):
        base_value = getter(baseline)
        cells = "".join(
            f"<td>{_delta_cell(base_value, getter(g), _METRIC_DIRECTION[direction])}</td>"
            for g in others
        )
        body.append(f"<tr><td>{html.escape(label)}</td>{cells}</tr>")
    return (
        "<h2>相对基线的变化</h2><table><thead><tr><th>指标</th>"
        f"{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def _render_task_matrix(groups: list[ModelGroup]) -> str:
    """task × model 的主分数矩阵 —— 识别"模型差异只集中在某个任务上"。"""
    tasks = sorted({t.task for g in groups for t in g.trials})
    if not tasks or not groups:
        return ""
    head = "".join(f"<th>{html.escape(g.label)}</th>" for g in groups)
    body = []
    for task in tasks:
        cells = []
        for group in groups:
            values = [
                t.rewards[MAIN_REWARD_KEY]
                for t in group.trials
                if t.task == task and MAIN_REWARD_KEY in t.rewards
            ]
            cells.append(
                f"<td>{_fmt(round(statistics.fmean(values), 4) if values else None)}</td>"
            )
        body.append(f"<tr><td>{html.escape(task)}</td>{''.join(cells)}</tr>")
    return (
        "<h2>分任务主分数</h2><table><thead><tr><th>任务</th>"
        f"{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def _render_reliability(groups: list[ModelGroup]) -> str:
    rows = []
    all_keys = sorted({k for g in groups for k in g.reward_keys()})
    for group in groups:
        errors = group.error_histogram()
        coverage = "、".join(
            f"{key} {group.reward_coverage(key):.0%}" for key in all_keys
        )
        stdev = group.reward_stdev(MAIN_REWARD_KEY)
        rows.append(
            "<tr>"
            f"<td>{html.escape(group.label)}</td>"
            f"<td>{group.n_trials}</td>"
            f"<td>{group.n_errors}</td>"
            f"<td>{_fmt(stdev)}</td>"
            f"<td>{html.escape(coverage) or 'n/a'}</td>"
            f"<td>{html.escape(json.dumps(errors, ensure_ascii=False)) if errors else '—'}</td>"
            "</tr>"
        )
    return (
        "<h2>可信度</h2><table><thead><tr><th>模型</th><th>trials</th>"
        "<th>异常</th><th>主分数 σ</th><th>维度覆盖</th><th>异常分布</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
        "<p class='sub'>σ 是在同模型同任务的多次尝试之间算的（需要 n_attempts &gt; 1）。"
        "维度覆盖低于 100% 说明该维度有 trial 没产出分数，"
        "此时均值只代表「有分数的那些 trial」。</p>"
    )


_CAVEATS = """
<div class="note">
  <h3>口径差异：缓存命中率不可跨厂商直接比较</h3>
  <p>不同厂商的 prompt 缓存语义完全不同：Anthropic 需要显式
  <code>cache_control</code> 断点，命中率强烈依赖 prompt 的组织方式；
  OpenAI 系是自动前缀缓存；部分自建网关根本没有缓存概念。
  所以「新模型缓存命中率 62% vs 老模型 41%」<b>不能</b>直接读成
  「新模型缓存更高效」——只有当两个模型来自同一厂商、走同一条链路时，
  这一列才有可比性。跨厂商时请把它当作"该链路下观测到的命中率"，
  而不是模型的属性。</p>
</div>
<div class="note">
  <h3>口径差异：token 计数口径</h3>
  <p>本报告的输入 token 是 <code>Σ n_input_tokens</code>，按 Harbor/ATIF 的定义
  <b>已包含</b>缓存命中部分；缓存命中率 = <code>Σcache / Σinput</code>，
  是先求和再相除（整体效率），不是各 trial 命中率的平均。</p>
  <p>如果某个模型的 token 列显示 <code>n/a</code>，说明它的 trial 没有回填
  <code>AgentContext</code>（agent 没有上报用量，也未从 ATIF
  <code>final_metrics</code> 回填）——这是"没记录"，不是"很省"。</p>
</div>
<div class="note">
  <h3>口径差异：耗时包含什么</h3>
  <p><b>agent 平均耗时</b>只算 agent 执行阶段（<code>TrialResult.agent_execution</code>）；
  <b>trial 平均总耗时</b>含环境构建、agent setup、判分。两者差值大时，
  说明环境/判分开销占比高，模型本身的差异被稀释了。</p>
</div>
<div class="note">
  <h3>关于位置偏差：本报告为什么不需要 Position Swap</h3>
  <p>原流程要为成对比较做 Position Swap（交换 A/B 位置各评一次再取平均），
  因为那里的"谁更好"是 <b>LLM 的主观判断</b>，会随呈现顺序变化。</p>
  <p>本报告的 Δ 是<b>确定性算术</b>：两侧都是固定的每模型分数，
  顺序只影响符号，不影响结论（表格同时给出 ↑/↓ 与好坏着色，
  不存在"看不出方向"的歧义）。真正需要偏差缓解的是<b>打分那一层</b>——
  它已经由 <code>tests/process/efficiency.toml</code> 的
  <code>samples = 3</code>（多次采样取中位数）处理，并可在
  <code>reward-details.json</code> 里查 <code>agreement</code>。</p>
</div>
"""


def render_html(
    *,
    title: str,
    groups: list[ModelGroup],
    baseline: ModelGroup,
    job_name: str,
    generated_at: str,
    warnings: list[str],
) -> str:
    warning_block = ""
    if warnings:
        items = "".join(f"<li>{html.escape(w)}</li>" for w in warnings)
        warning_block = (
            f'<div class="note"><h3>生成告警</h3><ul>{items}</ul></div>'
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<p class="sub">
  job <code>{html.escape(job_name)}</code> ·
  生成于 {html.escape(generated_at)} ·
  基线 <code>{html.escape(baseline.label)}</code> ·
  共 {len(groups)} 个模型 × {baseline.n_trials} 次尝试
</p>
{warning_block}
{_render_delta(groups, baseline)}
{_render_overview(groups, baseline)}
{_render_task_matrix(groups)}
{_render_reliability(groups)}
<h2>口径与注意事项</h2>
{_CAVEATS}
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# 插件
# ─────────────────────────────────────────────────────────────────────────────
class ModelComparisonReport(BaseJobPlugin):
    """在 job 结束时生成跨模型对比报告。

    Kwargs（通过 --pk key=value 或 --pk PLUGIN.key=value 传入）：
        baseline_model  基线模型名（默认取第一个 agent 的 model_name）
        output_name     HTML 文件名（默认 comparison.html）
        title           报告标题
    """

    def __init__(
        self,
        baseline_model: str | None = None,
        output_name: str = "comparison.html",
        title: str | None = None,
        **_ignored: Any,
    ) -> None:
        self.baseline_model = baseline_model
        self.output_name = output_name
        self.title = title
        self._job_dir: Path | None = None
        self._job_id: str | None = None
        self._first_agent_model: str | None = None
        self._warnings: list[str] = []

    # ── 生命周期 ────────────────────────────────────────────────────────────
    async def on_job_start(self, job: Job) -> None:
        """记下 job 目录与"默认基线"。

        on_job_start 的用途就在这里：JobResult 里**没有** job 目录字段，
        而报告要写到 job 目录；job.id 也只在这里拿得到最方便。
        """
        self._job_dir = Path(job.job_dir)
        self._job_id = str(job.id)
        for agent in job.config.agents:
            if agent.model_name:
                self._first_agent_model = agent.model_name
                break

    async def on_job_end(self, job_result: JobResult) -> None:
        job_dir = self._job_dir
        if job_dir is None:
            self._warnings.append("job_dir unavailable; report not written")
            return

        groups = build_groups(job_result, job_dir)
        if not groups:
            self._warnings.append("no trials found; report not written")
            return

        baseline, baseline_note = self._pick_baseline(groups)
        if baseline_note:
            self._warnings.append(baseline_note)
        self._warnings.extend(self._check_weight_consistency())

        generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        title = self.title or f"模型对比报告 · {job_dir.name}"

        payload = {
            "job_name": job_dir.name,
            "job_id": self._job_id,
            "generated_at": generated_at,
            "baseline": baseline.label,
            "pass_threshold": reward_lib.PASS_THRESHOLD,
            "feature_completeness_floor": reward_lib.FEATURE_COMPLETENESS_FLOOR,
            "warnings": self._warnings,
            "groups": [self._group_payload(g) for g in groups],
        }

        json_path = job_dir / "comparison.json"
        html_path = job_dir / self.output_name
        json_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        html_path.write_text(
            render_html(
                title=title,
                groups=groups,
                baseline=baseline,
                job_name=job_dir.name,
                generated_at=generated_at,
                warnings=self._warnings,
            ),
            encoding="utf-8",
        )

        print(f"[compare] 对比报告已生成: {html_path}")
        print(f"[compare] 机器可读结果:   {json_path}")
        if self._warnings:
            for warning in self._warnings:
                print(f"[compare] warning: {warning}")

    # ── 内部 ────────────────────────────────────────────────────────────────
    def _pick_baseline(self, groups: list[ModelGroup]) -> tuple[ModelGroup, str | None]:
        """挑基线。显式指定优先；否则用第一个 agent 的模型；再否则用第一组。"""
        if self.baseline_model:
            for group in groups:
                if group.model == self.baseline_model:
                    return group, None
            return groups[0], (
                f"baseline_model={self.baseline_model!r} 不在本次运行的模型里 "
                f"（实际: {[g.model for g in groups]}），已回退到 {groups[0].label}"
            )
        if self._first_agent_model:
            for group in groups:
                if group.model == self._first_agent_model:
                    return group, (
                        f"未显式指定 baseline_model，按 agents 列表顺序取 "
                        f"{self._first_agent_model!r} 为基线"
                    )
        return groups[0], (
            f"未显式指定 baseline_model，回退到 {groups[0].label}；"
            "建议用 --pk baseline_model=<模型名> 显式声明"
        )

    def _check_weight_consistency(self) -> list[str]:
        """交叉校验 tests/reward.toml 与 _lib/reward.py 的权重常量。

        两处权重无法共享（reward.toml 是声明式的），所以只能靠断言式检查把
        漂移暴露成告警 —— 权重不一致会让"总分"与"报告里按维度解读的结论"
        对不上，而这种不一致极难被察觉。
        """
        toml_path = _TESTS_DIR / "reward.toml"
        try:
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return [f"无法读取 {toml_path} 做权重校验: {exc}"]

        declared: dict[str, float] = {}
        for entry in data.get("reward", []):
            if isinstance(entry, dict) and entry.get("name") == MAIN_REWARD_KEY:
                declared = {
                    k: float(v) for k, v in (entry.get("weights") or {}).items()
                }
        if not declared:
            return [f"{toml_path} 里没有 name={MAIN_REWARD_KEY!r} 的权重声明"]

        warnings: list[str] = []
        for key, expected in reward_lib.DIMENSION_WEIGHTS.items():
            actual = declared.get(key)
            if actual is None:
                warnings.append(f"reward.toml 缺少维度权重 {key!r}")
            elif abs(actual - expected) > 1e-9:
                warnings.append(
                    f"权重不一致: reward.toml 的 {key}={actual} "
                    f"vs _lib/reward.py 的 {expected}"
                )
        return warnings

    @staticmethod
    def _group_payload(group: ModelGroup) -> dict[str, Any]:
        return {
            "label": group.label,
            "agent": group.agent,
            "model": group.model,
            "n_trials": group.n_trials,
            "n_errors": group.n_errors,
            "error_histogram": group.error_histogram(),
            "pass_rate": group.pass_rate(),
            "rewards": {
                key: {
                    "mean": group.reward_mean(key),
                    "stdev": group.reward_stdev(key),
                    "coverage": group.reward_coverage(key),
                }
                for key in group.reward_keys()
            },
            "tokens": {
                "input_total": group.total_input_tokens(),
                "output_total": group.total_output_tokens(),
                "cache_total": group.total_cache_tokens(),
                "cache_hit_rate": group.cache_hit_rate(),
                "input_per_trial": group.mean_tokens_per_trial(),
            },
            "cost": {
                "total_usd": group.total_cost_usd(),
                "mean_usd": group.mean_cost_usd(),
            },
            "timing": {
                "agent_mean_sec": group.mean_agent_seconds(),
                "trial_mean_sec": group.mean_total_seconds(),
                "verifier_mean_sec": group.mean_verifier_seconds(),
            },
            "trajectory": {
                "mean_steps": group.mean_steps(),
                "mean_tool_calls": group.mean_tool_calls(),
                "mean_tool_error_rate": group.mean_tool_error_rate(),
            },
        }
