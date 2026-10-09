#!/bin/sh
# ═════════════════════════════════════════════════════════════════════════════
# implement —— 独立 verifier 入口
#
# 这个文件必须存在于镜像的 /tests/test.sh：verifier 镜像命中 Harbor 镜像优先级
# 第 2 档（steps/implement/tests/Dockerfile）时 bundled_tests = True，运行期不会
# 再上传 tests/，Harbor 直接在 verifier 容器里执行这个脚本。
#
# Harbor 只认两件事：
#   1) 退出码 —— 非 0 会把这一步标记为 verifier 异常；
#   2) reward —— /logs/verifier/reward.json（多维）或 reward.txt（标量）。
#      两者都没有会被判为 RewardFileNotFoundError。
#
# 真正的判分逻辑全部在 /tests/verify.py 里：
#     阶段 1 构建 → 阶段 2 测试 → 阶段 3 LLM 打分 → 阶段 4 执行过程分析
# 本骨架里 verify.py 的四个阶段都还没实现，它会写 {"reward": 0.0} 并打印警告，
# 保证 harbor run 仍能跑完。
# ═════════════════════════════════════════════════════════════════════════════
set -eu

# /logs/verifier 由 Harbor 挂载（挂载自宿主 trial 目录的 verifier/），
# 这里兜底创建以防镜像被单独使用。
mkdir -p /logs/verifier

# exec 让 python 直接接管进程，退出码与信号原样透传给 Harbor。
exec python3 /tests/verify.py
