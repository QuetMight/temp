#!/bin/sh
# ═════════════════════════════════════════════════════════════════════════════
# datacom —— 独立 verifier 入口
#
# 这个文件必须存在于镜像的 /tests/test.sh：verifier 镜像是 tests/Dockerfile
# （镜像优先级第 2 档），Harbor 认定测试已烘进镜像，运行期不再上传 tests/，
# 所以入口只能由镜像自带。
#
# ── Harbor 只认两件事 ────────────────────────────────────────────────────────
#   1. 退出码：非 0 会把这次判分标记为 verifier 异常；
#   2. reward：必须写到 /logs/verifier/reward.json（多维）或 reward.txt（标量）。
#
# ── 判分链的组成 ─────────────────────────────────────────────────────────────
#   /tests/reward.toml          三维度权重（correctness 0.5 / quality 0.4 / process 0.1）
#   /tests/correctness/         feature_completeness —— rubric.md 逐项判定
#   /tests/quality/             code_quality —— 构建/测试 + 代码审查动态加权
#   /tests/process/             performance_trace + efficiency —— ATIF 轨迹判分
#   /tests/ground-truth/        rubric.md / contract.md / reference/（判分资产）
#   /tests/task.json            compileCommands / focusedTests（构建测试规则）
#   /opt/_lib/                  共享代码（从 tests/_lib 搬出去的，见 Dockerfile）
#
# ── 为什么 test.sh 这么薄 ────────────────────────────────────────────────────
# 判分的编排全部交给 rewardkit：它自己负责发现维度、并发跑 criterion、跑 LLM
# judge、按 reward.toml 聚合、写 reward.json 与 reward-details.json。
# 我们自己写的只有"每个维度内部怎么算分"——这是刻意的边界：
# **框架的事交给框架，业务的事才自己写。**
# ═════════════════════════════════════════════════════════════════════════════
set -eu

# 挂载自宿主 trial 目录的 verifier/，兜底创建以防镜像被单独使用。
mkdir -p /logs/verifier

# 显式传参而不依赖默认值：默认值恰好一致（workspace=/app、output=/logs/verifier/
# reward.json），但显式写出来能让"路径契约"在报错时一眼可见。
#
# --workspace /app 之所以成立，是因为 task.toml 用 artifacts 把 agent 的 /app
# 原样投递回了 verifier 的 /app（投递保留原始 source 路径）。
exec rewardkit /tests \
    --workspace /app \
    --output /logs/verifier/reward.json
