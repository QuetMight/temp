# ═════════════════════════════════════════════════════════════════════════════
# datacom —— 独立 verifier 镜像（separate verifier）
#
# 为什么这个文件会被用作 verifier 镜像：
#   task.toml 里 [steps.verifier] 设了 environment_mode = "separate"，但**故意
#   没有**写 [steps.verifier.environment].docker_image。Harbor 的 verifier 镜像
#   优先级是：
#       1. [steps.verifier.environment].docker_image
#       2. steps/<name>/tests/Dockerfile        ← 命中这一档
#       3. [verifier.environment].docker_image
#       4. tests/Dockerfile
#       5. 回退 agent 环境
#   命中 1–4 时 Harbor 认为测试已烘进镜像（bundled_tests = True），运行期**不再**
#   上传 tests/，所以 /tests/test.sh 必须由本镜像提供。
#
# 一次性把判分依赖装好，好处是避免运行期安装抖动，也让判分环境可复现。
# 改完这个文件后需要重建 verifier 镜像（harbor run 默认会重建；若之前构建过且
# 想强制重建，用 --force-build）。
# ═════════════════════════════════════════════════════════════════════════════
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ── 判分侧的系统依赖 ─────────────────────────────────────────────────────────
# 注意：这里要和 environment/Dockerfile 对齐 —— verifier 是独立容器，agent 环境
# 里装好的东西不会继承过来。verify.py 要构建被测代码，就得自己带工具链。
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        git \
        jq \
        make \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

# ── 判分侧的 Python 依赖 ─────────────────────────────────────────────────────
# 需要调 LLM 时就打开（按你实际用的客户端调整）：
# RUN pip install --no-cache-dir httpx pydantic
#
# 需要跑 pytest 时：
# RUN pip install --no-cache-dir pytest

# ── 判分资产 ─────────────────────────────────────────────────────────────────
# 整个 tests/ 目录成为 /tests：test.sh（入口）+ verify.py（总控）+ 你放进去的
# 测试用例、fixture、golden 文件等。镜像比较大时可以改用多阶段构建，只把需要
# 的产物 COPY 进来。
COPY . /tests/
RUN chmod +x /tests/test.sh

WORKDIR /tests

CMD ["sleep", "infinity"]
