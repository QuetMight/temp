# datacom —— 三步任务骨架

一个 `new_spec → new_plan → implement` 的多步任务骨架：

- 前两步的 **verify 流程置空**（只写恒定 reward，不做任何校验）；
- 只有最后一步 `implement` 做真正的验证；
- 自定义 verifier 用 **separate** 方式（独立容器），判分流程交给
  `steps/implement/tests/verify.py` 管理（构建 / 测试 / LLM 打分 / 执行过程分析）。

`solution/solve.sh` 有意留成占位，由你自行实现。

---

## 目录结构

```
datacom/
├── task.toml                       # 三步声明 + separate verifier 配置
├── environment/
│   └── Dockerfile                  # agent 环境（三步共享同一容器）
└── steps/
    ├── new_spec/
    │   ├── instruction.md          # 本步指令
    │   ├── tests/test.sh           # 占位 verifier（空：只写 reward 1.0）
    │   └── solution/solve.sh       # ⚠️ 待你实现
    ├── new_plan/
    │   ├── instruction.md
    │   ├── tests/test.sh           # 占位 verifier（空：只写 reward 1.0）
    │   └── solution/solve.sh       # ⚠️ 待你实现
    └── implement/
        ├── instruction.md
        ├── tests/                  # ← 独立 verifier 镜像的构建上下文
        │   ├── Dockerfile          # verifier 镜像；把本目录 COPY 到 /tests
        │   ├── test.sh             # verifier 入口：exec python3 /tests/verify.py
        │   └── verify.py           # ⚠️ 判分总控脚本，待你实现
        └── solution/solve.sh       # ⚠️ 待你实现
```

---

## 关键设计决策（以及为什么这么做）

### 1. 中间步骤"verify 置空"是怎么实现的

Harbor **没有** per-step 关闭验证的字段：`VerifierConfig.disable` 只存在于
**trial 级运行配置**（由 `--disable-verification` / `--install-only` 设置），
`task.toml` 里无法表达"这一步不验证"。

而且 `Task._validate_tests` 要求每个 step 都必须能解析出一个测试脚本，除非该
step 的 verifier 用的是自带测试的独立镜像（`bundled_tests`）。

所以这里采用：

- 前两步给一个**什么都不做的 `tests/test.sh`**，它只满足契约：
  `mkdir -p /logs/verifier && echo 1.0 > /logs/verifier/reward.txt`。
  以 `environment_mode = "shared"` 运行，不额外起容器。
- `multi_step_reward_strategy = "final"`，让试炼级 reward **只取最后一步**的
  verifier 结果，于是前两步那个恒为 1.0 的占位值不会稀释分数。

如果你想给中间步骤加真校验（例如断言 `spec.md` 各章节齐全），改那个 `test.sh`
把 reward 写成 0/1，并在 `task.toml` 对应 step 上加 `min_reward`（未达标即跳过
剩余步骤）。

### 2. 为什么用 `artifacts` 而不是让 verifier 直接读 agent 的文件系统

separate verifier 跑在一个**全新容器**里，agent 的文件系统改动**不会继承**。
能进 verifier 容器的只有：

1. 隐式注入的发布目录 `/logs/artifacts/`（agent 主动往里写东西就能带过去）；
2. 显式声明的 artifacts，投递时回到它们的**原始 source 路径**。

因此 `implement` step 上声明了：

```toml
artifacts = [
    "/app/spec.md",                    # 第 1 步产出（容器跨步持久，仍在）
    "/app/plan.md",                    # 第 2 步产出
    "/logs/agent/trajectory.json",     # ATIF 轨迹，用于"执行过程分析"
]
```

在 verifier 容器里，它们分别出现在 `/app/spec.md`、`/app/plan.md`、
`/logs/agent/trajectory.json`。

`artifacts` 只挂在最后一步，是为了让前两步不做无谓的采集（若挂在 task 级，
每一步都会各采一遍）。

> 采集是 **best-effort**：文件不存在只会记到 `artifacts/manifest.json` 里标为
> `failed`，不会中断试炼。所以 `verify.py` 必须自己判断文件是否缺失。

### 3. 为什么 verifier 镜像来自 `steps/implement/tests/Dockerfile`

Harbor 的 verifier 镜像优先级是：

| 优先级 | 定义 | 测试脚本来源 |
| --- | --- | --- |
| 1 | `[steps.verifier.environment].docker_image` | 必须烘进镜像 |
| 2 | `steps/<name>/tests/Dockerfile` | 必须烘进镜像 |
| 3 | `[verifier.environment].docker_image` | 上传 `tests/` |
| 4 | `tests/Dockerfile` | 上传 `tests/` |
| 5 | 回退 agent 环境 | 上传 `tests/` |

命中 1–4 时 `bundled_tests = True`，**运行期不再上传 `tests/`**，镜像必须自带
`/tests/test.sh`。`task.toml` 故意不设 `docker_image`，于是优先级 2 生效，
`steps/implement/tests/` 整个目录成为镜像构建上下文，`verify.py` 由
`Dockerfile` 里的 `COPY . /tests/` 带进镜像。

好处：判分依赖（LLM 客户端、编译器、测试框架）都能提前烘进镜像，避免运行期
安装抖动；`verify.py` 的改动只需要重建 verifier 镜像。

### 4. 判分脚本的契约

Harbor 只认两件事：

1. **退出码**：非 0 会把这一步标记为 verifier 异常；
2. **reward 文件**：
   - 标量 → `/logs/verifier/reward.txt`（内容是一个有限浮点数，如 `1.0`）
   - 多维 / 带标签 → `/logs/verifier/reward.json`（`{"key": number, ...}`）

两者都不存在 → `RewardFileNotFoundError`；空文件 → `RewardFileEmptyError`；
非数值或 NaN/Inf → `VerifierOutputParseError`。

`reward.json` 里的每个 key 都会进入试炼结果；若在 `task.toml` 里用
`min_reward = { key = 阈值 }` 就是按 key 卡。

### 5. ⚠️ 一个必须知道的副作用

Harbor 判断"某一步失败、要不要中止剩余步骤"的逻辑是（`multi_step.py`）：

```python
if step_result.exception_info and not step_result.verifier_result:
    # 中止剩余步骤
```

也就是说 **"有 verifier 结果" 会被当成 "这一步没崩"**。而占位 verifier 无论
agent 是否失败都会写出 `reward = 1.0`，所以：

> **前两步里 agent 超时或报错时，`implement` 仍然会被执行。**

这不是 bug，是"verify 置空"的直接代价 —— 没有校验就没有失败信号。你有三种处理
方式：

1. **接受它**（骨架默认）。最后一步的独立 verifier 才是唯一的判分，前两步崩了
   也能从最终结果看出来（spec/plan 缺失 → 采集标 `failed` → `verify.py` 扣分）。
2. **让占位 verifier 做一个最廉价的检查**：断言该步的交付物存在
   （`test -s /app/spec.md`），把 reward 写成 0/1，并在该 step 上加
   `min_reward = 1.0`。这样 agent 失败 → 交付物缺失 → reward 0 → 自动中止。
   这仍然不是"实质校验内容质量"，只是把"有没有产出"变成信号。
3. **在 job 配置里统一 `--disable-verification`**（trial 级），代价是连最后一步的
   判分也一起关掉 —— 对本任务不合适。

方案 2 的改法很小，只需要动 `steps/<name>/tests/test.sh` 和 `task.toml` 里对应
step 的一行。

---

## 中间产物回传（verifier → 宿主机）

verifier 跑在独立容器里，但它的中间产物需要能回到跑 harbor 的宿主机（构建日志、
测试报告、LLM 原始响应、过程分析明细）。Harbor 只接了**一个**通道。

### 写 `/logs/verifier/`

容器里写 `/logs/verifier/**` 的任何文件都会被回传：

| 环境 | 机制 |
| --- | --- |
| **mounted**（docker，默认） | `/logs/verifier` 是**实时 bind mount**，写进去立刻出现在宿主磁盘上 |
| **非 mounted**（daytona / modal / e2b / runloop…） | verify 阶段结束后把整个 `/logs/verifier` 下载回宿主 |

### 宿主上的位置（多步会"搬家"）

```
jobs/<job_name>/<task>__<rand>/verifier/…                     # 运行中 / 单步 trial
jobs/<job_name>/<task>__<rand>/steps/implement/verifier/…      # 多步 trial 的最终位置
```

多步 trial 在该 step 的 verify 结束后，会把 `trial_dir/verifier/` 的内容整体
relocate 到 `steps/<step_name>/verifier/`；这一步发生在 verifier 容器已经停掉之后，
不会有写入竞争。

### 保留文件名

| 路径 | 归属 |
| --- | --- |
| `reward.txt` / `reward.json` | reward 契约。`reward.json` 会被 Harbor 解析，**必须是纯数值的扁平 dict** —— 塞不进字符串或嵌套结构 |
| `test-stdout.txt` | Harbor 自己写：它把命令拼成 `(cmd) > /logs/verifier/test-stdout.txt 2>&1`，也就是说 **`verify.py` 的所有 print 都会落进这个文件**；`harbor analyze` / annotator 也会读它 |
| `test-stderr.txt` | `TrialPaths` 里有定义，但这条执行路径从不写入，别依赖 |

`verify.py` 已经把这套封装好了：`artifact_path()` / `write_text()` /
`write_json()` 会自动建父目录，并拒绝写上面这些保留名。

### 建议的产物布局

```
/logs/verifier/
├── reward.json          # Harbor 契约（纯数值、扁平）
├── test-stdout.txt      # Harbor 写：verify.py 的全部输出
├── report.json          # verify.py 写：汇总诊断
├── build/build.log
├── test/junit.xml
├── judge/response.json
└── trace/process-analysis.json
```

`report.json` 是机器可读的唯一出口（`schema = "datacom-verifier-report/1"`），内容
包括：每个阶段的状态（`ok` / `empty` / `skipped` / `not_implemented` / `error`）、
耗时、异常 traceback、阶段产出的分数、三个输入文件的存在性与大小、以及 `notes`。
因为 `reward.json` 只能是纯数值，"为什么失败""LLM 怎么说的"这些都必须放这里。

### 两个坑

1. **每次 verify 开始前 Harbor 会清空这个目录**（并 `chmod 777`），产物必须在本次
   verify 期间写出来；各步的产物分别归档到各自的 `steps/<name>/verifier/`，不要
   指望跨 step 累积。
2. **别往里拷大文件树**。docker 下它是实时 bind mount，体积会直接落到宿主磁盘，
   而且 `--verifier-exclude-logs` 的过滤**只对非 mounted 环境生效**。只回传日志、
   diff、指标、LLM 原始响应这类小文件。

---

## `verify.py` 需要实现的四个阶段

`steps/implement/tests/verify.py` 目前是可运行的骨架，每个阶段都是一个待填的
函数：

| 阶段 | 建议职责 | 可用输入 |
| --- | --- | --- |
| `phase_build` | 在 verifier 容器里构建被测代码，产出构建产物 / 编译日志 | `/app/`（含 spec、plan、实现代码） |
| `phase_test` | 跑功能测试、集成测试，解析 pass/fail | 构建产物、任务自带的测试用例 |
| `phase_judge` | 用 LLM 对实现质量 / 与 spec 的一致性打分 | `/app/spec.md`、`/app/plan.md`、实现 diff |
| `phase_analyze` | 分析执行过程（步数、工具使用、返工、是否走偏） | `/logs/agent/trajectory.json`（ATIF） |

每个阶段的返回约定：

```python
return {"build": 1.0}          # 正常产出分数 → 合并进 reward.json，report 记为 ok
return None                    # 尚未实现 → report 记为 not_implemented
raise Skipped("原因")          # 主动跳过（如没配 LLM key）→ report 记为 skipped
```

阶段内部抛出的其他异常会被 `main()` 捕获、把 traceback 记进 `report.json`，然后
继续跑后面的阶段 —— 单个阶段崩掉不应该让整份判分变成 verifier 错误。四个阶段都
没有产出分数时，脚本会写 `{"reward": 0.0}` 并打印醒目警告，这样 `harbor run` 仍能
跑完，你能先验证脚手架再填内容。

`verify.py` 里已经写好的辅助：

| 辅助 | 作用 |
| --- | --- |
| `load_trajectory()` | 读入 `/logs/agent/trajectory.json`，返回解析后的 dict（含 `steps`） |
| `artifact_path(*parts)` | 在 `/logs/verifier` 下开产物路径，自动建父目录、挡保留名 |
| `write_text(path, text)` / `write_json(path, payload)` | 写回传产物 |
| `run(cmd, ...)` | 在容器里执行命令并捕获输出，`check=True` 时失败即抛 |
| `note(msg)` | 记一条会出现在 `report.json.notes` 里的说明 |
| `emit_rewards(dict)` | 按契约写 reward 文件，过滤非数值 / NaN / Inf |
| `write_report(...)` | 汇总诊断写 `report.json`（由 `main()` 调用，一般不用手动调） |

---

## 运行

```bash
# 用 oracle（跑各步的 solution/solve.sh）冒烟，验证脚手架是否连通
uv run harbor run -p examples/tasks/datacom -a oracle

# 正常跑
uv run harbor run -p examples/tasks/datacom -a claude-code -m opus -e docker

# 传入 LLM 打分所需的宿主机变量（对应 [steps.verifier.env] 的 ${...}）
DATACOM_JUDGE_API_KEY=sk-... uv run harbor run -p examples/tasks/datacom -a codex -m gpt-5.6-sol
```

跑完之后看回传产物（多步 trial 的最终位置）：

```bash
TRIAL=$(ls -d jobs/*/*__*/ | head -1)
ls  "$TRIAL/steps/implement/verifier/"
cat "$TRIAL/steps/implement/verifier/report.json"       # 机器可读的汇总诊断
cat "$TRIAL/steps/implement/verifier/test-stdout.txt"   # verify.py 的全部输出
cat "$TRIAL/steps/implement/verifier/reward.json"       # Harbor 解析的分数
```

---

## 待办清单

- [ ] `environment/Dockerfile` —— 按 datacom 场景补齐编译器 / 构建系统 / 测试框架 / 数据文件
- [ ] `steps/*/instruction.md` —— 把指令改写成你真实的规格、计划、实现要求
- [ ] `steps/*/solution/solve.sh` —— 三个参考解
- [ ] `steps/implement/tests/Dockerfile` —— 安装 `verify.py` 需要的依赖
- [ ] `steps/implement/tests/verify.py` —— 四个阶段的实现（中间产物用
      `write_text()` / `write_json()` 写到 `/logs/verifier` 下回传）
- [ ] `[steps.verifier.env]` —— 需要用 LLM 打分时取消注释并配好 key
- [ ] `[task].name` / `[metadata]` —— 改成你的包名与元数据

---

## 可选的调整

### 让三步共享同一个 agent 会话

默认每个 step 都是全新对话，容器里的文件继续存在。若希望第 2、3 步续接上一步
的会话（对 spec → plan → implement 这种流程通常更自然，也让最后一步的
`/logs/agent/trajectory.json` 覆盖整条连续会话），在 job 配置或 `task.toml` 的
`[agent]` 之外启用：

```bash
uv run harbor run -p examples/tasks/datacom -a claude-code --resume-trajectory
```

或者在 job config 里：

```yaml
agents:
  - name: claude-code
    model_name: opus
    resume_trajectory: true
```

它要求 agent 声明 `capabilities.resume`，否则试炼会在启动环境之前就失败。
另外注意：`resume_trajectory` 属于**运行配置**，不能在 `task.toml` 里声明。

### 只让 agent 通过发布目录交付

如果不想逐个声明 artifact，可以在三步的指令里都要求把交付物写到
`/logs/artifacts/`（Harbor 隐式声明的发布目录，会被自动收集并投递到 verifier
容器的同一路径）。两种方式可以并存。

### 拆分 verifier 镜像

如果 verifier 镜像太大或构建太慢，可以先构建并推送镜像，然后在
`task.toml` 里直接写：

```toml
[steps.verifier.environment]
docker_image = "your-registry/datacom-verifier:latest"
```

此时优先级 1 生效，`steps/implement/tests/Dockerfile` 会被忽略 —— 记得把
`test.sh` 和 `verify.py` 烘进该镜像的 `/tests/`。
