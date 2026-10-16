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
import uuid
from dataclasses import dataclass
from typing import Callable

from .context import COMPACT_FLAG, ContextCompactor, is_prompt_too_long
from .diagnostics import RunTrace, traced_loop
from .events import POST_TOOL_USE, PRE_TOOL_USE, STOP, Hooks, StopDirective
from .llm import LLMClient, extract_text, make_tool_result, tool_use_blocks
from .tools.registry import ToolContext, ToolRegistry
from .tools.feedback import CommandFeedback
from .tools.result import ToolResult
from .tools.recovery import ToolRecovery
from .ui_events import UIEvent

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
    recovery: ToolRecovery | None = None,
) -> ToolResult:
    """PreToolUse -> handler -> PostToolUse.

    Every tool call in the system goes through here, which is why permission
    enforcement and auditing only ever have to be written once.
    """
    # An approved path-escape is good for exactly one call.  Clearing it here
    # means an approval can never leak into the next tool call.
    recovery = recovery if recovery is not None else ToolRecovery() #准备恢复管理器，取出工具名和参数
    attempts_start = len(recovery.events)
    name, args = block.get("name", ""), block.get("input", {})
    if not isinstance(name, str):
        name = "<invalid-tool-name>"  # 如果模型传来的工具名不是字符串，就先替换成一个安全的占位名称，避免后续查询注册表时发生类型错误。
    trace = ctx.extra.get("diagnostics")
    tool = registry.get(name)
    trace_name = name if tool else "<unknown>"
    call_started = time.monotonic()
    call_id = trace.tool_started(trace_name, args, ctx) if isinstance(trace, RunTrace) else ""
    key = recovery.key(name, args, ctx) # 创建唯一key

    def finish(output: ToolResult) -> ToolResult:
        if isinstance(trace, RunTrace):
            trace.tool_finished(trace_name, args, output, ctx,
                                read_only=bool(tool and tool.read_only),
                                attempts=recovery.events[attempts_start:], call_id=call_id,
                                elapsed=time.monotonic() - call_started)
        return output

    ctx.extra.pop("allow_outside", None) # 清除旧的临时权限
    # 实际上检查了两类情况 同一个工具，目标的修正次数已经耗尽或者这个文件上一次修改结果不确定，需要先检查状态
    exhausted = recovery.exhausted(key) # 检查当前调用是否已经被恢复规则拦住
    invalid = exhausted if exhausted is not None else registry.validate(name, args)
    if invalid is not None:
        recovery.record(name, args, invalid, phase="validation", attempt=0)
        return finish(recovery.observe(key, invalid))
    for attempt in range(1, recovery.max_retries + 2):
        # Re-check permission for EVERY attempt; never reuse an escape approval.
        ctx.extra.pop("allow_outside", None)
        blocked = hooks.trigger(PRE_TOOL_USE, block, ctx)
        if blocked is not None:
            output = blocked if isinstance(blocked, ToolResult) else ToolResult.failure(
                "POLICY_BLOCKED", str(blocked))
            recovery.record(name, args, output, phase="permission", attempt=0)
            return finish(output)
        started = time.monotonic()
        try:
            output = registry.dispatch(name, args, ctx)
        finally:
            ctx.extra.pop("allow_outside", None)
        hooks.trigger(POST_TOOL_USE, block, output, ctx)
        retry = recovery.can_retry(registry.get(name), output, attempt)
        recovery.record(name, args, output, phase="execution", attempt=attempt,
                        elapsed=time.monotonic() - started, retry=retry)
        if not retry: # 不再重试有三种原因，已经成功  失败，但不适合自动重试  失败，而且重试次数已经用完
            return finish(recovery.observe(key, output))
        recovery.wait(attempt)
    raise AssertionError("tool retry budget was not enforced")


@traced_loop
def run_loop(
    *,
    llm: LLMClient,
    registry: ToolRegistry,
    messages: list[dict],
    system: str | Callable[[], str],
    ctx: ToolContext,
    hooks: Hooks | None = None,
    max_turns: int = 150,
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
    command_note = ""
    command_feedback = CommandFeedback()
    result = LoopResult() # 创建默认结果对象
    recovery = ToolRecovery()
    ctx.extra["tool_attempts"] = recovery.events
    cancel_event = ctx.extra.get("cancel_event")

    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def cancel_result() -> LoopResult:
        result.stop_reason = "cancelled"
        result.error = "Stopped by user; already completed tool effects are retained."
        result.text = result.error
        ctx.extra.pop(COMPACT_FLAG, None)
        return result

    def ui(kind: str, call_id: str = "", text: str = "", **data) -> None:
        sink = ctx.extra.get("ui_events")
        if sink:
            sink(UIEvent(kind, ctx.owner, call_id, text, data))

    def emit(text: str) -> None:
        if on_event and text:
            on_event(text)

    def progress(text: str) -> None:
        if on_progress:
            on_progress(text)

    for turn in range(1, max(1, max_turns) + 1):
        if cancelled():
            return cancel_result()
        result.turns = turn
        trace = ctx.extra.get("diagnostics")
        if isinstance(trace, RunTrace):
            trace.round_started(turn, messages)
        progress(f"[model] round {turn}: preparing context")

        # -- layer 0: anything the harness owes the model right now ---------
        if before_call is not None:
            before_call(messages)  # 每轮先补充新消息

        # -- layer 1: make room before asking -------------------------------
        if compactor is not None:
            messages[:] = compactor.prepare(messages, active_request) # 正常的预防性压缩
        if isinstance(trace, RunTrace):
            trace.record("context_prepared", message_count=len(messages),
                         context_chars=ContextCompactor.estimate_chars(messages))

        system_prompt = system() if callable(system) else system # 因此 system prompt 能够随运行状态动态更新。工具
        if recovery_note:
            system_prompt += "\n\n" + recovery_note
        if command_note:
            system_prompt += "\n\n" + command_note

        # -- re-read the tool pool on EVERY iteration ----------------------
        # Not an optimisation detail: `connect_mcp` adds tools mid-turn, so a
        # pool computed once up front would let the model *execute* a tool it
        # was never *offered*.  Re-reading here is what makes the pool dynamic.
        tools = registry.definitions()

        # -- the model call, with one reactive retry ------------------------
        progress(f"[model] round {turn}: waiting for response")
        model_call_id = uuid.uuid4().hex
        ui("model_start", model_call_id, round=turn)
        try:
            stream_args = {}
            if ctx.extra.get("ui_events") and getattr(llm, "supports_text_stream", False):
                stream_args = {"on_text": lambda text: ui("text_delta", model_call_id, text),
                               "should_cancel": cancelled}
            response = llm.create(
                system=system_prompt,
                messages=messages,
                tools=tools,
                max_tokens=ctx.settings.max_tokens,
                **stream_args,
            )
            reactive_retries = 0
        except Exception as exc:  # noqa: BLE001
            ui("model_failed", model_call_id, type(exc).__name__, cancelled=cancelled())
            if cancelled():
                return cancel_result()
            if (
                compactor is not None      # 有压缩器
                and is_prompt_too_long(exc)  # 上下文太长
                and reactive_retries < MAX_REACTIVE_RETRIES # 尚未重试过
            ):
                emit("[reactive compact]")
                if isinstance(trace, RunTrace):
                    trace.record("recovery", kind="context_overflow")
                messages[:] = compactor.reactive_compact(messages, active_request) # 强制压缩
                reactive_retries += 1
                continue
            result.stop_reason = "error"
            result.error = f"{type(exc).__name__}: {exc}"
            progress(f"[model] round {turn}: request failed")
            return result

        ui("model_end", model_call_id, extract_text(response.content), stop_reason=response.stop_reason,
           tool_calls=len(tool_use_blocks(response.content)))
        if cancelled():
            return cancel_result()
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
            if incomplete_retries >= MAX_INCOMPLETE_RETRIES:
                result.stop_reason = "error"
                result.error = (
                    "Model output reached max_tokens during a tool call; the tool was not executed. "
                    "Automatic recovery exhausted. Increase AGENT_MAX_TOKENS or request smaller steps, then retry."
                )
                progress("[model] stopped: truncated tool call recovery exhausted")
                return result
            incomplete_retries += 1
            if isinstance(trace, RunTrace):
                trace.record("recovery", kind="truncated_tool_call", attempt=incomplete_retries)
            recovery_note = (
                "[harness recovery] The previous response reached the output limit while generating "
                "tool calls. None of that response's tool calls were executed; earlier completed "
                "tool calls remain valid. Regenerate the next step as ONE small, complete tool call. "
                "Keep reasoning short, split large file writes or edits into smaller chunks, and "
                "do not assume any discarded tool call succeeded."
            )
            progress(
                f"[model] tool call cut off by output limit; not executed; "
                f"retry {incomplete_retries}/{MAX_INCOMPLETE_RETRIES} with a smaller step"
            )
            continue

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
            if isinstance(trace, RunTrace):
                trace.record("recovery", kind="incomplete_response", attempt=incomplete_retries)
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
        command_notes = []
        for block in calls:
            if cancelled():
                skipped = ToolResult.failure("CANCELLED", "Not executed: user requested stop.")
                results.append(make_tool_result(block.get("id", ""), skipped.model_content(), is_error=True))
                continue
            name = block.get("name", "")
            # Tool names originate in the model response.  Keep terminal
            # progress free of control characters and unbounded labels.
            label = "".join(char for char in str(name) if char.isprintable())[:80] or "(unnamed)"
            progress(f"[tool] {label}: running")
            started = time.monotonic()
            ui_call_id = uuid.uuid4().hex
            ui("tool_start", ui_call_id, label, arguments=block.get("input", {}), workdir=str(ctx.workdir))
            output = execute_tool(block, registry, ctx, hook_registry, recovery)
            ui("tool_end", ui_call_id, str(output)[:8000], ok=output.ok,
               error_code=output.error_code, elapsed=time.monotonic() - started, result_chars=len(output))
            tool = registry.get(name) if isinstance(name, str) else None
            advice = command_feedback.observe(name, block.get("input", {}), output, tool)
            if advice:
                command_notes.append(advice)
                if isinstance(trace, RunTrace):
                    trace.record("command_feedback", failure_streak=command_feedback.streak)
            progress(f"[tool] {label}: returned in {time.monotonic() - started:.1f}s")
            result.tool_calls += 1
            preview_note = " (showing first 160 chars)" if len(output) > 160 else ""
            emit(f"{name}: [{len(output)} chars{preview_note}]\n{output[:160]}")
            results.append(make_tool_result(block.get("id", ""), output.model_content(),
                                            is_error=not output.ok))
        messages.append({"role": "user", "content": results})
        # One bounded host-authored note for the next model request. Do not add
        # conversational turns, replay commands, or force completion.
        command_note = max(command_notes, key=len, default="")
        if cancelled():
            return cancel_result()

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
