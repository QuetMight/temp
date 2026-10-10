# ═════════════════════════════════════════════════════════════════════════════
# datacom —— 独立 verifier 镜像
#
# 这个镜像承载整个判分链：rubric、参考答案、构建规则、判分代码、LLM judge 依赖。
# 它同时是原流程 §3.2「评测工作区」的等价物 —— 只不过隔离级别从"同机不同目录"
# 升级成"不同容器"，所以原来那套"driver 不许进 work_dir""exec_dir 必须取拷贝
# 副本"的约束全部消失。
#
# 为什么用镜像而不是运行期上传 tests/：
#   task.toml 里 [verifier] environment_mode = "separate" 且没有写 docker_image，
#   于是 Harbor 命中镜像优先级第 2 档（本文件的 Dockerfile），认定测试已烘进镜像，
#   运行期**不再上传 tests/**。结果是 rubric 与参考答案从来不会出现在 agent 容器
#   里 —— 评测有效性依赖 rubric 保密，这一点必须物理保证，不能靠"下一步会清掉"。
#
# ⚠️ 改动这个文件后必须重建 verifier 镜像（harbor run 会按内容哈希感知变化；
#    必要时用 --force-build）。
# ═════════════════════════════════════════════════════════════════════════════
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/opt

# ── 判分侧的系统依赖 ─────────────────────────────────────────────────────────
# 必须与 environment/Dockerfile 对齐：verifier 是独立容器，agent 环境里装好的
# 东西不会继承过来。判分要自己构建被测代码，就得自己带工具链。
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        git \
        jq \
        make \
        patch \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

# ── 判分框架 ─────────────────────────────────────────────────────────────────
# rewardkit 是 Harbor 自带的判分工具包：它负责"发现维度 → 跑 criterion/LLM judge
# → 按声明式权重聚合 → 写 reward.json + reward-details.json"。
# 它自带 litellm（我们的 _lib/llm.py 也复用它做 provider 路由）。
#
# ⚠️ 封闭内网：把 `pip install` 换成内网镜像源，或先
#    `pip download` 好 wheel 再 COPY 进来。构建期断网会直接失败。
RUN pip install --no-cache-dir "harbor-rewardkit==0.2.*" httpx

# ── 判分资产 ─────────────────────────────────────────────────────────────────
COPY . /tests/

# _lib/ 是共享代码，不是判分维度。
# rewardkit 的发现规则是「tests/ 下的每个子目录 = 一个 reward 维度」，
# 虽然"不注册 criterion 的文件不产生分数"能兜住它，但为了避免出现一个空的
# lib 维度、也让"哪些是维度"在目录结构上一眼可读，这里直接把它移出 /tests，
# 靠 PYTHONPATH=/opt 让它以 `_lib` 包的身份被导入。
# 宿主侧的对比插件也从同一份源码导入同一个包（见 compare/），
# 保证判分口径与对比口径不会漂移。
RUN mv /tests/_lib /opt/_lib \
    && chmod +x /tests/test.sh

WORKDIR /tests

CMD ["sleep", "infinity"]
