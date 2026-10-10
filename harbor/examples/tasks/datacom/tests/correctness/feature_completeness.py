"""维度 A：功能实现完整性 ``feature_completeness``（默认权重 0.50）。

=============================================================================
模块用途
=============================================================================
回答"这次实现是否满足了需求"，而不是"是否复现了参考答案"。

  数据源（按优先级）：
    1. /tests/ground-truth/rubric.md     百分制盲评标准（核心）
    2. /tests/ground-truth/contract.md   隐性行为契约（辅助参考）
    3. /tests/ground-truth/requirement.md 需求原文
    4. /app/**                            实现产物
    5. /logs/artifacts/changes.patch      变更范围（判断"是否真的动了代码"）

=============================================================================
实现理念
=============================================================================
1. **评分标准前置，而不是现编。**
   原流程用 rubric-extractor 在评测前预生成 rubric.md，把"评分标准"和
   "评分执行"彻底分开。移植后理由更强：rubric 是**跨模型对比的基准**，
   如果每次运行的标准都不一样，两个模型的分数就不可比。所以 rubric 是
   静态资产，烤进 verifier 镜像，运行期只读不改。

2. **逐项判定 + 自定义分母，而不是整体打分。**
   整体打分（"这个实现值几分"）方差极大，且无法解释。原体系的公式是
   ``Σ(item_score × item_points) / Σ(已判定项 item_points)``，
   关键是 ``unknown`` **不进分母** —— 证据不足时不强迫模型猜，只缩小分母。
   这让"我没看清"和"没实现"在分数上截然不同。

3. **分数与置信度分离。**
   如果每项都缺证据，分数可能仍然很高（小分母），但 confidence 会被压低。
   报告里两者并列展示，避免把"看不清"误读成"做得好"。

4. **rubric 缺失时降级而不是失败。**
   退回程序化证据（spec/plan 结构完整性 + 是否真的改了代码），
   并把 confidence 打 0.7 折、在 reasoning 里标注 data_gap。
   一次判分不该因为缺少一份可选资产而整体崩掉。

5. **保留验证标签。**
   rubric.md 里的 ``Verified / Static / Unverified / Contradicted`` 与分数
   **独立记录**：只能静态确认的实现不该被判成失败，但也不该伪装成已验证。
   标签随 reasoning 一起留档到 reward-details.json。
"""

from __future__ import annotations

import re
from pathlib import Path

from rewardkit import criterion

from _lib import evidence, llm, reward

RUBRIC_PATH = evidence.GROUND_TRUTH_DIR / "rubric.md"
CONTRACT_PATH = evidence.GROUND_TRUTH_DIR / "contract.md"
REQUIREMENT_PATH = evidence.GROUND_TRUTH_DIR / "requirement.md"

# 解析 rubric.md 的两种行：
#   "## 1. 维度名称 - 20"          分组 + 该组总分
#   "- 原子评分项描述: 8"          原子项 + 该项分值
_GROUP_RE = re.compile(r"^##\s+(?P<name>.+?)\s*-\s*(?P<points>\d+)\s*$")
_ITEM_RE = re.compile(r"^[-*]\s+(?P<text>.+?):\s*(?P<points>\d+)\s*$")

# rubric 缺失时的降级检查表（等权）。
# 这些只验证"过程产物是否齐全"，不能替代内容判定 —— 所以降级时置信度打折。
_FALLBACK_CHECKS: tuple[tuple[str, str], ...] = (
    ("spec_present", "spec.md 存在且包含接口契约与行为规则章节"),
    ("plan_present", "plan.md 存在且包含需求追溯表"),
    ("code_changed", "changes.patch 非空（确实改了代码而不是只写文档）"),
    ("tests_not_weakened", "changes.patch 未修改或删除既有测试"),
)

JUDGE_SYSTEM = (
    "你是一个严格的、实现无关的软件评审员。"
    "你评价候选实现是否满足**产品需求**，不评价它是否复现了某个参考答案。"
    "参考答案的证据优先级最低。"
    "证据不足时必须回答 unknown，绝对不要为了给出结论而猜测。"
)


def parse_rubric(text: str) -> tuple[list[dict[str, object]], list[str]]:
    """把 rubric.md 解析成原子评分项清单。

    Returns:
        ``(items, groups)``；items 每项含 ``group`` / ``text`` / ``points``。
        items 为空表示 rubric 不可用，调用方应走降级路径。
    """
    items: list[dict[str, object]] = []
    groups: list[str] = []
    current_group = ""
    for line in text.splitlines():
        stripped = line.strip()
        group_match = _GROUP_RE.match(stripped)
        if group_match:
            current_group = group_match.group("name")
            groups.append(current_group)
            continue
        item_match = _ITEM_RE.match(stripped)
        if item_match:
            items.append(
                {
                    "group": current_group,
                    "text": item_match.group("text"),
                    "points": int(item_match.group("points")),
                }
            )
    return items, groups


def _collect_evidence() -> dict[str, object]:
    """收集判分所需的证据（全部容忍缺失）。"""
    spec = evidence.read_text(evidence.WORKSPACE / "spec.md")
    plan = evidence.read_text(evidence.WORKSPACE / "plan.md")
    requirement = evidence.read_text(REQUIREMENT_PATH)
    contract = evidence.read_text(CONTRACT_PATH)
    changes = evidence.load_changes()

    # 只把变更涉及的文件名给 judge，不给正文：正文可能很大，而"改了哪些文件"
    # 已经足够判断实现是否落在正确的位置。
    touched = changes.get("files") or []
    return {
        "spec": spec,
        "plan": plan,
        "requirement": requirement,
        "contract": contract,
        "changes": changes,
        "touched_files": touched,
        "missing": [
            name
            for name, value in (
                ("spec.md", spec),
                ("plan.md", plan),
                ("requirement.md", requirement),
                ("changes.patch", changes.get("available")),
            )
            if not value
        ],
    }


def _build_prompt(items: list[dict[str, object]], ctx: dict[str, object]) -> str:
    listing = "\n".join(
        f'{index}. [{(item["points"])} 分] {item["text"]}'
        for index, item in enumerate(items, start=1)
    )
    return f"""请逐项判定下面的评分项是否在候选实现中被满足。

## 需求原文
{(ctx["requirement"] or "(缺失)")[:8000]}

## 隐性行为契约（辅助参考）
{(ctx["contract"] or "(缺失)")[:4000]}

## 候选实现的 spec.md
{(ctx["spec"] or "(缺失)")[:6000]}

## 候选实现的 plan.md
{(ctx["plan"] or "(缺失)")[:6000]}

## 变更涉及的文件
{ctx["touched_files"] or "(changes.patch 缺失或为空)"}

## 评分项
{listing}

## 输出格式（只输出 JSON，不要任何额外文字）
{{
  "items": [
    {{
      "index": 1,
      "verdict": "fully_implemented | partially_implemented | not_implemented | unknown",
      "validation": "Verified | Static | Unverified | Contradicted",
      "evidence": "一句话说明判定依据，指出具体文件或段落"
    }}
  ]
}}

判定口径：
- fully_implemented：有明确代码/文档证据，完全满足
- partially_implemented：基本满足但有明确遗漏
- not_implemented：未实现，或实现与要求逻辑相悖
- unknown：证据不足。**宁可 unknown，不要猜**。"""


@criterion
def feature_completeness(workspace: Path) -> dict:
    """按 rubric.md 逐项判定，套用原体系的加权公式。"""
    ctx = _collect_evidence()
    rubric_text = evidence.read_text(RUBRIC_PATH)
    items, groups = parse_rubric(rubric_text) if rubric_text else ([], [])
    degraded = not items

    if degraded:
        # ── 降级路径：rubric 不可用 ──────────────────────────────────────────
        items = [
            {"group": "fallback", "text": text, "points": 1}
            for _, text in _FALLBACK_CHECKS
        ]
        groups = ["fallback"]

    if not llm.judge_available():
        return reward.criterion_result(
            None,
            reasoning=(
                "judge unavailable: no credentials. "
                f"rubric_items={len(items)} groups={groups} "
                f"evidence_missing={ctx['missing']}"
            ),
            confidence=0.0,
        )

    try:
        payload, meta = llm.ask_json(
            _build_prompt(items, ctx), system=JUDGE_SYSTEM
        )
    except llm.JudgeUnavailable as exc:
        return reward.criterion_result(
            None,
            reasoning=f"judge unavailable: {exc}",
            confidence=0.0,
        )

    verdicts = {
        int(item["index"]): item
        for item in payload.get("items", [])
        if isinstance(item, dict) and isinstance(item.get("index"), int)
    }

    # ── 原体系公式：unknown 不进分母 ─────────────────────────────────────────
    earned = 0.0
    judged_points = 0
    per_group: dict[str, dict[str, float]] = {}
    labels: dict[str, int] = {}
    for index, item in enumerate(items, start=1):
        verdict = str(verdicts.get(index, {}).get("verdict", "unknown"))
        label = str(verdicts.get(index, {}).get("validation", "Unverified"))
        labels[label] = labels.get(label, 0) + 1
        factor = reward.IMPLEMENTATION_FACTOR.get(verdict)
        if factor is None:
            continue  # unknown：不计入分子也不计入分母
        points = int(item["points"])
        earned += factor * points
        judged_points += points
        bucket = per_group.setdefault(
            str(item["group"]), {"earned": 0.0, "points": 0}
        )
        bucket["earned"] += factor * points
        bucket["points"] += points

    if judged_points == 0:
        return reward.criterion_result(
            None,
            reasoning=(
                "judge marked every item unknown — no evidence to score against. "
                f"evidence_missing={ctx['missing']}"
            ),
            confidence=0.0,
            model=meta.get("model"),
        )

    score = earned / judged_points
    confidence = reward.calibrate_confidence(
        0.9,
        rubric_missing=degraded,
        evidence_count=len(items) - labels.get("Unverified", 0),
    )
    return reward.criterion_result(
        score,
        reasoning=(
            f"rubric items judged: {judged_points}/{sum(int(i['points']) for i in items)} points "
            f"({len(items)} items, {labels} verdict labels). "
            f"groups={sorted(per_group)} degraded={degraded}"
        ),
        confidence=confidence,
        model=meta.get("model"),
        evidence_missing=ctx["missing"],
        per_group={
            group: round(bucket["earned"] / bucket["points"], 4)
            for group, bucket in per_group.items()
            if bucket["points"]
        },
        judge_tokens=meta.get("total_tokens"),
    )
