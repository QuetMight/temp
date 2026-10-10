# 模型换代对比：运行与读报告

## 一次跑完

```bash
# 在仓库根目录。中文 Windows 上需要 PYTHONUTF8=1
# （task.toml / compare-job.yaml 含中文，Harbor 按平台默认编码读它们；
#   本机已通过 setx 配置好，换机器需重设。详见 ../README.md 第〇节）

PYTHONPATH=examples/tasks/datacom/compare \
uv run harbor run \
    -c examples/tasks/datacom/compare/compare-job.yaml \
    --plugin model_comparison_plugin:ModelComparisonReport \
    --pk baseline_model=<老模型名>
```

`PYTHONPATH` 是必需的：`--plugin` 接受的是 `module:ClassName` 导入路径
（`harbor/cli/plugin_registry.py`），模块必须在 `sys.path` 上。

### 先用 oracle 冒烟

对比之前先确认脚手架是通的，不要用两个真模型去调判分链：

```bash
uv run harbor run -p examples/tasks/datacom -a oracle -e docker
```

参考解拿到接近满分，才说明 rubric、构建命令、判分公式是对的。

### 关于基线选择与位置偏差

报告里所有 Δ 都相对基线计算，但**这里不存在位置偏差问题**，不需要复核：

原流程的 Position Swap 是为 **LLM 的成对主观判断**设计的 —— 让模型回答
"spec A 和 spec B 哪个更好"，答案会随 A/B 的呈现顺序变化。而本报告的 Δ 是
**确定性算术**：两侧都是固定的每模型分数，交换顺序只改变正负号，
表格同时给出 ↑/↓ 与好坏着色，不存在方向歧义。

真正需要偏差缓解的是**打分那一层**（LLM judge 读 spec/plan/轨迹给分），
它已经由 `tests/process/efficiency.toml` 的 `samples = 3` 处理 ——
rewardkit 会取中位数样本并在 `reward-details.json` 里给出 `agreement`
（样本一致率）。agreement 偏低时，该 trial 的这个维度分数应当被谨慎对待。

> 顺带记一个 CLI 事实，避免踩坑：`--plugin` **同名 import path 重复传不会
> 产生两个实例** —— `plugin_configs_from_cli` 用 import path 作为 dict 键，
> 重复项会塌缩成同参数的一份。想挂两个不同参数的同类插件是做不到的，
> 需要不同的类名。本插件通过"Δ 是带方向的确定性数值"绕开了这个需求。

## 报告内容

产物写在 `jobs/<job_name>/`：

| 文件 | 用途 |
| --- | --- |
| `comparison.html` | 人看的对比报告，自包含（内嵌 CSS，无 CDN/JS 依赖） |
| `comparison.json` | 机器可读的聚合结果，供后续脚本或看板消费 |

HTML 分五块：

1. **相对基线的变化** —— 每个指标一行、每个非基线模型一列，百分比 + 方向着色。
   方向意着色是刻意的：耗时 -12% 是好事，不能被染成红色。
2. **总览** —— 每个模型的全部指标绝对值，基线行高亮。
3. **分任务主分数** —— task × model 矩阵。用来识别"差异只集中在某一个任务上"，
   这类结论在平均值里会被淹没。
4. **可信度** —— trials 数、异常数、主分数 σ（标准差）、各维度覆盖率、异常分布。
   **没有这一块，任何"新模型更好"的结论都不成立。**
5. **口径与注意事项** —— 缓存命中率不可跨厂商比、token 计数口径、
   耗时包含范围、位置偏差复核方式。

## 与 Harbor 自带可视化分工

跑完也可以直接 `harbor view jobs` 看网页版：

| 需求 | 用哪个 |
| --- | --- |
| 快速看 reward/cost/tokens/time 的矩阵 | viewer 的 **`/compare`** 页（行分组 × 列分组可选 task/dataset/job/agent/model） |
| 看单 job 内多维度指标与 Pareto 前沿 | viewer 的 **job 详情页**（Evals 表按 metric key 动态生成列） |
| **缓存命中率** | ❌ viewer 没有 → 本报告 |
| **多维度对比 + 通过率 + σ + 分任务矩阵** | ❌ viewer 的 `/compare` 只给单一 `avg_reward` → 本报告 |
| **可归档、可 diff 的对比报告文件** | ❌ Harbor 不产出 → 本报告 |

一句话：**能跑的对比 Harbor 都有；"能归档、能解释、能支撑决策"的对比报告要自己出。**

## 指标定义（口径必须固定，否则跨模型不可比）

| 指标 | 定义 | 来源 |
| --- | --- | --- |
| 主分数 reward | `reward.json` 的 `reward` 键，三维度加权平均 | `TrialResult.verifier_result.rewards` |
| 通过率 | `reward >= 0.70 且 correctness >= 0.60` 的 trial 占比 | 报告层计算（阈值见 `_lib/reward.py`） |
| 输入 token | `Σ n_input_tokens`，**含**缓存命中部分 | `TrialResult.compute_token_cost_totals()` |
| 缓存命中率 | `Σcache / Σinput`（先求和再相除） | 同上 |
| agent 耗时 | `agent_execution.finished_at - started_at` | `TrialResult.agent_execution` |
| trial 总耗时 | `finished_at - started_at`（含环境构建与判分） | `TrialResult` |
| 步数 / 工具调用 / 工具错误率 | 从 ATIF 轨迹统计 | `_lib/trajectory.py`（与判分同一份实现） |
| σ | 同模型同任务多次尝试之间的样本标准差 | 报告层计算 |

## 实验纪律（比代码更重要）

1. **只改模型，别的逐字相同。** agent、skills、kwargs、env、超时、环境配置、
   workload 只要有一处不同，对比就不成立。
2. **判分模型固定。** 判分用的 LLM 绝不能跟着被测模型换。
3. **`n_attempts >= 3`。** σ 与两组均值差异同量级时，结论应写成
   "需要更多样本"，而不是"新模型更好"。
4. **同一台机器、同一个时间窗。** 耗时受宿主负载影响；跨时段对比要把
   "运行时间"也记录进报告（`comparison.json` 里有 `generated_at`）。
5. **先看维度，再看总分。** "新模型总分高 3%" 远不如
   "新模型功能完整性 +8% 但代码质量 -6%，且输入 token +40%" 有决策价值。
