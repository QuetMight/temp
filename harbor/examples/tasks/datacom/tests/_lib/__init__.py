"""datacom 评测判分链的共享代码（不是 rewardkit 的判分维度）。

⚠️ 这个目录在镜像构建时会被搬到 /opt/_lib（见 tests/Dockerfile）。原因是
rewardkit 的发现有两条规则：

  * 「tests/ 下的每个子目录 = 一个 reward 维度」；
  * 「不注册 criterion 的 Python 文件不产生分数」。

第二条能兜住普通文件，但为了避免出现一个空的 `_lib` 维度、也为了让
「哪些是判分维度」在目录结构上一眼可读，这里选择直接把它移出 /tests。
目录名的下划线前缀本身就是这个意思。

**同一份代码被两个执行环境复用**，所以包名必须一致：

    verifier 容器   /opt/_lib           （Dockerfile mv 出来的）
                    PYTHONPATH=/opt     → from _lib import ...
    宿主侧插件      tests/ 加到 sys.path → from _lib import ...

后者见 compare/model_comparison_plugin.py。复用是刻意的：**判分口径与对比口径
必须来自同一份实现**，否则"verifier 算的分数"和"报告里展示的指标"会漂移，
而这种漂移在跨模型对比里会被误读成模型差异。

模块职责：

    reward.py       判分数学：权重常量单一事实源、动态加权、扣分制、置信度校准
    trajectory.py   ATIF 轨迹解析与统计（token/缓存/轮次/工具错误率）
    evidence.py     证据读取：workspace、changes.patch、task.json、ground-truth
    llm.py          LLM judge 传输层（带优雅降级）
"""
