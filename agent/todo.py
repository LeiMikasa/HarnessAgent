"""TodoWrite -- plan first, then execute.

    without a list          with a list
    ------------------      ------------------
    the model drifts        the model has a spine
    steps get dropped       progress is visible
    "am I done?" unclear    completion is checkable

The list is session state, not a file.  It is re-rendered into the tool result
every time so the model always sees its own current plan, which is what keeps
it honest.

    todo_write(todos=[
        {"content": "read the parser",   "status": "in_progress"},
        {"content": "add the new token", "status": "pending"},
    ])
      ->
    [>] read the parser            (1/2)
    [ ] add the new token
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .tools.registry import ToolContext, ToolRegistry
from .tools.result import ToolResult

PENDING = "pending"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
VALID_STATUSES = (PENDING, IN_PROGRESS, COMPLETED)

MAX_ITEMS = 20
MAX_CONTENT_CHARS = 400

MARKERS = {PENDING: "[ ]", IN_PROGRESS: "[>]", COMPLETED: "[x]"}


class TodoValidationError(ValueError):
    """Raised when a todo payload cannot be accepted."""


@dataclass
class TodoItem:  # 单独一项待办
    content: str
    status: str = PENDING

    def render(self) -> str:
        return f"{MARKERS[self.status]} {self.content}"


@dataclass
class TodoList:  # 当前计划及其历史版本
    """The session's plan, plus every rendering of it."""

    items: list[TodoItem] = field(default_factory=list)  # 当前清单
    history: list[list[TodoItem]] = field(default_factory=list) # 保存每次替换之前的清单，便于回看之前的计划

    # -- mutation -----------------------------------------------------------
    # 替换整张清单
    def replace(self, todos: Any) -> list[TodoItem]:
        """Validate and install a new list, returning the parsed items."""
        items = normalize_todos(todos) # 先解析和验证输入
        self.history.append(list(self.items)) # 验证成功后，把就清单放进history
        self.items = items # 用新清单替换self.item
        return items
    # 清空
    def clear(self) -> None:
        if self.items:
            self.history.append(list(self.items))
        self.items = []

    # -- queries ------------------------------------------------------------
   # 统计已完成数量
    @property
    def done(self) -> int:
        return sum(1 for item in self.items if item.status == COMPLETED)
    # 统计总数
    @property
    def total(self) -> int:
        return len(self.items)
    # 找到正在进行的那项
    @property
    def active(self) -> TodoItem | None:
        return next((item for item in self.items if item.status == IN_PROGRESS), None)

    @property
    def complete(self) -> bool:
        return bool(self.items) and self.done == self.total
    # 返回未完成项目的文本
    def pending_contents(self) -> list[str]:
        return [item.content for item in self.items if item.status != COMPLETED]

    # -- rendering ----------------------------------------------------------
    # 把计划显示给用户和模型
    def render(self) -> str:
        if not self.items:
            return "Todo list is empty."
        lines = [item.render() for item in self.items]
        lines.append(f"({self.done}/{self.total} complete)")
        return "\n".join(lines)

    def summary(self) -> str:
        if not self.items:
            return ""
        return f"{self.done}/{self.total} todos complete"


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

# 把模型对工具的输入转为列表
def coerce_todos(raw: Any) -> list[Any]:
    """Accept a list, or a JSON string containing one.

    Models occasionally stringify their arguments.  Rather than failing the
    call, decode it -- the alternative is a wasted turn.
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TodoValidationError(f"todos is a string but not valid JSON: {exc}") from exc
        if isinstance(decoded, dict):
            decoded = decoded.get("todos", decoded)
        if not isinstance(decoded, list):
            raise TodoValidationError("todos must be a list or a JSON array string")
        return decoded
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    raise TodoValidationError(f"todos must be a list, got {type(raw).__name__}")


def normalize_todos(raw: Any) -> list[TodoItem]:
    """Validate a payload and turn it into `TodoItem`s."""
    entries = coerce_todos(raw)

    if len(entries) > MAX_ITEMS:
        raise TodoValidationError(f"at most {MAX_ITEMS} todos are allowed, got {len(entries)}")

    items: list[TodoItem] = []
    in_progress = 0

    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            entry = {"content": entry, "status": PENDING}
        if not isinstance(entry, dict):
            raise TodoValidationError(f"todos[{index}] must be an object or a string")

        content = entry.get("content")
        if not isinstance(content, str) or not content.strip():
            raise TodoValidationError(f"todos[{index}] needs a non-empty 'content'")
        content = " ".join(content.split())
        if len(content) > MAX_CONTENT_CHARS:
            content = content[:MAX_CONTENT_CHARS] + "..."

        status = str(entry.get("status", PENDING)).strip().lower().replace("-", "_")
        aliases = {
            "todo": PENDING,
            "open": PENDING,
            "not_started": PENDING,
            "in-progress": IN_PROGRESS,
            "inprogress": IN_PROGRESS,
            "active": IN_PROGRESS,
            "doing": IN_PROGRESS,
            "done": COMPLETED,
            "complete": COMPLETED,
            "finished": COMPLETED,
        }
        status = aliases.get(status, status)
        if status not in VALID_STATUSES:
            raise TodoValidationError(
                f"todos[{index}] has invalid status {entry.get('status')!r}; "
                f"expected one of {', '.join(VALID_STATUSES)}"
            )
        if status == IN_PROGRESS:
            in_progress += 1

        items.append(TodoItem(content=content, status=status))

    if in_progress > 1:
        raise TodoValidationError(
            f"at most one todo may be in_progress, got {in_progress}"
        )
    return items


# --------------------------------------------------------------------------
# Tool
# --------------------------------------------------------------------------

# 模型调用工具后真正执行的函数
def run_todo_write(args: dict, ctx: ToolContext) -> str:
    todos = args.get("todos")
    target = _todo_list(ctx)
    try:
        items = target.replace(todos)
    except TodoValidationError as exc:
        return ToolResult.failure('INVALID_ARGUMENT', f"Error: {exc}", action='correct_arguments', execution_status='not_executed')

    if not items:
        return "Cleared the todo list."
    return target.render()

# 找到当前Agent的计划
def _todo_list(ctx: ToolContext) -> TodoList:
    runtime = ctx.runtime
    if runtime is not None and getattr(runtime, "todos", None) is not None:
        return runtime.todos
    # Fallback so the tool works standalone (tests, one-off calls).
    if "todos" not in ctx.extra:
        ctx.extra["todos"] = TodoList()
    return ctx.extra["todos"]


TODO_SCHEMA = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "maxItems": MAX_ITEMS,
            "description": "The complete todo list. Replaces the previous list.",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "minLength": 1},
                    "status": {
                        "type": "string",
                        "enum": list(VALID_STATUSES),
                    },
                },
                "required": ["content", "status"],
            },
        }
    },
    "required": ["todos"],
}

TODO_DESCRIPTION = (
    "Create and manage the plan for the current task. Send the complete list "
    "every time; it replaces the previous one. Keep exactly one item "
    "in_progress while you work, and mark items completed as soon as they are "
    "done. Use it for any task with more than two steps."
)


def register_todo_tools(registry: ToolRegistry) -> ToolRegistry:
    registry.add("todo_write", TODO_DESCRIPTION, TODO_SCHEMA, run_todo_write)
    return registry
