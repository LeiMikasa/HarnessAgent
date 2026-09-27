"""Subagent -- a subtask gets fresh `messages[]`; its final text comes back.

    Parent agent                    Subagent
    +------------------+            +------------------+
    | messages=[...]   |            | messages=[prompt]|
    | tool: task       |  ------->  | own agent loop   |
    |                  |            | base tools only  |
    | tool_result      |  <-------  | final text       |
    +------------------+            +------------------+

Why it matters: exploration is expensive in context.  A subagent that reads
forty files and discards them costs the parent one paragraph instead of forty
tool results.

The subagent's tool pool deliberately excludes `task`, so it cannot delegate
again -- that rule is what makes recursion bounded and cost predictable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .events import Hooks
from .llm import LLMClient
from .loop import run_loop
from .tools.registry import ToolContext, ToolRegistry

SUBAGENT_SYSTEM_TEMPLATE = (
    "You are a coding agent at {workdir}. "
    "Complete the given task, then return a concise final answer. "
    "Do not ask clarifying questions; make reasonable assumptions and proceed."
)

#: Tools a subagent may never have, no matter what the parent has.
FORBIDDEN_SUBAGENT_TOOLS = ("task", "spawn_teammate", "connect_mcp") # 禁止子agent使用的工具

# 构造子agent工具池
def build_subagent_registry(parent: ToolRegistry, *, allow: tuple[str, ...] | None = None) -> ToolRegistry:
    """A copy of the parent's pool minus anything recursive or team-related."""
    registry = ToolRegistry()
    for name, tool in parent.tools().items():
        if name in FORBIDDEN_SUBAGENT_TOOLS:
            continue
        if allow is not None and name not in allow:
            continue
        registry.register(tool)
    return registry

# 真正运行
def run_subagent_loop(
    *,
    llm: LLMClient,
    registry: ToolRegistry,
    prompt: str,
    ctx: ToolContext,
    max_turns: int = 30,
    hooks: Hooks | None = None,
    system: str | None = None,
    on_event: Callable[[str], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    """Run one nested agent loop and return its final text.

    The message list is created here and thrown away when the call returns:
    that isolation *is* the feature.  No compactor is passed -- a subagent is
    short-lived by construction.
    """
    workdir = Path(ctx.workdir) # 工作目录
    system_prompt = system or SUBAGENT_SYSTEM_TEMPLATE.format(workdir=workdir) # 系统提示词

    messages: list[dict] = [{"role": "user", "content": prompt}]

    def emit(text: str) -> None:
        if on_event:
            on_event(f"[subagent] {text}")

    def progress(text: str) -> None:
        if on_progress:
            on_progress(f"[subagent] {text}")

    emit(f"start: {prompt[:80]}")

    result = run_loop(
        llm=llm,
        registry=registry,
        messages=messages,
        system=system_prompt,
        ctx=ctx,
        hooks=hooks,
        max_turns=max_turns,
        on_event=emit,
        on_progress=progress,
    )

    emit("done" if result.ok else f"stopped: {result.stop_reason}")
    if result.ok:
        return result.text or "(subagent produced no summary)"
    return f"Subagent failed: {result.error}"


# --------------------------------------------------------------------------
# Tool
# --------------------------------------------------------------------------

TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "minLength": 1,
            "description": (
                "A self-contained task description. The subagent sees none of "
                "this conversation, so include every needed detail."
            ),
        }
    },
    "required": ["prompt"],
}

TASK_DESCRIPTION = (
    "Run a subagent with a fresh conversation and return only its final text. "
    "Use it for focused exploration, or for any self-contained subtask whose "
    "intermediate steps would clutter this conversation. The subagent cannot "
    "delegate further."
)


def run_task(args: dict, ctx: ToolContext) -> str:
    prompt = args.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip():
        return "Error: prompt is required"

    runtime = ctx.runtime
    if runtime is None or not hasattr(runtime, "spawn_subagent"):
        return "Error: subagents are not available in this context"

    return runtime.spawn_subagent(prompt.strip(), ctx=ctx)


def register_subagent_tools(registry: ToolRegistry) -> ToolRegistry:
    registry.add("task", TASK_DESCRIPTION, TASK_SCHEMA, run_task)
    return registry
