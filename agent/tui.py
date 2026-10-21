"""A terminal presentation layer; agent work runs off the UI thread."""

from __future__ import annotations

import queue
import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.message import Message
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Collapsible, Footer, Markdown, Static, TextArea

from .runtime import Runtime
from .ui_events import UIEvent
from .permissions import MODE_LABELS, is_host_approval


def plain(value) -> str:
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(value))


@dataclass
class ApprovalRequest:
    tool: str
    arguments: str
    reason: str
    owner: str
    workdir: str = ""
    ready: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    allowed: bool = False

    def resolve(self, allowed: bool) -> None:
        with self.lock:
            if not self.ready.is_set():
                self.allowed = allowed
                self.ready.set()


class ApprovalScreen(ModalScreen[bool]):
    BINDINGS = [Binding("y", "allow", "允许"), Binding("n", "deny", "拒绝"), Binding("escape", "deny", "拒绝")]

    def __init__(self, request: ApprovalRequest):
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-box"):
            heading = "申请本次完全权限 · 宿主机执行" if is_host_approval(self.request.reason) else "需要你的确认"
            yield Static(heading, id="approval-heading")
            yield Static(f"调用者：{plain(self.request.owner)}\n工具：{plain(self.request.tool)}", markup=False)
            if self.request.workdir:
                yield Static(f"目录：{plain(self.request.workdir)}", markup=False)
            yield Static(plain(self.request.reason), markup=False)
            with VerticalScroll(id="approval-preview"):
                yield Static(plain(self.request.arguments), markup=False)
            with Horizontal(id="approval-buttons"):
                yield Button("拒绝 · N", id="deny")
                yield Button("允许本次 · Y", id="allow", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#deny", Button).focus()

    def action_allow(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "allow")


class Prompt(TextArea):
    BINDINGS = [
        Binding("enter", "submit", priority=True),
        Binding("shift+enter", "newline", priority=True),
        Binding("alt+enter", "newline", priority=True),
        Binding("ctrl+j", "newline", priority=True),
        Binding("ctrl+p", "previous", priority=True),
        Binding("ctrl+n", "next", priority=True),
    ]

    class Submitted(Message):
        def __init__(self, text: str):
            super().__init__()
            self.text = text

    def action_submit(self) -> None:
        if self.text.strip():
            self.post_message(self.Submitted(self.text.strip()))

    def action_newline(self) -> None:
        self.insert("\n")

    def action_previous(self) -> None:
        self.app.move_history(-1)

    def action_next(self) -> None:
        self.app.move_history(1)


class MessagePane(Vertical):
    def __init__(self, heading: str, text: str = "", *, classes: str = ""):
        super().__init__(classes="message " + classes)
        self.heading = Static(plain(heading), classes="message-heading", markup=False)
        self.body = Markdown(plain(text))
        self.text = text

    def compose(self) -> ComposeResult:
        yield self.heading
        yield self.body


class HarnessApp(App[int]):
    TITLE = "minicode"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+c", "stop", "停止", priority=True),
                Binding("ctrl+q", "request_quit", "退出", priority=True),
                Binding("ctrl+l", "focus_prompt", "输入", priority=True),
                Binding("ctrl+t", "toggle_theme", "主题", priority=True)]
    CSS_PATH = "tui.tcss"

    def __init__(self, runtime: Runtime, *, initial_request: str = "", show_details: bool = False):
        super().__init__()
        self.register_theme(Theme(
            name="harness-dark", primary="#d8b485", secondary="#a4afa7", accent="#91c9b2",
            foreground="#e4e8e1", background="#141816", surface="#1c221e", panel="#252d27",
            success="#91c9b2", warning="#d8b485", error="#e4a09a", dark=True,
            variables={"border": "#3b493f", "border-blurred": "#2b352e",
                       "footer-background": "#141816", "footer-key-foreground": "#91c9b2",
                       "input-cursor-background": "#91c9b2", "input-cursor-foreground": "#141816"},
        ))
        self.register_theme(Theme(
            name="harness-light", primary="#805e37", secondary="#52665b", accent="#2c7056",
            foreground="#28352d", background="#f5f3ed", surface="#eceee6", panel="#e0e5da",
            success="#2c7056", warning="#805e37", error="#ac433d", dark=False,
            variables={"border": "#a6b5a6", "border-blurred": "#d0d7ca",
                       "footer-background": "#f5f3ed", "footer-key-foreground": "#2c7056",
                       "input-cursor-background": "#2c7056", "input-cursor-foreground": "#ffffff"},
        ))
        self.theme = "harness-dark"
        self.runtime = runtime
        self.initial_request, self.show_details = initial_request, show_details
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.local = threading.local()
        self.cancel = threading.Event()
        self.shutdown = threading.Event()
        self.busy = False
        self.closing = False
        self.draining = False
        self.history: list[str] = []
        self.history_index = 0
        self.draft = ""
        self.model_panes: dict[str, MessagePane] = {}
        self.dirty: set[str] = set()
        self.tools: dict[str, tuple[Collapsible, Static, str]] = {}
        self.tool_arguments: dict[str, str] = {}
        self.pending: deque[ApprovalRequest] = deque()
        self.current_approval: ApprovalRequest | None = None
        self.last_final = ""
        self.worker: threading.Thread | None = None
        self.started_at: float | None = None
        # Snapshot of the last prepared lead request, not cumulative API usage.
        self.context_usage: dict | None = None

    def compose(self) -> ComposeResult:
        yield Static("窗口过小\n请调整到至少 60 列 × 18 行\nCtrl+Q 退出", id="minimum-size", markup=False)
        with Vertical(id="top"):
            with Horizontal(id="header-line"):
                yield Static("minicode  /  coding agent", id="brand", markup=False)
                yield Static(Text(plain(self.runtime.settings.model), no_wrap=True, overflow="ellipsis"), id="model")
                yield Static("● 就绪", id="activity", markup=False)
            yield Static(Text("工作区  " + plain(self.runtime.settings.workdir), no_wrap=True, overflow="ellipsis"), id="workspace")
        with Horizontal(id="body"):
            with Vertical(id="main"):
                yield VerticalScroll(id="chat")
                yield Static("上下文估计 · 等待首轮请求", id="context-compact", markup=False)
                yield Static("就绪 · 输入任务即可开始", id="status", markup=False)
                with Vertical(id="input-area"):
                    yield Prompt(id="prompt", placeholder="输入一个明确的任务…", highlight_cursor_line=False)
                    yield Static("Enter 发送   Ctrl+J 换行   Ctrl+P/N 历史   :help 命令", id="hint", markup=False)
            with Vertical(id="sidebar"):
                with VerticalScroll(id="sidebar-details"):
                    yield Static("会话累计", id="sidebar-title", classes="section-title")
                    yield Static("", id="session", markup=False)
                    yield Static("执行环境", classes="section-title")
                    yield Static("", id="features", markup=False)
                    yield Static("后台队友", classes="section-title")
                    yield Static("", id="team", markup=False)
                yield Static("上下文估计 · token\n等待首轮请求", id="context-usage", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.runtime.on_ui_event = self.receive_event
        self.runtime.on_progress = None
        self.runtime.on_event = None
        self.runtime.permissions.approver = self.approve
        self.set_interval(0.05, self.drain_events)
        self.set_interval(0.5, self.update_sidebar)
        self.query_one("#prompt", Prompt).focus()
        self.query_one("#prompt", Prompt).border_title = "任务"
        context_tooltip = (
            "最近一次主 Agent 请求的输入估计，包含系统提示词、历史消息和工具定义，已加 20% 余量。"
            "预算是压缩触发阈值，不是模型容量或累计账单；每次模型请求前更新。"
            "提示词包含系统规则、计划、技能目录和注入的记忆；工具定义是名称、说明和参数格式；"
            "对话消息包含用户、助手、思考和工具调用，工具结果块单独统计，JSON 结构也计入。"
        )
        self.query_one("#context-usage").tooltip = context_tooltip
        self.query_one("#context-compact").tooltip = context_tooltip
        # Follow layout growth, including delayed Markdown rendering. Textual
        # releases this anchor when the user scrolls and restores it at bottom.
        self.query_one("#chat", VerticalScroll).anchor()
        self.update_sidebar()
        self.on_resize()
        await self.add_message(
            "准备就绪",
            "### 从一个明确的任务开始。\n\n"
            "告诉我目标、相关文件和验收条件。\n"
            "我会执行工具、展示过程，并在需要时请求你的确认。\n\n"
            "**试试**  检查项目并修复失败的测试\n\n"
            "`:tools` 查看工具  ·  `:todos` 当前计划  ·  `:trace` 运行记录",
            "welcome",
        )
        if self.initial_request:
            await self.submit(self.initial_request)

    def on_resize(self, event: Resize | None = None) -> None:
        if self.is_mounted and self.query("#sidebar"):
            # The resize message can arrive before App.size has been updated.
            size = event.size if event is not None else self.size
            self.query_one("#sidebar").display = size.width >= 100
            screen = self.query_one("#top").screen
            screen.set_class(size.width < 100, "compact")
            screen.set_class(size.height < 30, "short")
            screen.set_class(size.width < 60 or size.height < 18, "too-small")

    def receive_event(self, event: UIEvent) -> None:
        self.local.owner = event.owner
        if "workdir" in event.data:
            self.local.workdir = event.data["workdir"]
        if not self.shutdown.is_set():
            self.events.put(event)

    def approve(self, tool: str, args: dict, reason: str) -> bool:
        if self.shutdown.is_set() or self.cancel.is_set():
            return False
        if self.runtime.permissions.approval == "allow" and not is_host_approval(reason):
            return True
        if self.runtime.permissions.approval == "deny":
            return False
        request = ApprovalRequest(tool, json.dumps(args, ensure_ascii=False, indent=2, default=str), reason,
                                  getattr(self.local, "owner", "lead"),
                                  getattr(self.local, "workdir", str(self.runtime.settings.workdir)))
        self.events.put(request)
        while not request.ready.wait(0.1):
            if self.shutdown.is_set() or self.cancel.is_set():
                request.resolve(False)
        return request.allowed

    async def add_message(self, heading: str, text: str = "", classes: str = "") -> MessagePane:
        pane = MessagePane(heading, text, classes=classes)
        await self.query_one("#chat", VerticalScroll).mount(pane)
        return pane

    async def on_prompt_submitted(self, event: Prompt.Submitted) -> None:
        await self.submit(event.text)

    async def submit(self, text: str) -> None:
        if self.busy:
            self.query_one("#status", Static).update("当前任务尚未结束；Ctrl+C 请求停止后再发送")
            return
        if text.strip().lower() in (":quit", ":q", ":exit", "quit", "exit", "q"):
            self.action_request_quit()
            return
        if text.strip() == ":clear":
            self.runtime.messages.clear()
            self.runtime.goal.clear()
            await self.query_one("#chat", VerticalScroll).remove_children()
            self.model_panes.clear()
            self.tools.clear()
            self.tool_arguments.clear()
            self.context_usage = None
            self.update_context_usage()
            self.query_one("#prompt", Prompt).clear()
            self.query_one("#chat", VerticalScroll).scroll_end(animate=False)
            return
        for welcome in self.query(".welcome"):
            await welcome.remove()
        self.history.append(text)
        self.history_index = len(self.history)
        self.query_one("#prompt", Prompt).clear()
        await self.add_message("你", text, "user")
        # Sending a new request explicitly resumes following, even after the
        # user scrolled up. Deferred scrolling waits for the new message layout.
        self.query_one("#chat", VerticalScroll).scroll_end(animate=False)
        self.busy, self.last_final = True, ""
        self.started_at = time.monotonic()
        self.cancel = threading.Event()
        self.runtime.cancel_event = self.cancel
        self.query_one("#status", Static).update("正在准备任务…")
        self.worker = threading.Thread(target=self.run_request, args=(text,), daemon=True, name="harness-ui-request")
        self.worker.start()

    def run_request(self, text: str) -> None:
        failed = False
        try:
            if text.startswith(":") and not text.startswith(":goal"):
                answer = self.read_command(text)
            else:
                answer = self.runtime.submit(text)
                if not self.cancel.is_set():
                    answer = self.runtime.wait_for_teammates(initial_answer=answer)
        except Exception as error:
            failed = True
            answer = f"{type(error).__name__}: {error}"
        finally:
            if self.cancel.is_set() and self.runtime.teams:
                self.runtime.teams.stop_all()
        self.events.put(UIEvent("turn_done", text=answer, data={"cancelled": self.cancel.is_set(), "failed": failed}))

    def read_command(self, text: str) -> str:
        name = text.strip().lower()
        runtime = self.runtime
        commands = {
            ":help": lambda: ":status 状态 · :tools 工具 · :todos 计划 · :tasks 任务 · :team 队友\n\n"
                              ":skills 技能 · :memory 记忆 · :mcp 外部服务 · :trace 诊断\n\n"
                              ":goal <条件> 设置目标 · :goal clear 清除目标 · :clear 清空对话 · :quit 退出",
            ":status": runtime.status,
            ":tools": lambda: "\n".join(f"{tool.name}: {tool.description}" for tool in runtime.tools),
            ":todos": runtime.todos.render,
            ":tasks": runtime.tasks.render_list,
            ":team": runtime.summon_report,
            ":skills": runtime.skills.catalog,
            ":mcp": lambda: ", ".join(runtime.mcp.names()) or "暂无连接",
            ":memory": lambda: "\n".join(f"{r['name']}: {r['description']}" for r in runtime.memory.list_records()) or "暂无记忆",
            ":notes": lambda: "\n".join(runtime.notes) or "暂无记录",
            ":trace": lambda: runtime.diagnostics.report() if runtime.diagnostics else "诊断已关闭",
        }
        return commands[name]() if name in commands else "未知命令，请输入 :help"

    async def drain_events(self) -> None:
        if self.shutdown.is_set() or not self.query("#chat"):
            return
        chat = self.query_one("#chat", VerticalScroll)
        for _ in range(256):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if isinstance(event, ApprovalRequest):
                self.pending.append(event)
                continue
            if event.kind == "context_usage":
                if event.owner == "lead":
                    self.context_usage = dict(event.data)
                    self.update_context_usage()
            elif event.kind == "model_start":
                self.query_one("#status", Static).update(f"{plain(event.owner)} · 第 {event.data['round']} 轮 · 等待模型输出…")
            elif event.kind in ("text_delta", "model_end"):
                pane = self.model_panes.get(event.call_id)
                if event.text and pane is None:
                    pane = await self.add_message("Agent" if event.owner == "lead" else event.owner)
                    self.model_panes[event.call_id] = pane
                if pane:
                    pane.text = pane.text + event.text if event.kind == "text_delta" else event.text
                    self.dirty.add(event.call_id)
                    if event.kind == "model_end" and event.data.get("stop_reason") == "max_tokens":
                        pane.heading.update(f"{plain(event.owner)} · 输出截断，尚未确认完成")
                if event.kind == "model_end" and event.owner == "lead" and not event.data.get("tool_calls"):
                    self.last_final = event.text
            elif event.kind == "model_failed":
                pane = self.model_panes.get(event.call_id)
                if pane:
                    pane.heading.update(f"{plain(event.owner)} · 输出未完成")
                    pane.add_class("notice")
            elif event.kind == "tool_start":
                args = event.data.get("arguments", {})
                summary = next((str(args[field]) for field in ("path", "command", "to", "task_id") if isinstance(args, dict) and field in args), "")
                owner = "" if event.owner == "lead" else f"{event.owner} / "
                title = owner + event.text + (f" · {summary[:100]}" if summary else "")
                body = Static(plain(repr(args)[:3000]), markup=False)
                card = Collapsible(body, title=escape("… " + plain(title)), collapsed=not self.show_details, classes="tool running")
                await chat.mount(card)
                self.tools[event.call_id] = card, body, title
                self.tool_arguments[event.call_id] = plain(repr(args)[:3000])
                self.query_one("#status", Static).update(f"{plain(event.owner)} · 正在执行 {plain(event.text)}")
            elif event.kind == "tool_end" and event.call_id in self.tools:
                card, body, title = self.tools[event.call_id]
                ok = event.data["ok"]
                card.title = escape(f"{'✓' if ok else '×'} {plain(title)} · {event.data['elapsed']:.1f}s")
                card.remove_class("running")
                detail = self.tool_arguments[event.call_id] + "\n\n输出：\n" + plain(event.text)
                if event.data.get("result_chars", len(event.text)) > len(event.text):
                    detail += f"\n…界面预览已截断；工具实际返回 {event.data['result_chars']} 字符。"
                body.update(detail)
                if not ok:
                    card.add_class("failed")
                    card.collapsed = False
            elif event.kind == "turn_done":
                if event.text and event.text != self.last_final:
                    await self.add_message("已停止" if event.data.get("cancelled") else "结果", event.text, "notice")
                self.draining = bool(event.data.get("cancelled") and self.teammates_alive())
                self.busy = self.draining
                self.query_one("#status", Static).update("正在等待后台队友停止…" if self.draining else "已停止" if event.data.get("cancelled") else "就绪 · 可以继续输入任务")
                if self.closing and not self.busy:
                    self.exit(0)
        for call_id in tuple(self.dirty):
            pane = self.model_panes[call_id]
            await pane.body.update(plain(pane.text))
        self.dirty.clear()
        self.show_next_approval()
        if self.draining and not self.teammates_alive():
            self.draining = self.busy = False
            self.query_one("#status", Static).update("已停止 · 可以继续输入")
            if self.closing:
                self.exit(0)

    def show_next_approval(self) -> None:
        if self.current_approval:
            return
        while self.pending:
            request = self.pending.popleft()
            if request.ready.is_set():
                continue
            self.current_approval = request

            def decided(allowed: bool) -> None:
                request.resolve(allowed and not self.cancel.is_set())
                self.current_approval = None

            self.push_screen(ApprovalScreen(request), decided)
            break

    def teammates_alive(self) -> bool:
        return bool(self.runtime.teams and any(t.alive for t in list(self.runtime.teams.teammates.values())))

    def update_context_usage(self) -> None:
        widget = self.query_one("#context-usage", Static)
        compact_widget = self.query_one("#context-compact", Static)
        for item in (widget, compact_widget):
            item.remove_class("near-budget", "over-budget")
        if self.context_usage is None:
            widget.update("上下文估计 · token\n等待首轮请求")
            compact_widget.update("上下文估计 · 等待首轮请求")
            return
        tokens = self.context_usage["estimated_input_tokens"]
        limit = self.context_usage.get("input_token_limit")
        text = Text("上下文估计 · token\n最近请求")
        if limit is None:
            text.append(f"\n已用  ≈{tokens:,}", style="bold")
            text.append("\n自动压缩已关闭")
            compact = Text(f"上下文估计 ≈{tokens:,} token · 自动压缩已关闭")
        else:
            remaining = max(0, limit - tokens)
            text.append(f" · 占用 {tokens / limit:.1%}")
            text.append(f"\n已用  ≈{tokens:,}", style="bold")
            text.append(f"\n预算   {limit:,}\n剩余  ≈{remaining:,}")
            compact = Text(f"上下文估计 ≈{tokens:,}/{limit:,} · 剩余 ≈{remaining:,} · {tokens / limit:.1%}")
            if tokens > limit:
                text.append(f"\n超出预算 {tokens - limit:,}")
                compact.append(f" · 超出 {tokens - limit:,}")
            for item in (widget, compact_widget):
                item.set_class(tokens >= limit * 0.9, "near-budget")
                item.set_class(tokens > limit, "over-budget")
        breakdown = self.context_usage.get("estimated_input_token_breakdown")
        if breakdown is not None:
            for key, label in (("system_prompt", "提示词"), ("tool_definitions", "工具定义"),
                               ("tool_results", "工具结果"), ("conversation", "对话消息"),
                               ("request_structure", "请求结构")):
                text.append(f"\n{label}  ≈{breakdown[key]:,}")
        widget.update(text)
        compact_widget.update(compact)

    def update_sidebar(self) -> None:
        if self.shutdown.is_set() or not self.query("#session"):
            return
        runtime = self.runtime
        metrics = []
        if runtime.diagnostics:
            summary = runtime.diagnostics.summary()
            counts = summary["counts"]
            tokens = summary["token_totals"]
            metrics = [("模型调用", f"{counts.get('model_calls', 0):,}"),
                       ("工具调用", f"{counts.get('tool_calls', 0):,}"),
                       ("工具错误", f"{counts.get('tool_errors', 0):,}"), ("", ""),
                       ("输入 token", f"{tokens['input_tokens']:,}" if tokens['input_tokens'] is not None else "未知"),
                       ("输出 token", f"{tokens['output_tokens']:,}" if tokens['output_tokens'] is not None else "未知")]
        else:
            metrics = [("诊断记录", "已关闭")]
        if self.busy and self.started_at is not None:
            metrics += [("", ""), ("本次耗时", f"{time.monotonic() - self.started_at:.0f} 秒")]
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column()
        table.add_column(justify="right", style="bold")
        for label, value in metrics:
            table.add_row(label, value)
        self.query_one("#session", Static).update(table)
        approval = {"ask": "询问", "allow": "允许", "deny": "拒绝"}.get(runtime.permissions.approval, runtime.permissions.approval)
        features = (f"工具环境  {'Docker' if runtime.sandbox_enabled else '本机'}\n"
                    f"权限模式  {MODE_LABELS.get(runtime.permission_mode, '旧版接口')}\n"
                    f"权限策略  {approval}\n团队协作  {'开启' if runtime.teams else '关闭'}\n"
                    f"长期记忆  {'开启' if runtime.memory.enabled else '关闭'}")
        self.query_one("#features", Static).update(features)
        roster = runtime.teams.roster() if runtime.teams else "未开启\n--teams 可启用协作"
        self.query_one("#team", Static).update(plain(roster))
        activity = self.query_one("#activity", Static)
        activity.update("◌ 执行中" if self.busy else "● 就绪")
        activity.set_class(self.busy, "working")
        self.query_one("#status").set_class(self.busy, "working")
        self.query_one("#sidebar").display = self.size.width >= 100

    def move_history(self, direction: int) -> None:
        prompt = self.query_one("#prompt", Prompt)
        if self.history_index == len(self.history):
            self.draft = prompt.text
        self.history_index = max(0, min(len(self.history), self.history_index + direction))
        prompt.load_text(self.history[self.history_index] if self.history_index < len(self.history) else self.draft)
        prompt.move_cursor(prompt.document.end)

    def action_focus_prompt(self) -> None:
        self.query_one("#prompt", Prompt).focus()

    def action_toggle_theme(self) -> None:
        self.theme = "harness-light" if self.theme == "harness-dark" else "harness-dark"

    def action_stop(self) -> None:
        if not self.busy and not self.teammates_alive():
            self.query_one("#prompt", Prompt).clear()
            return
        if not self.busy:
            self.draining = self.busy = True
        self.cancel.set()
        for request in list(self.pending) + ([self.current_approval] if self.current_approval else []):
            request.resolve(False)
        if self.current_approval and isinstance(self.screen, ApprovalScreen):
            self.screen.dismiss(False)
        if self.runtime.teams:
            for teammate in list(self.runtime.teams.teammates.values()):
                teammate.stop()
        self.query_one("#status", Static).update("正在停止；等待当前网络或命令操作返回…")

    def action_request_quit(self) -> None:
        if self.busy or self.teammates_alive():
            self.closing = True
            self.action_stop()
        else:
            self.exit(0)

    def on_unmount(self) -> None:
        self.shutdown.set()
        self.cancel.set()
        for request in list(self.pending) + ([self.current_approval] if self.current_approval else []):
            request.resolve(False)


def run_tui(runtime: Runtime, *, initial_request: str = "", show_details: bool = False) -> int:
    return HarnessApp(runtime, initial_request=initial_request, show_details=show_details).run() or 0
