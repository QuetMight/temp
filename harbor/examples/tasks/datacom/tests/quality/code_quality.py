"""维度 B：代码质量 ``code_quality``（默认权重 0.40）。

=============================================================================
模块用途
=============================================================================
回答"这次改动本身好不好"，与"是否满足需求"解耦。三个子维度（原体系 §7.3）：

    B1 model_quality      权重 0.5   code_similarity × 0.5 + llm_code_analysis × 0.5
                                     （无 code_similarity 时退化为纯 LLM 评分）
    B2 build_pass_rate    权重 0.3   task.json 的 compileCommands → 通过率
    B3 test_pass_rate     权重 0.2   task.json 的 focusedTests   → 通过率

=============================================================================
实现理念
=============================================================================
1. **动态加权是硬需求，所以这一维度不能拆成三个 criterion。**
   原体系要求"缺失子维度忽略，剩余项按原比例重归一化"。rewardkit 的聚合模式
   没有这个语义。如果拆成三个 criterion 交给声明式权重，那么"没配构建命令"
   会被当成"构建失败 0 分"，系统性压低所有模型的分数，并让跨模型对比失真。
   所以这里用**一个 criterion 内部算完**，对外只暴露一个 code_quality 分数。

2. **构建/测试是"证据"，不是"门槛"。**
   构建失败不中断判分，而是作为一个低分进入加权 —— 因为"构建失败"本身
   就是要测的信号之一（新模型可能写出编译不过的代码）。
   同时把命令输出尾部留档，让报告能解释分数为什么低。

3. **构建/测试的"缺失"与"失败"必须区分。**
   没有 compileCommands → 子维度为 None（忽略）；
   有命令但退出码非 0 → 子维度为 0.0（扣分）。这两者混淆会让对比结论完全反过来。

4. **代码审查用扣分制而不是加分制。**
   10 个检查项各自带权重，命中即按严重度扣分，基准 1.0。
   理由：加分制下"没检查到问题"和"没检查"无法区分，而扣分制天然把
   "没有明显问题"作为默认状态 —— 与人类评审的直觉一致。
   严重度系数 high/medium/low → 1.0/0.6/0.3，避免一个"低危"问题
   和一"高危"问题等价。

5. **代码相似度是"下界参考"，不是"标准答案匹配"。**
   原流程用 code-rating 引擎 + CodeHub MR 算相似度，依赖内网凭证与外部服务。
   移植后改成"相对 ground-truth/reference/ 的差异"，因为它可离线、可复现。
   它的作用是给 LLM 评分一个锚点（防止"写得天花乱坠"拿高分），
   而不是要求模型复刻参考实现。
"""

from __future__ import annotations

import difflib
from pathlib import Path

from rewardkit import criterion

from _lib import evidence, llm, reward

REFERENCE_DIR = evidence.GROUND_TRUTH_DIR / "reference"
BUILD_LOG_LIMIT = 2000

JUDGE_SYSTEM = (
    "你是一个严格的代码评审员，只依据给定的变更与源码判断质量风险。"
    "对每一个检查项，只有当你能指出具体证据时才判定为违规；"
    "不确定就判为不违规，并在 evidence 里说明你没能确认。"
)


# ─────────────────────────────────────────────────────────────────────────────
# B2 / B3：构建与测试
# ─────────────────────────────────────────────────────────────────────────────
def _run_command_set(
    commands: list[str], *, timeout_sec: float
) -> tuple[float | None, dict[str, object]]:
    """跑一组命令，返回通过率（无命令时 None）与留档详情。

    "无命令"必须返回 None 而不是 0.0 —— 见模块头的实现理念 3。
    """
    if not commands:
        return None, {"skipped": True, "reason": "no commands configured"}

    results = []
    passed = 0
    for command in commands:
        completed = evidence.run_command(command, timeout_sec=timeout_sec)
        ok = completed.returncode == 0
        passed += int(ok)
        results.append(
            {
                "command": command,
                "returncode": completed.returncode,
                "ok": ok,
                "tail": evidence.tail(completed.stdout or completed.stderr, 15),
            }
        )
    return passed / len(commands), {"results": results, "passed": passed}


def _parse_test_counts(output: str) -> tuple[int, int] | None:
    """从常见测试框架输出里抽出 (passed, failed)。

    只在能可靠解析时使用；解析不到就退回退出码判定。
    覆盖 pytest（``N passed, M failed``）与 go test（``ok`` / ``FAIL``）的常见形态。
    """
    import re

    match = re.search(
        r"(?P<passed>\d+)\s+passed(?:.*?(?P<failed>\d+)\s+failed)?", output
    )
    if match:
        passed = int(match.group("passed"))
        failed = int(match.group("failed") or 0)
        return passed, failed
    return None


def _test_pass_rate(commands: list[str], *, timeout_sec: float) -> tuple[float | None, dict]:
    if not commands:
        return None, {"skipped": True, "reason": "no focused tests configured"}

    results = []
    total_passed = total_failed = 0
    counted = False
    for command in commands:
        completed = evidence.run_command(command, timeout_sec=timeout_sec)
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        counts = _parse_test_counts(output)
        if counts is not None:
            total_passed += counts[0]
            total_failed += counts[1]
            counted = True
        elif completed.returncode == 0:
            # 解析不出计数时，把"整体通过"记为一个用例，保证分母非零。
            total_passed += 1
            counted = True
        else:
            total_failed += 1
            counted = True
        results.append(
            {
                "command": command,
                "returncode": completed.returncode,
                "parsed_counts": counts,
                "tail": evidence.tail(output, 15),
            }
        )

    if not counted or (total_passed + total_failed) == 0:
        return None, {"results": results, "reason": "no test results parsed"}
    rate = total_passed / (total_passed + total_failed)
    return rate, {
        "results": results,
        "passed_tests": total_passed,
        "failed_tests": total_failed,
    }


# ─────────────────────────────────────────────────────────────────────────────
# B1a：代码相似度（替代 CodeHub / auto_code_rating）
# ─────────────────────────────────────────────────────────────────────────────
def _code_similarity(changes: dict) -> tuple[float | None, dict]:
    """相对参考答案目录的相似度。

    方法：把 patch 的新增行与参考实现的源码行做行集合比对，取覆盖率。
    这是**粗糙但可离线可复现**的下界参考。真实场景建议把这里的实现替换成
    你们自己的评分引擎（原流程的 auto_code_rating），只要保持
    "返回 0..1 或 None"这个契约即可，上层加权逻辑不用改。
    """
    if not REFERENCE_DIR.is_dir():
        return None, {"skipped": True, "reason": "no reference/ directory"}

    patch_text = evidence.read_text(evidence.CHANGES_PATCH_PATH) or ""
    added: set[str] = set()
    for line in patch_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            normalized = line[1:].strip()
            if len(normalized) >= 8:  # 忽略括号、空行等无信息行
                added.add(normalized)
    if not added:
        return None, {"skipped": True, "reason": "no added lines in changes.patch"}

    reference_lines: set[str] = set()
    for path in sorted(REFERENCE_DIR.rglob("*")):
        if not path.is_file():
            continue
        text = evidence.read_text(path) or ""
        reference_lines.update(
            line.strip() for line in text.splitlines() if len(line.strip()) >= 8
        )
    if not reference_lines:
        return None, {"skipped": True, "reason": "reference/ has no comparable lines"}

    matched = len(added & reference_lines)
    coverage = matched / len(added)

    # 纯集合覆盖率会把顺序、结构完全不同的实现判成高相似，所以再叠一层
    # 序列相似度作为上限约束，让"抄了关键字但结构完全不同"拿不到满分。
    sequence_ratio = difflib.SequenceMatcher(
        None,
        "\n".join(sorted(added)),
        "\n".join(sorted(reference_lines)),
    ).ratio()
    score = min(coverage, max(sequence_ratio, coverage * 0.6))
    return reward.clamp01(score), {
        "matched_lines": matched,
        "added_lines": len(added),
        "reference_lines": len(reference_lines),
        "coverage": round(coverage, 4),
        "sequence_ratio": round(sequence_ratio, 4),
    }


# ─────────────────────────────────────────────────────────────────────────────
# B1b：LLM 代码审查（10 检查项扣分制）
# ─────────────────────────────────────────────────────────────────────────────
def _build_review_prompt(changes: dict) -> str:
    checklist = "\n".join(
        f"- {key}（权重 {weight}）：{label}"
        for key, label, weight in reward.CODE_REVIEW_CHECKS
    )
    patch_excerpt = (evidence.read_text(evidence.CHANGES_PATCH_PATH) or "")[:20000]
    return f"""审查下面这次代码变更，逐项判断是否违规。

## 变更统计
文件数 {changes.get("n_files")}，hunk 数 {changes.get("n_hunks")}，
新增 {changes.get("added_lines")} 行，删除 {changes.get("removed_lines")} 行。
测试相关文件：{changes.get("test_files_touched") or "无"}

## 变更内容（unified diff，已截断）
```diff
{patch_excerpt or "(changes.patch 缺失)"}
```

## 检查项
{checklist}

## 输出格式（只输出 JSON）
{{
  "violations": [
    {{"check": "<检查项 key>", "severity": "high|medium|low", "evidence": "<文件:行 或说明>"}}
  ]
}}

只列出你能指出具体证据的违规项。没有违规就返回 {{"violations": []}}。"""


def _llm_code_review(changes: dict) -> tuple[float | None, dict]:
    if not llm.judge_available():
        return None, {"skipped": True, "reason": "no judge credentials"}
    try:
        payload, meta = llm.ask_json(
            _build_review_prompt(changes), system=JUDGE_SYSTEM
        )
    except llm.JudgeUnavailable as exc:
        return None, {"skipped": True, "reason": str(exc)}

    violations = [
        item for item in payload.get("violations", []) if isinstance(item, dict)
    ]
    score, detail = reward.code_review_deduction(violations)
    return score, {"violations": detail, "judge_model": meta.get("model")}


# ─────────────────────────────────────────────────────────────────────────────
# 维度入口
# ─────────────────────────────────────────────────────────────────────────────
@criterion
def code_quality(workspace: Path) -> dict:
    """三个子维度动态加权求 code_quality。"""
    changes = evidence.load_changes()
    task_json = evidence.load_task_json()

    similarity, similarity_detail = _code_similarity(changes)
    review, review_detail = _llm_code_review(changes)
    build_rate, build_detail = _run_command_set(
        task_json["compile_commands"], timeout_sec=900.0
    )
    test_rate, test_detail = _test_pass_rate(
        task_json["focused_tests"], timeout_sec=900.0
    )

    # model_quality 的两条合成规则（原体系 §7.3 B1）
    if similarity is None and review is None:
        model_quality = None
    elif similarity is None:
        model_quality = review
    elif review is None:
        model_quality = similarity
    else:
        model_quality = similarity * 0.5 + review * 0.5

    score, weight_detail = reward.dynamic_weighted_mean(
        {
            "model_quality": model_quality,
            "build_pass_rate": build_rate,
            "test_pass_rate": test_rate,
        },
        reward.SUB_WEIGHTS,
    )

    missing = weight_detail["missing"]
    confidence = reward.calibrate_confidence(
        0.9,
        code_quality_missing_subdim=bool(missing),
        evidence_count=len(weight_detail["available"]),
    )
    return reward.criterion_result(
        score,
        reasoning=(
            f"sub_dims available={weight_detail['available']} missing={missing} "
            f"(renormalized over weight {weight_detail['renormalized_weight_total']})"
        ),
        confidence=confidence,
        model=review_detail.get("judge_model"),
        similarity=similarity_detail,
        code_review=review_detail,
        build=build_detail,
        tests=test_detail,
    )
