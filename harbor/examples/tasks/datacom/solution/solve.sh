#!/bin/bash
# ═════════════════════════════════════════════════════════════════════════════
# datacom —— 参考解（骨架占位，待你实现）
#
# 调用方：OracleAgent。Harbor 把 solution/ 拷到容器 /solution 后以 root 执行。
#
# 用途有两个，都很关键：
#
#   1. **校准 verifier**。`harbor run -a oracle` 应当稳定拿到接近满分的
#      reward。拿不到就说明 rubric / 构建命令 / 判分公式有问题——这是调
#      verifier 最快的手段，比反复跑真模型便宜得多。
#
#   2. **给 code_similarity 提供上界**。参考解就是"1.0 相似度"的锚点。
#
# 注意：这是**单步** task，参考解要一次性走完 SDD 三阶段的效果——
# 写出 /app/spec.md、/app/plan.md，并把限流与背压实现完。
# 它不需要真的"按方法论走"，只需要产出方法论应得的最终结果。
#
# 为什么现在什么都不做：骨架的作用是先让
# agent → artifacts → collect 钩子 → separate verifier → rewardkit 这条链路跑通。
# 这里不产生交付物，避免用假文件掩盖真实问题；verifier 会如实报低分，
# 正好也验证了"判分对坏输入敏感"。
# ═════════════════════════════════════════════════════════════════════════════
set -euo pipefail

echo "TODO(datacom): reference solution not implemented yet; no deliverable produced." >&2

# TODO: 走完三阶段并留下产物
#
# cat > /app/spec.md <<'SPEC'
# # datacom 限流与背压规格
# ## 1. 接口契约
# ...
# SPEC
#
# cat > /app/plan.md <<'PLAN'
# # 实现计划
# ## 1. 需求追溯
# ...
# PLAN
#
# cd /app
# ... 实现代码 ...
# make -j"$(nproc)"
# make test

exit 0
