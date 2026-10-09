#!/bin/bash
# ═════════════════════════════════════════════════════════════════════════════
# new_plan —— 参考解（骨架占位，待你实现）
#
# 调用方：OracleAgent。Harbor 会把 solution/ 目录整体拷到容器 /solution，
# 然后以 root 执行 /solution/solve.sh。
#
# 请在这里实现"完美完成本步骤"的动作，也就是写出一份满足
# steps/new_plan/instruction.md 要求的 /app/plan.md（需求追溯表 + 设计 +
# 接口 + 工作拆解 + 验证策略 + 风险）。
#
# 用途：`uv run harbor run -p examples/tasks/datacom -a oracle` —— 用参考解复现
# 满分，验证任务本身（环境、指令、判分）是否自洽。
#
# 注意：本步骤默认是全新会话，但**容器是同一个**，所以上一步写下的
# /app/spec.md 仍然存在，可以直接读取。
#
# 为什么现在故意什么都不做：
#   本骨架的目的是先让整条流水线跑通。这里不产生交付物，避免用假文件掩盖真实
#   问题；verifier 会如实报告 0 分。
# ═════════════════════════════════════════════════════════════════════════════
set -euo pipefail

echo "TODO(new_plan): reference solution not implemented yet; no deliverable produced." >&2

# TODO: 读取 /app/spec.md，写出 /app/plan.md
#
# test -f /app/spec.md || { echo "spec.md missing" >&2; exit 1; }
# cat > /app/plan.md <<'PLAN'
# # datacom implementation plan
#
# ## 1. Requirement traceability
# ...
# PLAN

exit 0
