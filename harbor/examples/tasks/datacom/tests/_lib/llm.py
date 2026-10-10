"""LLM judge 传输层：结构化 JSON 提问 + 优雅降级。

=============================================================================
为什么自己写一层，而不是全用 rewardkit 的 judge TOML
=============================================================================
rewardkit 的 judge TOML 是**声明式判分**（一条 criterion = 一句自然语言 + 一个
输出格式），它覆盖了"给某个文件/某个维度打个分"这一类需求，非常合适。

但原评分体系里有三类逻辑是声明式表达不了的，必须自己算：

  1. **feature_completeness 的逐项加权公式**
     `Σ(item_score × item_points) / Σ(已判定项 item_points)`，
     其中 `unknown` 不进分母。这是"证据不足不强迫猜测"的语义，
     rewardkit 的 rubric 类型只给一个归一化等级，没有自定义分母。
  2. **code_quality 的缺失项重归一化**（见 _lib/reward.py 的说明）。
  3. **两条 LLM 结论之间的算术**（例如 model_quality 的
     `code_similarity × 0.5 + llm_code_analysis × 0.5`）。

这三类都需要"我来提问、我来解析、我来算"，所以需要一个薄传输层。
它不重复 rewardkit 的判分框架，只负责"把 prompt 变成结构化 JSON"。

**实现理念：走 litellm，不自己拼 HTTP。**
rewardkit 的核心依赖就是 litellm，所以它一定在镜像里；用它能白拿 provider
路由、base_url、各家 key 的环境变量约定，以及成本估算 —— 自己写 httpx 只会
在一个封闭内网环境里踩一遍各家 API 的差异。

**实现理念：判分不可用不等于判分失败。**
缺少凭证、网络不通、响应无法解析时抛 `JudgeUnavailable`；调用方捕获它，
退回纯程序化判分，并在 confidence 上打折、在 reasoning 里记 data_gap。
绝不能让"LLM 暂时不可用"表现成"被测代码写得差"。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

# judge 配置：优先用 datacom 专用变量，回落到各家通用变量。
# 这些值由 task.toml 的 [verifier.env] 从宿主机注入。
JUDGE_MODEL_ENV = "DATACOM_JUDGE_MODEL"
DEFAULT_MODEL = "anthropic/claude-opus-5-5"

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.DOTALL)


class JudgeUnavailable(RuntimeError):
    """LLM judge 本次不可用（缺凭证 / 网络失败 / 响应不可解析）。"""


def judge_model() -> str:
    return os.environ.get(JUDGE_MODEL_ENV) or DEFAULT_MODEL


def judge_available() -> bool:
    """是否具备调用 judge 的最低条件。

    只看"有没有凭证"，不探测网络 —— 探测会让每次判分多一次往返，
    而且真正的失败会在 ask_json 里被降级处理。
    """
    keys = (
        "DATACOM_JUDGE_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "AZURE_API_KEY",
    )
    return any(os.environ.get(key) for key in keys)


def _extract_json(text: str) -> dict[str, Any] | None:
    """从模型回复里抽 JSON。

    顺序：整段解析 → ```json 围栏 → 第一个平衡的花括号块。
    原流程的 evaluation_finalize 也做同样的"宽松解析"，
    因为把 JSON 塞进自然语言是模型的默认行为。
    """
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    for match in _JSON_BLOCK_RE.finditer(text):
        try:
            parsed = json.loads(match.group("body"))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    start = text.find("{")
    while start != -1:
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : index + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def ask_json(
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    temperature: float = 0.0,
    timeout_sec: float = 300.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """问一次 judge，要求返回 JSON 对象。

    Args:
        prompt: 用户消息（判分指令 + 证据）。
        system: 可选的系统消息。
        model:  覆盖 JUDGE_MODEL_ENV。
        temperature: 默认 0 —— 判分要可复现，位置偏差靠多轮/换序缓解，
                     而不是靠随机性。

    Returns:
        ``(payload, meta)``；meta 含 model / tokens / cost，供留档。

    Raises:
        JudgeUnavailable: 缺凭证、调用失败、或响应里找不到 JSON。
    """
    if not judge_available():
        raise JudgeUnavailable(
            "no judge credentials found; set [verifier.env] in task.toml "
            "(see the commented block) or export ANTHROPIC_API_KEY/OPENAI_API_KEY"
        )

    try:
        import litellm  # 延迟导入：rewardkit 的判分路径不需要这个模块
    except ImportError as exc:  # pragma: no cover - 镜像里一定有
        raise JudgeUnavailable(f"litellm is not installed: {exc}") from exc

    resolved_model = model or judge_model()
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        response = litellm.completion(
            model=resolved_model,
            messages=messages,
            temperature=temperature,
            timeout=timeout_sec,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # litellm 的异常种类随 provider 变化，统一折叠
        raise JudgeUnavailable(f"judge call failed ({resolved_model}): {exc}") from exc

    try:
        content = response.choices[0].message.content or ""
    except (AttributeError, IndexError) as exc:
        raise JudgeUnavailable(f"unexpected judge response shape: {exc}") from exc

    payload = _extract_json(content)
    if payload is None:
        raise JudgeUnavailable(
            f"judge returned no parsable JSON object; head={content[:200]!r}"
        )

    usage = getattr(response, "usage", None)
    meta: dict[str, Any] = {"model": resolved_model}
    if usage is not None:
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(usage, field, None)
            if isinstance(value, int):
                meta[field] = value
    try:
        cost = litellm.completion_cost(completion_response=response)
        if isinstance(cost, (int, float)) and cost > 0:
            meta["cost_usd"] = round(float(cost), 6)
    except Exception:
        pass  # 成本拿不到不影响判分

    return payload, meta
