# ═════════════════════════════════════════════════════════════════════════════
# datacom —— agent 环境（三个 step 共享同一个容器）
#
# ⚠️ 占位实现：只提供一个最小可运行的基线。请按你的 datacom 场景补齐：
#     * 构建工具链（cmake / ninja / protoc / rust / go ...）
#     * 被测代码需要的运行时与系统库
#     * 测试框架与其依赖
#     * 需要预置的数据文件、schema、fixture
#
# 注意：verifier 是 separate 的，它用的是 steps/implement/tests/Dockerfile，
#       两个镜像相互独立 —— 判分依赖不要只装在这里。
# ═════════════════════════════════════════════════════════════════════════════
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

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

# TODO: 在这里安装 datacom 需要的额外依赖
# RUN pip install --no-cache-dir <your-packages>

WORKDIR /app
RUN mkdir -p /app

# 任务不自带被测代码时，可以在这里放一份初始脚手架
# COPY --chown=root:root seed/ /app/

CMD ["sleep", "infinity"]
