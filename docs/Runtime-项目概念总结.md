# Runtime：项目的运行总管

> 说明：项目中的实际类名是 `Runtime`，定义在 `agent/runtime.py`；本文把它称为“Agent Runtime”，是为了描述它在整个 Agent 系统中的角色。

`Runtime` 负责把一个可运行 Agent 所需的部件创建好、连接起来，并持有一次会话的长期状态。它本身不直接实现“模型循环”或“读写文件”的细节，而是把这些能力交给 `run_loop` 和各个工具模块。

## 一句话理解

```text
Runtime = 组装并管理 Agent 会话的总控制器
run_loop = 驱动模型与工具多轮交互的引擎
ToolContext = 每次工具执行时携带的运行环境
```

可以把它们理解为：

```text
Runtime（整辆车与长期状态）
  └─ run_loop（发动机：一轮轮驱动）
       └─ ToolContext（交给每项工具的驾驶环境与权限）
```

## Runtime 创建和持有的组件

`Runtime.__init__()` 会创建或保存以下对象：

```text
Runtime
├─ settings       全局配置：工作目录、状态目录、模型参数等
├─ llm            模型客户端
├─ tools          工具注册表
├─ hooks          工具前、工具后、停止时的事件处理器
├─ permissions    权限决策与确认机制
├─ lead_ctx       主 agent 的 ToolContext
├─ messages       主 agent 当前对话历史
├─ active_request 当前用户请求
├─ todos          当前会话的计划清单
├─ tasks          持久化任务看板
├─ skills         技能目录与按需加载器
├─ memory         长期记忆选择、提取、整合
├─ compactor      五阶段上下文压缩器
├─ mcp            外部 MCP 服务及其工具
├─ teams          teammate、邮箱、协议与 worktree 管理
└─ stats          回合数、工具次数、请求次数等统计
```

因此，`Runtime` 是一次 Agent 会话的“单一事实来源”：主会话消息、当前请求、工具系统和各项状态机制都从这里找到。

## 它和 run_loop 的关系

用户提交请求后，`Runtime` 会调用 `run_turn()`；后者把准备好的依赖传进 `run_loop()`：

```python
result = run_loop(
    llm=self.llm,
    registry=self.tools,
    messages=target,
    system=self.build_system,
    ctx=self.lead_ctx,
    hooks=self.hooks,
    compactor=self.compactor,
    active_request=self.active_request,
    before_call=self._inject_team_events,
    on_event=self._emit,
)
```

对应关系如下：

| Runtime 提供的东西 | run_loop 如何使用 |
| --- | --- |
| `llm` | 发起模型请求 |
| `tools` | 每轮取工具定义并分发工具调用 |
| `messages` | 保存模型回复、工具调用与工具结果 |
| `build_system` | 每轮动态构造 system prompt |
| `lead_ctx` | 交给所有 lead 工具调用的环境 |
| `hooks` | 工具前后检查、模型结束前检查 |
| `compactor` | 调模型前正常压缩，超限时应急压缩 |
| `_inject_team_events` | 每轮开始时把 teammate 邮箱消息注入对话 |

`run_loop` 结束后，`Runtime` 会更新统计信息；对话历史则因为使用了 `messages[:] = ...` 的原地修改，已经保存在 `Runtime.messages` 中。

## 它和 ToolContext 的关系

主 agent 的工具上下文由 `Runtime` 创建：

```python
self.lead_ctx = ToolContext(
    settings=settings,
    workdir=settings.workdir,
    owner="lead",
    interactive=True,
    runtime=self,
)
```

这个对象会传给 `run_loop`，再传给每一个工具 handler：

```text
Runtime.lead_ctx
  → run_loop(ctx=...)
  → execute_tool(..., ctx=...)
  → registry.dispatch(..., ctx)
  → tool.handler(args, ctx)
```

工具由此获知：

- 自己应在哪个 `workdir` 中读写文件；
- 调用者是 lead 还是某个 teammate；
- 是否允许向用户请求确认；
- 需要时如何通过 `ctx.runtime` 访问任务、团队、MCP 等长期服务；
- `ctx.extra` 中有哪些临时协调状态，例如 `compact_requested`。

## 为什么不让工具直接访问全局变量

同一个工具会被不同身份调用：

```text
lead 调用 write_file
  工作目录：主工作区
  可请求用户确认：是

teammate 调用 write_file
  工作目录：分配给它的 worktree
  可请求用户确认：否
```

若工具直接读取全局目录或全局权限状态，就很难做到隔离。通过 `ToolContext`，同一份工具实现能够根据调用环境安全复用。

## Runtime 处理的关键时序

一次用户请求的大致生命周期：

```text
用户提交请求
  ↓
Runtime.submit()
  ├─ 更新 active_request / messages
  ├─ 选择相关长期记忆
  └─ 调用 Runtime.run_turn()
       ↓
    run_loop()
       ├─ 注入 teammate 邮箱事件
       ├─ 压缩上下文
       ├─ 调用模型
       ├─ 执行模型要求的工具
       └─ 没有工具调用时返回最终文本
  ↓
Runtime 更新统计并返回结果
```

## 与上下文压缩的关系

`Runtime` 持有 `self.compactor`，但压缩的具体时机由 `run_loop` 控制：

```text
每次模型调用前：compactor.prepare(...)
服务商报上下文超长：compactor.reactive_compact(...)
模型调用 compact 工具：当前工具批次结束后 compact_history(...)
```

压缩会改变 `messages` 的内存内容，但完整历史或大型工具输出会先写入磁盘。因此 `Runtime` 继续持有的是“当前可发送给模型的精简历史”，不是永久丢失的信息。

## 与 teammate 的关系

`Runtime` 创建 `TeamManager` 并在每轮模型调用前读取 lead 邮箱：

```text
teammate 写入 mailbox
  ↓
Runtime._inject_team_events(messages)
  ↓
run_loop 的 before_call
  ↓
模型下一轮看到 Team events
```

teammate 不与 lead 共用 `lead_ctx`。它会创建自己的 `ToolContext`，拥有自己的工作目录、身份和非交互式权限设定。

## 边界：Runtime 不负责什么

| 事项 | 主要负责者 |
| --- | --- |
| 模型与工具的循环流程 | `run_loop` |
| 单个工具的实现 | 各工具 handler |
| 工具注册、查找与异常转字符串 | `ToolRegistry` |
| 历史压缩算法 | `ContextCompactor` |
| 文件权限决策 | `PermissionManager` / hook |
| teammate 邮箱与任务协议 | `TeamManager` |

`Runtime` 的职责不是代替这些模块，而是创建它们、连接它们，并在一次会话期间保有它们的状态。
