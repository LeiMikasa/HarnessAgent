# HarnessAgent

一个**用 Python 3 从零搭起来的编码 Agent 外壳（harness）**，设计参照
[`learn-claude-code`](https://github.com/shareAI-lab/learn-claude-code) 的
s01–s10、s13、s14 章，把它们从"每章一个独立文件"重构成一个**可运行的模块化工程**。

核心理念只有一句话：

> **能动性来自模型，外壳（harness）负责给能动性一个落脚的地方。**

模型是司机，外壳是车。这个仓库造的是车。

---

## 1. 它长什么样

```
                    THE AGENT PATTERN
                    =================

    User --> messages[] --> LLM --> response
                                      |
                              contains tool_use block?
                           /                          \
                         yes                           no
                          |                             |
                    execute tools                    return text
                    append results
                    loop back -----------------> messages[]
```

这个循环**永远不变**。整份代码里只有 `agent/loop.py` 那一处是 **agent 循环**
（"调模型 → 执行工具 → 回灌结果"）—— 所有 18 个机制都是围绕它的四种扩展点挂上去的，
没有一个是往循环里塞 `if`。

（顺便说清楚：代码里还有另外 6 处 `while`，但都不是 agent 循环 ——
`cli.py` 的 REPL 读输入、`teams.py` 的信箱等待和队友工作循环、
`context.py` 的配对扫描、`mcp.py` 等 JSON-RPC 响应、`tasks.py` 的依赖图遍历、
`runtime.py` 等队友。别把它们和 agent 循环搞混。）

```
    UserPromptSubmit   用户输入到达，还没进模型
    PreToolUse         工具即将执行 —— 返回字符串可以否决这次调用
    PostToolUse        工具执行完毕 —— 只能观察
    Stop               模型没要工具、想停下了 —— 返回字符串可以强制它继续
```

---

## 2. 与 learn-claude-code 的对应关系

| 课程章节 | 机制 | 本工程实现 |
|---|---|---|
| s01 | Agent Loop | `agent/loop.py` |
| s02 | Tool Use | `agent/tools/registry.py`、`agent/tools/basic.py` |
| s03 | Permission | `agent/permissions.py` |
| s04 | Hooks | `agent/events.py` |
| s05 | TodoWrite | `agent/todo.py` |
| s06 | Subagent | `agent/subagent.py` |
| s07 | Skill Loading | `agent/skills.py` |
| s08 | Context Compact | `agent/context.py` |
| s09 | Memory | `agent/memory.py` |
| s10 | Task System | `agent/tasks.py` |
| s13 | Agent Teams | `agent/teams.py`、`agent/team_tools.py` |
| s14 | MCP Plugin | `agent/mcp.py` |
| s17 | Goal Loop | `agent/goal.py`、`agent/runtime.py` |
| — | 系统提示词组装 | `agent/prompt.py` |
| — | 全部接线 | `agent/runtime.py` |
| — | 终端入口 | `agent/cli.py` |
| — | 模型接入（含离线 mock） | `agent/llm.py` |

尚未接入 **s11（后台任务）、s12（定时任务）、s15/s16**。

Goal Loop 使用 `:goal <验收条件>`（也支持 `/goal <验收条件>`）设定目标并立即执行。主 Agent 想结束时，独立、无工具的模型调用根据近期对话和工具结果判断目标是否达成；未达成会带着原因继续，同一目标每次请求最多自动续轮 8 次。`:goal` 查看状态，`:goal clear` 取消，新的条件可替换旧条件。评估异常或达到续轮上限会保留目标并返回控制权；队友还在执行关联任务时暂缓判断。目标仅保存在当前 Runtime 会话内。
离线 `mock` 模式没有真实判断能力；测试通过注入确定性的评估器验证目标循环。

模型仅返回思考、没有正文和工具调用时，不会按正常完成处理：最多自动恢复两次，仍不完整则明确报错。输出因 `max_tokens` 截断的正文会尝试续写；包含工具调用的截断响应会整批丢弃，不执行其中任何工具，也不加入历史，然后提示模型重新生成一个较小的完整调用。之前已执行的工具结果保留。不完整响应共用最多两次自动重试，恢复也受 `AGENT_MAX_TURNS` 总轮数限制。`--verbose` 会显示响应结束原因、重试进度、正文长度，以及带参数名和结果长度的工具摘要。

---

## 3. 目录结构

```
HarnessAgent/
  agent/
    loop.py          唯一的 agent 循环
    llm.py           模型接入：Anthropic 兼容端点 + 离线 MockLLM
    events.py        Hooks：四个扩展点
    permissions.py   三道闸门：deny list / 规则 / 用户批准
    prompt.py        系统提示词组装（每次调用前重建）
    runtime.py       把上面所有东西接在一起
    cli.py           终端入口（含 :命令）

    todo.py          s05 计划清单
    subagent.py      s06 子 agent（独立上下文）
    skills.py        s07 按需加载知识
    context.py       s08 五级上下文压缩
    memory.py        s09 记忆：选择 / 提取 / 整合
    tasks.py         s10 落盘的任务依赖图
    teams.py         s13 队友、信箱、协议、任务绑定 worktree
    team_tools.py    s13 团队工具
    mcp.py           s14 外部能力路由（含 stdio JSON-RPC 客户端）
    goal.py          s17 独立目标评估与自动续轮

    tools/
      registry.py    Tool / ToolContext / ToolRegistry
      basic.py       bash read_file write_file edit_file glob grep

  skills/            SKILL.md 知识包（启动只读 frontmatter）
  tests/             离线测试，不需要 API key

  docs/
    loop-逐行讲解.md  agent/loop.py 的逐行导读（从这里开始读源码）
```

> **想读源码？先看 [`docs/loop-逐行讲解.md`](docs/loop-逐行讲解.md)。**
> 它把 `agent/loop.py` 181 行逐行讲完，包括每个设计取舍背后的问题。
> 读懂那一个文件，剩下的都是"机制挂在哪"的问题。

---

## 4. 快速开始

```sh
pip install -r requirements.txt
cp .env.example .env        # 填入 ANTHROPIC_API_KEY

python preflight.py --tools    # 先验证供应商：模型 id、连通性、工具调用
python -m agent                                     # 交互式
python -m agent "修复失败的测试"                      # 单次任务
python -m agent --quiet "修复失败的测试"              # 只输出最终答案
python -m agent --verbose                           # 显示详细事件日志
python -m agent --mock --demo                       # 不需要 key 的离线自检
python -m agent --status                            # 打印解析后的配置
```

默认会在终端实时显示模型轮次和工具执行状态；进度写到标准错误输出，
单次任务的标准输出仍只有最终答案。`--quiet` 关闭进度，`--verbose` 额外显示
工具结果预览与诊断事件。普通进度不会包含模型内部思考、工具参数或工具输出；
详细日志可能包含工具参数和结果片段，请勿把它直接分享给不可信的人。

默认指向 DeepSeek 的 Anthropic 兼容端点。**换供应商只改 `.env`，不改代码**：

```ini
# DeepSeek（默认）
AGENT_PROVIDER=deepseek
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
MODEL_ID=deepseek-chat

# Anthropic 官方
AGENT_PROVIDER=anthropic
MODEL_ID=claude-sonnet-4-6

# 离线
AGENT_PROVIDER=mock
```

### 为什么先跑 `preflight.py`

离线测试证明的是**外壳的机械结构**对不对。它证明不了**供应商这条路径**能跑 ——
那需要真 key 和真端点。`preflight.py` 一条命令补上这个缺口：

```
resolved configuration          解析后的配置（key 打码）
probing model ids               逐个试模型 id，打印每个的**原始报错**
tool-calling round trip         工具调用一次完整往返（外壳真正依赖的东西）
```

它会明确区分三种失败，因为**它们的排查方向完全不同**：

| 现象 | 含义 |
|---|---|
| 网络 / 连接错误 | `base_url` 写错，或没有网络 |
| 400 参数错误 | 请求体结构和端点不匹配 |
| 401 认证失败 | 端点通了、请求格式对了，**只是 key 不对** |

最后一步的 `--tools` 最值得跑：它会发一次 `tool_use`，再把 `tool_result` 回传。
如果端点不接受 `tool_result` 这一轮，**整个外壳一行都用不了** ——
这比"能不能聊天"重要得多。

交互模式下，`:` 开头的是外壳命令，其它都是给 agent 的请求：

```
:help  :status  :tools  :skills  :todos  :tasks  :team  :mcp  :memory  :notes  :clear  :quit
```

---

## 5. 每个机制在做什么

### s01 循环 —— `loop.py`

```python
response = llm.create(system=..., messages=..., tools=...)
messages.append({"role": "assistant", "content": response.content})
calls = tool_use_blocks(response.content)
if not calls: return            # 模型决定停下
for block in calls: ...         # 执行工具，收集结果
messages.append({"role": "user", "content": results})
```

`run_loop` 被**主 agent、每个子 agent、每个队友**共用，区别只在传入的参数。
循环外还包了三层：上下文压缩 → hooks → 反应式压缩重试。

### s02 工具 —— `tools/`

一个注册表、一张分发表。加能力 = 注册一个 `Tool`，循环不动。

`ToolContext` 是让"一个注册表服务多个调用者"的关键：主 agent、子 agent、三个队友
共用同一份工具代码，但各自带着不同的工作目录、owner、批准行为。

所有路径都过 `safe_path(ctx, path)` —— 越界在**工具层**被硬拦住，
不是只靠权限钩子提醒。

### s03 权限 —— `permissions.py`

```
Gate 1  deny list   硬编码，永不协商，不弹窗
Gate 2  规则        路径越界 / 破坏性 shell / 未审核的外部工具
Gate 3  批准        暂停问人；没有人在时按策略 fail closed
```

批准过的越界**只对这一次调用有效**：`loop.execute_tool` 每次进入前清空
`ctx.extra["allow_outside"]`。队友跑在独立线程上，永远不会弹窗（否则会把终端卡死），
它会改用邮箱去问 lead。

### s04 钩子 —— `events.py`

hooks 是实例而不是全局变量，所以每个队友、每个子 agent 都能带自己的一套，
互不串扰。`trigger` 返回**第一个非 None** 的结果 —— 这正是 `PreToolUse`
能靠返回一个字符串来否决调用的原理。

### s05 计划 —— `todo.py`

清单是会话状态，不是文件。每次工具返回都重新渲染一遍，所以模型始终看得见
自己的计划。校验：状态只能三选一、内容非空、最多 20 条、**最多一条 in_progress**。
模型偶尔会把 list 塞成 JSON 字符串，这里直接解码而不是报错浪费一轮。
主 agent 和每个线程队友各有一份内存 Todo 清单；队友认领新任务时会清空自己的旧清单。
跨队友的任务状态、依赖和认领仍由持久化任务板管理，Todo 不承担团队同步。

### s06 子 agent —— `subagent.py`

子 agent 是**函数调用**：新上下文、一个答案、然后消失。

它的工具池**故意排除** `task` / `spawn_teammate` / `connect_mcp` —— 递归被结构性
掐断，不会出现成本爆炸。

### s07 技能 —— `skills.py`

启动只读每个 `SKILL.md` 的 frontmatter，100 个技能只花 100 行目录。
正文只在模型判断"这个技能适用"时才 `load_skill` 拉进来。

### s08 上下文压缩 —— `context.py`

五级流水线，**便宜的先跑，模型调用永远最后**：

```
每一轮都跑
  stage 1  tool_result_budget   超大结果落盘，只留预览
  stage 2  snip_compact         超过 50 条就把中间归档，留一个存档标记

只有 estimate_chars(messages) > 50000 才跑
  stage 3  micro_compact        已读过的旧结果换成一行指针
  stage 4  fit_tool_results     还超就把最大的继续瘦身
  stage 5  compact_history      一次模型调用，总结全部历史
```

外加一条应急路径：请求被供应商以 "prompt too long" 拒了，
就 `reactive_compact` 后重试**一次**。

模型也可以**主动**要求腾地方：调用 `compact` 工具。它不是立刻执行，而是等到
**这一批工具全部跑完、结果都追加进去之后**才压缩 —— 否则同一批里的
`write_file` 还没来得及进历史就被丢掉了。

两条不变量贯穿始终：

* `tool_use` 永远不会丢掉它的 `tool_result`，反之亦然（`snip_compact` 和
  `reactive_compact` 里各有一处配对修复）
* 每一步有损操作都**先落盘**，存档路径还要通过"必须位于 tool-results 目录内且文件存在"
  的信任校验，伪造的指针会被无视

### s09 记忆 —— `memory.py`

三个子系统，三件不同的事：

```
选择 selection      哪些记录跟**这次请求**有关？    读取 MEMORY.md 目录，每轮都跑
提取 extraction     这次对话里有什么值得留下？      一轮结束时跑一次
整合 consolidation  库长大了，合并 / 纠正 / 删过时   只有真的存了东西才触发
```

真正承重的是 `scope` 字段：模型要把候选标成 `persistent` 或 `current_task`，
**`current_task` 是完全合法的回答，但永远不会落盘**。这一条规矩就是
"当前先用 8080 端口"没跑进长期记忆的全部原因。

另有 19 个"临时话术"标记（`for now` / `本次会话` / `このセッション` …）在入库前被拦下。

### s10 任务系统 —— `tasks.py`

一个任务一个 JSON 文件，所以图能扛住崩溃，也能被任何读 JSON 的东西检查：

```
.agent/tasks/task_a1b2c3d4.json
{ "id": ..., "subject": ..., "status": "pending", "owner": null,
  "blockedBy": ["task_e5f6a7b8"] }
```

`blockedBy` 就是全部重点：`claim` 时六道闸门在**同一把锁**里跑完，
`complete` 会回报"哪些任务刚刚解锁"，所以模型永远知道下一步做什么，
不需要把整张图记在脑子里。

> 注意：课程 s10 本身**完全没有加锁**。本实现取了 s13 的升级版 ——
> 可重入锁 + 临时文件 `os.replace` 原子落盘。否则两个队友真的会同时"抢到"同一个任务。

### s13 团队 —— `teams.py` / `team_tools.py`

队友**不是**子 agent。子 agent 是函数调用；队友是**同事**：有名字、有自己的对话、
有自己的任务占有、还有一个它会持续读的信箱。

```
Lead agent                          队友 "researcher"
+--------------------+              +------------------------+
| task graph         |  spawn       | 自己的 messages[]      |
| mailbox: lead.jsonl| -----------> | mailbox: researcher... |
| worktree leases    |              | 自己的工具上下文        |
|                    | <----------- | 自主认领就绪任务        |
+--------------------+   信箱        +------------------------+
```

三个机制撑起协作：

* **信箱** —— 每个 agent 一个 JSONL 文件，发送是追加，接收是"读走即删"。
  简单到可以用 `type` 命令调试。
* **原子认领** —— `TaskStore` 在锁里完成状态迁移，所以**有且只有一个**队友能赢，
  不需要任何协商。
* **任务绑定 worktree** —— 队友进入它认领任务绑定的 git worktree 里干活，
  工具 `claim_task` 和自主认领会同步队友状态及目录分配，优先复用任务已有的 worktree。
  认领后同一批次的后续工具立即使用新目录，系统提示词在每次模型调用前刷新。
  `complete_task` 成功后释放目录分配，失败时保留；队友退出时归还未完成任务。
  非 Git 工作区仍使用共享目录；Git worktree 创建或校验失败会返回明确错误并归还新认领任务。

协议消息（计划申请/回复、关闭申请/回复）靠 `request_id` 关联，
迟到的或伪造的回复会被 `match_response` 拒掉（校验类型 + 发起人 + 是否已解决）。

### s14 MCP —— `mcp.py`

```
connect_mcp("docs")  ->  tools/list  ->  mcp__docs__search
                                          ^^^^^^ 命名空间
```

两件事让它既安全又无聊：

* **命名空间**：两个服务器都能有 `search`，不会撞车，模型还看得出工具从哪来。
* **宿主侧授权**：服务器自己描述的 `annotations` 是**不可信输入**。
  一个工具能不能无人值守地跑，由宿主的 `MCP_HOST_POLICY` 决定，跟服务器说什么无关。

除了课程里的进程内 mock 服务器，这里还实现了一个真的
`StdioMCPClient`（换行分隔的 JSON-RPC，后台读线程 + 队列，不用 `select`
所以 Windows 也能跑）。

**关键在时序**：工具池在**每次模型调用前重新读取**，不是开局算一次。
所以模型在第 N 轮调用 `connect_mcp`，第 N+1 轮就能看到新工具 ——
同一个回合内，不需要重启。

```
call 0:  27 tools, mcp=[]                    <- 还没有 docs
         └─ 模型调用 connect_mcp("docs")
call 1:  30 tools, mcp=[search, get_version, list_topics]
```

如果工具池只算一次，模型就**执行不了它没被告知过的工具** —— 而这个问题
用脚本化的 mock 测试会假通过（脚本强行指定了工具名）。所以测试直接断言
**发给模型的工具定义本身**发生了变化，而不是断言工具能跑。

---

## 6. 工具清单（27 个）

| 分类 | 工具 |
|---|---|
| 文件与执行 | `bash` `read_file` `write_file` `edit_file` `glob` `grep` |
| 计划 | `todo_write` |
| 知识 | `load_skill` |
| 上下文 | `compact` |
| 任务图 | `create_task` `update_task` `list_tasks` `get_task` `claim_task` `complete_task` |
| 委派 | `task`（子 agent） |
| 外部能力 | `connect_mcp` + 运行时的 `mcp__<server>__<tool>` |
| 团队 | `spawn_teammate` `list_teammates` `send_message` `request_plan` `review_plan` `pending_requests` `request_shutdown` `create_worktree` `remove_worktree` `list_worktrees` |

> `grep` 不在课程的工具集里（s02 是 bash/read/write/edit/glob 五个）。
> 这里加上了，因为一个没有 grep 的编码 agent 是残废的。
>
> `compact` 只在启用上下文压缩时才注册 —— 没有压缩器时它什么都做不了，
> 与其给模型一个假工具，不如不给。

---

## 7. 测试

```sh
python -m unittest discover -s tests
```

**测试全部离线**，不需要 API key、不需要网络。
`agent/llm.py` 里的 `MockLLM` 支持两种驱动方式：

```python
MockLLM(script=[
    {"tool": "bash", "input": {"command": "echo hi"}},   # 一次工具调用
    [ {"tool": "a", ...}, {"tool": "b", ...} ],          # 一轮并行调用
    "最终答复",                                            # 纯文本 = 停下
])

MockLLM(responder=lambda i, messages, tools: ...)        # 按上下文反应
```

测试覆盖的重点不是"函数返回值对不对"，而是**不变量**：

* 越界路径即使绕开钩子也写不出去；批准不会泄漏到下一次调用
* 一个 `tool_use` 永远不会丢掉它的 `tool_result`
* 四个线程同时抢一个任务，只有一个人赢
* 伪造的存档指针、伪造的协议回复、`current_task` 记忆全部被拒
* 队友的工具池里没有 `task`，子 agent 的工具池里没有 `task`
* 工作区不是 git 仓库时，worktree 请求给出可读的错误而不是崩溃

任务级耗时评测可运行 `python -m evals.team_speed --provider mock` 先检查流程，
再运行 `python -m evals.team_speed --provider deepseek --repeats 3` 做真实对照。
它会分别记录单 Agent 与团队模式从开始到独立验收通过的耗时、模型调用量和失败状态；
设计与局限见 [evals/README.md](evals/README.md)。真实运行会消耗 API 额度。
验证反馈对照可运行 `python -m evals.verification_feedback --provider mock`，
再用 `--provider deepseek` 比较笼统失败提示与具体失败用例对修复成功率的影响。

---

## 8. 已知边界

诚实清单 —— 这些是设计取舍，不是疏漏：

* **信箱没有跨进程锁。** 读走即删，进程内加锁。两个**进程**共享同一个工作区
  会丢消息。当前设计（线程队友）下这是够的。
* **任务是进程内加锁 + 原子写。** 跨进程安全还需要文件锁，而 `fcntl`
  是 POSIX-only，所以这里没做。
* **队友的工具池是固定子集**，不跟随主 agent 动态变化（MCP 工具不会自动下发给队友）。
* **`StdioMCPClient` 没有在受限沙箱里实测过**（沙箱禁止管道通信）；
  进程内 MCP 服务器是主路径，也是被测试覆盖的那条。
* **`--provider mock` 不是模型。** 它只会照本宣科，用来验证外壳，
  不会给你任何智能。
* **压缩阈值按字符数算，不按 token。** 课程如此，所以它可能估错 ——
  这正是 `reactive_compact` 存在的理由。
* **记忆的去重是"归一化后完全相等"，不是语义相似。** 换个说法讲同一件事，
  两条都会被存下来。整合（consolidation）是用来收拾这个的。

---

## 9. 许可

MIT。
