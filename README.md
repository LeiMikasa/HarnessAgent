# HarnessAgent

用 Python 实现的模块化编码 Agent：模型负责决定下一步，运行时负责工具执行、权限检查、上下文管理、任务协作和运行记录。

项目参考 [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 的 Agent 构建思路，提供普通 CLI、Textual 终端界面和可选的 Docker 工具沙箱。支持 DeepSeek、Anthropic，以及无需 API 的离线 Mock 模式。

## 功能概览

| 能力 | 当前实现 |
|---|---|
| Agent Loop | 模型调用 → 工具执行 → 结果回传；完整回答经过 Stop Hook 后结束 |
| 基础工具 | `bash`、`read_file`、`write_file`、`edit_file`、`glob`、`grep` |
| 工具恢复 | JSON Schema 校验、结构化错误、有限自动重试、模型参数修正、未知写入状态检查 |
| 权限控制 | 硬拒绝列表、风险规则、人工批准或 allow/deny 策略 |
| Docker 沙箱 | 六个基础工具在容器内执行；挂载指定工作区，使用 bridge 网络 |
| 上下文管理 | 五层压缩、历史归档、工具结果落盘、上下文超限后的应急恢复 |
| Skills | 启动加载技能目录，按需读取 `SKILL.md` 正文 |
| 子 Agent | 独立对话完成一次委派，将结果返回主 Agent |
| 团队协作 | 后台线程队友、共享任务板、消息信箱、计划审核、Git worktree |
| 长期记忆 | 选择、提取与整合；区分长期信息和当前任务临时要求 |
| MCP | 连接外部工具，刷新后续模型调用可见的工具池 |
| 终端界面 | 流式回答、工具卡片、权限弹窗、输入历史与队友状态 |
| 诊断与评测 | 调用计数、供应商 token 用量、运行轨迹、独立验收与压缩对照 |

普通 CLI 默认关闭团队和长期记忆，保留上下文压缩、权限检查和诊断记录。需要时通过启动参数开启。

## 快速开始

以下命令以 Windows PowerShell 为例，建议使用 Python 3.11 或更新版本。在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 已有 .env 时保留原配置
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

### 先跑免费演示

```powershell
.\.venv\Scripts\python.exe -m agent --mock --demo --workdir ".scratch\quickstart"
```

演示使用预设的 Mock 响应，在指定工作区创建并验证 `greeting.py`，不调用真实模型。Mock 用于验证运行流程，不具备真实模型的推理能力。

### 配置真实模型

编辑 `.env`，例如：

```dotenv
AGENT_PROVIDER=deepseek
ANTHROPIC_API_KEY=填写你的供应商密钥
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
MODEL_ID=deepseek-v4-flash
AGENT_APPROVAL=ask
```

这里的模型名称与项目内置预设一致；实际可用名称取决于供应商及账号。查看解析后的配置不会调用模型：

```powershell
.\.venv\Scripts\python.exe -m agent --status
```

需要检查真实接口和工具调用往返时运行：

```powershell
.\.venv\Scripts\python.exe preflight.py --tools
```

`preflight.py` 会发送真实模型请求并消耗 API 额度。

切换到 Anthropic 时设置 `AGENT_PROVIDER=anthropic`、对应的 `MODEL_ID` 和密钥，并清空 `ANTHROPIC_BASE_URL`，让 SDK 使用默认端点。项目通过 Anthropic SDK 访问官方或兼容接口，不直接接收任意 OpenAI 格式接口。

### 指定开发目录启动

工作区是 Agent 要修改的项目目录，不必是 HarnessAgent 自身目录。

```powershell
# 普通交互模式
.\.venv\Scripts\python.exe -m agent --workdir "D:\btest"

# 终端界面
.\.venv\Scripts\python.exe -m agent --tui --workdir "D:\btest"

# 执行一个任务后退出
.\.venv\Scripts\python.exe -m agent --workdir "D:\btest" "检查项目并修复失败的测试"
```

## 启动选项与交互命令

| 参数 | 作用 |
|---|---|
| `--workdir PATH` | 指定目标项目目录 |
| `--tui` | 使用 Textual 界面，流式显示模型回答 |
| `--sandbox` | 将六个基础工具交给 Docker 执行 |
| `--teams` | 开启后台队友和团队工具 |
| `--memory` | 开启长期记忆 |
| `--full` | 同时开启团队和长期记忆 |
| `--no-teams`、`--no-memory` | 显式关闭对应功能，优先于 `--full` |
| `--provider`、`--model`、`--base-url` | 覆盖模型连接配置 |
| `--env-file PATH` | 指定配置文件 |
| `--approval ask\|allow\|deny` | 对需要批准的操作采用对应策略 |
| `--verbose` | 显示详细事件和工具结果预览 |
| `--quiet` | 关闭进度，只输出最终答案；不能与 `--tui` 同用 |
| `--no-trace` | 关闭运行诊断记录 |
| `--no-compaction` | 关闭上下文压缩 |
| `--mock` | 使用离线 Mock 模型 |

`--approval allow` 不会绕过硬拒绝列表，也不会扩大 Docker 文件工具允许访问的工作区。

普通交互模式支持：

```text
:help      :status    :tools     :skills    :todos
:tasks     :team      :mcp       :memory    :notes
:trace     :clear     :quit
:goal <验收条件>
:goal
:goal clear
```

`:clear` 清空当前对话和目标，不删除任务、记忆或磁盘记录。`:goal <验收条件>` 会启动任务，并在停止阶段通过模型判断目标是否达成，可能产生额外模型调用；它不能代替独立测试。

TUI 中 Enter 发送、Ctrl+J 换行、Ctrl+P/N 浏览输入历史、Ctrl+T 切换深浅主题、Ctrl+C 请求停止、Ctrl+Q 退出。点击工具卡片可查看参数与输出。向上滚动时暂停自动跟随，回到底部或发送新消息后恢复。模型回答流式显示，Bash 输出仍在命令结束后返回。

## Docker 工具沙箱

### 构建与启动

先安装并启动 Docker Desktop，使用 Linux 容器模式。在项目根目录构建镜像：

```powershell
docker build -t harness-sandbox:dev sandbox
```

镜像包含 Node.js、npm、Python 3、Git、Bash 和 ripgrep。开启沙箱：

```powershell
.\.venv\Scripts\python.exe -m agent --tui --sandbox --workdir "D:\btest"
```

确保目标工作区存在。当前沙箱绑定 Runtime 的固定工作目录，暂不支持队友切换到不同 worktree；使用沙箱时关闭团队，不与 `--teams` 或启用团队的 `--full` 组合。

### 执行位置

```text
宿主机 HarnessAgent
  ├─ Agent Loop、模型 API、权限确认、界面、状态管理
  └─ Docker 容器
       ├─ bash / read_file / write_file / edit_file / glob / grep
       └─ /workspace ← 挂载指定的宿主机工作区
```

例如 `--workdir D:\btest` 对应容器内的 `/workspace`。修改 `/workspace/src/App.vue` 会直接修改 `D:\btest\src\App.vue`，无需复制回来；容器删除后，工作区文件仍然保留。

当前容器配置：

| 项目 | 设置 |
|---|---|
| 镜像 | `harness-sandbox:dev` |
| 工作区 | 指定目录以可写 bind mount 挂载到 `/workspace` |
| 网络 | `bridge`，允许尝试联网安装依赖 |
| 用户 | `node` |
| 资源限制 | 2 GiB 内存、2 CPU、128 PID |
| 权限收敛 | `--cap-drop ALL`、`no-new-privileges` |

因此 Bash 可以执行 `npm install`、`git clone` 等联网操作，是否成功仍取决于网络、代理和依赖源。容器中的 Python 命令使用 `python3`；安装 Python 依赖时可先创建虚拟环境。

第一次使用基础工具时创建容器，同一个 Runtime 后续通过 `docker exec` 复用它；正常退出时删除容器。重新启动 Runtime 会生成新的容器名称和 ID，但镜像及挂载文件可以相同。异常终止可能留下容器，可在 Docker Desktop 的 **Containers** 页面检查；**Images** 页面显示的是镜像。

沙箱会执行工作区内的实际修改，并不回滚文件。MCP 服务、记忆和任务管理等宿主机能力没有因此迁入 Docker；bridge 网络也没有额外的域名白名单。

### 免费验证文件工具

```powershell
.\.venv\Scripts\python.exe verify_file_tools_sandbox.py
```

脚本在独立的临时工作区中验证写入、读取、精确编辑、嵌套文件搜索、正则搜索、输出上限及越界拦截，最后清理本次工作区和容器。不调用付费模型。

## 运行机制

### 主循环与工具恢复

```text
用户请求
  → 准备上下文、系统提示词和当前工具池
  → 调用模型
  → 有工具调用：恢复状态检查与参数校验 → 权限钩子 → 执行 → 回传结果
  → 无工具调用且回答完整：经过 Stop Hook
  → 允许结束则返回，否则继续；达到预算或发生错误则停止
```

主 Agent、一次性子 Agent 和线程队友复用 `run_loop`，分别持有对应的对话和执行上下文。

工具恢复区分两种路径：

- **程序重试**：工具必须同时满足只读、允许安全重试、错误属于暂时性故障等条件，首次执行后最多自动重试两次，每次重新检查权限。
- **模型修正**：参数错误、未知工具、目标不存在或替换前提不满足时，将结构化错误回传模型。相同调用者、工作目录、工具和目标，首次失败后最多提供两次修正机会；成功会清除该计数。

写入或编辑结果未知时，不自动重复修改。恢复器要求先完整读取目标文件、确认实际状态，再允许后续写入。读取被截断或只读取部分行，不能解除该状态。

模型返回空内容、只有思考内容或被截断的正文时，最多进行两次不完整响应恢复。若工具调用响应以 `max_tokens` 截断，该批调用会被丢弃，避免执行不完整参数。循环正常结束也不等于代码已经正确，仍需测试验收。

### 基础工具

| 工具 | 行为与主要限制 |
|---|---|
| `bash` | 执行命令，默认超时 120 秒；返回退出状态和 stdout/stderr |
| `read_file` | 按行读取，支持 `offset`、`limit`；正文最多 50,000 字符 |
| `write_file` | 创建或覆盖文件，按需创建父目录 |
| `edit_file` | 精确文本替换；默认要求唯一匹配，`replace_all=true` 才允许多处替换 |
| `glob` | 按通配符查找路径，最多返回 200 个匹配 |
| `grep` | Python 正则搜索，返回路径、行号及命中片段 |

`grep` 最多返回 500 个匹配行，每行片段最多 1,000 字符，总输出最多 50,000 字符；跳过超过 2,000,000 字节的文件及识别出的二进制内容。长行预览围绕首次命中位置生成，截断时提示缩小范围。

未开启 Docker 时使用宿主机工具实现；开启后六个基础工具都走容器分支，容器执行失败不会回退到宿主机执行。

### 五层上下文压缩

压缩器在模型调用前准备上下文，先执行本地处理，仍超限时才调用模型摘要。

| 阶段 | 默认触发条件 | 处理方式 |
|---|---|---|
| 1. 工具结果预算 | 最新一批结果超过 300,000 字符 | 将其中超过 50,000 字符的大结果落盘，保留 6,000 字符预览 |
| 2. 中段归档 | 对话超过 200 条消息 | 归档中间历史，保留约 120 条及归档标记，边界按工具配对调整 |
| 3. 旧结果替换 | 消息序列化后超过 160,000 字符 | 将较大的已消费旧结果替换为文件指针，保留最近 12 个已消费结果和未消费结果 |
| 4. 工具结果瘦身 | 阶段 3 后仍超过 160,000 字符 | 进一步处理较大结果，使用 4,000 字符预览 |
| 5. 模型摘要 | 阶段 4 后仍超过 160,000 字符 | 调用模型生成摘要；摘要输入上限 160,000 字符，输出预算 4,000 token |

阶段 3、4 的目标消息规模为 128,000 字符。模型还可调用 `compact` 主动请求压缩，在当前批工具结果追加完成后执行；供应商拒绝超长上下文时，也有应急压缩后的有限重试。

**触发阈值使用字符数或消息数，不是 token 数。** 实际模型用量由供应商返回的 usage 记录。历史归档先落盘，再以指针或摘要替换上下文；这不保证模型一定能找回所有信息，也不保证每次任务都会触发全部五层压缩。

### Skills、记忆与协作

Skills 从工作区的 `skills/` 或 `AGENT_SKILL_DIRS` 指定目录发现，启动只读取技能元数据，由 `load_skill` 按需加载正文。

长期记忆通过选择、提取和整合管理记录；提取阶段区分 `persistent` 与 `current_task`，后者不作为长期记忆入库。开启记忆会增加相应模型调用。

一次性子 Agent 使用独立对话完成委派，工具池排除 `task`、`spawn_teammate`、`connect_mcp`，防止继续递归委派。线程队友则保留自己的状态和信箱，可认领共享任务，按计划门规则申请审核，并在任务绑定的 Git worktree 中工作。worktree 需要目标项目本身是 Git 仓库，成果通过 Git 流程合并，不会自动合并到主工作目录。

团队任务认领使用进程内锁；任务文件原子写入。当前没有多进程共享同一状态目录所需的完整协调保障。

## 配置、状态与诊断

主要配置项：

| 配置 | 默认值或用途 |
|---|---|
| `AGENT_WORKDIR` | 当前目录，可被 `--workdir` 覆盖 |
| `AGENT_STATE_DIR` | `<workdir>/.agent` |
| `AGENT_SKILL_DIRS` | 默认 `<workdir>/skills`；Windows 多目录以分号分隔 |
| `AGENT_MAX_TOKENS` | 单次模型调用输出预算，默认 8,000 |
| `AGENT_MAX_TURNS` | 单次主循环轮数上限，默认 150 |
| `AGENT_SUBAGENT_MAX_TURNS` | 子 Agent 轮数上限，默认 150 |
| `AGENT_APPROVAL` | 默认 `ask`，可设 `allow` 或 `deny` |
| `GOAL_EVALUATOR_MODEL_ID` | 可选的目标评估模型，默认使用当前模型 |
| `AGENT_GOAL_BLOCK_CAP` | 每次请求的目标自动续轮上限，默认 8 |

轮数限制与单次输出预算不等于整个任务的精确 token 或费用上限。摘要、记忆、目标评估和队友也可能产生模型调用。

默认状态目录：

```text
<workdir>/.agent/
├─ tasks/          共享任务文件
├─ mailbox/        团队消息
├─ worktrees/      任务工作树
├─ memory/         长期记忆
├─ transcripts/    压缩前的历史归档
├─ tool-results/   落盘的工具结果
└─ runs/<运行编号>/
   ├─ events.jsonl
   └─ summary.json
```

`:trace` 查看调用分类、工具统计、停止原因和日志路径。runs 记录调用元数据、参数指纹及供应商用量，不保存参数正文、工具结果正文、回答正文或模型思考。历史归档和工具结果文件属于另一类数据，可能包含完整内容；`--verbose` 的终端输出也可能包含参数与结果片段。

供应商 token 字段缺失时保留未知状态，不用字符数冒充 token。缓存读写 token 单独记录，不跨供应商假定相同计费语义。

## 测试与评测

### 离线测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

单独验证新增 Docker 文件工具逻辑，无需启动 Docker：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_sandbox_file_tools.py -v
```

真实容器验证使用前面的 `verify_file_tools_sandbox.py`，需要 Docker 和本地镜像，但不需要模型密钥。

### 上下文召回与 token 对照

固定的 50 个合成场景分为信息事实、任务约束、版本修订、相似干扰和长文本摘要五类，每类 10 个。每例对照 `full` 与 `compact`，检查六项已知信息和一项正确拒答。

```powershell
# 只查看运行计划，不调用模型
.\.venv\Scripts\python.exe -m evals.recall50 --provider mock --plan

# 免费检查小批量评测流程
.\.venv\Scripts\python.exe -m evals.recall50 --provider mock --limit 5
```

真实评测会产生 API 费用。需要时可先跑一例：

```powershell
.\.venv\Scripts\python.exe -m evals.recall50 --provider deepseek --model deepseek-v4-flash --ids facts-start-high
```

去掉 `--ids` 可运行全部 50 个场景；默认双组、一次重复，共 100 次 Agent 运行，每次可能包含多个模型调用。评测脚本默认最多 40 轮，与普通 CLI 的 150 轮默认值分开配置，可用 `--max-turns` 调整。

结果保存到 `.scratch/evals/recall50-*/`，包括 `report.md`、`summary.json` 和逐次运行记录。主要检查：

- 已知信息逐项召回率，以及独立的正确拒答检查。
- Agent 是否正常结束、最终 JSON 是否有效、最终交付正确项数及整例通过率。
- 供应商实际返回的输入、输出和缓存 token，以及成对用量变化。
- 是否触发压缩、工具调用量、模型调用量、耗时和失败原因。

Mock 的满分只能说明预设流程通过，不能代表真实召回能力。检查压缩效果前，应确认压缩实际触发，并将额外摘要调用计入用量。历史评测报告对应各自的代码、配置和样本，不代表当前版本效果。

其他评测包括上下文压缩任务对照、单 Agent 与团队耗时对照、验证反馈对照。用法和指标定义见 [评测说明](evals/README.md) 与 [50 组召回评测说明](docs/50组上下文召回评测使用说明.md)。

## 项目结构与阅读顺序

```text
agent/
├─ cli.py / tui.py          命令行与终端界面
├─ runtime.py              组装服务、状态、工具和钩子
├─ loop.py                 Agent 主循环与工具执行入口
├─ llm.py                  供应商客户端、流式返回、usage、Mock
├─ permissions.py          应用层权限决策
├─ sandbox.py              Docker 容器与工具调用
├─ sandbox_file_tools.py   容器内编辑及搜索脚本
├─ context.py              五层上下文压缩
├─ skills.py / memory.py   技能加载与长期记忆
├─ subagent.py             一次性委派
├─ tasks.py / teams.py     任务图与线程队友
├─ team_tools.py           团队工具与 worktree 接线
├─ mcp.py                  外部工具接入
├─ diagnostics.py          运行元数据与统计
└─ tools/
   ├─ registry.py          工具定义、校验与分发
   ├─ result.py            结构化结果
   ├─ recovery.py          重试、修正预算与未知状态
   └─ basic.py             六个基础工具入口
sandbox/Dockerfile         工具容器镜像
skills/                    技能文件
tests/                     离线测试
evals/                     任务级对照评测
docs/                      实现讲解与评测说明
```

建议从 [Agent Loop 执行流程总结](docs/AgentLoop-执行流程总结.md) 开始，再阅读：

- [工具错误恢复说明](docs/工具错误恢复说明.md)
- [命令执行与失败诊断](docs/命令执行与失败诊断.md)
- [终端界面与流式输出](docs/终端界面与流式输出.md)
- [运行轮次诊断](docs/运行轮次诊断.md)
- [Runtime 项目概念总结](docs/Runtime-项目概念总结.md)

## 许可

[MIT](LICENSE)。
