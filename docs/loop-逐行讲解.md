# `agent/loop.py` 逐行讲解

> 这是整个项目唯一的一处 **agent 循环**。181 行（含空行），149 行有内容。
> 读懂这一个文件，剩下的都是"机制挂在哪"的问题，而不是"它怎么转"的问题。

**目录**

- [〇、先建立心智模型](#〇先建立心智模型)
- [一、模块 docstring（1–27 行）](#一模块-docstring127-行)
- [二、导入（29–39 行）](#二导入2939-行)
- [三、LoopResult（42–52 行）](#三loopresult4252-行)
- [四、execute_tool（55–76 行）](#四execute_tool5576-行)
- [五、run_loop 签名（79–92 行）](#五run_loop-签名7992-行)
- [六、初始化（99–105 行）](#六初始化99105-行)
- [七、循环头（107–108 行）](#七循环头107108-行)
- [八、Layer 0：注入（110–112 行）](#八layer-0注入110112-行)
- [九、Layer 1：压缩（114–116 行）](#九layer-1压缩114116-行)
- [十、提示词与工具池（118–124 行）](#十提示词与工具池118124-行)
- [十一、模型调用与反应式重试（126–147 行）](#十一模型调用与反应式重试126147-行)
- [十二、判断要不要继续（149–160 行）](#十二判断要不要继续149160-行)
- [十三、执行工具批次（162–169 行）](#十三执行工具批次162169-行)
- [十四、延迟压缩（171–176 行）](#十四延迟压缩171176-行)
- [十五、转数用光（178–181 行）](#十五转数用光178181-行)
- [十六、全景时间线](#十六全景时间线)
- [十七、动手验证](#十七动手验证)
- [十八、一句话总结](#十八一句话总结)

---

## 〇、先建立心智模型

```
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

三句话概括：

1. **循环不是"重试"，是"把工具结果当新输入再问一遍"。** 模型完全不知道有循环这回事 —— 它每次只看到一个**更长**的对话。所谓"agent 在思考"，其实是同一个模型被反复问，每次多带一点它自己上一步的产出。

2. **循环什么都不知道。** 它不知道工具干什么、模型在哪、压缩怎么做、队友怎么协作。这些全是**函数参数**（第 81–91 行）。这是依赖倒置，也是 18 个机制能加进来而循环一行不改的原因。

3. **三个地方能改变循环行为，都在钩子上，不在循环里：**

   | 钩子 | 能力 | 行号 |
   |---|---|---|
   | `PreToolUse` | **否决**工具调用 | 70–72 |
   | `PostToolUse` | **只能观察**（返回值被丢弃） | 75 |
   | `Stop` | **强制再转一轮**（唯一能改写退出条件的地方） | 154–157 |

---

## 一、模块 docstring（1–27 行）

### 第 1 行

```python
"""The agent loop.  Everything else in this package is built around it.
```

**三引号字符串放在文件最开头 = docstring（文档字符串）**，不是普通注释。

- `#` 注释：解释器直接丢掉，`import` 之后拿不到
- docstring：**会被保留**，可以用 `agent.loop.__doc__` 读到，`help()` 也会显示

### 第 3–11 行：流程图

| 行 | 意思 |
|---|---|
| 3 | 用户输入进 `messages[]`，交给 LLM |
| 4 | LLM 返回 `response` |
| 5 | 判断：response 里有 `tool_use` 块吗？ |
| 6–7 | 分成两条路：yes / no |
| 9 | **yes**：执行工具 |
| 10 | 把结果**追加**进消息列表 |
| 11 | **回到 `messages[]`** ← 关键 |

### 第 13–15 行

```python
The loop does not know what a tool does, where the model lives, or what
compaction is.  It is handed those things.  That is the whole reason the
harness can grow to eighteen mechanisms without the loop ever changing shape.
```

翻译：**循环不知道工具干什么、不知道模型在哪、不知道压缩是什么。这些是"被交给"它的。**

验证方式：看第 34–37 行的 import —— 全是抽象类型（协议、数据类、常量函数），**没有一个具体机制**。没有 `memory`、没有 `teams`、没有 `tasks`。

### 第 17–23 行：三层包裹

```python
Three layers wrap the loop from the outside, in this order:

    1. context compaction   before each call, make room if needed
    2. hooks                PreToolUse can veto, PostToolUse can observe,
                            Stop can force another turn
    3. reactive compaction  if the provider rejects the request anyway,
                            summarize and retry exactly once
```

| 层 | 时机 | 行号 |
|---|---|---|
| 1 上下文压缩 | 调用**前**，事前预防 | 114–116 |
| 2 钩子 | 工具调用**前后** + 想停时 | 70 / 75 / 154 |
| 3 反应式压缩 | 被**拒绝后**，事后补救 | 135–144 |

**层 1 是按字符数估算的**，而字符数 ≈ token 数只是近似（中文一个字符常常比英文占更多 token），所以会估错 —— 这才需要层 3 兜底。

### 第 25–26 行

```python
The lead agent, every subagent, and every teammate run *this* function.  They
differ only in the arguments they pass.
```

`*this*` 的星号是 Markdown 斜体，**不是代码**。

验证：

```powershell
Select-String -Path agent\*.py -Pattern "run_loop\("
```

`runtime.py` 里三处调用：主循环、子 agent、队友 runner。

---

## 二、导入（29–39 行）

### 第 29 行

```python
from __future__ import annotations
```

**"未来导入"，必须放在所有代码最前面**（docstring 之后）。

作用：让**类型注解延迟求值**。不加这行，`def f(x: str | Callable[[], str])` 在 Python 3.9 会直接报错（`|` 语法 3.10 才有）。加了之后注解变成字符串存着，只在需要时求值。

**注意**：它只影响注解。`str | None` 写在函数体里（真运行时）仍然需要 3.10+。

### 第 31–32 行

```python
from dataclasses import dataclass
from typing import Callable
```

只导入了真正用到的两个。（原先这里还导入了 `field` 和 `Any`，但全文从未使用 —— 已清理。）

### 第 34–37 行：全部是抽象，没有具体机制

```python
from .context import COMPACT_FLAG, ContextCompactor, is_prompt_too_long
from .events import POST_TOOL_USE, PRE_TOOL_USE, STOP, Hooks
from .llm import LLMClient, extract_text, make_tool_result, tool_use_blocks
from .tools.registry import ToolContext, ToolRegistry
```

**开头的 `.` 是相对导入** —— "同一个包里的那个文件"。

| 名字 | 用在哪 |
|---|---|
| `COMPACT_FLAG` | 174 行（取延迟压缩标记） |
| `ContextCompactor` | 88 行（类型注解） |
| `is_prompt_too_long` | 138 行（判断错误类型） |
| `POST_TOOL_USE` / `PRE_TOOL_USE` / `STOP` | 70 / 75 / 154 行 |
| `Hooks` | 86 / 99 行 |
| `LLMClient` | 81 行 |
| `extract_text` | 158 行 |
| `make_tool_result` | 168 行 |
| `tool_use_blocks` | 150 行 |
| `ToolContext` / `ToolRegistry` | 82 / 85 / 58 / 57 行 |

**事件名用常量而不是直接写字符串**：打错字立刻 `NameError`，而不是静默不触发钩子 —— 后者极难查。

### 第 39 行

```python
MAX_REACTIVE_RETRIES = 1
```

**全大写 = 模块级常量约定**（Python 没有真常量，这是约定）。

**为什么是 1，不是 2 或 3？** 因为反应式压缩**本身要花一次模型调用**（要模型总结历史）。如果压完还是超限，说明问题**不是总长度** —— 很可能单条消息自己就超了，压不动。再试只是白烧钱。

---

## 三、`LoopResult`（42–52 行）

```python
@dataclass
class LoopResult:
    text: str = ""
    turns: int = 0
    stop_reason: str = "final"      # final | max_turns | error
    error: str = ""
    tool_calls: int = 0

    @property
    def ok(self) -> bool:
        return self.stop_reason == "final"
```

### 第 42 行：`@dataclass`

**装饰器**，自动生成 `__init__`、`__repr__`、`__eq__`。不加就得手写：

```python
def __init__(self, text="", turns=0, stop_reason="final", error="", tool_calls=0):
    self.text = text
    ...
```

### 第 44–48 行：为什么需要这些字段

| 字段 | 含义 | 为什么需要 |
|---|---|---|
| `text` | 模型最后说的文本 | 正常结束时要交给用户 |
| `turns` | 跑了几轮 | 统计/诊断 |
| `stop_reason` | **怎么结束的** | 最关键 |
| `error` | 错误详情 | 失败时才有内容 |
| `tool_calls` | 工具调用**总次数** | 统计 |

**`stop_reason` 的三个值**（注释里的 `|` 是"三选一"惯例）：

| 值 | 含义 | 调用者该怎么做 |
|---|---|---|
| `final` | 模型正常说完了 | 把 `text` 给用户 |
| `max_turns` | 转数用光，**没做完** | 告诉用户没做完 |
| `error` | 供应商报错，**没做完** | 报错 |

**为什么必须区分后两个和 `final`？** 举个真实后果：队友循环里，如果 `max_turns` 被当成"完成任务"，队友会去 `complete_task()`，**把一个没做完的任务标记成完成** —— 整个任务图就开始骗人了。

### 第 50–52 行：`@property`

让方法**调用时不用加括号**：写 `result.ok`，不是 `result.ok()`。

`-> bool` 是返回类型注解（给人看 + 给类型检查器看，运行时不强制）。

一行实现，语义很重：**只有 `final` 算成功。**

---

## 四、`execute_tool`（55–76 行）

> **这是全系统唯一的工具执行入口。** 权限和审计只需写一遍，就是因为所有调用都过这里。

### 第 55–60 行：签名

```python
def execute_tool(
    block: dict,
    registry: ToolRegistry,
    ctx: ToolContext,
    hooks: Hooks,
) -> str:
```

**注意这里没有 `*`** —— 四个参数是位置参数。

| 参数 | 是什么 |
|---|---|
| `block` | `{"type":"tool_use","id":"toolu_x","name":"bash","input":{"command":"ls"}}` |
| `registry` | 工具池，靠 `block["name"]` 查 handler |
| `ctx` | 本次调用的环境（工作目录、owner、权限策略） |
| `hooks` | 钩子注册表 |

返回 `-> str`：**永远返回字符串**。

### 第 66–68 行：一行注释藏着安全修复

```python
    # An approved path-escape is good for exactly one call.  Clearing it here
    # means an approval can never leak into the next tool call.
    ctx.extra.pop("allow_outside", None)
```

**背景**：工具默认被限制在工作区内（`safe_path` 硬拦）。但用户可以**批准**一次越界写。批准通过 `ctx.extra["allow_outside"] = True` 传达。

**问题**：`ctx` 是**跨调用复用**的（主 agent 从头到尾一个）。如果批准后不清，**第一次批准会永久解锁整个会话的越界写**。

**为什么用 `pop` 不是 `get`？** `pop(key, default)` **取出并删除**；键不存在时返回 `default`（`None`）而不报错。用 `pop` 才是"一次性凭证"的正确语义 —— `get` 只读不删，标记会永远留着。

有测试兜底：`test_allow_outside_does_not_leak_between_calls` —— 用"只批准第一次"的 approver，断言第二次的文件没被创建。

### 第 70 行：触发 `PreToolUse`

```python
    blocked = hooks.trigger(PRE_TOOL_USE, block, ctx)
```

`trigger` 的语义：**依次调用所有钩子，返回第一个非 `None` 的结果**。

```python
# events.py 的实现
for callback in self._events.get(event, ()):
    result = callback(*args)
    if result is not None:
        return result        # ← 提前返回，短路
return None
```

- 返回 `None` = 这个钩子没意见，问下一个
- 返回字符串 = **有意见，立刻停止**，后面的钩子不再执行

### 第 71–72 行：否决路径

```python
    if blocked:
        return str(blocked)
```

三件事：

1. **直接 return** —— 不执行 handler，也**不触发 `PostToolUse`**（没执行的工具不该有"执行后"事件）
2. **`str(blocked)`** —— 钩子可能返回非字符串，强制转换保险
3. **返回值成了工具结果** —— 模型会读到 `"Permission denied: ..."` 并据此调整

> ⚠️ **一个棱角**：`if blocked:` 是**真值判断**，不是 `is not None`。
> 如果某个钩子返回**空字符串 `""`**，`if ""` 为假 → **不会拦下调用**。
> 目前没有钩子返回空串（`check_hook` 至少返回 `"Permission denied."`），所以不是 bug。
> **但你以后自己写钩子时，别用空串当拒绝** —— 要用明确的文字。
>
> （课程里这点也不一致：s10/s14 用 `if blocked:`，s11/s12 用 `if blocked is not None:`。）

### 第 74 行：查表并执行

```python
    output = registry.dispatch(block.get("name", ""), block.get("input", {}), ctx)
```

- `block.get("name", "")` —— 用 `get` 不用 `[]`，**坏数据不抛 `KeyError`**，而是走到 dispatch 里返回 `"Unknown tool: "`
- `dispatch` 的契约：**永不抛异常**。工具内部炸了会变成 `"Error: ValueError: ..."` 字符串给模型看

### 第 75 行：`PostToolUse`，返回值被丢掉

```python
    hooks.trigger(POST_TOOL_USE, block, output, ctx)
```

**这是有意的不对称：**

| 钩子 | 能改变什么 |
|---|---|
| `PreToolUse` | **能**否决（返回值有意义） |
| `PostToolUse` | **只能观察**（返回值被忽略） |

**为什么？** 工具**已经执行完了**，副作用已经发生。如果 `PostToolUse` 能改 output，一个"日志钩子"就能悄悄篡改模型看到的事实。

### 第 76 行

```python
    return output
```

返回给 `run_loop`，最终变成 `tool_result` 的内容。

---

## 五、`run_loop` 签名（79–92 行）

### 第 79–80 行：孤零零的 `*`

```python
def run_loop(
    *,
```

**关键字分隔符** —— 它后面的所有参数**必须用关键字传**。

```python
run_loop(llm, registry, messages)              # ✗ TypeError
run_loop(llm=llm, registry=registry, ...)      # ✓
```

**为什么需要？** 这函数有 10 个参数、其中 5 个可选。允许位置传参的话，`run_loop(llm, reg, msgs, sys, ctx, True, 50, None, ...)` 读起来是灾难，改签名顺序还会**静默出错**。强制关键字 = **调用点自解释**。

### 第 81–85 行

| 行 | 参数 | 说明 |
|---|---|---|
| 81 | `llm: LLMClient` | 模型客户端（可能是真的，也可能是 `MockLLM`） |
| 82 | `registry: ToolRegistry` | 工具池 |
| 83 | `messages: list[dict]` | 会话历史。**关键契约：函数原地修改它** |
| 84 | `system: str \| Callable[[], str]` | 见下 |
| 85 | `ctx: ToolContext` | 连 `max_tokens` 都从这里取（132 行） |

**第 84 行的 `Callable[[], str]` 拆开读：**

- `Callable[...]` = 可调用的东西（函数）
- `[[], str]` = **接收 0 个参数**（空参数列表 `[]`），**返回 `str`**

**为什么允许函数？** 因为主 agent 的提示词**每轮都要重建**（队友名单变了、计划变了、记忆选了新记录）。传方法进来，第 118 行每轮调一次就能拿到最新版。子 agent 传固定字符串。

### 第 86–91 行：五个可选参数

```python
    hooks: Hooks | None = None,
    max_turns: int = 50,
    compactor: ContextCompactor | None = None,
    active_request: str = "",
    before_call: Callable[[list[dict]], None] | None = None,
    on_event: Callable[[str], None] | None = None,
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `hooks` | `None` | 第 99 行会换成空注册表，循环体不用判空 |
| `max_turns` | `50` | 防死循环的硬闸门 |
| `compactor` | `None` | **`None` = 不压缩**。子 agent 就传 None（生命周期短，不值得多花一次模型调用） |
| `active_request` | `""` | **用户这轮原始请求的文本** —— 见下 |
| `before_call` | `None` | 团队信箱事件进模型的通道 |
| `on_event` | `None` | 纯观测回调 |

**`active_request` 为什么单独存一份？** 因为 `messages` 里有很多 `role="user"` 的消息 —— **工具结果也是 user 角色**！压缩后要生成"当前请求是 X"这一行，得知道哪条才是用户真正打的字。

**`Callable[[list[dict]], None]` 拆开读**：接收 `list[dict]`，返回 `None`。返回 `None` 是**契约** —— 这个钩子只能往 messages 里写，不能自己决定循环走向。

### 第 92–98 行：docstring

```python
    """Drive the model until it stops asking for tools.

    `messages` is mutated in place so the caller keeps the full conversation.
    `before_call` runs at the top of every iteration and may append to
    `messages` -- that is how team mailbox events reach the model mid-turn.
    """
```

**"mutated in place"（原地修改）是最重要的契约。** 反引号是 Markdown 代码标记，纯排版。

---

## 六、初始化（99–105 行）

### 第 99 行

```python
    hook_registry = hooks or Hooks()
```

**`or` 的短路求值**：`hooks` 是真值就用它，是 `None`（假值）就建空注册表。

等价于 `hooks if hooks is not None else Hooks()`，但短得多。

**为什么建空注册表而不是允许 `None`？** 这样循环体可以无条件写 `hook_registry.trigger(...)`。空注册表的 `trigger` 永远返回 `None`，行为等于"没有钩子"。

变量名用 `hook_registry` 而非 `hooks` —— 避免和第 86 行的参数同名。

### 第 100 行

```python
    reactive_retries = 0
```

**局部变量**（不是全局）。所以重试预算按**一次 `run_loop` 调用**算 —— 也就是按**一个用户回合**算。

如果它是全局的，第一个请求用掉额度后，**后面所有请求都不能补救了**。

### 第 101 行

```python
    result = LoopResult()
```

用默认值建结果对象。

> ⚠️ 注意 `stop_reason` 默认是 `"final"` —— 所以**任何提前 return 的地方都必须显式改它**，否则失败会被误报成成功。
> 看第 145–146 行（改 `error`）和第 178–179 行（改 `max_turns`）就是在做这件事。

### 第 103–105 行

```python
    def emit(text: str) -> None:
        if on_event and text:
            on_event(text)
```

**嵌套函数（闭包）** —— 能访问外层的 `on_event`。

两个条件：`on_event` 存在（没传回调就什么都不做）、`text` 非空。

**为什么要包这一层？** 因为 `emit(...)` 出现在 141、167、176 三处。不包的话每处都要写 `if on_event:`。

---

## 七、循环头（107–108 行）

### 第 107 行

```python
    for turn in range(1, max(1, max_turns) + 1):
```

三层拆解：

**① `range(1, N + 1)`** 产生 `1, 2, ..., N`（`range` 右端**不包含**）。所以 `max_turns=3` 跑 3 轮。

**② `max(1, max_turns)`** 是防御：如果传 `max_turns=0` 或负数，`range(1, 1)` 是**空循环** —— 函数直接跳到第 178 行返回 `max_turns`。行为上"一轮不跑"，比崩溃好。

**③ 为什么用 `for` 而不是 `while True`？**

因为**转数上限是结构性的**，写在循环头上，不可能忘。用 `while True` + 循环体内 break 的话，"记得处理上限"就成了每个修改者都要记住的纪律 —— 迟早忘。

### 第 108 行

```python
        result.turns = turn
```

每轮开头记录。因为后面有 `continue` 和 `return`，放在最后统一赋值会漏掉 `continue` 路径。

---

## 八、Layer 0：注入（110–112 行）

```python
        # -- layer 0: anything the harness owes the model right now ---------
        if before_call is not None:
            before_call(messages)
```

注释里拖尾的 `---` 只是视觉分节符。

**"layer 0" 是因为它比 docstring 里说的三层还靠前。**

**为什么必须排在最前？** 因为它往 `messages` 里塞东西，而**第 116 行的压缩会重排甚至归档 `messages`**。如果注入放在压缩之后，刚塞进去的团队消息可能**立刻被归档掉**。

**顺序在这里不是风格问题，是正确性问题。**

传的是 `messages` 本身（不是副本）—— 这个钩子的契约就是原地追加。

---

## 九、Layer 1：压缩（114–116 行）

```python
        # -- layer 1: make room before asking -------------------------------
        if compactor is not None:
            messages[:] = compactor.prepare(messages, active_request)
```

### 第 116 行是全文最容易写错的一行

`compactor.prepare(...)` 返回一个**新的 list**（做了五级处理：持久化超大结果 → 裁剪中间 → 换掉旧结果 → 瘦身 → 总结）。

**关键在于左边的 `messages[:] =`：**

| 写法 | 发生了什么 | 结果 |
|---|---|---|
| `messages = compactor.prepare(...)` | 只改**局部变量** `messages` 指向 | 调用者手里的 list **完全没变**，压缩白做 |
| `messages[:] = compactor.prepare(...)` | **切片赋值**：清空原 list 再填入新内容 | 调用者的 list **内容变了，对象身份不变** |

**为什么必须是后者？**

调用者传进来的是**它自己的会话历史**。循环必须让调用者看到压缩结果 —— 否则下一轮调用者还会拿超长历史去问模型，**压缩等于没做**。

**为什么不用 `messages.clear(); messages.extend(...)`？** 效果完全一样，但 `[:] =` 是 Python 惯用写法，更短，而且一眼能看出"原地替换"。

**顺带**：第 149、156、169 行用的是 `.append()`，也是原地修改。所以整个函数里 `messages` 的**身份从头到尾不变，只变内容**。

---

## 十、提示词与工具池（118–124 行）

### 第 118 行

```python
        system_prompt = system() if callable(system) else system
```

**三元表达式**：`A if 条件 else B`。

- `callable(x)` 判断 x 能不能被调用（函数、方法、带 `__call__` 的对象都是 True）
- 是函数 → 调它拿字符串（第 84 行的动态重建）
- 是字符串 → 直接用

**代价**：每轮拼一次字符串（微秒级）。**收益**：队友名单、todo 进度、记忆选择永远最新。

### 第 120–124 行：全文件最值得读的一段注释

```python
        # -- re-read the tool pool on EVERY iteration ----------------------
        # Not an optimisation detail: `connect_mcp` adds tools mid-turn, so a
        # pool computed once up front would let the model *execute* a tool it
        # was never *offered*.  Re-reading here is what makes the pool dynamic.
        tools = registry.definitions()
```

**这段注释记着一个真 bug，实际踩过。** 完整链条：

我原来把这行写在循环**外面**（只算一次）。后果：

1. 模型调用 `connect_mcp("docs")` → 工具**真的**注册进注册表了
2. 下一轮模型要 `mcp__docs__search` → **能执行**（`dispatch` 是实时查注册表的）
3. **但模型从没在工具列表里见过这个工具** → 真实模型根本不会去调它

**而且测试还通过了。** 因为 `MockLLM` 是脚本驱动的，脚本里写死了 `mcp__docs__search`，不管有没有被 offer 都会"调"它。

**这是"测试给假安全感"的教科书案例**：执行路径正常，**缺的是告知路径**。

修复后实测：

```
call 0:  27 tools, mcp=[]          ← 还没 connect
         └─ 模型调用 connect_mcp
call 1:  30 tools, mcp=[search, get_version, list_topics]
```

`definitions()` 只是把每个 `Tool` 转成 API 要的 dict，开销很小，每轮重算完全划算。

**注意左边没有 `[:]`** —— `tools` 是局部变量，不是调用者传进来的。

---

## 十一、模型调用与反应式重试（126–147 行）

### 第 128–133 行

```python
            response = llm.create(
                system=system_prompt,
                messages=messages,
                tools=tools,
                max_tokens=ctx.settings.max_tokens,
            )
```

**`llm` 是接口不是实现** —— 可能是真的 `AnthropicLLM`，也可能是测试用的 `MockLLM`。循环完全不关心。

四个参数全部来自外部：提示词（118 行重建）、语料（可能刚压缩）、工具池（124 行重读）、输出上限（从 `ctx.settings` 取）。

### 第 134 行

```python
            reactive_retries = 0
```

**这一行在 `try` 里面、调用成功之后。** 意思是"这次成功了，把重试额度还回来"。放在循环开头行为会不同 —— 这里更精确：**只有真正成功才还额度**。

### 第 135 行

```python
        except Exception as exc:  # noqa: BLE001
```

**`except Exception`** 捕获几乎所有异常（除了 `KeyboardInterrupt`、`SystemExit` 这类 `BaseException`）。

**为什么敢这么宽？** 供应商的异常类型太多（认证、限流、超时、连接、格式），列举不现实。而且这里的处理是"包装成 error 返回"，**不吞掉信息**。

**`# noqa: BLE001`** 是**给 linter 看的指令**（ruff 规则编号），意思是"我知道我在宽捕，别报我"。

**这个注释的存在本身就是文档** —— 它告诉你作者是**故意**的，不是忘了收窄。

### 第 136–140 行：三个条件，顺序有意义

```python
            if (
                compactor is not None
                and is_prompt_too_long(exc)
                and reactive_retries < MAX_REACTIVE_RETRIES
            ):
```

`and` 短路，从便宜到贵：

| # | 条件 | 为什么在这一位 |
|---|---|---|
| 1 | `compactor is not None` | 最便宜 —— 没压缩器就无从补救 |
| 2 | `is_prompt_too_long(exc)` | 次便宜（字符串匹配） |
| 3 | 额度没用完 | 最后判 |

**条件 2 是最重要的设计判断**：401 认证失败、网络断了、参数格式错 —— **压缩一百次也没用**。只有"上下文超限"才值得补救。

`is_prompt_too_long` 的实现是**对错误文本做子串匹配**（`prompt_too_long`、`too many tokens`、`context_length`、`maximum context`）。

**必须承认这是在猜。** 供应商错误格式各家不同，没有标准。所以它只是**尽力而为的兜底** —— 第 115 行的事前压缩才是主要防线。

### 第 141–144 行

```python
                emit("[reactive compact]")
                messages[:] = compactor.reactive_compact(messages, active_request)
                reactive_retries += 1
                continue
```

方括号是日志惯例（突出这是**外壳的动作**，不是模型说的）。

**主动压缩 vs 反应式压缩的区别**：主动把**全部**历史换成一条摘要；反应式**保留最新 5 条**，只总结老的部分（刚被拒绝，要尽量少丢东西）。

**`continue` 回到循环顶，但 `for` 计数器已经 +1** —— 所以重试要**吃掉一次转数**。这是有意的第二道保险。

### 第 145–147 行

```python
            result.stop_reason = "error"
            result.error = f"{type(exc).__name__}: {exc}"
            return result
```

**注意：不抛异常，而是返回 `error` 结果。**

**为什么不抛？** 调用链上面是 REPL 循环和队友线程 —— 抛出去就得**处处** try/except。返回结果让每个调用者自己决定怎么表现。

`f"{type(exc).__name__}: {exc}"` 拆开：

- `f"..."` = f-string，`{}` 里放表达式
- `type(exc).__name__` = 异常**类名**（`"AuthenticationError"`）
- `exc` = 异常**内容**

**为什么要类名前缀？** 光看消息你分不清"认证失败"还是"网络断了"，类名一眼区分。

---

## 十二、判断要不要继续（149–160 行）

### 第 149 行

```python
        messages.append({"role": "assistant", "content": response.content})
```

**无条件先把模型这轮说的话记下来。**

**为什么无条件？** 因为 `response.content` 里可能有 `tool_use` 块。Anthropic API 有**硬性要求**：`tool_use` 必须和对应的 `tool_result` 成对出现。记下来了，下面就必须执行并记录结果。

`response.content` 是**块列表**，可能长这样：

```python
[
  {"type": "text", "text": "我来看看文件。"},
  {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "x.py"}}
]
```

### 第 150 行

```python
        calls = tool_use_blocks(response.content)
```

筛出所有 `tool_use` 块。模型可能一次要 **3 个工具**（并行调用），所以是**列表**。只说话的话，`calls` 是空列表。

### 第 152–153 行

```python
        # -- no tool call: the model wants to stop --------------------------
        if not calls:
```

空列表是假值，所以 = "如果没要工具"。

**这是"模型想停下来"的唯一信号。** 没有专门的停止指令 —— 模型不再要工具，就等于它说完了。

### 第 154 行：`Stop` 钩子

```python
            forced = hook_registry.trigger(STOP, messages)
```

**全部 18 个机制里唯一能改写退出条件的地方。**

- 返回 `None` = 放行，循环结束
- 返回字符串 = **强制再转一轮**，字符串作为 user 消息喂回去

**这条通路的实际用途：**

- `memory.py` 的记忆提取挂在 Stop 上 —— 提取完返回 `None`，不影响退出
- 课程 s17 的 goal loop 整个就靠它实现"目标没达成不许停"

### 第 155–157 行

```python
            if forced:
                messages.append({"role": "user", "content": str(forced)})
                continue
```

`str(forced)` 强制转字符串（API 要求 content 是字符串）。

模型刚说"我完事了"，外壳说"不，继续" —— 而模型会看到一条新的 user 消息（就是那个 forced 字符串）。

**这就是 goal loop 的全部机制：三行代码。**

### 第 158–160 行：正常结束

```python
            result.text = extract_text(response.content).strip()
            result.stop_reason = "final"
            return result
```

`extract_text` 只取 `text` 类型的块并拼接 —— content 里可能还有思考块（thinking），那些不该给用户看。

`.strip()` 去掉首尾空白。

`stop_reason = "final"` **显式设置**：虽然默认值就是它，但显式写出来让读者不用回去查默认值，也防止以后改默认值出意外。

---

## 十三、执行工具批次（162–169 行）

```python
        # -- execute the batch, collect results -----------------------------
        results = []
        for block in calls:
            output = execute_tool(block, registry, ctx, hook_registry)
            result.tool_calls += 1
            emit(f"{block.get('name')}: {output[:160]}")
            results.append(make_tool_result(block.get("id", ""), output))
        messages.append({"role": "user", "content": results})
```

| 行 | 说明 |
|---|---|
| 163 | 每轮**新建**列表，不跨轮复用 |
| 165 | 钩子 → 执行 → 钩子，**永远返回字符串** |
| 166 | 累加到 `result`（**跨轮**累计）。一轮 3 个工具会 +3 |
| 167 | **`output[:160]` 只是给终端看的** |
| 168 | `make_tool_result(tool_use_id, content)` 造出配对结果 |
| 169 | **所有结果打包成 ONE 条 user 消息** |

### 第 167 行的关键区别

**进模型历史的是完整 `output`（168 行），这里截断的只是显示。** 别搞混 —— 模型看到的是全的。

### 第 168 行的配对键

```python
{"type": "tool_result", "tool_use_id": "toolu_1", "content": output}
```

**`tool_use_id` 就是靠它把结果和请求对上。** id 错了或丢了，API 会报错。

### 第 169 行：为什么必须打包成一条

**Anthropic API 要求**：**一条** assistant 消息里的**每个** `tool_use` 块，都必须在紧随其后的**同一条** user 消息里得到 `tool_result`。

分成 3 条会直接 **400 报错**。

### ⚠️ 一个重要的性能性质

第 164 行的 `for` 是**串行执行**的。三个工具会**一个接一个**跑完（子 agent 甚至在这个循环里同步跑完整轮）。

所以"模型并行要 3 个工具" **≠** "3 个工具真的并行执行"。

这是有意的简化（真并发要处理上下文共享和线程安全），但也是**性能天花板**。

---

## 十四、延迟压缩（171–176 行）

```python
        # -- the model asked for room: compact now that the batch is safe ---
        # Deferred on purpose.  A `write_file` in this same batch must be
        # recorded in the history before that history is discarded.
        if ctx.extra.pop(COMPACT_FLAG, False) and compactor is not None and compactor.enabled:
            messages[:] = compactor.compact_history(messages, active_request)
            emit("[compact] history replaced with a summary")
```

**背景**：模型可以主动调 `compact` 工具要求腾地方。但那个 handler **不压缩**，只把 `ctx.extra["compact_requested"] = True` 挂起。真正的压缩在这里。

**为什么必须延迟？** 举个具体例子：

模型同一批里要了 `write_file` 和 `compact`。如果 `compact` 立即执行，历史被换成一条摘要 —— **那个 `write_file` 的结果还没进历史就一起没了**。模型下一轮会以为自己没写过文件，可能重写一遍。

### 第 174 行拆解

```python
if ctx.extra.pop(COMPACT_FLAG, False) and compactor is not None and compactor.enabled:
```

- **`pop(COMPACT_FLAG, False)`** —— 取出标记**并删除**，不存在时返回 `False`
- **为什么用 `pop` 不用 `get`？** 标记取出就该清掉，否则第 175 行会**每轮重复压缩**
- `compactor is not None` / `compactor.enabled` —— 两道存在性检查

**`and` 短路**：主 agent 不用压缩功能时，第一个 `pop` 返回 `False` 就短路了，几乎零开销。

---

## 十五、转数用光（178–181 行）

```python
    result.stop_reason = "max_turns"
    result.error = f"stopped after {max_turns} turns without a final answer"
    result.text = result.error
    return result
```

**注意缩进** —— 这四行和 `for`（第 107 行）**平级**，在循环**外面**。

能执行到这里，说明 `for` 正常跑完所有轮数、**没有**从循环体内 `return`。也就是：模型一直要工具，一直有活干，转数被耗光。

| 行 | 说明 |
|---|---|
| 178 | **`"max_turns"` 不是 `"final"`** —— 队友循环靠这个区分"做完了"和"炸了" |
| 179 | 把 `max_turns` 的实际值填进去，不写死 "50" |
| 180 | **`text` 也设成错误信息** —— 有些调用者只读 `text`，留空会让用户看到一片空白 |

---

## 十六、全景时间线

一次带工具调用的完整流程：

```
107  第 1 轮开始
108    turns = 1
112    before_call          → 注入团队消息（如果有）
116    prepare()            → 五级压缩（如果超限）        ← messages[:] = 原地替换
118    system()             → 重建提示词
124    definitions()        → 取工具池                  ← 每轮都取（真 bug 修复点）
128    llm.create()         → 调模型
134    成功，重置重试额度
149    append(assistant)    → 记下模型的话（无条件）
150    tool_use_blocks()    → 有几个工具调用？
153    有 → 不走停止分支
165      execute_tool()     → 钩子 + 执行 + 钩子
168      make_tool_result   → 用 id 配对
169    append(user, results)→ 一次性回灌全部结果
174    compact 标记？       → 延迟压缩（保证同批副作用已入历史）
107  ── 回到第 2 轮 ──
153    没有工具调用 → Stop 钩子
155      forced？→ 强制再转一轮
160      否则 return "final"
```

---

## 十七、动手验证

**一、证明第 124 行必须在循环内。**

把 `tools = registry.definitions()` 挪到循环外，然后跑：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_core.MCPTests.test_a_late_connected_tool_is_offered_in_the_same_turn
```

会失败。那个测试就是为这个 bug 写的 —— 它断言的是**发给模型的工具定义本身**变了，而不是"工具能不能跑"。

**二、证明第 116 行的 `[:]` 是必需的。**

把 `messages[:] =` 改成 `messages =`，然后跑：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_stateful.CompactionTests
```

压缩相关的测试会挂 —— 因为调用者看不到压缩结果了。

**三、确认 `execute_tool` 是唯一入口。**

```powershell
Select-String -Path agent\*.py -Pattern "execute_tool"
```

只会有 `loop.py`（定义）和 `runtime.py`（转发）。

**四、确认机制挂在钩子上，而不是塞进循环里。**

```powershell
Select-String -Path agent\*.py -Pattern "register\(PRE_TOOL_USE"
```

权限、计划闸门、日志全在这里注册 —— `loop.py` 一行都不用改。

---

## 十八、一句话总结

这个函数做的事是：

> **拿一个会话语料，反复问模型；模型要工具就执行、把结果记进语料、再问一遍；模型不要工具就停。**

它自己不知道工具是什么、模型在哪、压缩怎么做、队友怎么协作 —— **这些全是参数**。

所以 18 个机制加进来，这 181 行**一行都不用改**。

---

## 附：下一步读什么

| 顺序 | 文件 | 行数 | 为什么 |
|---|---|---|---|
| 1 | `agent/todo.py` | 202 | **最小的完整机制模板**：handler + schema + register 三段式 |
| 2 | `agent/events.py` | 81 | 四个扩展点的完整实现 |
| 3 | `agent/permissions.py` | 309 | 钩子的第一个真实使用者（三道闸门） |
| 4 | `agent/prompt.py` | 96 | 模型到底被告知了什么 |
| 5 | `agent/runtime.py` | 517 | 接线图，重点看 `__init__` 和 `build_system` |

看懂 `todo.py` 之后，`skills` / `subagent` / `tasks` / `mcp` 全是它的变体，可以跳读。只有 `teams.py`（1004 行）需要单独花时间。
