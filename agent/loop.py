"""The agent loop.  Everything else in this package is built around it.

    User --> messages[] --> LLM --> response
                                      |
                              contains tool_use block?
                           /                          \\
                         yes                           no
                          |                             |
                    execute tools                    return text
                    append results
                    loop back -----------------> messages[]

The loop does not know what a tool does, where the model lives, or what
compaction is.  It is handed those things.  That is the whole reason the
harness can grow to eighteen mechanisms without the loop ever changing shape.

Three layers wrap the loop from the outside, in this order:

    1. context compaction   before each call, make room if needed
    2. hooks                PreToolUse can veto, PostToolUse can observe,
                            Stop can force another turn
    3. reactive compaction  if the provider rejects the request anyway,
                            summarize and retry exactly once

The lead agent, every subagent, and every teammate run *this* function.  They
differ only in the arguments they pass.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .context import COMPACT_FLAG, ContextCompactor, is_prompt_too_long
from .events import POST_TOOL_USE, PRE_TOOL_USE, STOP, Hooks, StopDirective
from .llm import LLMClient, extract_text, make_tool_result, tool_use_blocks
from .tools.registry import ToolContext, ToolRegistry

MAX_REACTIVE_RETRIES = 1
MAX_INCOMPLETE_RETRIES = 2

# 循环最终返回什么
@dataclass
class LoopResult:
    text: str = "" # 模型最后说的文本
    turns: int = 0 # 跑了几轮  统计诊断
    stop_reason: str = "final"      # final | max_turns | error | goal_* # 怎么结束的
    error: str = ""   # 错误详情
    tool_calls: int = 0 # 工具调用总次数

    @property
    def ok(self) -> bool:
        return self.stop_reason == "final"


def execute_tool(
    block: dict, #
    registry: ToolRegistry,# 工具池
    ctx: ToolContext, #本次调用的环境
    hooks: Hooks, # 钩子注册表
) -> str:
    """PreToolUse -> handler -> PostToolUse.

    Every tool call in the system goes through here, which is why permission
    enforcement and auditing only ever have to be written once.
    """
    # An approved path-escape is good for exactly one call.  Clearing it here
    # means an approval can never leak into the next tool call.
    ctx.extra.pop("allow_outside", None) # 即便上一次工具调用被允许访问工作目录外的路径，该授权也只能使用一次
    # 这样授权不会泄漏到后续任意工具调用，属于权限隔离。
    blocked = hooks.trigger(PRE_TOOL_USE, block, ctx)
    if blocked:
        return str(blocked)

    output = registry.dispatch(block.get("name", ""), block.get("input", {}), ctx)
    hooks.trigger(POST_TOOL_USE, block, output, ctx)
    return output


def run_loop(
    *,
    llm: LLMClient,
    registry: ToolRegistry,
    messages: list[dict],
    system: str | Callable[[], str],
    ctx: ToolContext,
    hooks: Hooks | None = None,
    max_turns: int = 50,
    compactor: ContextCompactor | None = None,
    active_request: str = "",
    before_call: Callable[[list[dict]], None] | None = None,
    on_event: Callable[[str], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_diagnostic: Callable[[str], None] | None = None,
) -> LoopResult:
    """Drive the model until it stops asking for tools.

    `messages` is mutated in place so the caller keeps the full conversation.
    `before_call` runs at the top of every iteration and may append to
    `messages` -- that is how team mailbox events reach the model mid-turn.
    `on_progress` carries payload-free, human-facing status.  It is separate
    from `on_event`, whose tool-result events are used by teammate scheduling.
    """
    hook_registry = hooks or Hooks() # hook_registry = hooks or Hooks()
    reactive_retries = 0  # 应急压缩重试次数从 0 开始；
    incomplete_retries = 0
    recovery_note = ""
    result = LoopResult() # 创建默认结果对象

    def emit(text: str) -> None:
        if on_event and text:
            on_event(text)

    def progress(text: str) -> None:
        if on_progress:
            on_progress(text)

    for turn in range(1, max(1, max_turns) + 1):
        result.turns = turn
        progress(f"[model] round {turn}: preparing context")

        # -- layer 0: anything the harness owes the model right now ---------
        if before_call is not None:
            before_call(messages)  # 每轮先补充新消息

        # -- layer 1: make room before asking -------------------------------
        if compactor is not None:
            messages[:] = compactor.prepare(messages, active_request) # 正常的预防性压缩

        system_prompt = system() if callable(system) else system # 因此 system prompt 能够随运行状态动态更新。工具
        if recovery_note:
            system_prompt += "\n\n" + recovery_note

        # -- re-read the tool pool on EVERY iteration ----------------------
        # Not an optimisation detail: `connect_mcp` adds tools mid-turn, so a
        # pool computed once up front would let the model *execute* a tool it
        # was never *offered*.  Re-reading here is what makes the pool dynamic.
        tools = registry.definitions()

        # -- the model call, with one reactive retry ------------------------
        progress(f"[model] round {turn}: waiting for response")
        try:
            response = llm.create(
                system=system_prompt,
                messages=messages,
                tools=tools,
                max_tokens=ctx.settings.max_tokens,
            )
            reactive_retries = 0
        except Exception as exc:  # noqa: BLE001
            if (
                compactor is not None      # 有压缩器
                and is_prompt_too_long(exc)  # 上下文太长
                and reactive_retries < MAX_REACTIVE_RETRIES # 尚未重试过
            ):
                emit("[reactive compact]")
                messages[:] = compactor.reactive_compact(messages, active_request) # 强制压缩
                reactive_retries += 1
                continue
            result.stop_reason = "error"
            result.error = f"{type(exc).__name__}: {exc}"
            progress(f"[model] round {turn}: request failed")
            return result

        calls = tool_use_blocks(response.content) # 获取工具的调用的字典列表
        final_text = extract_text(response.content).strip()
        provider_stop = response.stop_reason or "unknown"
        block_types = ",".join(str(block.get("type", "unknown")) for block in response.content) or "none"
        if on_diagnostic:
            on_diagnostic(
                f"[response] stop_reason={provider_stop}; blocks={block_types}; "
                f"text_chars={len(final_text)}; tool_calls={len(calls)}"
            )

        # An output-limit response can contain an unfinished tool argument.
        # Never execute it or append an unmatched tool_use to the history.
        if calls and provider_stop == "max_tokens":
            result.stop_reason = "error"
            result.error = (
                "Model output reached max_tokens during a tool call; the tool was not executed. "
                "Increase AGENT_MAX_TOKENS or request smaller steps, then retry."
            )
            progress("[model] stopped: tool call was cut off by the output limit")
            return result

        # Empty / thinking-only and truncated text are not final answers.
        # Retry within both a small recovery budget and the overall turn cap.
        if not calls and (not final_text or provider_stop == "max_tokens"):
            detail = "output reached max_tokens" if provider_stop == "max_tokens" else "no answer text or tool call"
            if incomplete_retries >= MAX_INCOMPLETE_RETRIES:
                result.stop_reason = "error"
                result.error = (
                    f"Model repeatedly returned an incomplete response ({detail}; stop_reason={provider_stop}). "
                    "The task was not confirmed complete. If max_tokens is reported, "
                    "increase AGENT_MAX_TOKENS or request smaller steps."
                )
                progress("[model] stopped: incomplete response recovery exhausted")
                return result
            incomplete_retries += 1
            recovery_note = (
                "[harness recovery] The previous response was incomplete. Continue the user's task "
                "with a concrete tool call or a concise answer. Keep reasoning and each step short; "
                "do not claim completion without evidence."
            )
            if final_text:
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": "Your response was cut off. Continue from where it stopped."})
            # Discard empty/thinking-only turns rather than sending empty
            # assistant content back to the provider. Retain earlier work.
            progress(f"[model] incomplete response ({detail}); retry {incomplete_retries}/{MAX_INCOMPLETE_RETRIES}")
            continue

        incomplete_retries = 0
        recovery_note = ""
        messages.append({"role": "assistant", "content": response.content})

        # -- no tool call: the model wants to stop --------------------------
        if not calls: # 准备结束
            progress(f"[model] round {turn}: processing final response")
            forced = hook_registry.trigger(STOP, messages)
            if isinstance(forced, StopDirective):
                if forced.action == "continue":
                    messages.append({"role": "user", "content": forced.message})
                    continue
                if forced.action == "return":
                    result.text = extract_text(response.content).strip()
                    result.stop_reason = forced.stop_reason
                    result.error = forced.message
                    return result
                raise ValueError(f"Unknown Stop directive: {forced.action}")
            if forced:
                messages.append({"role": "user", "content": str(forced)})
                continue
            result.text = extract_text(response.content).strip()
            result.stop_reason = "final"
            return result

        # -- execute the batch, collect results -----------------------------
        results = []
        for block in calls:
            name = block.get("name", "")
            # Tool names originate in the model response.  Keep terminal
            # progress free of control characters and unbounded labels.
            label = "".join(char for char in str(name) if char.isprintable())[:80] or "(unnamed)"
            progress(f"[tool] {label}: running")
            started = time.monotonic()
            output = execute_tool(block, registry, ctx, hook_registry)
            progress(f"[tool] {label}: returned in {time.monotonic() - started:.1f}s")
            result.tool_calls += 1
            preview_note = " (showing first 160 chars)" if len(output) > 160 else ""
            emit(f"{name}: [{len(output)} chars{preview_note}]\n{output[:160]}")
            results.append(make_tool_result(block.get("id", ""), output))
        messages.append({"role": "user", "content": results})

        # -- the model asked for room: compact now that the batch is safe ---
        # Deferred on purpose.  A `write_file` in this same batch must be
        # recorded in the history before that history is discarded.
        if ctx.extra.pop(COMPACT_FLAG, False) and compactor is not None and compactor.enabled:
            messages[:] = compactor.compact_history(messages, active_request)
            emit("[compact] history replaced with a summary")

    result.stop_reason = "max_turns"
    result.error = f"stopped after {max_turns} turns without a final answer"
    result.text = result.error
    progress("[model] stopped: maximum turns reached")
    return result
