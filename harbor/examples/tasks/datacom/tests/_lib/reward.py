"""判分数学：权重常量单一事实源 + 动态加权 + 扣分制 + 置信度校准。

=============================================================================
为什么单独一个模块
=============================================================================
原流程把权重散落在 `.cac/scripts/lib/_eval_weights.py`（常量）和 `_eval_math.py`
（算术矫正）两处，靠"唯一事实源"的纪律约束。移植后同样需要一个地方放：

  * 维度/子维度权重（reward.toml 里的权重必须与这里一致，改一处要改两处，
    所以这里只放"判分内部用到的"权重，不重复 reward.toml 的声明）；
  * 扣分制公式（LLM 代码审查的 10 个检查项）；
  * 缺失项重归一化（rewardkit 没有这个语义，必须自己实现）；
  * 置信度校准。

**实现理念：把"哪些分数缺失"当作一等公民。**
rewardkit 的聚合模式（weighted-mean / required-pass / …）对缺失项只有两种处理：
当 0 或直接门控失败。而原评分体系要求"缺失子维度忽略，剩余项按原比例重归一化"
——否则"没跑构建"会被当成"构建失败"，把分数压低并污染跨模型对比。
所以这条逻辑不能交给声明式配置，必须在 Python 里算完再报一个数。

**实现理念：置信度与分数分离。**
分数回答"做得多好"，置信度回答"这个分数有多可信"。两者混在一起会让
"证据不足"看起来像"做得差"。原体系把它们分开记录（confidence calibration），
这里通过 CriterionResult 的 `confidence` 字段原样保留到 reward-details.json。
"""

from __future__ import annotations

import math
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# 权重单一事实源
#
# 三个维度的权重同时写在 tests/reward.toml（声明式聚合）里，这里只做交叉校验，
# 不参与实际聚合 —— 避免出现两份会漂移的真相。
# ─────────────────────────────────────────────────────────────────────────────
DIMENSION_WEIGHTS: dict[str, float] = {
    "correctness": 0.50,   # 功能实现完整性
    "quality": 0.40,       # 代码质量
    "process": 0.10,       # 性能轨迹（产物类场景降权）
}

# code_quality 的三个子维度（原体系 §7.3）
SUB_WEIGHTS: dict[str, float] = {
    "model_quality": 0.5,
    "build_pass_rate": 0.3,
    "test_pass_rate": 0.2,
}

# performance_trace 的两个子维度（原体系 §7.4）
TRACE_WEIGHTS: dict[str, float] = {
    "execution_trace": 0.6,
    "response_quality": 0.4,
}

# 通过条件（原体系 §7.1）。Harbor 的加权平均表达不了"跨维度阈值 AND"，
# 所以这两个阈值在对比报告层使用，不在 reward.json 里。
PASS_THRESHOLD = 0.70
FEATURE_COMPLETENESS_FLOOR = 0.60

# LLM 代码质量审查：10 个检查项 + 各自权重（原体系 §7.3 B1b）
CODE_REVIEW_CHECKS: tuple[tuple[str, str, float], ...] = (
    ("public_interface_changed", "是否改变公共接口", 0.15),
    ("too_many_unrelated_files", "是否修改过多无关文件", 0.10),
    ("existing_logic_removed", "是否删除已有逻辑", 0.10),
    ("validation_relaxed", "是否放宽关键校验", 0.10),
    ("exceptions_swallowed", "是否吞掉异常", 0.10),
    ("default_behavior_changed", "是否改变默认行为", 0.10),
    ("unhandled_new_branch", "是否增加未经处理的新分支", 0.05),
    ("tests_modified_or_bypassed", "是否修改测试或绕过校验", 0.10),
    ("large_unrelated_refactor", "是否有大规模无关重构", 0.10),
    ("resource_security_concurrency_risk", "是否引入明显的资源/安全/并发风险", 0.10),
)

SEVERITY_FACTOR: dict[str, float] = {"high": 1.0, "medium": 0.6, "low": 0.3}

# 功能项判定系数（原体系 §7.2）
IMPLEMENTATION_FACTOR: dict[str, float] = {
    "fully_implemented": 1.0,
    "partially_implemented": 0.5,
    "not_implemented": 0.0,
    # unknown 不计入分母 —— 证据不足不该被迫猜一个分数。
}


# ─────────────────────────────────────────────────────────────────────────────
# 基础工具
# ─────────────────────────────────────────────────────────────────────────────
def clamp01(value: float) -> float:
    """夹到 [0, 1]。rewardkit 对越界分数只发 warning，不会拒绝，
    所以越界要靠我们自己挡，否则会把 reward.json 弄脏。"""
    if not is_finite(value):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def is_finite(value: Any) -> bool:
    """True 表示这是一个可用于 reward 的有限实数。

    刻意排除 bool —— Python 里 True 是 int 的子类，混进 reward.json 会让
    Harbor 的解析结果难以预期。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def round4(value: float | None) -> float | None:
    return None if value is None else round(float(value), 4)


def dynamic_weighted_mean(
    sub_scores: dict[str, float | None],
    weights: dict[str, float],
) -> tuple[float | None, dict[str, Any]]:
    """缺失项忽略、剩余项按原比例重归一化（原体系 §7.3 的动态加权机制）。

    Args:
        sub_scores: 子维度名 → 分数（None 表示该子维度本次不可得）。
        weights:    子维度名 → 权重（缺失的子维度也会被忽略）。

    Returns:
        ``(score, detail)``；全部子维度都缺失时 score 为 None，由调用方决定
        是降级还是给 0。
    """
    available = {
        name: score
        for name, score in sub_scores.items()
        if score is not None and is_finite(score) and name in weights
    }
    total_weight = sum(weights[name] for name in available)
    detail = {
        "sub_scores": {k: round4(v) for k, v in sub_scores.items()},
        "weights": weights,
        "available": sorted(available),
        "missing": sorted(set(sub_scores) - set(available)),
        "renormalized_weight_total": round(total_weight, 4),
    }
    if not available or total_weight <= 0:
        return None, detail
    score = sum(float(available[n]) * weights[n] for n in available) / total_weight
    return clamp01(score), detail


def code_review_deduction(
    violations: list[dict[str, str]],
) -> tuple[float, list[dict[str, Any]]]:
    """LLM 代码质量审查的扣分制（原体系 §7.3 B1b）。

    Args:
        violations: ``[{"check": <CODE_REVIEW_CHECKS 的键>, "severity": "high|medium|low",
                        "evidence": "<文件:行 或说明>"}]``

    扣分公式::

        deduction = Σ(weight × severity_factor[severity])
        llm_code_analysis = max(0, 1 - deduction)
    """
    weight_by_key = {key: weight for key, _, weight in CODE_REVIEW_CHECKS}
    detail: list[dict[str, Any]] = []
    total = 0.0
    for item in violations:
        key = str(item.get("check", ""))
        severity = str(item.get("severity", "medium")).lower()
        if key not in weight_by_key:
            continue  # 未知检查项直接忽略，不让 LLM 的自由发挥污染分数
        factor = SEVERITY_FACTOR.get(severity, SEVERITY_FACTOR["medium"])
        penalty = weight_by_key[key] * factor
        total += penalty
        detail.append(
            {
                "check": key,
                "severity": severity,
                "penalty": round(penalty, 4),
                "evidence": item.get("evidence", ""),
            }
        )
    return clamp01(1.0 - total), detail


def calibrate_confidence(base: float, **signals: bool | float) -> float:
    """置信度校准（原体系 §7.7）。

    ``base`` 是原始置信度，``signals`` 里的每个 True / 数值触发一次衰减：

        position_swap_inconsistent   ×0.6      （两轮结论不一致）
        few_evidence                 ×0.7~1.0  （证据条数不足）
        code_quality_missing_subdim  ×0.85
        performance_trace_missing    ×0.85
        rubric_missing               ×0.7      （回退到无 rubric 的判分）

    上限 0.99 —— 永远不给"完全确定"。
    """
    confidence = float(base)
    if signals.get("position_swap_inconsistent"):
        confidence *= 0.6
    evidence_count = signals.get("evidence_count")
    if isinstance(evidence_count, (int, float)) and evidence_count < 3:
        confidence *= 0.7 + 0.3 * (max(0.0, float(evidence_count)) / 3.0)
    if signals.get("code_quality_missing_subdim"):
        confidence *= 0.85
    if signals.get("performance_trace_missing"):
        confidence *= 0.85
    if signals.get("rubric_missing"):
        confidence *= 0.7
    return round(min(0.99, max(0.0, confidence)), 4)


def criterion_result(
    score: float | None,
    *,
    reasoning: str,
    confidence: float | None = None,
    model: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """组装 rewardkit 的 CriterionResult。

    rewardkit 只认 ``score`` / ``reasoning`` / ``confidence`` / ``model`` 四个键，
    且会写进 reward-details.json。其余细节塞进 ``reasoning`` 的可读文本里，
    不要新增顶层键（CriterionResult 是严格模型）。

    ``score=None`` 时给 0.0 —— rewardkit 需要每个 criterion 都有分数；
    "不可得"这件事由 confidence 与 reasoning 表达，而不是靠缺字段。
    """
    payload: dict[str, Any] = {
        "score": clamp01(score if score is not None else 0.0),
        "reasoning": reasoning
        + ("".join(f"\n[{k}] {v}" for k, v in extra.items()) if extra else ""),
    }
    if confidence is not None:
        payload["confidence"] = round(float(confidence), 4)
    if model:
        payload["model"] = model
    return payload
