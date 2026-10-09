#!/bin/sh
# ═════════════════════════════════════════════════════════════════════════════
# new_spec —— 占位 verifier：本步骤不做任何校验
#
# 为什么需要这个文件：
#   Harbor 的 verifier 契约要求脚本最终必须写出 reward 文件，否则整个 trial 会
#   以 RewardFileNotFoundError 结束。同时 Task 的配置校验要求每个 step 都能解析
#   出一个测试脚本（除非该 step 用自带测试的独立 verifier 镜像）。Harbor 没有
#   per-step 关闭验证的字段，所以"verify 置空"就落成这个只写恒定 reward 的脚本。
#
# 为什么 reward 写成 1.0 也没关系：
#   task.toml 里多步评分策略是 multi_step_reward_strategy = "final"，试炼级
#   reward 只取最后一个执行过的 step 的 verifier 结果，这里的值不进入总分。
#
# 运行方式：
#   本 step 的 verifier 是 environment_mode = "shared"，即在 agent 容器内直接
#   执行 /tests/test.sh，不额外起容器、不产生额外费用。/tests 与 /logs/verifier
#   由 Harbor 挂载与复位。
#
# 想在这里做真校验时：
#   例如断言 /app/spec.md 存在且六个章节齐全，然后把 reward 写成 0/1，并在
#   task.toml 的 [[steps]](new_spec) 上加 min_reward，未达标即跳过后续步骤。
# ═════════════════════════════════════════════════════════════════════════════
set -eu

mkdir -p /logs/verifier
printf '1.0\n' > /logs/verifier/reward.txt

echo "[new_spec] placeholder verifier: no checks performed, reward = 1.0"

exit 0
