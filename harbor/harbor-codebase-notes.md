# Harbor 代码仓调研笔记

> 调研对象：`D:\Code\harbor`（Harbor framework，Python CLI + 文档站 + 结果查看器 + 基准适配器）
> 关注点：**Task 的组织形式** 与 **ATIF 轨迹的记录方式**
> 依据：源码实际行为，而非文档描述；文档与代码不一致处已单独标注。

---

## 目录

- [Harbor 代码仓调研笔记](#harbor-代码仓调研笔记)
  - [目录](#目录)
  - [1. 仓库分层与主链路](#1-仓库分层与主链路)
  - [2. Task 的组织形式](#2-task-的组织形式)
    - [2.1 磁盘布局](#21-磁盘布局)
    - [2.2 task.toml schema](#22-tasktoml-schema)
    - [2.3 多步任务](#23-多步任务)
    - [2.4 Task 的三种来源身份](#24-task-的三种来源身份)
    - [2.5 Dataset 层](#25-dataset-层)
    - [2.6 Verifier 的两种组织方式](#26-verifier-的两种组织方式)
    - [2.7 Artifacts 的收集与再投递](#27-artifacts-的收集与再投递)
    - [2.8 运行期目录：Trial 与 Job](#28-运行期目录trial-与-job)
  - [3. ATIF 轨迹的记录方式](#3-atif-轨迹的记录方式)
    - [3.1 规范结构](#31-规范结构)
      - [根对象 `Trajectory`](#根对象-trajectory)
      - [`Step`](#step)
      - [关联与嵌套](#关联与嵌套)
      - [多模态](#多模态)
    - [3.2 模型层的硬约束](#32-模型层的硬约束)
    - [3.3 版本演进](#33-版本演进)
    - [3.4 写入链路](#34-写入链路)
    - [3.5 读取链路](#35-读取链路)
    - [3.6 校验与消费](#36-校验与消费)
  - [4. 端到端走一遍](#4-端到端走一遍)
  - [5. 速查表与已知坑](#5-速查表与已知坑)
    - [速查表](#速查表)
    - [已知坑](#已知坑)

---

## 1. 仓库分层与主链路

```
JobConfig ──► Job ──► TrialQueue ──► Trial (SingleStep / MultiStep)
                                        ├── Task            从目录加载的"任务包"
                                        ├── BaseEnvironment docker / daytona / modal / ...
                                        ├── BaseAgent       claude-code / codex / terminus-2 / ...
                                        └── BaseVerifier    跑 tests/，产出 reward
```

| 层 | 职责 | 入口 |
| --- | --- | --- |
| Job | 一次实验：把 config 展开成一批 trial，并发跑、重试、聚合统计 | `src/harbor/job.py`、`src/harbor/job_plan.py`、`src/harbor/models/job/config.py` |
| Trial | 一次 `agent × task` 尝试：环境生命周期、agent 阶段、判分阶段、结果落盘 | `src/harbor/trial/trial.py`（基类）、`single_step.py`、`multi_step.py` |
| Task | 磁盘上的任务目录：解析 `task.toml`、校验、提供 instruction / step / 测试路径 | `src/harbor/models/task/task.py`、`config.py`、`paths.py` |
| Trajectories | ATIF 轨迹的 Pydantic 模型 | `src/harbor/models/trajectories/`、`rfcs/0001-trajectory-format.md` |

> **注意**：`AGENTS.md` 的目录树里写的 `src/harbor/orchestrators/` 在仓库中**不存在**；编排逻辑实际在 `job.py` + `trial/queue.py` + `trial/trial.py`。

---

## 2. Task 的组织形式

### 2.1 磁盘布局

单步任务（`src/harbor/models/task/paths.py:13`）：

```
my-task/
├── instruction.md          # 给 agent 的自然语言指令（必需）
├── task.toml               # 配置（必需）
├── trajectory.json         # 可选：ATIF 形式的"先验上下文"，启动前注入 agent session
├── environment/            # 必需：Dockerfile / docker-compose.yaml / Apptainer.def ...
├── solution/               # 可选：solve.sh（OracleAgent 专用，拷到容器 /solution）
└── tests/                  # 验证脚本 test.sh（拷到容器 /tests）
```

约定与校验：

- **脚本发现顺序**：`discover_script()` 只支持 `.sh` 与 `.bat`，`.sh` 优先；按 `[environment].os` 过滤（Linux 只看 `.sh`，Windows 只看 `.bat`）。需要 PowerShell 要在 `.bat` 里自己调（`src/harbor/utils/scripts.py:23`）。
  ⚠️ `task.py` 文档串里写的 `solve.{sh,ps1,cmd,bat}` 是**过期描述**。
- **step 名即目录名**：`validate_step_name()` 禁止路径分隔符、Windows 保留字符/设备名（`CON`、`LPT1`…）、结尾点或空格、超过 255 UTF-8 字节；去重用 `NFC + casefold`，防止大小写不敏感或 Unicode 归一化的文件系统上撞车（`src/harbor/models/task/step_name.py`）。
- **输入树安全**：`validate_input_tree()` 检查 `steps/`、`tests/`、`solution/`、`environment/` 下没有逃出任务目录的软链接、没有目录链接环（`src/harbor/utils/path_safety.py:5`）。不存在的目录会被安全跳过。
- **canary 串**：`instruction.md` 开头的 `<!-- ...canary... -->` 或 `# ...canary...` 注释行会被 `strip_canary()` 剥掉，避免基准数据污染标记泄漏给 agent（`src/harbor/models/task/task.py:13`）。
- **额外指令注入**：`extra_instruction_paths` / `extra_instructions`（job/trial 级）会追加到 instruction 末尾，先文件内容、后内联字符串。

### 2.2 task.toml schema

`TaskConfig`（`src/harbor/models/task/config.py:801`），当前 `schema_version = "1.4"`：

| 段 | 类型 | 关键字段 |
| --- | --- | --- |
| `schema_version` | str | 默认 `"1.4"`；旧键 `version` 自动改名 |
| `[task]` | `PackageInfo` | `name`（`org/name`）、`version`、`description`、`authors`、`keywords` |
| `[metadata]` | 自由 dict | `difficulty`、`category`、`tags`…（不参与运行） |
| `[agent]` | `AgentConfig` | `timeout_sec`、`user`（以哪个 UID/用户名跑 agent）、网络策略 phase 覆盖 |
| `[verifier]` | `VerifierConfig` | `timeout_sec`、`user`、`env`、`environment_mode`、`environment`、`[[verifier.collect]]` |
| `[environment]` | `EnvironmentConfig` | `build_timeout_sec`、`docker_image`、`os`、`cpus`/`memory_mb`/`storage_mb`/`gpus`/`tpu`、`mcp_servers`、`env`、`skills_dir`、`healthcheck`、`workdir`、`network_mode`/`allowed_hosts` |
| `[solution]` | `SolutionConfig` | `env` |
| `[[steps]]` | `list[StepConfig]` | `name`、`agent`、`verifier`、`min_reward`、`healthcheck`、`artifacts` |
| `artifacts` | `list[str \| ArtifactConfig]` | 任务级产物收集声明 |
| `multi_step_reward_strategy` | `"mean"`（默认）/ `"final"` | 多步 reward 聚合策略 |

设计上较真的几点：

- **网络策略三层**：`NetworkMode = no-network | public | allowlist`。环境基线用 `BaselineNetworkPolicyConfig`；`[agent]` / `[verifier]` 用 `PhaseNetworkPolicyConfig`，只做**显式覆盖**（未设置即继承）。allowlist 条目被归类为 hostname / 通配 hostname / IPv4 / IPv6 / CIDR，并拒绝 URL、端口、路径（`config.py:111`）。
- **兼容性迁移内建**：`allow_internet`（废弃）→ `network_mode`；`memory`/`storage` 字符串 → `memory_mb`/`storage_mb`，冲突时报错；`mounts_json` → `mounts`。
- **解析即校验**：`Task.__init__` 校验测试脚本存在性、verifier 的有效 OS、`bundled_tests`、artifact 集是否互相遮蔽、step 名唯一性。
- `Task.checksum` 已废弃，改用 trial lock 里的 `TrialLock.task.digest`。

### 2.3 多步任务

```
my-task/
├── task.toml               # 顶层按执行顺序声明 [[steps]]
├── environment/Dockerfile  # 所有 step 共享同一个容器（文件系统跨步持久）
├── tests/helpers.sh        # 可选：共享判分工具，先拷到 /tests，再被该 step 的 tests/ 覆盖叠加
└── steps/
    ├── <step-1>/
    │   ├── instruction.md      # 必需
    │   ├── tests/test.sh       # 可选（有独立 verifier 镜像时可省）
    │   ├── solution/solve.sh   # 可选
    │   ├── workdir/            # 可选：该 step 开始前上传到 agent 工作目录
    │   │   └── setup.sh        # 可选：上传后以 bash 执行
    │   └── trajectory.json     # 可选，仅第一个 step 目录会被读取
    └── <step-2>/...
```

`StepConfig` 可配：`name` / `agent`（timeout、user）/ `verifier`（timeout、user、env、mode、environment）/ `min_reward` / `healthcheck` / `artifacts`。

每步的执行顺序（`src/harbor/trial/multi_step.py:100`）：

```
创建 step 目录
→ 复位 /logs/agent（resume 时保留）
→ 复位上一步 shared verifier 留下的 /tests 与 /logs/verifier
→ 上传 steps/<name>/workdir/ 到工作目录，跑 setup.sh
→ step 级 healthcheck
→ agent 阶段（可 resume / load）
→ 上传 agent 日志、回填 context
→ 采集 artifacts
→ 判分（shared 或 separate）
→ 把 agent/ verifier/ artifacts/ 搬进 steps/<name>/
→ 判断 min_reward，决定是否继续
```

**早停**：`min_reward` 是 float 则只检查 `reward` 键，是 dict 则每个键都必须达标（缺失键视为 `-inf`，相等算通过）。step 出错且没有 verifier 结果也直接终止剩余步骤。验证被全局关闭时阈值检查被忽略。

**trial 级 reward**：`multi_step_reward_strategy` = `"mean"`（对每个 reward key 在各 step 间取均值，缺失键按 0，没有 verifier 结果的 step 不计入分母）或 `"final"`（直接取最后一个执行过的 step 的 verifier 结果）。

**resume**：`agent.resume_trajectory = true` 时第 2 步起调用 `agent.resume()` 而不是 `agent.run()`，并且不会清空 `/logs/agent`（原生 session 就在这里）。要求 agent 声明 `capabilities.resume`，否则 trial 在花环境钱之前直接失败。

### 2.4 Task 的三种来源身份

`src/harbor/models/task/id.py` + `src/harbor/tasks/client.py`：

| 类型 | 形态 | 物化方式 |
| --- | --- | --- |
| `LocalTaskId` | 本地目录 | 直接用 |
| `GitTaskId` | `git_url` + 可选 `git_commit_id` + 仓库内相对路径 | 浅克隆 + `sparse-checkout --no-cone --stdin`（绕开 Windows ~8192 字符命令行限制）、可选 `git lfs pull`；含软链接时用 `_materialize_task_tree` 把相对链接**实体化**并做环检测；路径逐段拒绝软链接 |
| `PackageTaskId` | `org/name@ref`（tag / 序号 / `sha256:...`） | RegistryDB 解析版本 + Supabase 下载 `dist.tar.gz`；缓存布局 `~/.cache/harbor/tasks/<org>/<name>/<content_hash>/`，`sha256` 命中直接短路 |

导出模式（`--export`）落成 `<output-dir>/<task-name>/` 扁平布局并强制任务名唯一；缓存模式落成内容寻址布局。

### 2.5 Dataset 层

`DatasetConfig`（`src/harbor/models/job/config.py:24`）支持四种源：

| 判定 | 形态 |
| --- | --- |
| `is_local()` | `path` |
| `is_package()` | `name` 含 `/`（`org/name@ref`） |
| `is_registry()` | `name` 不含 `/`，可配 `registry_url` / `registry_path` |
| `is_repo()` | `repo`（Git 仓库），可配 `registry_path` + `name` |

过滤：`task_names` / `exclude_task_names`（glob，用 `fnmatch` 匹配 task 名）、`n_tasks`（在过滤后截断）。

元数据模型 `DatasetMetadata`（`src/harbor/models/registry.py:23`）：`task_ids`、`metrics`、`files`（如 `metric.py`）、`dataset_version_id`。dataset 的 `source` 名会一路带到 `TrialResult.source`，成为 Job 统计分组用的 dataset 维度。

外部基准通过 `adapters/<name>/src/<pkg>/task-template/` 目录模板 + 占位符渲染成上述任务格式。

### 2.6 Verifier 的两种组织方式

`src/harbor/models/task/verifier_mode.py` 定义 `shared` / `separate`：

**模式解析优先级**（单步与多步略有差别）：

| 场景 | 解析顺序 |
| --- | --- |
| 单步 | `[verifier].environment_mode` > `[verifier.environment]` 存在即 separate > 默认 shared |
| 多步（每步） | `[steps.verifier].environment_mode` > `[steps.verifier.environment]` 存在即 separate > **继承 task 级解析** > 默认 shared |

**verifier 镜像优先级**（`resolve_verifier_environment_definition`）：

1. `[steps.verifier.environment].docker_image`
2. `steps/<name>/tests/Dockerfile` 或 `docker-compose.yaml`
3. `[verifier.environment].docker_image`
4. `tests/Dockerfile` 或 `docker-compose.yaml`
5. 回退到 agent 环境（`environment/`）

命中 1–4 时 `bundled_tests = True`：

- **运行期不再上传 `tests/`**（`skip_tests_upload=True`），镜像必须自带 `/tests/test.sh`；
- **任务校验不再要求宿主侧测试脚本存在**（`Task._validate_tests` 会 `continue`）。

命中 5 时按 shared 语义上传 `tests/`（多步先传 task 级 `tests/`，再用 step 级覆盖）。

**shared 模式的两个副作用**：

- 每步判分前 `_reset_shared_step_verifier_dirs()` 会清空 `/logs/verifier` 与 `/tests`；flag `_shared_verifier_dirs_need_reset` 确保"上一步真的跑过 shared verifier"才复位，不会误删镜像自带的 `/tests`。
- 判分脚本以 agent 容器身份执行，**能读到 agent 的全部痕迹**，但也意味着 agent 可能影响判分。

**reward 契约**：容器内 `/logs/verifier/reward.txt`（标量，如 `1.0`）或 `/logs/verifier/reward.json`（多维 dict）。两者都没有 → `RewardFileNotFoundError`；空文件 → `RewardFileEmptyError`；非数值或非有限值 → `VerifierOutputParseError`。Windows 任务路径前缀为 `C:/`。

**没有"per-step 关闭验证"的字段**：`VerifierConfig.disable` 只存在于 **trial 级运行配置**（`src/harbor/models/trial/config.py:350`，由 `--disable-verification` / `--install-only` 设置），对 task.toml 不可用。要让某个 step 不做实质校验，只能给它一个**什么都不做的测试脚本**，并靠 `multi_step_reward_strategy = "final"` 让它不污染总分。

### 2.7 Artifacts 的收集与再投递

声明位置三级合并：task 级 `artifacts`、step 级 `[[steps]].artifacts`、trial 级（job/trial config）。

- 每次 step 结束做**一次 collection pass**，操作对象是 `task 级 ∪ trial 级 ∪ 该 step 级`。
- 隐式注入发布目录 `/logs/artifacts`（agent 的"公布区"），除非常量条目已被显式声明（可带 `exclude`）。
- **宿主落盘是单一扁平目录**：绝对容器路径直接镜像到 `artifacts/` 下（`/var/log/x` → `artifacts/var/log/x`），service 名不进路径；冲突保留第一个并告警（`src/harbor/trial/artifact_handler.py:234`）。
- `destination` 只影响宿主落点，**不影响 verifier 侧**：投递到 verifier 时所有条目回到它们的原始 `source` 路径。
- `manifest.json` 记录每次尝试（`ok` / `empty` / `skipped` / `failed`）。下载是 **best-effort**，失败不中断 trial。
- 有 sidecar（compose 非 main 服务）时才做第二遍采集；separate verifier 且是最后一步时，会先停 main 服务再采 sidecar。
- `[[verifier.collect]]` 是"agent 阶段结束后、采集之前"在服务里跑的钩子，用来把运行态快照成文件。

### 2.8 运行期目录：Trial 与 Job

`TrialPaths`（`src/harbor/models/trial/paths.py:84`）单步：

```
<jobs_dir>/<job_name>/<task_name>__<7位随机>/     # trial_dir
├── agent/          # 挂载到容器 /logs/agent；trajectory.json、原生 sessions/ 在这里
├── user-agent/     # 仅模拟用户 trial 存在
├── verifier/       # 挂载到 /logs/verifier；test-stdout.txt、test-stderr.txt、reward.{txt,json}
├── artifacts/      # 收集到的产物 + manifest.json
├── config.json     # TrialConfig（复现用）
├── lock.json       # TrialLock：task digest、解析后的输入
├── result.json     # TrialResult
├── exception.txt
└── trial.log
```

多步 trial 把 `agent/ verifier/ artifacts/` 当**临时挂载点**，每步结束 relocate 到 `steps/<name>/`，最后 `cleanup_empty_mount_dirs()` 清掉空的根级目录：

```
<trial_dir>/steps/<step_name>/agent/trajectory.json
```

Job 层：

```
jobs/<job_name>/
├── config.json     # JobConfig
├── lock.json       # JobLock
├── job.log
├── result.json     # JobResult（JobStats + trial_results）
└── <每个 trial 一个子目录>
```

- **并发与重试**：`TrialQueue`，总并发 `n_concurrent_trials`，另有 per-agent 的 `n_concurrent` 子池（`concurrency_group` 可共享池）；重试按异常白/黑名单 + 指数退避，重试前 `rmtree` 失败的 trial 目录。
- **断点续跑**：Job 启动读回 `result.json` + 每个 trial 的 `config.json`，按配置相等性对账，只跑剩下的；`config.json` 不匹配则 `FileExistsError`。
- **统计分组键**：`agent__model__dataset`（`src/harbor/models/job/result.py:60`）；产出 `reward_stats`（值 → trial 名）、`metrics`、`pass_at_k`。
- **脱敏**：`Trial._scrub_jobs_dir()` 会把 job 目录下所有文本文件里出现的敏感 env 值替换为 `[REDACTED]`。
- **regrade / diff**：可以从已有 job 派生"只重跑判分"或"只跑变化部分"的 trial。

---

## 3. ATIF 轨迹的记录方式

ATIF = **A**gent **T**rajectory **I**nterchange **F**ormat，规范见 `rfcs/0001-trajectory-format.md`（RFC 0001）。目标是同一份数据可用于调试、可视化、SFT、RL。

### 3.1 规范结构

#### 根对象 `Trajectory`

| 字段 | 状态 | 说明 |
| --- | --- | --- |
| `schema_version` | 必需 | 模型接受 `ATIF-v1.0` … `ATIF-v1.8`，**模型默认值是 v1.8**（`src/harbor/models/trajectories/trajectory.py:15`） |
| `agent` | 必需 | `name` + `version` 必需；`model_name`、`tool_definitions`（OpenAI function-calling schema）、`extra` 可选 |
| `steps` | 必需 | `min_length=1`；`step_id` 必须**从 1 连续递增** |
| `session_id` | 可选（v1.7 起） | **run 级**标识：父轨迹、内嵌 subagent、续接分片可共享 |
| `trajectory_id` | 可选 | **document 级**标识；v1.7 引入；内嵌 subagent 上必需且同一父数组内唯一 |
| `final_metrics` | 可选 | token / cost / step 总数聚合 |
| `continued_trajectory_ref` | 可选 | 指向续接文件（上下文压缩产生多文件时） |
| `subagent_trajectories` | 可选（v1.7） | 内嵌的完整 ATIF 文档数组 |
| `notes` / `extra` | 可选 | 说明 / 自定义元数据 |

#### `Step`

`step_id`、`timestamp`（ISO 8601，真校验）、`source ∈ {system, user, agent}`、`model_name`、`reasoning_effort`、`message`（`str` 或 `list[ContentPart]`）、`reasoning_content`、`tool_calls`、`observation`、`metrics`、`llm_call_count`、`is_copied_context`、`extra`。

#### 关联与嵌套

- `Observation.results[].source_call_id` 必须能在**同一步**的 `tool_calls[].tool_call_id` 中找到（`trajectory.py:164`）。
- `SubagentTrajectoryRef` 必须"可解析"：至少给 `trajectory_id`（内嵌）或 `trajectory_path`（外链）之一；**`session_id` 单独存在不算有效引用**（v1.6 → v1.7 的破坏性变更）。
- `Step.is_copied_context = True` 表示"从旧轨迹复制过来的上下文"，SFT 消费方**必须**过滤。
- `extra.context_management = {type: compaction|pruning|injection, boundary: replace|append|truncate}`：`boundary = "replace"` 时，后续步骤的上下文窗口 = 该 step 的 observation content + 之后的新轮次。

#### 多模态

- v1.6：`ContentPart`（`type ∈ {text, image}`）+ `ImageSource`（`media_type` + `path`）。媒体**按路径引用**，不内联 base64；本地约定放 `images/` 子目录。
- v1.8：新增 `type = "audio"` + `AudioSource`（多一个可选 `duration_sec`，因为按秒计费且不解码拿不到）；MIME 别名（`audio/mp3`、`audio/x-wav`…）会在校验时归一化到注册类型；约定放 `audio/` 子目录。
- 流式/实时音频（裸 PCM、G.711）与视频**明确不在范围内**。

### 3.2 模型层的硬约束

`src/harbor/models/trajectories/step.py`：

1. `source != "agent"` 时，`model_name` / `reasoning_effort` / `reasoning_content` / `tool_calls` / `metrics` **一律禁止出现**。
2. `llm_call_count == 0` 且 `source == "agent"` ⇒ 这是"确定性派发"（图引擎/规则管线，无 LLM 推理），`metrics` 与 `reasoning_content` 必须缺席。
3. 所有模型 `extra="forbid"` —— **未声明字段一律报错**；扩展只能走各级 `extra`。
4. 内嵌 subagent 必须带唯一 `trajectory_id`。

### 3.3 版本演进

| 版本 | 关键新增 |
| --- | --- |
| v1.1 | 根级 `extra` |
| v1.2 | system step 也能带 `observation`；明确 `prompt_tokens` 含缓存 |
| v1.3 / v1.4 | `completion_token_ids` / `prompt_token_ids`（避免 RL 重分词漂移） |
| v1.5 | `tool_definitions`；`is_copied_context` |
| v1.6 | 多模态 `ContentPart` + `ImageSource` |
| v1.7 | `subagent_trajectories`、`trajectory_id`、`llm_call_count`、`ToolCall.extra`、`ObservationResult.extra`、`context_management` |
| v1.8 | `audio` content type + `AudioSource` + MIME 别名归一化 |

**版本不统一是常态**：模型默认 v1.8，但实际产出因 agent 而异——claude-code / codex / junie 等写 `v1.7`，cline / copilot / acp 写 `v1.6`，cortex-code / goose / hermes 还写 `v1.2`。文档 `docs-mintlify/agents/atif.mdx` 说"当前 v1.7、接受 v1.0–v1.7"，实际代码已接受 v1.8。

### 3.4 写入链路

**统一落点**：`self.logs_dir / "trajectory.json"`，即宿主上的 `trial_dir/agent/trajectory.json`（多步：`trial_dir/steps/<step>/agent/trajectory.json`）。能力由 `AgentCapabilities.atif` 声明。

三种生产方式：

**(a) 宿主端从原生日志转换（最主流）**

`BaseInstalledAgent` 定义两个钩子（`src/harbor/agents/installed/base.py:359`）：

```python
@property
def remote_session_logs_dir(self) -> PurePosixPath | None: ...      # 原生 jsonl 目录
def convert_trajectory(self, logs_dir: Path) -> Trajectory | None: ...  # 原生 → ATIF
```

以 claude-code 为例（`src/harbor/agents/installed/claude_code.py:606`、`:1632`）：先把 session jsonl 规范化成 `message` / `agent_step` / `tool_call` 三类事件，每类映射成一个 `Step`（user → `source="user"`；assistant → `source="agent"` + `llm_call_count=1` + `model_name` + `reasoning_content` + `metrics`；工具调用 → `ToolCall` + 对应 `ObservationResult`），再算 `FinalMetrics`（token 聚合 + 成本：优先用 stream-json 的真实 `total_cost_usd`，否则 LiteLLM 估算并在 `final_metrics.extra.cost_source` 标注）。

`populate_context_post_run()` 写文件并**回填 `AgentContext`**（`cost_usd` / `n_input_tokens` / `n_cache_tokens` / `n_output_tokens`）——ATIF 不只是日志，它也是 token/成本统计的权威来源。

**(b) Agent 运行中直接构造 ATIF**

terminus-2（`src/harbor/agents/terminus_2/terminus_2.py:2025`）、computer-1、openhands-sdk、antigravity-sdk、nemo-agent（借 NAT 的 `IntermediateStepToATIFConverter`）等。terminus-2 的做法很典型：

- 边跑边用 Pydantic 模型累积 `Step`；
- 上下文压缩时把子轨迹单独写 `trajectory.summarization-<n>-{summary,questions,answers}.json`，用 `SubagentTrajectoryRef` 挂到父轨迹；
- `linear_history` 模式下按片切分 `trajectory.json` / `trajectory.cont-1.json` / …，用 `continued_trajectory_ref` 串起来，并把跨片重复的 step 标 `is_copied_context=True`。

**(c) 流式预写（run 进行中持续刷新）**

`src/harbor/trial/sync_trajectory.py` 是一个 `asynccontextmanager`，被 `Trial._run_agent_phase()` 用来**包住 agent 的执行**：

- 启用条件：非模拟用户 trial + `environment.stream_enabled` + agent 是 `BaseInstalledAgent` + `remote_session_logs_dir` 非 None；
- 后台任务每 2 秒通过 SSH `find . -name '*.jsonl' | tar -czf -` 拉回原生日志 → 调 `agent.convert_trajectory()` → 复制轨迹引用的媒体文件（逐个 `validate_output_path` 防路径逃逸）→ **原子写** `trajectory.tmp` 再 `replace` 成 `trajectory.json`；
- `finally` 先停轮询，再让 agent 的 `populate_context_post_run` 写"权威版本"。所以运行中能实时看到轨迹，最终文件一定是最新最全的。

**(d) 收尾与回填**

`Trial._sync_agent_output()`：下载 agent 日志 → `populate_context_post_run` → 若 `model_usage` 仍为空，就从 `agent/trajectory.json` 解析每模型用量（`src/harbor/utils/trajectory_utils.py:54`）。

### 3.5 读取链路

两个层级（`src/harbor/trial/trial.py:1206`）：

| 层级 | 配置 | 格式 | 关系 |
| --- | --- | --- | --- |
| Task 级 | 任务目录里的 `trajectory.json`（多步放**第一个** step 目录） | **只支持 ATIF** | task 是 agent 无关的，所以只能用可移植格式 |
| Run 级 | `--load-trajectory` / `agents[].load_trajectory` | `.json` = ATIF；其他后缀 = 原生（如 `.jsonl`） | **覆盖** task 级 |

实现细节：

- `_resolve_load_trajectory()`：run 级优先；`oracle` 与 `nop` 会被跳过（它们不消费先验上下文，且 oracle 是默认 agent，task 作者常用它自检）。
- **fail-fast 在花钱之前**：task 级轨迹先做 `Trajectory.model_validate_json()`；为了不让一个坏 task 取消同 job 的兄弟 trial，错误会暂存到 `_prepare()` 才抛。run 级则直接检查能力位与文件存在性。
- ATIF → 原生：agent 实现 `atif_to_native_trajectory(trajectory, session_id) -> (filename, content)` + `_upload_load_trajectory()`；`_atif_session_id()` 在 `session_id` 是合法 UUID 时复用它，否则铸一个新的。
- 与 `resume_trajectory` 组合：多步是 `(load, resume, resume, …)`，否则 `(load, fresh, fresh, …)`。
- **加载不恢复沙箱文件**，只恢复会话内容。

### 3.6 校验与消费

- **校验器**：`src/harbor/utils/trajectory_validator.py`
  `uv run python -m harbor.utils.trajectory_validator path/to/trajectory.json`
  做三件事：Pydantic schema 校验（把 `extra_forbidden` 翻译成 "not part of ATIF schema"，一次收齐所有错误）；连续性与引用关系（由模型 validator 完成）；**引用媒体文件是否存在**（`--no-validate-images` 可跳过，URL 跳过）。
- **查看器**：`src/harbor/viewer/server.py:2383` 的 `GET /api/jobs/{job}/trials/{trial}/trajectory`（支持指定 step）直接读 `<trial>/agent/trajectory.json`；模拟用户 trial 另外读 `user-agent/trajectory.json` 与 `agent/bridge-trajectory.json`。
- **导出训练数据**：`src/harbor/utils/traces_utils.py` 只发现含 `agent/trajectory.json` 的 trial，把 ATIF steps 折成 `conversations` 行，可输出 ShareGPT；混入多模态时抛 `MultimodalExportError` 而不是静默丢数据。多步任务优先读主 `trajectory.json`，否则回退到最早的 continuation 分片。
- **下载**：`harbor trials download --trajectory` 只勾 `trajectory.json`。

---

## 4. 端到端走一遍

以 `examples/tasks/hello-world` + `claude-code` 为例：

1. `harbor run -p examples/tasks/hello-world -a claude-code -m …`
   → `JobConfig` → `JobPlan` 把 task 展开成 `TrialConfig`（trial 名 = `hello-world__<7位随机>`）。
2. `Job.create()`：agent preflight、解析 metrics、把 task 缓存到 `~/.cache/harbor/tasks/…`、写 `jobs/<name>/lock.json`。
3. `TrialQueue` 起 `Trial.create()` → `SingleStepTrial`；`Task` 读取 `task.toml` 与 `instruction.md` 并校验。
4. `_prepare()`：起容器、healthcheck、上传 skills、`agent.setup()`。
5. `_run_agent_phase()`：以 `sync_trajectory` 包住 `agent.run()`；`agent/` bind-mount 到容器 `/logs/agent`，原生 session 落在 `/logs/agent/sessions/…`，宿主每 2 秒把它转成 ATIF 并原子写 `agent/trajectory.json`。
6. `_sync_agent_output()`：写最终 ATIF（含 `agent`、逐步 `tool_calls`/`observation`/`metrics`、`final_metrics`），并用它回填 `AgentContext` 的 token/成本。
7. 采集 artifacts → 跑 `tests/test.sh` → `/logs/verifier/reward.txt` → 落 `verifier/reward.txt`。
8. `result.json` 写 `TrialResult`；Job 端 `JobStats.increment()` 把它记进 `evals["claude-code__<model>__adhoc"]`。
9. 打开 viewer 的 **Trajectory** 页签，渲染 `agent/trajectory.json`。

多步 trial 的差异：每步结束后 `agent/ verifier/ artifacts/` 被搬进 `steps/<name>/`，所以轨迹路径变成 `steps/<name>/agent/trajectory.json`；若开启 `resume_trajectory`，`/logs/agent` 跨步保留，最后一步的 `trajectory.json` 会包含整条连续会话。

---

## 5. 速查表与已知坑

### 速查表

| 想做的事 | 怎么做 |
| --- | --- |
| 新建单步任务 | `harbor task init org/name -p <dir>` |
| 新建多步任务 | `harbor task init ... --steps N` |
| 任务配置 JSON Schema | `harbor task schema` |
| 本地跑一个任务 | `harbor run -p <task-dir> -a <agent> -m <model>` |
| 用参考解冒烟 | `harbor run -p <task-dir> -a oracle` |
| 校验轨迹 | `uv run python -m harbor.utils.trajectory_validator <trajectory.json>` |
| 只下轨迹 | `harbor trials download --trajectory` |
| 只重跑判分 | `harbor job regrade`（source_jobs action = `regrade`） |
| 给 agent 传环境变量 | `--ae KEY=VALUE` |

### 已知坑

1. **`session_id` ≠ `trajectory_id`**：前者 run 级（可重复、可省略以继承父 run），后者 document 级（解析内嵌 subagent 的唯一钥匙）。v1.7 之前用 `session_id` 当解析键，现已废止。
2. **一份轨迹可以有多个文档**：`trajectory.json` + `trajectory.cont-N.json` + `trajectory.summarization-*.json` + `subagent_trajectories`，靠 `continued_trajectory_ref` / `SubagentTrajectoryRef` / `trajectory_id` 串起来。
3. **`extra` 是唯一扩展口**，因为所有模型 `extra="forbid"`。
4. **没有 per-step 关闭验证的字段**。要让某步不实质校验，只能给一个空测试脚本写恒定 reward，并配 `multi_step_reward_strategy = "final"`。
5. **separate verifier 拿不到 agent 的文件系统**。能进 verifier 容器的只有：`/logs/artifacts`（隐式发布目录）+ 声明的 artifacts（回到原始 source 路径）。要分析执行过程就显式声明 `/logs/agent/trajectory.json`。
6. **多步 trial 的 `/logs/agent` 每步会被清空**（除非 `resume_trajectory = true`），所以容器里最后只留当前步的轨迹；历史各步的轨迹在宿主 `steps/<name>/agent/` 下。
7. **Token 语义**：`prompt_tokens` **包含**缓存命中，`cached_tokens` 是其子集。成本公式 `(prompt - cached) × 单价 + cached × 缓存价 + completion × 输出价`；Anthropic 的 `cache_creation_input_tokens` 等额外计费项放 `metrics.extra`。
8. **`${VAR}` 无默认值时缺失会直接报错**（`resolve_env_vars`）。要可选就写 `${VAR:-}` 或注释掉。
9. **文档滞后点**：
   - `AGENTS.md` 里的 `src/harbor/orchestrators/` 不存在；
   - `task.py` / `paths.py` 文档串里的脚本扩展名 `ps1`/`cmd` 实际不支持（只有 `.sh` / `.bat`）；
   - `TrialPaths` 文档串写 `results.json`，实际是 `result.json`；
   - `docs-mintlify/agents/atif.mdx` 说"当前 v1.7、接受 v1.0–v1.7"，代码已接受 v1.8 且模型默认 v1.8。
10. **`disable_verification` 只关掉宿主侧的 artifact 检查**，路径包含性检查仍然执行。
