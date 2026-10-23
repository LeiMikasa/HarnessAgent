# minicode

用 Python 实现的模块化编码 Agent：模型负责决定下一步，运行时负责工具执行、权限检查、上下文管理、任务协作和运行记录。

提供普通 CLI、Textual 终端界面、浏览器工作台和可选的 Docker 工具沙箱。支持 DeepSeek、Anthropic，以及无需 API 的离线 Mock 模式。

CLI 使用 `python -m minicode`，Web 使用 `python -m minicode.web`。核心实现位于 `agent/`，也兼容 `python -m agent` 和 `python -m agent.web` 启动方式。

## 功能概览

| 能力 | 当前实现 |
|---|---|
| Agent Loop | 模型调用 → 工具执行 → 结果回传；完整回答经过 Stop Hook 后结束 |
| 基础工具 | `bash`、`read_file`、`write_file`、`edit_file`、`glob`、`grep` |
| 工具恢复 | JSON Schema 校验、结构化错误、有限自动重试、模型参数修正、未知写入状态检查 |
| 权限控制 | 硬拒绝列表、风险规则、人工批准或 allow/deny 策略 |
| Docker 沙箱 | Bash 在容器内执行；文件工具在宿主机执行并检查权限，容器挂载同一工作区 |
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

以下命令以 Windows PowerShell 为例，建议使用 Python 3.11 或更新版本：

```powershell
git clone https://github.com/LeiMikasa/minicode.git
cd minicode

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 已有 .env 时保留原配置
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

### 先跑免费演示

```powershell
.\.venv\Scripts\python.exe -m minicode --mock --demo --permission-mode full --workdir ".scratch\quickstart"
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
.\.venv\Scripts\python.exe -m minicode --status
```

需要检查真实接口和工具调用往返时运行：

```powershell
.\.venv\Scripts\python.exe preflight.py --tools
```

`preflight.py` 会发送真实模型请求并消耗 API 额度。

切换到 Anthropic 时设置 `AGENT_PROVIDER=anthropic`、对应的 `MODEL_ID` 和密钥，并清空 `ANTHROPIC_BASE_URL`，让 SDK 使用默认端点。项目通过 Anthropic SDK 访问官方或兼容接口，不直接接收任意 OpenAI 格式接口。

### 指定开发目录启动

工作区是 Agent 要修改的项目目录，不必是 minicode 自身目录。

```powershell
# 普通交互模式
.\.venv\Scripts\python.exe -m minicode --workdir "D:\btest"

# 终端界面
.\.venv\Scripts\python.exe -m minicode --tui --workdir "D:\btest"

# 执行一个任务后退出
.\.venv\Scripts\python.exe -m minicode --workdir "D:\btest" "检查项目并修复失败的测试"
```

## 启动选项与交互命令

### 浏览器工作台

Web 使用 **Vue 3 + TypeScript + Vite** 前端和 **FastAPI** 后端。复用现有 Runtime、Agent Loop、工具、权限与 Docker；不需要把 Agent 本体搬进浏览器。

首次使用，在项目根目录执行：

```powershell
# 在项目根目录执行
.\.venv\Scripts\python.exe -m pip install -r requirements-web.txt
cd web
npm.cmd install
npm.cmd run build
cd ..
.\.venv\Scripts\python.exe -m minicode.web --workdir "D:\btest"
```

打开 **http://127.0.0.1:8765**。后续启动只需要最后一条 Python 命令。PowerShell 使用 `npm.cmd` 可避免 `npm.ps1` 被本机执行策略阻止。

新建会话时先选择已有工作目录、使用模型和权限模式：

工作目录右侧的“选择文件夹”会打开本机系统选择窗口，选好后自动回填完整路径；取消选择会保留原来的路径，也可以手动输入。选择窗口需要本机桌面环境和 Python Tkinter，最长等待 3 分钟。这个动作只选择目录路径，不上传目录内容或启动 Agent。

- **只读**：读取与搜索走 Docker，工作区只读挂载。
- **工作区内修改**：基础工具默认在 Docker 工作区执行。
- **完全权限**：基础工具直接在宿主机执行。

会话创建后，右上角可以随时调整权限或切换模型，无需新建会话。运行中提交的设置会显示“等待生效”：当前调用先完成，在下一次模型/工具调用前应用；同一批工具的后续调用也会重新检查权限。切到完全权限须勾选确认；切换权限会拒绝未完成的旧审批，并关闭旧容器，容器里的后台进程会结束，后续按新挂载方式创建容器。正在执行的宿主机操作不会被追溯撤销。

模型可切到免费 Mock，或填写当前已配置服务支持的模型 ID，沿用该服务的地址与密钥；不支持在页面内新增其他服务的密钥。切换保留对话与统计，下次请求才验证模型 ID 是否可用；设置动作本身不调用模型、不消耗 token。

前两种模式需要运行 Docker Desktop 并构建 `minicode-sandbox:dev` 镜像。工作区外文件或显式宿主机申请会在网页里显示审批弹窗，默认聚焦拒绝；批准仅作用于当次调用。浏览器通过 SSE 接收流式回答、工具状态与上下文统计，任务、停止与审批通过 HTTP 提交。

模型密钥从 Python 后端的 `.env` 读取，不发送到前端配置接口。会话列表、对话和工具记录保存在后端状态目录的 `web/<会话 ID>/` 下，刷新页面或重启服务后可继续会话。长期记忆与团队默认关闭；第一版不提供团队工作树和多人远程访问。服务只监听 `127.0.0.1`，并检查 Host 和网页来源。

模型服务返回可读的思考内容时，页面会显示可折叠的“思考过程”，展开后可实时查看，正文和工具记录分别展示。生成完成后保留在会话中；断流或取消会标记中断。未返回思考内容时不显示该区域，也不会为展示额外发起模型请求；旧会话之前未保存到页面记录中的思考不会自动补录。

**免费试用**：新建会话时选 `Mock · 免费流程演示`。如果只验证界面、且本机 Docker 未运行，可选完全权限；演示仅调用任务清单与顶层文件搜索等预设步骤，不调用真实模型，也不声称完成输入的开发任务。Mock 的 token 实际用量显示“未知”，上下文估计正常展示。

“停止”沿用现有协作式取消：立即拒绝等待中的审批，后续循环不再执行新调用；正在执行的模型请求或工具可能仍需要等待结束。关闭页面不会自动停止后端任务，重新打开该会话可查看状态或请求停止。未处理的审批最多等待 5 分钟，超时拒绝。

开发前端时保留 Python 服务，再开一个终端：

```powershell
# 在项目根目录执行
cd web
npm.cmd run dev
```

访问 `http://127.0.0.1:5173`，Vite 将 `/api` 转发到本机 `8765`。页面修改后需要重新执行 `npm.cmd run build` 才会更新 Python 服务提供的生产页面。

免费检查：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_web.py -v
cd web
npm.cmd test
npm.cmd run build
npm.cmd run test:e2e
```

浏览器测试会启动独立的本机 `8767` 测试服务，使用 `.scratch` 临时项目和免费 Mock。Windows 默认使用已安装的 Edge；其他平台可安装 Playwright Chromium，或通过 `PLAYWRIGHT_CHANNEL` 指定浏览器。

### 终端启动参数

| 参数 | 作用 |
|---|---|
| `--workdir PATH` | 指定目标项目目录 |
| `--tui` | 使用 Textual 界面，流式显示模型回答 |
| `--permission-mode read-only\|workspace\|full` | 只读、工作区内修改、完全权限；默认 `workspace` |
| `--sandbox` | 保留的 Docker 开关；不能与完全权限模式同时使用 |
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

`--approval allow` 仅影响普通风险规则，不会自动批准受限模式下的宿主机执行申请，也不会绕过硬拒绝列表。`--approval deny` 直接拒绝提权。`--full` 是团队与记忆开关，不是完全权限；当前 Docker 模式不支持团队工作树。

### 三种权限模式

| 模式 | 默认行为 | 需要超出范围时 |
|---|---|---|
| `read-only` 只读 | 文件工具在宿主机读取、搜索；禁止普通命令和修改，若创建容器则使用只读挂载 | 越界读取或本次修改需要批准；宿主机命令须显式申请 |
| `workspace` 工作区内修改 | Bash 走 Docker；文件工具在宿主机执行，检查真实路径是否在工作区内 | 越界文件路径自动申请；宿主机命令须显式申请 |
| `full` 完全权限 | 六个基础工具直接在宿主机执行，可访问当前系统用户有权操作的资源 | 不再为基础工具逐次确认；硬拒绝及自定义拒绝规则仍有效 |

```powershell
# 只读（普通读取、搜索不需要启动 Docker）
.\.venv\Scripts\python.exe -m minicode --tui --permission-mode read-only --workdir "D:\btest"

# 工作区内修改，默认模式（需要 Docker 和本地镜像）
.\.venv\Scripts\python.exe -m minicode --tui --permission-mode workspace --workdir "D:\btest"

# 完全权限：在宿主机执行
.\.venv\Scripts\python.exe -m minicode --tui --permission-mode full --workdir "D:\btest"
```

也可设置 `AGENT_PERMISSION_MODE`，命令行参数优先。只读限制针对项目操作；Agent 仍会在宿主机保存自己的会话、诊断等状态。联网与文件权限独立，Docker 使用 bridge 网络。

例如工作区是 `D:\btest`，模型调用 `read_file` 读取 `D:\资料\接口说明.md` 时，会展示本次越界文件操作供用户批准。批准后读取该文件，把内容返回模型；下一次普通文件调用仍受工作区限制，不新增挂载，不修改会话模式。符号链接解析后越界也会走批准。

命令中的路径不使用字符串猜测自动提权。若需要执行宿主机命令，模型应提交下面的工具参数（Windows 宿主机默认使用 cmd 语法）：

```json
{
  "command": "type D:\\资料\\接口说明.md",
  "execution_mode": "host",
  "justification": "需要读取工作区外的接口说明"
}
```

六个基础工具都支持 `execution_mode: "host"` 与 `justification`。对 Bash，这是申请绕过 Docker；对文件工具，这是申请本次放宽应用层限制。该参数不能自行授权。CLI 展示完整参数，TUI 显示“申请本次完全权限 · 宿主机执行”；拒绝、取消或无人可批准时不执行。临时权限在成功、失败、后续钩子拦截或异常时清除，也不会复制给子 Agent；单次批准的调用不会自动重试。Docker 命令故障不自动回退宿主机。

Python API 建议显式传入 `Runtime.create(..., permission_mode="workspace", teams_enabled=False)`。为兼容已有测试、嵌入调用和验证脚本，未传 `permission_mode` 的旧 API 仍按原 `sandbox_enabled` 配置工作；关闭沙箱时的路径检查不构成命令隔离。

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

右下角显示主 Agent 最近一次请求的上下文估计、压缩预算、剩余和占用百分比，每轮压缩处理后更新；窄窗口收起侧栏时，在输入框附近保留简洁提示。默认预算 200,000 个估计 token，估计值已含 20% 余量；这是距压缩阈值的空间，不是模型容量或累计账单。关闭压缩时只显示估计输入量，右侧会话累计 token 仍取接口 `usage`。

完整面板还分别显示提示词、工具定义、工具结果、对话消息和请求结构的估计 token，各项之和等于总占用。工具结果从对话消息中单独扣出，避免重复统计；日志的 `context_prepared` 事件也记录相同分项。

## 执行范围与重复检查

项目分析默认通过阅读代码完成；修复任务执行与变动相关的验证。常见测试、构建和依赖安装命令需要提供 `task_reason`，说明与当前请求的关系，不需要用户逐次确认。

同一轮请求内，已完成的常见检查在工作区输入未变时会被拦截重复执行，即使换了超时设置或日志输出路径。针对偶发问题的必要复跑可提供 `rerun_reason`。检查捕获的完整输出会保存到当前状态目录的 `command-logs/`，工具结果附带路径；可能掩盖检查失败的管道、分号后续命令和失败兜底会被拒绝。

命令识别与快照都有明确边界，详见[执行范围与重复验证控制](docs/执行范围与重复验证控制.md)。

## Docker 工具沙箱

### 构建与启动

先安装并启动 Docker Desktop，使用 Linux 容器模式。在项目根目录构建镜像：

```powershell
docker build -t minicode-sandbox:dev sandbox
```

镜像包含 Node.js、npm、Python 3、Git、Bash 和 ripgrep。开启沙箱：

```powershell
.\.venv\Scripts\python.exe -m minicode --tui --sandbox --workdir "D:\btest"
```

确保目标工作区存在。当前沙箱绑定 Runtime 的固定工作目录，暂不支持队友切换到不同 worktree；使用沙箱时关闭团队，不与 `--teams` 或启用团队的 `--full` 组合。

### 执行位置

```text
宿主机 minicode
  ├─ Agent Loop、模型 API、权限确认、界面、状态管理
  ├─ read_file / write_file / edit_file / glob / grep（应用层权限与路径检查）
  └─ Docker 容器
       ├─ bash
       └─ /workspace ← 挂载指定的宿主机工作区
```

例如 `--workdir D:\btest` 对应容器内的 `/workspace`。修改 `/workspace/src/App.vue` 会直接修改 `D:\btest\src\App.vue`，无需复制回来；容器删除后，工作区文件仍然保留。

当前容器配置：

| 项目 | 设置 |
|---|---|
| 镜像 | `minicode-sandbox:dev` |
| 工作区 | 挂载到 `/workspace`；只读模式使用只读挂载，工作区修改模式使用可写挂载 |
| 网络 | `bridge`，允许尝试联网安装依赖 |
| 用户 | `node` |
| 资源限制 | 2 GiB 内存、2 CPU、128 PID |
| 权限收敛 | `--cap-drop ALL`、`no-new-privileges` |

因此 Bash 可以执行 `npm install`、`git clone` 等联网操作，是否成功仍取决于网络、代理和依赖源。容器中的 Python 命令使用 `python3`；安装 Python 依赖时可先创建虚拟环境。

第一次执行沙箱 Bash 时创建容器，同一个 Runtime 后续通过 `docker exec` 复用它；文件工具不会创建或进入容器。正常退出时删除容器。重新启动 Runtime 会生成新的容器名称和 ID，但镜像及挂载文件可以相同。异常终止可能留下容器，可在 Docker Desktop 的 **Containers** 页面检查；**Images** 页面显示的是镜像。

沙箱会执行工作区内的实际修改，并不回滚文件。MCP 服务、记忆和任务管理等宿主机能力没有因此迁入 Docker；bridge 网络也没有额外的域名白名单。

### 免费验证文件工具

```powershell
.\.venv\Scripts\python.exe verify_file_tools_sandbox.py
```

脚本在独立的临时工作区中验证宿主机文件工具、路径映射、输出上限及越界拦截，再用真实 Docker Bash 验证双向文件共享，最后清理本次工作区和容器。不调用付费模型。

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

五个文件工具始终在宿主机执行并检查权限；默认路径相对于工作区，Docker 模式下兼容 `/workspace/...` 到宿主工作区的映射。显式 `execution_mode: "host"` 使用原始宿主路径。工作区外的读取也需批准，不默认开放宿主机文件。Bash 在 Docker 开启时走容器，失败不会自动回退。文件工具的路径检查属于应用层边界，不能替代 Bash 的进程隔离。

Docker 中的 Bash 开启 `pipefail`：如 `测试命令 | tail -40` 的测试阶段失败，工具会返回失败，避免只看到 `tail` 的成功退出码。`head` 提前关闭管道也可能使上游收到 SIGPIPE，需要按具体输出判断；此设置没有改变宿主机 cmd 或 `/bin/sh` 的语法。

每次 Agent Loop 保存一份有界的执行记录，向下一轮模型提示已返回成功的依赖安装/检查，以及相同参数、相同可见结果的重复读取。记录不包含原始命令或文件正文，不会缓存结果、跳过工具、强制结束任务或新增模型调用。可能修改状态的工具会使读取和检查记录失效，依赖清单写入会清除安装记录，工作目录或沙箱环境切换会清空记录；外部改动仍需模型根据实际证据重新检查。项目概览默认以必要的静态阅读为主，不自动扩展成安装依赖、全量测试与修复全部问题；用户明确要求深入执行时遵循用户要求。

系统提示词和工具描述要求：文本读取必须用 `read_file`，找文件用 `glob`，搜内容用 `grep`；禁止用 `cat`、`find`、Shell `rg/grep` 等命令或脚本替代。文件工具已实现参考 DSH 的程序级写前读取与版本校验：覆盖或编辑已有文件需要当前会话观察过该文件，读后内容/版本发生变化时返回 `FS_STALE_VERSION` 并要求重新读取，没读过返回 `FS_NOT_OBSERVED`。成功创建、写入或编辑会更新观察记录，可以继续修改；全新文件允许直接创建，但不会覆盖并发创建者。普通分段读取也建立版本观察；未知写入状态的恢复仍要求完整读取。工具切换规则仍是提示词约束；Bash 用于 Git、依赖安装、测试、构建和运行程序。`glob` 不支持花括号展开，多扩展名需分别搜索。详见[文件工具保护说明](docs/文件工具写前读取与版本保护.md)。

`glob` 和 `grep` 默认在遍历前跳过 `node_modules`、`.venv`、`venv`、`.git`、`__pycache__`、`dist`、`build`、`coverage`、`.next`、`.nuxt`、`.cache` 目录。需要检查它们时，在 `glob.pattern`、`grep.path` 或 `grep.include` 中明确写出目录名，或设置 `include_ignored=true`（同时包含隐藏路径）；显式包含目录不会扩大文件访问权限。不支持的花括号展开会返回 `INVALID_ARGUMENT`，而不是“没有匹配”；grep 的正则表达式量词不受影响。

Docker 客户端不可用或容器启动失败时，Bash 返回 `SANDBOX_UNAVAILABLE`，说明请求的命令尚未执行、文件工具仍可使用，以及恢复环境后再重试的条件。同一轮任务中的后续模型调用会保留该故障提醒；换一条命令不代表环境恢复，权限/执行范围变化或后续执行结果会刷新记录。提醒不拦截工具，也不会自动转为宿主机执行；命令已经提交后的失败和超时仍保留原有错误与副作用未知状态。

项目分析提示词要求先从 README 和相关目录定位入口，再搜索核心函数并读取实现；区分文档声明、代码证据和实际运行验证，证据足够后停止扩展搜索。仓库中的提示词、注释和 Git 记录不能作为当前 Agent 运行身份或项目自举开发的证明。

相同 Bash 参数连续调用达到 3 次后，下一轮提示模型检查上次结果、说明重跑依据或换方法；成功调用和输出变化也计数，插入只读检查不会清零，不同 Bash 参数、其他可能修改状态的工具或执行范围变化会重置。提醒只表示参数重复，不认定无进展或任务完成，也不会拦截调用或绕过权限。

### 五层上下文压缩

压缩器在模型调用前准备上下文，先执行本地处理，仍超限时才调用模型摘要。

| 阶段 | 默认触发条件 | 处理方式 |
|---|---|---|
| 1. 工具结果预算 | 最新一批结果超过 150,000 个估计 token | 将其中超过 25,000 个估计 token 的大结果落盘，保留 6,000 字符预览 |
| 2. 中段归档 | 对话超过 200 条消息 | 归档中间历史，保留约 120 条及归档标记，边界按工具配对调整 |
| 3. 旧结果替换 | 完整请求输入超过 200,000 个估计 token | 将较大的已消费旧结果替换为文件指针，保留最近 12 个已消费结果和未消费结果 |
| 4. 工具结果瘦身 | 阶段 3 后仍超过 200,000 个估计 token | 进一步处理较大结果，使用 4,000 字符预览 |
| 5. 模型摘要 | 阶段 4 后仍超过 200,000 个估计 token | 调用模型生成摘要；摘要请求输入预算 80,000 个估计 token，输出预算 4,000 token |

阶段 3、4 尽量将请求输入降到 160,000 个估计 token；已低于触发阈值时，不强制继续进入下一层。模型还可调用 `compact` 主动请求压缩，在当前批工具结果追加完成后执行；供应商拒绝超长上下文时，也有应急压缩后的有限重试。

**估计公式：`ceil((中文字符数 × 0.6 + 其他字符数 × 0.3) × 1.2)`。** 20% 余量已包含在估计值中，不重复添加。完整请求估计包含系统提示词、历史消息、工具定义及 JSON 结构字符；这不是精确 tokenizer，实际用量仍由供应商返回的 `usage` 记录，评测不会用估计值代替实际用量。

200,000 是项目的输入压缩策略预算，不代表模型上下文上限，也不是经过真实评测确定的最优值；实际触发随中英文比例变化，输出额度由 `max_tokens` 单独控制。摘要调用独立使用 80,000 的输入估计预算，以限制摘要开销。预览长度仍按字符限制，中段归档仍按消息条数触发，所以不足 200,000 个估计 token 时也可能发生归档。历史归档先落盘，再以指针或摘要替换上下文；这不保证模型一定能找回所有信息，也不保证每次任务都会触发全部五层压缩。

### Skills、记忆与协作

Skills 从工作区的 `skills/` 或 `AGENT_SKILL_DIRS` 指定目录发现，启动只读取技能元数据，由 `load_skill` 按需加载正文。

长期记忆通过选择、提取和整合管理记录；提取阶段区分 `persistent` 与 `current_task`，后者不作为长期记忆入库。开启记忆会增加相应模型调用。

一次性子 Agent 使用独立对话完成委派，工具池排除 `task`、`spawn_teammate`、`connect_mcp`，防止继续递归委派。线程队友则保留自己的状态和信箱，可认领共享任务，按计划门规则申请审核，并在任务绑定的 Git worktree 中工作。worktree 需要目标项目本身是 Git 仓库，成果通过 Git 流程合并，不会自动合并到主工作目录。

团队任务认领使用进程内锁；任务文件原子写入。当前没有多进程共享同一状态目录所需的完整协调保障。

团队收尾时，主 Agent 应先整合队友提交并完成项目验证，再调用 `remove_worktree({"name": "工作树名", "delete_branch": true})` 清理。后端要求工作树处于受管理路径、使用对应的 `wt/<name>` 分支、没有租约、绑定任务全部完成、没有未提交/未跟踪/被忽略的文件，且分支提交已包含在主 Agent 当前 HEAD 中。检查失败会保留；不传 `delete_branch` 时仍保留分支。删除分支使用 `git branch -d`，不会强删未合入的提交。通过 cherry-pick 或 squash 整合后，原分支可能因祖先关系无法证明而被保留。该流程由主 Agent 调用工具触发，不是后台定时清理；Git 整合检查也不代替功能测试。

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
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_permission_modes.py -v
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
