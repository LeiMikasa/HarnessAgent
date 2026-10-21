"""Payload-free execution traces, separate from the agent's conversation.

One Runtime owns a trace. Context variables keep nested loops and teammate
threads separate; a lock serializes their JSONL writes and counters. Tracing
never decides whether a tool should execute.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from .llm import extract_text


TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
)
COORDINATION_TOOLS = {
    "todo_write", "create_task", "update_task", "get_task", "list_tasks", "claim_task",
    "complete_task", "spawn_teammate", "list_teammates", "send_message", "request_plan",
    "submit_plan", "review_plan", "pending_requests", "request_shutdown", "create_worktree",
    "remove_worktree", "list_worktrees",
}
MEMORY_KINDS = {
    "You select relevant memory records. Reply with JSON only.": "memory_selection",
    "You extract durable memory records from a conversation.": "memory_extraction",
    "You consolidate a memory store. Reply with JSON only.": "memory_consolidation",
}


def fingerprint(value) -> str:
    try:
        text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


@dataclass
class LoopTrace:
    owner: str
    loop_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    parent_loop_id: str | None = None
    round: int = 0
    finished: bool = False
    arguments_seen: set = field(default_factory=set)
    read_results_seen: set = field(default_factory=set)


class RunTrace:
    def __init__(self, root: Path, *, provider: str, model: str):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
        self.directory = Path(root) / self.run_id
        self.events_path = self.directory / "events.jsonl"
        self.summary_path = self.directory / "summary.json"
        self.provider, self.model = provider, model
        self.error = ""
        self._lock = threading.RLock()
        self._current: ContextVar = ContextVar(f"trace-{self.run_id}", default=None)
        self._counts: Counter = Counter()
        self._model_kinds: Counter = Counter()
        self._tools: Counter = Counter()
        self._errors: Counter = Counter()
        self._stops: Counter = Counter()
        self._tokens: Counter = Counter()
        self._token_calls: Counter = Counter()
        self._epochs: Counter = Counter()
        self._owners: dict[str, Counter] = {}
        self._sequence = 0

    def context(self) -> dict:
        loop = self._current.get()
        return ({"loop_id": loop.loop_id, "parent_loop_id": loop.parent_loop_id,
                 "owner": loop.owner, "round": loop.round} if loop else {})

    def record(self, event: str, **fields) -> None:
        with self._lock:
            self._sequence += 1
            payload = {"event": event, "sequence": self._sequence,
                       "timestamp": datetime.now(timezone.utc).isoformat(),
                       **self.context(), **fields}
            owner_counts = self._owners.setdefault(payload.get("owner", "outside_loop"), Counter())
            metric = {"round_start": "loop_rounds", "model_start": "model_calls",
                      "tool_start": "tool_calls"}.get(event)
            if metric:
                owner_counts[metric] += 1
            if event == "round_start":
                self._counts["loop_rounds"] += 1
            elif event == "loop_start":
                self._counts["loops_started"] += 1
            elif event == "loop_end":
                self._counts["loops_finished"] += 1
                self._stops[fields["stop_reason"]] += 1
            elif event == "model_start":
                self._counts["model_calls"] += 1
                self._model_kinds[fields["kind"]] += 1
            elif event == "model_end":
                self._counts["model_calls_finished"] += 1
                self._counts["model_errors"] += not fields["ok"]
                for name, value in fields.get("usage", {}).items():
                    self._tokens[name] += value
                    self._token_calls[name] += 1
            elif event == "tool_start":
                self._counts["tool_calls"] += 1
                self._tools[fields["tool"]] += 1
                self._counts["coordination_calls"] += fields["tool"] in COORDINATION_TOOLS
            elif event == "tool_end":
                self._counts["tool_calls_finished"] += 1
                self._counts["tool_errors"] += not fields["ok"]
                self._counts["same_arguments_repeats"] += fields["same_arguments_repeat"]
                self._counts["same_read_result_repeats"] += fields["same_read_result_repeat"]
                if not fields["ok"]:
                    self._errors[fields["error_code"] or "unknown"] += 1
            elif event == "tool_attempt":
                self._counts["execution_attempts"] += fields["phase"] == "execution"
                self._counts["auto_retry_events"] += fields["auto_retry"]
            elif event == "recovery":
                self._counts["recovery_events"] += 1
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                with self.events_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            except OSError as exc:
                self.error = f"Trace write failed: {type(exc).__name__}"

    @contextmanager
    def loop(self, owner: str):
        parent = self._current.get()
        loop = LoopTrace(owner=owner, parent_loop_id=parent.loop_id if parent else None)
        token = self._current.set(loop)
        self.record("loop_start")
        try:
            yield loop
        finally:
            if not loop.finished:
                self.record("loop_end", stop_reason="interrupted")
            self._current.reset(token)
            self.flush()

    def round_started(self, number: int, messages: list) -> None:
        loop = self._current.get()
        if loop:
            loop.round = number
        self.record("round_start", message_count=len(messages))

    def tool_started(self, name, args, ctx) -> str:
        call_id = uuid.uuid4().hex
        self.record("tool_start", tool=name, owner=ctx.owner, tool_call_id=call_id,
                    arguments_sha256=fingerprint(args))
        return call_id

    def tool_finished(self, name, args, result, ctx, *, read_only: bool,
                      attempts: list, call_id: str, elapsed: float) -> None:
        loop = self._current.get()
        arguments = fingerprint(args)
        output = fingerprint(str(result))
        try:
            scope = str(Path(ctx.workdir).resolve())
        except (OSError, RuntimeError, ValueError):
            scope = str(ctx.workdir)
        with self._lock:
            repeat_args = repeat_result = False
            # Include mutations initiated outside an instrumented loop too.
            if not read_only:
                self._epochs[scope] += 1
            if loop:
                key = (scope, name, arguments)
                repeat_args = key in loop.arguments_seen
                loop.arguments_seen.add(key)
                # Any potentially mutating call invalidates repeat observations
                # for this workspace, including failed calls with unknown effects.
                if read_only and result.ok:
                    observation = (key, output, self._epochs[scope])
                    repeat_result = observation in loop.read_results_seen
                    loop.read_results_seen.add(observation)
            for attempt in attempts:
                self.record("tool_attempt", **{**attempt, "tool": name, "owner": ctx.owner,
                                               "tool_call_id": call_id})
            self.record("tool_end", tool=name, owner=ctx.owner, arguments_sha256=arguments,
                        tool_call_id=call_id, elapsed_seconds=round(elapsed, 6),
                        result_sha256=output, result_chars=len(result), read_only=read_only,
                        ok=result.ok, error_code=result.error_code,
                        execution_status=result.execution_status,
                        same_arguments_repeat=repeat_args, same_read_result_repeat=repeat_result)

    def summary(self) -> dict:
        with self._lock:
            calls = self._counts["model_calls"]
            tokens = {name: self._tokens[name] if calls and self._token_calls[name] == calls else None
                      for name in TOKEN_FIELDS}
            return {"version": 1, "run_id": self.run_id, "provider": self.provider,
                    "model": self.model, "counts": dict(self._counts),
                    "by_owner": {owner: dict(counts) for owner, counts in self._owners.items()},
                    "model_calls_by_kind": dict(self._model_kinds),
                    "tools_by_name": dict(self._tools), "tool_errors_by_code": dict(self._errors),
                    "loop_stop_reasons": dict(self._stops), "token_totals": tokens,
                    "token_usage_complete": tokens["input_tokens"] is not None and tokens["output_tokens"] is not None,
                    "token_field_missing_calls": {name: calls - self._token_calls[name] for name in TOKEN_FIELDS},
                    "trace_error": self.error}

    def flush(self) -> None:
        with self._lock:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                temporary = self.directory / "summary.tmp"
                temporary.write_text(json.dumps(self.summary(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temporary.replace(self.summary_path)
            except OSError as exc:
                self.error = f"Trace write failed: {type(exc).__name__}"

    def report(self) -> str:
        summary = self.summary()
        return (json.dumps(summary, ensure_ascii=False, indent=2)
                + f"\nEvents: {self.events_path}\nSummary: {self.summary_path}")


class ObservedLLM:
    """Count calls without retaining prompts, answers, thinking, or raw errors."""

    def __init__(self, client, trace: RunTrace, kind: str):
        self.client, self.trace, self.kind = client, trace, kind

    def __getattr__(self, name):
        return getattr(self.client, name)

    def create(self, **kwargs):
        kind = MEMORY_KINDS.get(kwargs.get("system"), self.kind) if self.kind == "memory" else self.kind
        context = self.trace.context()
        call_id = uuid.uuid4().hex
        self.trace.record("model_start", **context, call_id=call_id, kind=kind,
                          provider=getattr(self.client, "provider", "unknown"),
                          model=getattr(self.client, "model", "unknown"),
                          message_count=len(kwargs.get("messages", [])))
        started = time.monotonic()
        try:
            response = self.client.create(**kwargs)
        except Exception as exc:
            self.trace.record("model_end", **context, call_id=call_id, kind=kind, ok=False,
                              error_type=type(exc).__name__, usage={},
                              elapsed_seconds=round(time.monotonic() - started, 6))
            raise
        raw_usage = getattr(response, "usage", None)
        usage = {name: value for name in TOKEN_FIELDS
                 if isinstance(raw_usage, dict)
                 and type(value := raw_usage.get(name)) is int and value >= 0}
        stop = getattr(response, "stop_reason", None)
        if stop not in {"end_turn", "tool_use", "max_tokens", "stop_sequence", "pause_turn", "refusal"}:
            stop = "unknown"
        self.trace.record("model_end", **context, call_id=call_id, kind=kind, ok=True,
                          stop_reason=stop, usage=usage,
                          text_chars=len(extract_text(response.content)),
                          elapsed_seconds=round(time.monotonic() - started, 6))
        return response


def traced_loop(function):
    @wraps(function)
    def run(*args, **kwargs):
        ctx = kwargs["ctx"]
        trace = ctx.extra.get("diagnostics")
        if not isinstance(trace, RunTrace):
            return function(*args, **kwargs)
        with trace.loop(ctx.owner) as loop:
            kwargs["llm"] = ObservedLLM(kwargs["llm"], trace, "agent")
            result = function(*args, **kwargs)
            trace.record("loop_end", stop_reason=result.stop_reason, turns=result.turns,
                         tool_calls=result.tool_calls)
            loop.finished = True
            return result
    return run
