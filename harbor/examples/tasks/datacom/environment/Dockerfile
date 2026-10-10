# ═════════════════════════════════════════════════════════════════════════════
# datacom —— 被测工作区（agent 环境）
#
# 这个镜像定义的是**评测的起点状态**。它替代了原流程里的"work_dir 拷贝副本"：
# Harbor 每个 trial 都从这份镜像起一个新容器，所以副本隔离、并发互不干扰、
# 不污染源工程这三件事是免费得到的，不需要任何拷贝逻辑。
#
# ⚠️ 占位实现：只提供最小可运行基线。请补齐：
#     * 构建工具链（cmake / ninja / protoc / rust / go ...）
#     * 被测工程的运行时依赖
#     * 预置的 git 仓库（必须有 .git —— verifier 侧的变更分析依赖它）
#     * 工程自带的测试框架
#
# 关键约定：
#   * WORKDIR 必须是 /app，与 task.toml 的 [environment].workdir、
#     rewardkit 的 --workspace 默认值保持一致；
#   * 预置源码要 `git init && git add -A && git commit`，并打一个基线 tag
#     （例如 eval-base），这样 collect 钩子的 `git add -A && git diff --cached`
#     才能给出干净的变更集；
#   * 不要把任何评测规则（rubric/参考答案/task.json）放进这个镜像 ——
#     它们在 verifier 镜像里。
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

# TODO: 被测工程的额外依赖
# RUN pip install --no-cache-dir <your-packages>

WORKDIR /app
RUN mkdir -p /app

# TODO: 把 datacom 源码预置进来，并建立 git 基线
#
# COPY --chown=root:root seed/ /app/
# RUN cd /app \
#     && git init -q \
#     && git config user.email eval@local && git config user.name eval \
#     && git add -A \
#     && git commit -qm "baseline" \
#     && git tag eval-base
#
# collect 钩子用的是 `git add -A && git diff --cached`，它相对 HEAD（而不是
# eval-base）取差集。如果希望变更分析相对 tag 而不是 HEAD，把 task.toml 里的
# collect 命令换成 `git diff eval-base`。

CMD ["sleep", "infinity"]
