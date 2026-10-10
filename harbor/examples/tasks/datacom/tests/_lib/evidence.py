"""证据读取：workspace、变更 patch、task.json、ground-truth、命令执行。

=============================================================================
为什么需要这个模块
=============================================================================
separate verifier 容器里能看到的"证据"只有四类，全部由 Harbor 投递：

    /app/**                        agent 的工作区（artifacts 投递回原路径）
    /logs/agent/trajectory.json    ATIF 轨迹
    /logs/artifacts/changes.patch  collect 钩子在 agent 容器里打出的变更快照
    /tests/**                      verifier 镜像自带：rubric、参考答案、构建规则

这个模块把"证据在哪、怎么读、读不到怎么办"集中到一处，让三个维度文件只关心
判分逻辑本身。

**实现理念：变更分析走 patch，不走工作区。**
`/app` 是 agent 的最终状态，它回答"现在是什么样"；`changes.patch` 回答
"相对基线改了什么"。代码质量审查（改了哪些文件、是否动了无关文件、是否放宽
校验）依赖后者。task.toml 刻意把 `.git` 排除在 artifact 之外（体积），
所以 verifier 里无法自己算 diff —— patch 是唯一来源。

**实现理念：所有读取都是 best-effort。**
artifacts 采集本身是 best-effort（失败只记 manifest，不中断 trial），所以
每个读取函数都必须能返回"不可得"而不是抛异常，把判断权交给调用方，
再由调用方通过 confidence 表达"证据不足"。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

# ─── 路径契约（与 task.toml 的 artifacts / collect 钩子一一对应）──────────────
WORKSPACE = Path(os.environ.get("DATACOM_WORKSPACE", "/app"))
TRAJECTORY_PATH = Path(
    os.environ.get("DATACOM_TRAJECTORY_PATH", "/logs/agent/trajectory.json")
)
CHANGES_PATCH_PATH = Path(
    os.environ.get("DATACOM_CHANGES_PATCH", "/logs/artifacts/changes.patch")
)
TESTS_DIR = Path(os.environ.get("DATACOM_TESTS_DIR", "/tests"))
GROUND_TRUTH_DIR = TESTS_DIR / "ground-truth"
TASK_JSON_PATH = Path(os.environ.get("DATACOM_TASK_JSON", str(TESTS_DIR / "task.json")))

DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(?P<path>.+)$", re.MULTILINE)
_HUNK_RE = re.compile(r"^@@ .* @@", re.MULTILINE)


# ─── 基础读取 ────────────────────────────────────────────────────────────────
def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def read_json(path: Path) -> Any | None:
    text = read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def exists(path: Path) -> bool:
    return path.is_file()


# ─── 变更 patch ──────────────────────────────────────────────────────────────
def parse_patch(patch_text: str | None) -> dict[str, Any]:
    """把 unified diff 压成"改了什么"的结构化摘要。

    返回::

        {
          "available": bool,
          "files": ["src/ratelimit.py", ...],
          "n_files": int,
          "n_hunks": int,
          "added_lines": int,      # 已排除 +++ 头
          "removed_lines": int,    # 已排除 --- 头
          "added_by_file": {...},
          "removed_by_file": {...},
          "test_files_touched": [...],   # 路径里含 test/spec 的文件
        }

    刻意不返回完整 diff 正文：它会进 reward-details.json，几十 KB 的 diff 会
    把判分日志淹掉。正文留在 /logs/artifacts/changes.patch 里，需要时人工看。
    """
    if not patch_text or patch_text.strip() == "":
        return {
            "available": False,
            "files": [],
            "n_files": 0,
            "n_hunks": 0,
            "added_lines": 0,
            "removed_lines": 0,
            "added_by_file": {},
            "removed_by_file": {},
            "test_files_touched": [],
        }

    added_by_file: dict[str, int] = {}
    removed_by_file: dict[str, int] = {}
    current: str | None = None
    added = removed = 0

    for line in patch_text.splitlines():
        if line.startswith("+++ "):
            current = line[4:].strip()
            if current.startswith("b/"):
                current = current[2:]
            if current == "/dev/null":
                current = None
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("@@"):
            continue
        if current is None:
            continue
        if line.startswith("+"):
            added += 1
            added_by_file[current] = added_by_file.get(current, 0) + 1
        elif line.startswith("-"):
            removed += 1
            removed_by_file[current] = removed_by_file.get(current, 0) + 1

    files = sorted(set(added_by_file) | set(removed_by_file))
    return {
        "available": True,
        "files": files,
        "n_files": len(files),
        "n_hunks": len(_HUNK_RE.findall(patch_text)),
        "added_lines": added,
        "removed_lines": removed,
        "added_by_file": dict(sorted(added_by_file.items())),
        "removed_by_file": dict(sorted(removed_by_file.items())),
        "test_files_touched": [
            f for f in files if re.search(r"(^|/)(test|tests|spec)", f, re.IGNORECASE)
        ],
    }


def load_changes() -> dict[str, Any]:
    """读取 collect 钩子产出的变更快照。"""
    return parse_patch(read_text(CHANGES_PATCH_PATH))


# ─── 构建/测试规则 ───────────────────────────────────────────────────────────
def load_task_json() -> dict[str, Any]:
    """读 task.json（原流程里放在 ground-truth 同级的构建/测试规则）。

    期望结构::

        {"validation": {"compileCommands": [...], "focusedTests": [...]}}

    额外容忍 ``compile_commands`` / ``focused_tests`` 等 snake_case 写法，
    因为原流程里这两套命名都存在过。
    """
    data = read_json(TASK_JSON_PATH)
    if not isinstance(data, dict):
        return {"available": False, "compile_commands": [], "focused_tests": []}

    validation = data.get("validation") if isinstance(data.get("validation"), dict) else {}
    compile_commands = (
        validation.get("compileCommands")
        or validation.get("compile_commands")
        or data.get("compileCommands")
        or data.get("compile_commands")
        or []
    )
    focused_tests = (
        validation.get("focusedTests")
        or validation.get("focused_tests")
        or data.get("focusedTests")
        or data.get("focused_tests")
        or []
    )
    return {
        "available": True,
        "raw": data,
        "compile_commands": [str(c) for c in compile_commands],
        "focused_tests": [str(c) for c in focused_tests],
    }


def run_command(
    command: str,
    *,
    cwd: Path | None = None,
    timeout_sec: float = 900.0,
) -> subprocess.CompletedProcess[str]:
    """在 verifier 容器里执行一条命令，绝不抛异常。

    构建/测试失败是**预期结果**（正是要测的东西），不是错误；所以统一返回
    CompletedProcess，由调用方判断 returncode。超时也折叠成 returncode=-1。
    """
    try:
        return subprocess.run(
            command,
            cwd=str(cwd or WORKSPACE),
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=command,
            returncode=-1,
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=f"TIMEOUT after {timeout_sec}s",
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            args=command, returncode=-1, stdout="", stderr=f"OSError: {exc}"
        )


def tail(text: str | None, lines: int = 20) -> str:
    """截取输出尾部用于留档 —— 报告里只放结尾，避免刷屏。"""
    if not text:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])
