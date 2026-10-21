"""Threaded runtime adapter, approval bridge and local conversation storage."""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..config import Settings
from ..llm import build_client
from ..permissions import PERMISSION_MODES, is_host_approval
from ..runtime import Runtime
from ..ui_events import UIEvent


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def demo_response(index, _messages, _tools):
    """Exercise real events and a harmless tool, without any model request."""
    steps = [
        {"text": "这是免费 Mock 演示。我会展示流式回答、任务清单和一次目录查询。",
         "tool": "todo_write", "input": {"todos": [
             {"content": "验证会话与流式事件", "status": "completed"},
             {"content": "列出工作区顶层文件", "status": "in_progress"},
         ]}},
        {"tool": "glob", "input": {"pattern": "*"}},
        {"tool": "todo_write", "input": {"todos": [
            {"content": "验证会话与流式事件", "status": "completed"},
            {"content": "列出工作区顶层文件", "status": "completed"},
        ]}},
        "演示流程已结束。这是预设的 Mock 响应，仅用于验证界面与工具链路，"
        "没有调用真实模型，也不代表已完成你输入的开发任务。切换到已配置的模型后可执行实际任务。",
    ]
    return steps[index % len(steps)]


def make_runtime(**kwargs) -> Runtime:
    if kwargs["settings"].is_mock:
        kwargs["responder"] = demo_response
    return Runtime.create(**kwargs)


def make_model_client(settings):
    return build_client(settings, responder=demo_response if settings.is_mock else None)


@dataclass
class PendingApproval:
    id: str
    tool: str
    arguments: dict
    reason: str
    owner: str
    workdir: str
    host_execution: bool
    ready: threading.Event = field(default_factory=threading.Event)
    allowed: bool = False

    def public(self):
        return {key: getattr(self, key) for key in (
            "id", "tool", "arguments", "reason", "owner", "workdir", "host_execution",
        )}


class WebSession:
    def __init__(self, manager, metadata: dict, saved: dict | None = None):
        self.manager = manager
        self.metadata = metadata
        self.lock = threading.RLock()
        self.runtime: Runtime | None = None
        self.worker: threading.Thread | None = None
        self.cancel = threading.Event()
        self.closing = False
        self.busy = False
        self.seq = 0
        self.events = deque(maxlen=2048)
        self.pending: dict[str, PendingApproval] = {}
        self.pending_settings: dict = {}
        self.pending_client = None
        saved = saved or {}
        self.timeline = saved.get("timeline", [])[-500:]
        for entry in self.timeline:
            if entry.get("status") == "running":
                entry["status"] = "interrupted"
                if entry.get("thinking_status") == "streaming":
                    entry["thinking_status"] = "interrupted"
        self.messages = saved.get("messages", [])
        self.todos = saved.get("todos", [])
        self.context_usage = saved.get("context_usage")
        self.metrics = saved.get("metrics", {})
        self.previous_metrics = copy.deepcopy(self.metrics)
        self.activity = "就绪"
        self.last_error = ""
        self.local = threading.local()
        self.last_paint: dict[str, float] = {}

    @property
    def directory(self):
        return self.manager.directory / self.metadata["id"]

    def _publish(self, kind: str, data: dict):
        self.seq += 1
        self.events.append({"seq": self.seq, "kind": kind, "data": copy.deepcopy(data)})

    def _state(self):
        return {
            "metadata": dict(self.metadata), "busy": self.busy,
            "cancel_requested": self.cancel.is_set(), "activity": self.activity,
            "pending_approvals": [item.public() for item in self.pending.values()],
            "pending_settings": dict(self.pending_settings),
            "context_usage": self.context_usage, "metrics": self.metrics,
            "todos": self.todos, "last_error": self.last_error,
        }

    def snapshot(self):
        with self.lock:
            return copy.deepcopy({**self._state(), "timeline": self.timeline, "seq": self.seq})

    def events_after(self, cursor: int):
        with self.lock:
            if self.events and cursor < self.events[0]["seq"] - 1:
                return [{"seq": self.seq, "kind": "snapshot", "data": self.snapshot()}]
            return [copy.deepcopy(event) for event in self.events if event["seq"] > cursor]

    def _entry(self, entry: dict, *, publish=True):
        existing = next((item for item in self.timeline if item["id"] == entry["id"]), None)
        if existing is None:
            self.timeline.append(entry)
            self.timeline = self.timeline[-500:]
        else:
            existing.update(entry)
            entry = existing
        if publish:
            self._publish("entry", entry)
        return entry

    def _refresh_metrics(self):
        if self.runtime is not None:
            self.todos = [asdict(item) for item in self.runtime.todos.items]
            if self.runtime.diagnostics:
                summary = self.runtime.diagnostics.summary()
                counts = dict(summary["counts"])
                for key, value in self.previous_metrics.get("counts", {}).items():
                    counts[key] = counts.get(key, 0) + value
                tokens = dict(summary["token_totals"])
                if self.previous_metrics.get("counts", {}).get("model_calls", 0):
                    for key, value in self.previous_metrics.get("token_totals", {}).items():
                        tokens[key] = (value + tokens[key] if value is not None and tokens.get(key) is not None else None)
                self.metrics = {"counts": counts, "token_totals": tokens}

    def receive(self, event: UIEvent):
        self.local.owner = event.owner
        if "workdir" in event.data:
            self.local.workdir = event.data["workdir"]
        with self.lock:
            if event.kind == "context_usage" and event.owner == "lead":
                self.context_usage = dict(event.data)
            elif event.kind == "model_start":
                self.activity = f"{event.owner} · 第 {event.data['round']} 轮 · 等待模型"
            elif event.kind in ("text_delta", "thinking_delta", "model_end"):
                identity = "model-" + event.call_id
                previous = next((item for item in self.timeline if item["id"] == identity), None)
                previous = previous or {}
                text = previous.get("text", "")
                thinking = previous.get("thinking", "")
                thinking_status = previous.get("thinking_status", "done")
                if event.kind == "text_delta":
                    text += event.text
                    thinking_status = "done"
                    if previous.get("thinking_status") == "streaming":
                        self.activity = f"{event.owner} · 正在输出"
                        self._publish("state", self._state())
                elif event.kind == "thinking_delta":
                    thinking += event.text
                    thinking_status = "streaming"
                    self.activity = f"{event.owner} · 正在思考"
                    if previous.get("thinking_status") != "streaming":
                        self._publish("state", self._state())
                else:
                    text = event.text
                    thinking = event.data.get("thinking", "")
                    thinking_status = "interrupted" if event.data.get("stop_reason") == "max_tokens" else "done"
                if text or thinking or previous:
                    timestamp = time.monotonic()
                    paint = event.kind == "model_end" or timestamp - self.last_paint.get(identity, 0) > 0.06
                    self._entry({"id": identity, "kind": "message", "role": "assistant",
                                 "owner": event.owner, "text": text[:200_000],
                                 "thinking": thinking[:200_000], "thinking_status": thinking_status,
                                 "status": ("interrupted" if event.data.get("stop_reason") == "max_tokens"
                                            else "done" if event.kind == "model_end" else "running")}, publish=paint)
                    if paint:
                        self.last_paint[identity] = timestamp
            elif event.kind == "model_failed":
                identity = "model-" + event.call_id
                if any(item["id"] == identity for item in self.timeline):
                    self._entry({"id": identity, "status": "interrupted", "thinking_status": "interrupted"})
            elif event.kind == "tool_start":
                self.activity = f"{event.owner} · 正在执行 {event.text}"
                self._entry({"id": "tool-" + event.call_id, "kind": "tool", "name": event.text,
                             "owner": event.owner, "arguments": event.data.get("arguments", {}),
                             "status": "running", "text": ""})
            elif event.kind == "tool_end":
                self._entry({"id": "tool-" + event.call_id,
                             "status": "done" if event.data["ok"] else "failed",
                             "text": event.text, "elapsed": event.data.get("elapsed", 0),
                             "result_chars": event.data.get("result_chars", len(event.text))})
            if event.kind not in ("text_delta", "thinking_delta"):
                self._refresh_metrics()
                self._publish("state", self._state())

    def _runtime(self):
        with self.lock:
            return self._runtime_locked()

    def _runtime_locked(self):
        if self.runtime is None:
            settings = replace(
                self.manager.settings, workdir=Path(self.metadata["workdir"]),
                state_dir=self.directory / "runtime",
                skill_dirs=[Path(self.metadata["workdir"]) / "skills"],
                provider=self.metadata["provider"], model=self.metadata["model"],
            )
            self.runtime = self.manager.runtime_factory(
                settings=settings, permission_mode=self.metadata["permission_mode"],
                approval="ask", approver=self.approve, teams_enabled=False, memory_enabled=False,
                on_ui_event=self.receive,
            )
            self.runtime.messages = self.messages
            self.runtime.lead_ctx.extra["apply_controls"] = self.apply_settings
            if self.todos:
                self.runtime.todos.replace(self.todos)
        return self.runtime

    def update_settings(self, *, permission_mode=None, provider=None, model=None, confirm_full=False):
        """HTTP threads enqueue changes; only the execution thread mutates a busy runtime."""
        with self.lock:
            if self.closing:
                raise RuntimeError("会话正在关闭")
            requested = dict(self.pending_settings)
            if permission_mode is not None:
                if permission_mode not in PERMISSION_MODES:
                    raise ValueError("无效的权限模式")
                if permission_mode == "full" and self.metadata["permission_mode"] != "full" and not confirm_full:
                    raise ValueError("切换到完全权限需要明确确认")
                requested["permission_mode"] = permission_mode
            client = self.pending_client
            if provider is not None or model is not None:
                provider = provider or requested.get("provider", self.metadata["provider"])
                model = (model or "").strip()
                if provider not in {"mock", self.manager.settings.provider}:
                    raise ValueError("只能使用当前已配置的模型服务或免费 Mock")
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}", model):
                    raise ValueError("请填写有效的模型 ID（最多 200 个字符）")
                if provider != "mock" and not self.manager.settings.api_key:
                    raise ValueError("当前模型服务尚未配置密钥")
                requested.update(provider=provider, model=model)
                # Constructing an SDK client sends no request and cannot spend tokens.
                client = self.manager.client_factory(replace(self.manager.settings, provider=provider, model=model))
            self.pending_settings = {key: value for key, value in requested.items()
                                     if value != self.metadata[key]}
            self.pending_client = client if {"model", "provider"} & self.pending_settings.keys() else None
            if "permission_mode" in self.pending_settings:
                # A permission answer is valid only for the policy under which it was asked.
                for request in self.pending.values():
                    request.allowed = False
                    request.ready.set()
            if not self.busy:
                self.apply_settings()
            self._publish("state", self._state())
            return self.snapshot()

    def apply_settings(self):
        """Called between model/tool calls (or while idle), including within one tool batch."""
        with self.lock:
            if not self.pending_settings:
                return
            changes = dict(self.pending_settings)
            try:
                if self.runtime:
                    if "permission_mode" in changes:
                        self.runtime.change_permission_mode(changes["permission_mode"])
                    if {"model", "provider"} & changes.keys():
                        self.runtime.change_model(
                            self.pending_client,
                            provider=changes.get("provider", self.metadata["provider"]),
                            model=changes.get("model", self.metadata["model"]))
                self.metadata.update(changes, updated_at=now())
                labels = {"read-only": "只读", "workspace": "工作区内修改", "full": "完全权限"}
                detail = []
                if "permission_mode" in changes:
                    detail.append("权限：" + labels[changes["permission_mode"]])
                if {"model", "provider"} & changes.keys():
                    detail.append("模型：" + self.metadata["provider"] + " / " + self.metadata["model"])
                self._entry({"id": uuid.uuid4().hex, "kind": "notice", "text": "设置已生效 · " + "；".join(detail)})
            except Exception as exc:
                self._entry({"id": uuid.uuid4().hex, "kind": "notice", "status": "failed",
                             "text": f"设置未能生效：{type(exc).__name__}: {exc}"})
            finally:
                self.pending_settings = {}
                self.pending_client = None
                self._publish("state", self._state())
                self._save()

    def approve(self, tool: str, args: dict, reason: str) -> bool:
        with self.lock:
            if self.cancel.is_set() or self.closing:
                return False
            request = PendingApproval(
                uuid.uuid4().hex, tool, copy.deepcopy(args), reason,
                getattr(self.local, "owner", "lead"),
                getattr(self.local, "workdir", self.metadata["workdir"]), is_host_approval(reason),
            )
            self.pending[request.id] = request
            self.activity = "等待你的批准"
            self._publish("state", self._state())
            self._save()
        deadline = time.monotonic() + 300
        while not request.ready.wait(0.1):
            if self.cancel.is_set() or self.closing or time.monotonic() > deadline:
                break
        with self.lock:
            allowed = request.allowed and request.ready.is_set() and not self.cancel.is_set() and not self.closing
            self.pending.pop(request.id, None)
            self.activity = "已批准本次操作" if allowed else "本次操作未获批准"
            self._publish("state", self._state())
            return allowed

    def decide(self, request_id: str, allowed: bool):
        with self.lock:
            request = self.pending.get(request_id)
            if request is None or request.ready.is_set() or self.cancel.is_set():
                raise KeyError("申请已过期或已经处理")
            request.allowed = allowed
            request.ready.set()

    def submit(self, text: str):
        with self.lock:
            if self.busy or self.closing:
                raise RuntimeError("当前任务尚未结束，请先停止或等待完成")
            self.busy = True
            self.cancel = threading.Event()
            self.last_error = ""
            self.activity = "正在准备任务"
            self.metadata["updated_at"] = now()
            if not self.messages and not self.timeline:
                self.metadata["title"] = text.strip()[:40]
            self._entry({"id": uuid.uuid4().hex, "kind": "message", "role": "user", "text": text})
            self._publish("state", self._state())
            self._save()
            self.worker = threading.Thread(target=self._run, args=(text,), daemon=True,
                                           name="harness-web-" + self.metadata["id"][:8])
            self.worker.start()

    def _run(self, text: str):
        try:
            runtime = self._runtime()
            runtime.cancel_event = self.cancel
            answer = runtime.submit(text)
            with self.lock:
                last = next((item for item in reversed(self.timeline)
                             if item.get("role") == "assistant"), None)
                if answer and (last is None or last["text"] != answer):
                    self._entry({"id": uuid.uuid4().hex, "kind": "message", "role": "assistant",
                                 "text": answer, "status": "done"})
        except Exception as exc:
            with self.lock:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._entry({"id": uuid.uuid4().hex, "kind": "notice", "text": self.last_error,
                             "status": "failed"})
        finally:
            with self.lock:
                self.apply_settings()
                self._refresh_metrics()
                self.busy = False
                self.activity = "已停止" if self.cancel.is_set() else "执行出错" if self.last_error else "就绪"
                self.metadata["updated_at"] = now()
                self._publish("state", self._state())
                self._save()
                if self.closing and self.runtime:
                    self.runtime.close()

    def stop(self):
        with self.lock:
            if self.busy:
                self.cancel.set()
                self.activity = "正在停止；等待当前模型或工具调用结束"
                for request in self.pending.values():
                    request.allowed = False
                    request.ready.set()
                self._publish("state", self._state())

    def _save(self):
        """Called with lock held. Credentials are never part of this payload."""
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "metadata": self.metadata, "timeline": self.timeline,
                   "messages": self.runtime.messages if self.runtime else self.messages,
                   "todos": self.todos, "context_usage": self.context_usage, "metrics": self.metrics}
        temporary = self.directory / "session.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        temporary.replace(self.directory / "session.json")

    def close(self):
        self.stop()
        with self.lock:
            self.closing = True
            if not self.busy and self.runtime:
                self.runtime.close()


class SessionManager:
    def __init__(self, settings: Settings, *, directory: Path | None = None,
                 runtime_factory: Callable = make_runtime, client_factory: Callable = make_model_client):
        self.settings = settings
        self.directory = directory or settings.state_dir / "web"
        self.runtime_factory = runtime_factory
        self.client_factory = client_factory
        self.lock = threading.RLock()
        self.sessions: dict[str, WebSession] = {}
        if self.directory.exists():
            for file in self.directory.glob("*/session.json"):
                try:
                    saved = json.loads(file.read_text(encoding="utf-8"))
                    metadata = saved["metadata"]
                    identity = metadata["id"]
                    if (len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity)
                            or identity != file.parent.name or metadata["permission_mode"] not in PERMISSION_MODES):
                        continue
                    self.sessions[identity] = WebSession(self, metadata, saved)
                except (OSError, ValueError, KeyError, TypeError):
                    continue

    def create(self, workdir: str, mode: str, *, mock: bool = False):
        root = Path(workdir).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("工作目录不存在，请填写已有项目目录")
        if mode not in PERMISSION_MODES:
            raise ValueError("无效的权限模式")
        if not mock and not self.settings.is_mock and not self.settings.api_key:
            raise ValueError("尚未配置模型密钥，请修改 .env 或选择免费 Mock")
        metadata = {"id": uuid.uuid4().hex, "title": "新会话", "workdir": str(root),
                    "permission_mode": mode, "provider": "mock" if mock else self.settings.provider,
                    "model": "mock-1" if mock else self.settings.model,
                    "created_at": now(), "updated_at": now()}
        with self.lock:
            session = WebSession(self, metadata)
            with session.lock:
                session._save()
            self.sessions[metadata["id"]] = session
            return session

    def get(self, identity: str):
        with self.lock:
            return self.sessions[identity]

    def list(self):
        with self.lock:
            return sorted([dict(session.snapshot()["metadata"], busy=session.busy)
                           for session in self.sessions.values()], key=lambda item: item["updated_at"], reverse=True)

    def close(self):
        with self.lock:
            for session in self.sessions.values():
                session.close()
