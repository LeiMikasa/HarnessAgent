"""The tool pool.

One registry, one dispatch map, one definition list.  Adding a capability means
registering a `Tool`; the loop is never touched.

    registry = ToolRegistry()
    register_basic_tools(registry)        # bash, read, write, edit, glob, grep
    register_todo_tools(registry)         # todo_write
    register_subagent_tools(registry)     # task
    ...
    mcp_tools, mcp_handlers = mcp.assemble_tool_pool()   # external capability
    registry.merge(mcp_tools, mcp_handlers)

`ToolContext` is what makes one registry serve many callers: the lead agent, a
subagent, and three teammates all share the code but carry different working
directories, owners, and approval behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from ..config import Settings


@dataclass
class ToolContext: # 表示一次工具调用的上下文，会传给每个工具处理函数
    """Per-call environment handed to every tool handler."""

    settings: Settings  # 全局配置
    workdir: Path  # 当前工作目录，不同agent可能不同
    owner: str = "agent"          # "agent" for the lead, teammate name otherwise，调用者身份
    interactive: bool = True       # may this caller prompt a human? 是否允许向人类提问
    runtime: Any = None            # back-reference for tools that need the harness 反向引用，工具需要访问 harness/runtime 时用
    extra: dict = field(default_factory=dict) # 扩展字段
    # 复制一份上下文，返回一个对象
    def child(self, **overrides: Any) -> "ToolContext":
        """A copy with fields replaced -- used to enter a worktree, or to hand a
        non-interactive context to a teammate thread."""
        data: dict[str, Any] = {
            "settings": self.settings,
            "workdir": self.workdir,
            "owner": self.owner,
            "interactive": self.interactive,
            "runtime": self.runtime,
            "extra": dict(self.extra),
        }
        data.update(overrides)
        return ToolContext(**data)

# 工具处理类型
ToolHandler = Callable[[dict, ToolContext], Any]


@dataclass
class Tool:  # 工具类
    """One action the model may take."""

    name: str
    description: str
    input_schema: dict
    handler: ToolHandler
    source: str = "builtin"        # "builtin" or "mcp__<server>"
    read_only: bool = False  # 是否只读，可用于权限控制，并发调度

    def definition(self) -> dict:
        """The shape the model API expects."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:# 工具注册表
    """An ordered, name-keyed collection of tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # -- registration -------------------------------------------------------

    def register(self, tool: Tool, *, replace: bool = False) -> Tool:
        if tool.name in self._tools and not replace:
            raise ValueError(f"Tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool
        return tool

    def add(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: ToolHandler,
        *,
        source: str = "builtin",
        read_only: bool = False,
    ) -> Tool:
        return self.register(
            Tool(
                name=name,
                description=description,
                input_schema=input_schema,
                handler=handler,
                source=source,
                read_only=read_only,
            )
        )

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
    # 移除某个来源的所有工具，典型场景：断开一个MCP server,把它带来的工具全部清掉
    def unregister_source(self, source: str) -> list[str]:
        """Drop every tool from a source (e.g. disconnect an MCP server)."""
        removed = [name for name, tool in self._tools.items() if tool.source == source]
        for name in removed:
            del self._tools[name]
        return removed
    # 合并另一个注册表或字典，用于把MCP工具池合并进主注册表
    def merge(self, tools: dict[str, Tool] | "ToolRegistry", *, replace: bool = False) -> None:
        if isinstance(tools, ToolRegistry):
            items = tools.tools().items()
        else:
            items = tools.items()
        for name, tool in items:
            self.register(tool, replace=replace)
    # 拷贝注册表，适合给subagent一份独立工具集
    def clone(self) -> "ToolRegistry":
        copy = ToolRegistry()
        copy._tools = dict(self._tools)
        return copy

    # -- lookup -------------------------------------------------------------

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def tools(self) -> dict[str, Tool]:
        return dict(self._tools)

    def names(self) -> list[str]:
        return list(self._tools)

    def definitions(self) -> list[dict]: # 返回所有工具给模型看的定义列表；
        return [tool.definition() for tool in self._tools.values()]

    def source_of(self, name: str) -> str:
        tool = self._tools.get(name)
        return tool.source if tool else ""

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    # -- execution ----------------------------------------------------------
    # 执行相关 ； 运行工具并返回文本输出
    def dispatch(self, name: str, args: dict | None, ctx: ToolContext) -> str:
        """Run one tool and return its output as text.

        Never raises: a broken tool becomes an error string for the model to
        read, which is what lets the loop keep going instead of dying.
        """
        tool = self._tools.get(name)
        if tool is None:
            available = ", ".join(self._tools) or "none"
            return f"Unknown tool: {name}. Available: {available}"
        try:
            return str(tool.handler(dict(args or {}), ctx))
        except Exception as exc:  # noqa: BLE001 - surface every failure to the model
            return f"Error: {type(exc).__name__}: {exc}"
