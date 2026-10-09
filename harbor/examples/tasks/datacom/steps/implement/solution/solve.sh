#!/bin/bash
# ═════════════════════════════════════════════════════════════════════════════
# implement —— 参考解（骨架占位，待你实现）
#
# 调用方：OracleAgent。Harbor 会把 solution/ 目录整体拷到容器 /solution，
# 然后以 root 执行 /solution/solve.sh。
#
# 请在这里实现"完美完成本步骤"的动作：按 /app/plan.md 把 datacom 实现出来，
# 并保证 steps/implement/tests/verify.py 里的四个阶段（构建 / 测试 / LLM 打分 /
# 执行过程分析）都能拿到满分。
#
# 用途：`uv run harbor run -p examples/tasks/datacom -a oracle` —— 用参考解复现
# 满分，验证任务本身（环境、指令、判分）是否自洽。这是调 verifier 时最有用的
# 手段：verifier 给不出 1.0，就说明任务或判分写错了。
#
# 注意：
#   * 容器跨步持久，/app/spec.md 与 /app/plan.md 都还在。
#   * verifier 是**独立容器**，看不到这里的文件系统；能带过去的只有 task.toml
#     里声明的 artifacts（/app/spec.md、/app/plan.md、/logs/agent/trajectory.json）
#     以及 /logs/artifacts/ 发布目录。verifier 会自己重新构建。
#
# 为什么现在故意什么都不做：
#   本骨架的目的是先让整条流水线跑通。这里不产生交付物，避免用假文件掩盖真实
#   问题；verifier 也会如实报告 0 分。
# ═════════════════════════════════════════════════════════════════════════════
set -euo pipefail

echo "TODO(implement): reference solution not implemented yet; no deliverable produced." >&2

# TODO: 按 /app/plan.md 实现，然后跑一遍本地的构建与测试
#
# cd /app
# ...
# make -j"$(nproc)"
# make test

exit 0
