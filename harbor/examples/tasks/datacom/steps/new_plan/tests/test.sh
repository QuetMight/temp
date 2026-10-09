#!/bin/sh
# ═════════════════════════════════════════════════════════════════════════════
# new_plan —— 占位 verifier：本步骤不做任何校验
#
# 与 steps/new_spec/tests/test.sh 同理：
#   * Harbor 要求 verifier 必须写出 reward 文件，且配置校验要求每个 step 都能
#     解析出测试脚本；
#   * Harbor 没有 per-step 关闭验证的字段，所以"verify 置空"落成这个空脚本；
#   * task.toml 的 multi_step_reward_strategy = "final" 保证这里的恒定 reward
#     不进入试炼级总分。
#
# 本 step 的 verifier 是 environment_mode = "shared"，在 agent 容器内执行。
#
# 想在这里做真校验时：断言 /app/plan.md 存在且需求追溯表覆盖了 spec.md 的每条
# 需求，把 reward 写成 0/1，并在 task.toml 的 [[steps]](new_plan) 上加 min_reward。
# ═════════════════════════════════════════════════════════════════════════════
set -eu

mkdir -p /logs/verifier
printf '1.0\n' > /logs/verifier/reward.txt

echo "[new_plan] placeholder verifier: no checks performed, reward = 1.0"

exit 0
