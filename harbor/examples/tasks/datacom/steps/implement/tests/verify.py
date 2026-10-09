#!/usr/bin/env python3
"""datacom —— 独立 verifier 的判分总控脚本。

由 ``/tests/test.sh`` 调用（``exec python3 /tests/verify.py``），运行在 Harbor 为
``implement`` 这一步启动的 **separate verifier 容器**里。

===============================================================================
运行环境里有什么（输入）
===============================================================================
1. 声明的 artifacts，投递回它们的**原始 source 路径**：

       /app/spec.md                    第 1 步的规格说明
       /app/plan.md                    第 2 步的实现计划
       /logs/agent/trajectory.json     第 3 步的 ATIF 轨迹（执行过程分析用）
       /logs/artifacts/                agent 主动发布的东西（Harbor 隐式收集）

   ⚠️ 采集是 best-effort：文件可能不存在。用 ``exists()`` 判断，不要直接读。
   ⚠️ agent 环境里的其他文件（构建产物、源码树）**不会**到这里来，除非把它们
      也声明成 artifacts。verifier 自己负责构建。

2. ``/tests/`` 里的判分资产。因为 verifier 镜像是
   ``steps/implement/tests/Dockerfile``（命中镜像优先级第 2 档，
   ``bundled_tests = True``），Harbor **不会**在运行期上传 ``tests/``，
   ``/tests`` 的内容完全来自镜像的 ``COPY . /tests/``。

3. 环境变量：``task.toml`` 的 ``[steps.verifier.env]`` 会以宿主机环境变量解析后
   注入。默认全部注释掉了，见下面的常量。

===============================================================================
回传通道：中间产物写到 /logs/verifier/
===============================================================================
Harbor 只把 verifier 容器里的这**一个**目录接回宿主：

    容器内 /logs/verifier/**  →  宿主 <trial_dir>/verifier/**
                                 多步任务最终归档到
                                 <trial_dir>/steps/<step_name>/verifier/

* mounted 环境（docker，默认）：该目录是**实时 bind mount**，写进去立刻出现在宿主
  磁盘上，无需任何配置。
* 非 mounted 环境（daytona / modal / e2b / runloop ...）：verify 阶段结束后 Harbor
  把整个 ``/logs/verifier`` 下载回宿主。

Harbor 保留的文件名，不要占用：

    reward.txt / reward.json   reward 契约。json 会被解析，**必须是纯数值的扁平
                               dict**；富信息请另开文件（本脚本用 report.json）。
    test-stdout.txt            Harbor 把 test 脚本的命令拼成
                               ``(cmd) > /logs/verifier/test-stdout.txt 2>&1``，
                               也就是说**本脚本的所有 print 都会落进这个文件**，
                               并且 ``harbor analyze`` / annotator 会读它。
    test-stderr.txt            路径有定义，但这条执行路径从不写入，别依赖。

建议的组织方式（用 ``artifact_path()`` / ``write_text()`` / ``write_json()`` 写，
它们会自动建父目录并挡住上面这些保留名）：

    /logs/verifier/
    ├── reward.json          # Harbor 契约（纯数值、扁平）
    ├── test-stdout.txt      # Harbor 写：本脚本的全部输出
    ├── report.json          # 本脚本写：汇总诊断（任意嵌套结构）
    ├── build/build.log
    ├── test/junit.xml
    ├── judge/response.json
    └── trace/process-analysis.json

两个注意点：

* 每次 verify 开始前 Harbor 会**清空**这个目录（并 ``chmod 777``），所以产物必须
  在本次 verify 期间写出来，不要指望跨 step 累积 —— 各步的产物分别归档到各自的
  ``steps/<name>/verifier/``。
* docker 下它是实时 bind mount，**别往里拷整个构建树**：体积会直接落到宿主磁盘，
  而且 ``--verifier-exclude-logs`` 的过滤只对非 mounted 环境生效。只回传日志、diff、
  指标、LLM 原始响应这类小文件。

===============================================================================
必须遵守的输出契约
===============================================================================
* 退出码：0 表示判分正常完成；非 0 会把这一步标记成 verifier 异常。
* reward：必须写下面之一

      /logs/verifier/reward.json    {"build": 1.0, "test": 0.5, "judge": 0.8}
      /logs/verifier/reward.txt     1.0

  缺文件、空文件、非数值、NaN/Inf 都会被 Harbor 判为 verifier 错误。reward.json
  里每个 key 都会进入试炼结果；``task.toml`` 的 ``min_reward = { key = 阈值 }``
  就是按 key 卡。

===============================================================================
待实现的四个阶段
===============================================================================
每个阶段返回 ``dict[str, float]``、``None``，或抛 ``Skipped``：

    return {...}                  正常产出分数，合并进 reward.json
    return None                   尚未实现（会记进 report.json 的 status）
    raise Skipped("原因")         主动跳过（例如 LLM key 没配），原因会进报告

阶段内部抛出的其他异常会被 main() 捕获、记进 report.json 的 traceback，并继续跑
后面的阶段 —— 单个阶段崩掉不应该让整份判分变成 verifier 错误。
四个阶段都没有产出时，本脚本写入 ``{"reward": 0.0}`` 并打印醒目警告，这样
``harbor run`` 仍能跑完，方便你先验证脚手架。
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

# ─── 回传通道 ────────────────────────────────────────────────────────────────
# 容器里写这个目录下的任何文件都会回到宿主 trial 目录（见模块 docstring）。
VERIFIER_DIR = Path("/logs/verifier")
REPORT_PATH = VERIFIER_DIR / "report.json"
REPORT_SCHEMA = "datacom-verifier-report/1"

# Harbor 自己管理的文件名，禁止用 artifact_path() 写（否则会覆盖 reward 契约，
# 或覆盖 Harbor 正在写入的本脚本 stdout 归档）。
HARBOR_RESERVED_PATHS = frozenset(
    {
        "reward.txt",
        "reward.json",
        "test-stdout.txt",
        "test-stderr.txt",
    }
)

# ─── 输出契约 ────────────────────────────────────────────────────────────────
REWARD_JSON_PATH = VERIFIER_DIR / "reward.json"
REWARD_TEXT_PATH = VERIFIER_DIR / "reward.txt"

# ─── 输入路径（可用 [steps.verifier.env] 覆盖）───────────────────────────────
WORKSPACE = Path(os.environ.get("DATACOM_WORKSPACE", "/app"))
TRAJECTORY_PATH = Path(
    os.environ.get("DATACOM_TRAJECTORY_PATH", "/logs/agent/trajectory.json")
)
SPEC_PATH = WORKSPACE / "spec.md"
PLAN_PATH = WORKSPACE / "plan.md"

# ─── LLM 打分配置（推荐由 [steps.verifier.env] 注入，不要硬编码）────────────
JUDGE_API_KEY = os.environ.get("DATACOM_JUDGE_API_KEY", "")
JUDGE_MODEL = os.environ.get("DATACOM_JUDGE_MODEL", "")
JUDGE_BASE_URL = os.environ.get("DATACOM_JUDGE_BASE_URL", "")

# 会写进 report.json 的自由说明。
NOTES: list[str] = []


class Skipped(Exception):
    """阶段主动跳过时抛出；原因会记进 report.json 的 status=skipped。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def log(message: str) -> None:
    """带前缀的进度输出。

    ⚠️ 本脚本的 stdout/stderr 会被 Harbor 重定向进 /logs/verifier/test-stdout.txt，
    所以 ``print`` 出来的东西本身也是回传产物 —— 但那只是人类可读的流水账，
    程序可读的诊断请用 ``write_json()`` / ``write_report()``。
    """
    print(f"[datacom-verify] {message}", flush=True)


def note(message: str) -> None:
    """记一条说明，最终出现在 report.json 的 notes 里。"""
    NOTES.append(message)
    log(message)


# ═════════════════════════════════════════════════════════════════════════════
# 中间产物写出（统一走 /logs/verifier，自动建父目录 + 挡保留名）
# ═════════════════════════════════════════════════════════════════════════════
def artifact_path(*parts: str) -> Path:
    """在 /logs/verifier 下开一个产物路径，并确保父目录存在。

    这些文件会被 Harbor 回传到宿主的 trial 目录：

        单步任务   jobs/<job>/<trial>/verifier/<parts...>
        多步任务   jobs/<job>/<trial>/steps/<step_name>/verifier/<parts...>

    Raises:
        ValueError: 目标是 Harbor 保留的文件名。
    """
    relative = PurePosixPath(*parts)
    if relative.as_posix() in HARBOR_RESERVED_PATHS:
        raise ValueError(
            f"{relative.as_posix()!r} is reserved by Harbor; "
            "pick another filename (see the module docstring)"
        )
    path = VERIFIER_DIR.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_text(relative_path: str, text: str) -> Path:
    """把文本写到 /logs/verifier/<relative_path>（回传到宿主）。"""
    path = artifact_path(relative_path)
    path.write_text(text, encoding="utf-8")
    log(f"wrote {path} ({len(text)} chars)")
    return path


def write_json(relative_path: str, payload: Any) -> Path:
    """把 JSON 写到 /logs/verifier/<relative_path>（回传到宿主）。"""
    try:
        text = json.dumps(payload, indent=2, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        # 报告类数据里混进不可序列化对象时，退化成可读文本而不是丢掉整个文件。
        log(f"cannot serialize {relative_path} as JSON ({exc}); falling back to repr")
        text = repr(payload)
    return write_text(relative_path, text + "\n")


def _describe(path: Path) -> dict[str, Any]:
    """描述一个输入文件的存在性与大小，写进 report.json 的 inputs。"""
    if not path.is_file():
        return {"path": str(path), "exists": False}
    return {"path": str(path), "exists": True, "size_bytes": path.stat().st_size}


def write_report(
    *,
    phases: dict[str, dict[str, Any]],
    rewards: dict[str, float],
    notes: list[str],
) -> None:
    """写 /logs/verifier/report.json —— 汇总诊断的机器可读出口。

    只有这个文件承载富信息：reward.json 必须是纯数值的扁平 dict，塞不进嵌套结构
    或字符串，所以"哪个阶段失败了、为什么失败、LLM 怎么说的"都放在这里。
    """
    payload = {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "workspace": str(WORKSPACE),
            "spec": _describe(SPEC_PATH),
            "plan": _describe(PLAN_PATH),
            "trajectory": _describe(TRAJECTORY_PATH),
        },
        "phases": phases,
        "rewards": rewards,
        "notes": list(notes),
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    log(f"wrote {REPORT_PATH}")


# ═════════════════════════════════════════════════════════════════════════════
# 通用工具
# ═════════════════════════════════════════════════════════════════════════════
def run(
    command: list[str] | str,
    *,
    cwd: Path | None = None,
    timeout_sec: float = 1800.0,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """在 verifier 容器里执行一条命令，捕获输出。

    ``check=False``（默认）时返回结果而不抛异常，方便"构建失败也要给出分数"这种
    判断；把 ``check=True`` 用于"失败就应该中断判分"的命令。
    """
    log(f"$ {command if isinstance(command, str) else ' '.join(command)}")
    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        shell=isinstance(command, str),
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        env={**os.environ, **(env or {})},
    )
    if completed.stdout:
        print(completed.stdout, end="", flush=True)
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr, flush=True)
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"command failed with exit code {completed.returncode}: {command}"
        )
    return completed


def load_json(path: Path) -> Any | None:
    """读一个 JSON 文件；不存在或解析失败时返回 None（采集是 best-effort）。"""
    if not path.is_file():
        log(f"input missing: {path}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"input unreadable: {path} ({exc})")
        return None


def load_trajectory() -> dict[str, Any] | None:
    """读入 ATIF 轨迹。

    返回原始 dict（含 ``steps`` 列表）方便你自己统计；轨迹里的 ``extra`` 字段是
    各 agent 的自定义元数据，结构不保证一致，读的时候要防御性处理。

    常用统计入口::

        trajectory = load_trajectory()
        steps = (trajectory or {}).get("steps") or []
        agent_steps = [s for s in steps if s.get("source") == "agent"]
        tool_calls = [tc for s in agent_steps for tc in (s.get("tool_calls") or [])]
        final = (trajectory or {}).get("final_metrics") or {}

    注意：多步任务默认每步是独立会话，``/logs/agent`` 在每步开始前会被清空，
    所以这个文件通常只覆盖 ``implement`` 这一步。要让轨迹串起三步，用
    ``--resume-trajectory`` 运行（见任务 README）。
    """
    return load_json(TRAJECTORY_PATH)


# ═════════════════════════════════════════════════════════════════════════════
# 阶段 1：构建
# ═════════════════════════════════════════════════════════════════════════════
def phase_build() -> dict[str, float] | None:
    """在 verifier 容器里构建被测代码。

    建议返回：

        {"build": 1.0}                        构建成功
        {"build": 0.0}                        构建失败
        {"build": 0.7, "build_warnings": 0.0} 想看更细的维度就拆 key

    注意 verifier 容器是**独立**的：agent 环境里的构建产物不会带过来，必须在这里
    重新构建。构建日志用 write_text() 落到 /logs/verifier/ 下，就会回传到宿主的
    ``verifier/build/build.log``。
    """
    log("phase_build: not implemented yet")

    # TODO: 实现构建。示例骨架：
    #
    # completed = run("make -j4", cwd=WORKSPACE)
    # write_text("build/build.log", completed.stdout + completed.stderr)
    # write_json(
    #     "build/metrics.json",
    #     {"returncode": completed.returncode, "command": "make -j4"},
    # )
    # return {"build": 1.0 if completed.returncode == 0 else 0.0}

    return None


# ═════════════════════════════════════════════════════════════════════════════
# 阶段 2：功能 / 集成测试
# ═════════════════════════════════════════════════════════════════════════════
def phase_test() -> dict[str, float] | None:
    """跑测试并解析结果。

    建议按 spec.md 里的需求分组给分，例如::

        {"test_core": 1.0, "test_edge": 0.5, "test_perf": 0.0}

    测试用例来自哪里由你决定：可以放进 ``/tests/``（随镜像烘进去），也可以在
    这里生成。不建议依赖 agent 自己写的测试 —— 那是"自己给自己打分"。
    """
    log("phase_test: not implemented yet")

    # TODO: 实现测试执行与结果解析。
    #
    # completed = run(["pytest", "-q", "--junitxml=/logs/verifier/test/junit.xml",
    #                  "/tests/cases"], cwd=WORKSPACE)
    # return {"test": 1.0 if completed.returncode == 0 else 0.0}

    return None


# ═════════════════════════════════════════════════════════════════════════════
# 阶段 3：LLM 打分
# ═════════════════════════════════════════════════════════════════════════════
def phase_judge() -> dict[str, float] | None:
    """用 LLM 对"实现是否满足规格 / 计划"打分。

    需要 API key 时从 ``[steps.verifier.env]`` 注入（见 task.toml 里注释掉的
    那一段），不要硬编码。没配 key 就 ``raise Skipped(...)`` 跳过这一阶段。

    典型做法：把 ``spec.md`` + ``plan.md`` + 关键 diff / 源码片段放进 prompt，
    要求模型按固定 schema 输出分数。建议至少覆盖：
      * spec 符合度
      * plan 符合度
      * 代码质量 / 可维护性

    可复现性：LLM 打分有随机性，建议固定 temperature，并把 prompt 与**原始响应**
    用 write_text() / write_json() 落到 ``judge/`` 下回传，方便日后复核。
    """
    if not JUDGE_API_KEY:
        raise Skipped("DATACOM_JUDGE_API_KEY is not set")

    log(f"phase_judge: not implemented yet (model={JUDGE_MODEL!r})")

    # TODO: 实现 LLM 打分。
    #
    # spec = SPEC_PATH.read_text(encoding="utf-8") if SPEC_PATH.is_file() else ""
    # plan = PLAN_PATH.read_text(encoding="utf-8") if PLAN_PATH.is_file() else ""
    # write_text("judge/prompt.txt", prompt)
    # ... 调模型 ...
    # write_json("judge/response.json", raw_response)
    # return {"judge_spec": 0.9, "judge_plan": 0.7, "judge_quality": 0.8}

    return None


# ═════════════════════════════════════════════════════════════════════════════
# 阶段 4：执行过程分析
# ═════════════════════════════════════════════════════════════════════════════
def phase_analyze() -> dict[str, float] | None:
    """分析 agent 的执行过程（ATIF 轨迹）。

    可以量化的东西（示例，按你的题意取舍）：
      * 是否走了弯路：失败的构建/测试命令次数、同一错误的重复次数
      * 效率：agent step 数、工具调用数、token 与成本（``final_metrics``）
      * 是否读全了输入：有没有读过 ``/app/spec.md`` 与 ``/app/plan.md``
      * 是否遵守约束：有没有改自己写的测试、有没有触碰非目标文件

    这些指标适合作为**扣分项**：``{"process_penalty": 0.0}`` 表示无扣分。
    注意这些是辅助信号，不要让它们主导最终分数。

    明细建议用 write_json("trace/process-analysis.json", ...) 回传，即便这一阶段
    最终因为数据缺失而 ``raise Skipped(...)``，明细也已经留在宿主上了。
    """
    trajectory = load_trajectory()
    if trajectory is None:
        raise Skipped("no trajectory available")

    steps = trajectory.get("steps") or []
    agent_steps = [s for s in steps if s.get("source") == "agent"]
    log(
        "phase_analyze: not implemented yet "
        f"(steps={len(steps)}, agent_steps={len(agent_steps)})"
    )

    # TODO: 实现过程分析。
    #
    # tool_calls = [tc for s in agent_steps for tc in (s.get("tool_calls") or [])]
    # final = trajectory.get("final_metrics") or {}
    # write_json(
    #     "trace/process-analysis.json",
    #     {"steps": len(steps), "agent_steps": len(agent_steps),
    #      "tool_calls": len(tool_calls), "final_metrics": final},
    # )
    # return {"process_penalty": 0.0}

    return None


PHASES = (
    ("build", phase_build),
    ("test", phase_test),
    ("judge", phase_judge),
    ("analyze", phase_analyze),
)


# ═════════════════════════════════════════════════════════════════════════════
# reward 写出
# ═════════════════════════════════════════════════════════════════════════════
def emit_rewards(rewards: dict[str, float]) -> dict[str, float]:
    """按 Harbor 的契约写出 reward 文件，返回实际写出的数值字典。

    这里额外做了一层防御：非数值、NaN、Inf 会被滤掉，避免因为打分代码的一个
    笔误把整份结果变成 verifier 错误。
    """
    clean: dict[str, float] = {}
    for key, value in rewards.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            log(f"dropping non-numeric reward {key}={value!r}")
            continue
        if not math.isfinite(float(value)):
            log(f"dropping non-finite reward {key}={value!r}")
            continue
        clean[key] = float(value)

    REWARD_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    REWARD_JSON_PATH.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
    log(f"wrote {REWARD_JSON_PATH}: {clean}")

    if not clean:
        # reward.json 是空 dict 时 Harbor 会当成"有效但无 reward"，明确给个 0 更清楚。
        REWARD_TEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REWARD_TEXT_PATH.write_text("0.0\n", encoding="utf-8")
        log(f"no numeric rewards; wrote fallback {REWARD_TEXT_PATH}")

    return clean


def _elapsed_sec(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)


def main() -> int:
    expected_inputs = (SPEC_PATH, PLAN_PATH, TRAJECTORY_PATH)
    missing = [str(p) for p in expected_inputs if not p.is_file()]
    if missing:
        note(f"expected inputs are missing: {', '.join(missing)}")

    records: dict[str, dict[str, Any]] = {}
    rewards: dict[str, float] = {}
    implemented = 0

    for name, phase in PHASES:
        started_at = time.monotonic()
        try:
            result = phase()
        except Skipped as exc:
            records[name] = {
                "status": "skipped",
                "reason": exc.reason,
                "duration_sec": _elapsed_sec(started_at),
            }
            log(f"phase {name} skipped: {exc.reason}")
            continue
        except Exception:
            # 单个阶段崩掉不应该让整份判分变成 verifier 错误：记下来，继续跑。
            records[name] = {
                "status": "error",
                "traceback": traceback.format_exc(),
                "duration_sec": _elapsed_sec(started_at),
            }
            log(f"phase {name} raised:\n{traceback.format_exc()}")
            continue

        if result is None:
            records[name] = {
                "status": "not_implemented",
                "duration_sec": _elapsed_sec(started_at),
            }
            continue

        rewards.update(result)
        implemented += 1
        records[name] = {
            "status": "ok" if result else "empty",
            "rewards": result,
            "duration_sec": _elapsed_sec(started_at),
        }

    if implemented == 0:
        print(
            "\n"
            "=====================================================================\n"
            "  datacom verifier is still a SKELETON: none of the four phases\n"
            "  (build / test / judge / analyze) produced any score, so the reward\n"
            "  below is a placeholder 0.0.\n"
            "  Implement them in /tests/verify.py (source:\n"
            "  steps/implement/tests/verify.py) and rebuild the verifier image.\n"
            "=====================================================================\n",
            file=sys.stderr,
            flush=True,
        )
        rewards = {"reward": 0.0}
    elif not rewards:
        note("phases ran but produced no numeric score; emitting reward 0.0")
        rewards = {"reward": 0.0}

    clean = emit_rewards(rewards)

    # reward 已经落地，报告写失败不影响判分，所以单独兜住。
    try:
        write_report(phases=records, rewards=clean, notes=NOTES)
    except OSError:
        log(f"failed to write report:\n{traceback.format_exc()}")

    log(f"done: {implemented}/{len(PHASES)} phases produced scores")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # 到这一层说明判分脚本本身有 bug，让 Harbor 把它记成 verifier 异常。
        traceback.print_exc()
        try:
            REWARD_TEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
            REWARD_TEXT_PATH.write_text("0.0\n", encoding="utf-8")
        except OSError:
            pass
        sys.exit(1)
