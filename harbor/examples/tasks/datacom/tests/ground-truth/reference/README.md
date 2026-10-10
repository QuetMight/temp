# 参考答案目录（code_similarity 的数据源）

> ⚠️ **这个目录里的内容**：
> * 会被烘进 verifier 镜像，**永远不会**进 agent 容器；
> * 用于 `quality/code_quality.py` 的 B1a 代码相似度；
> * 也用于人工复核"模型是不是走了完全不同的（更好的）路线"。

## 放什么

可以放两种东西，`code_quality.py` 只做行级比对，不关心语义：

1. **参考实现的关键源码文件**（推荐）—— 例如限流器的实现、配置的接线处。
   不需要是整个仓库，只需要"这次改动本应触及的那几处"。
2. **参考实现说明**（可选）—— 比如 `NOTES.md` 描述设计取舍。它也会进入
   相似度比对，但因为行级比对只看字面，影响有限。

## 为什么不用原流程的 CodeHub MR

原流程的 B1a 从 CodeHub 拉标准答案 MR 并调用 `auto_code_rating` 引擎逐文件评分。
移植后改成"静态目录 + 行级比对"，理由：

* **离线可复现**。MR + PAT 需要内网服务与凭证，而凭证进 verifier 容器本身
  就是一个不必要的暴露面；静态目录跟着任务版本走，历史可追溯。
* **基准恒定**。跨模型对比要求"两个模型面对完全相同的评分基准"。
  MR 会随时间变化，静态目录不会。
* **可替换**。`_code_similarity()` 的契约只是"返回 0..1 或 None"。
  你们想接回真实评分引擎时，替换那一个函数即可，上层动态加权逻辑不用动 ——
  这正是把它隔离成独立函数的原因。

## 怎么替换

编辑 `tests/ground-truth/reference/`，把本文件删掉（它不是比对对象，
但行级比对会把它的内容也纳入参考行集合 —— 如果希望它不参与，
请把说明文件放到 `ground-truth/` 下而不是 `reference/` 下）。

## 与 `task.json` 的关系

`reference/` 回答"像不像参考实现"，`task.json` 的 `compileCommands` /
`focusedTests` 回答"能不能编译、既有测试过不过"。
两者是 code_quality 里不同子维度的数据源，互不替代：

| 子维度 | 数据源 | 缺失时 |
| --- | --- | --- |
| `model_quality` | `reference/` + LLM 代码审查 | 退化为纯 LLM 评分 |
| `build_pass_rate` | `task.json` 的 compileCommands | 该子维度为 None（动态加权忽略） |
| `test_pass_rate` | `task.json` 的 focusedTests | 该子维度为 None（动态加权忽略） |
