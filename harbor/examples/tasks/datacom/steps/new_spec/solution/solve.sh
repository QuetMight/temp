#!/bin/bash
# ═════════════════════════════════════════════════════════════════════════════
# new_spec —— 参考解（骨架占位，待你实现）
#
# 调用方：OracleAgent。Harbor 会把 solution/ 目录整体拷到容器 /solution，
# 然后以 root 执行 /solution/solve.sh。
#
# 请在这里实现"完美完成本步骤"的动作，也就是写出一份满足
# steps/new_spec/instruction.md 要求的 /app/spec.md。
#
# 用途：`uv run harbor run -p examples/tasks/datacom -a oracle` —— 用参考解复现
# 满分，验证任务本身（环境、指令、判分）是否自洽。
#
# 为什么现在故意什么都不做：
#   本骨架的目的是先让整条流水线（三步 → artifacts 采集 → 独立 verifier）跑通。
#   这里不产生任何交付物，避免用假文件掩盖真实问题；verifier 也会如实报告
#   0 分。
# ═════════════════════════════════════════════════════════════════════════════
set -euo pipefail

echo "TODO(new_spec): reference solution not implemented yet; no deliverable produced." >&2

# TODO: 写出 /app/spec.md
#
# cat > /app/spec.md <<'SPEC'
# # datacom specification
#
# ## 1. Scope and goals
# ...
# SPEC

exit 0
