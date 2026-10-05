"""Agent teams -- when the work is too big for one agent.

A teammate is not a subagent.  A subagent is a function call: fresh context,
one answer, then it is gone.  A teammate is a *colleague*: it has a name, its
own conversation, its own task claim, and a mailbox it keeps reading.

    Lead agent                          Teammate "researcher"
    +--------------------+              +------------------------+
    | task graph         |  spawn       | own messages[]         |
    | mailbox: lead.jsonl| -----------> | mailbox: researcher... |
    | worktree leases    |              | own tool context       |
    |                    | <----------- | claims ready tasks     |
    +--------------------+   mailbox    +------------------------+
              |                                  |
              +-------- .agent/mailbox/*.jsonl --+
                       .agent/tasks/*.json
                       .agent/worktrees/<name>/  (git worktree per task)

Three mechanisms carry the coordination:

  **Mailbox** -- one JSONL file per agent, append to send, read-and-consume to
  receive.  Simple enough to debug with `type`.

  **Atomic claims** -- exactly one teammate wins a task, because `TaskStore`
  guards the transition under a lock.  Nobody has to negotiate.

  **Task-bound worktrees** -- a teammate editing files enters the worktree its
  claimed task is bound to, so two teammates can edit the same repository
  without stepping on each other.  The lease survives until the turn boundary,
  which is why a failed `complete` never strands a teammate outside its work.

Typed protocols (plan request/response, shutdown request/response) correlate on
a `request_id`, so a late or duplicated reply cannot be mistaken for a fresh
one.

Known boundary: mailbox files use a destructive read with no cross-process
lock, so two *processes* sharing one workspace can lose messages.  Within one
process -- the design here -- the lock is sufficient.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .events import Hooks
from .llm import LLMClient, extract_text
from .tasks import IN_PROGRESS, PENDING, Task, TaskError, TaskStore
from .todo import TodoList
from .tools.registry import Tool, ToolContext, ToolRegistry

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
WORKTREE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

IDLE_SCAN_INTERVAL = 2.0      # 没事做时大约每2秒检查一次
DEFAULT_IDLE_ROUNDS = 15      # 连续空闲超过15次后退出
MAX_TEAMMATE_TURNS = 150      # 每次调用Agent主循环最多跑150轮

# Message types.
MESSAGE = "message"
PLAN_REQUEST = "plan_request"
PLAN_RESPONSE = "plan_response"
PLAN_DECISION = "plan_decision"
SHUTDOWN_REQUEST = "shutdown_request"
SHUTDOWN_RESPONSE = "shutdown_response"

PROTOCOL_TYPES = (PLAN_REQUEST, PLAN_RESPONSE, PLAN_DECISION, SHUTDOWN_REQUEST, SHUTDOWN_RESPONSE)


class TeamError(ValueError):
    """Raised for an invalid team operation."""


def is_valid_agent_name(name: str) -> bool:
    return bool(isinstance(name, str) and AGENT_NAME_PATTERN.fullmatch(name))


#: Names the runtime owns.  A teammate may not take one, because the lead's
#: mailbox and the default task owner are addressed by these strings.
RESERVED_AGENT_NAMES = frozenset({"lead", "agent"})

#: Tools a plan gate actually gates.  Reading stays open on purpose: a
#: teammate must be able to investigate the repository in order to write a
#: plan worth approving.
PLAN_GATED_TOOLS = ("bash", "write_file", "edit_file")


# --------------------------------------------------------------------------
# Mailbox
# --------------------------------------------------------------------------

# 一封信的数据格式
@dataclass
class Message:
    sender: str
    to: str
    content: str
    type: str = MESSAGE
    ts: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=True)

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        return cls(
            sender=str(data.get("from", data.get("sender", "unknown"))),
            to=str(data.get("to", "")),
            content=str(data.get("content", "")),
            type=str(data.get("type", MESSAGE)),
            ts=float(data.get("ts", time.time())),
            metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
        )

# 每个Agent一个邮箱文件
class MessageBus:
    """One append-only JSONL file per agent."""

    def __init__(self, mailbox_dir: Path):
        self.mailbox_dir = Path(mailbox_dir)
        self._lock = threading.RLock() # 保护同意进程内多个线程的读写
        self._condition = threading.Condition(self._lock) #用于通知等待消息的teammate
        self._sizes: dict[str, int] = {}

    def _path(self, agent: str) -> Path:
        if not is_valid_agent_name(agent):
            raise TeamError(f"Invalid agent name: {agent!r}")
        self.mailbox_dir.mkdir(parents=True, exist_ok=True)
        path = (self.mailbox_dir / f"{agent}.jsonl").resolve()
        if not path.is_relative_to(self.mailbox_dir.resolve()):
            raise TeamError(f"Invalid agent name: {agent!r}")
        return path

    def send(self, sender: str, to: str, content: str, *, type: str = MESSAGE, metadata: dict | None = None) -> Message:
        message = Message(sender=sender, to=to, content=str(content), type=type, metadata=dict(metadata or {}))
        with self._condition:
            path = self._path(to)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(message.to_json() + "\n")
            self._sizes[str(path)] = path.stat().st_size
            self._condition.notify_all()
        return message

    def _read(self, agent: str) -> list[Message]:
        path = self._path(agent)
        if not path.is_file():
            return []
        messages: list[Message] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                messages.append(Message.from_dict(data))
        return messages
    # 读取邮箱，但保留文件
    def peek(self, agent: str) -> list[Message]:
        """Read without consuming."""
        with self._lock:
            return self._read(agent)
    # 读取邮箱后删除文件
    def read_inbox(self, agent: str) -> list[Message]:
        """Destructive read: the messages are removed from the mailbox."""
        with self._lock:
            path = self._path(agent)
            messages = self._read(agent)
            if path.is_file():
                try:
                    path.unlink()
                except OSError:
                    pass
            self._sizes[str(path)] = 0
            return messages
    # 等待新消息或超时
    def wait_for_messages(self, agent: str, timeout: float = IDLE_SCAN_INTERVAL) -> list[Message]:
        """Block until a message arrives or the timeout expires."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                path = self._path(agent)
                size = path.stat().st_size if path.is_file() else 0
                # read_inbox removes the file, so any non-empty mailbox is
                # unread. send() updates _sizes before notifying this wait;
                # comparing against that value would delay delivery to timeout.
                if size > 0:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=min(remaining, 0.5))
        return self.read_inbox(agent)

    def has_messages(self, agent: str) -> bool:
        path = self._path(agent)
        return path.is_file() and path.stat().st_size > 0

    def clear(self, agent: str) -> None:
        with self._lock:
            path = self._path(agent)
            if path.is_file():
                try:
                    path.unlink()
                except OSError:
                    pass


# --------------------------------------------------------------------------
# Protocol state
# --------------------------------------------------------------------------


@dataclass
class PendingRequest:   # 一条待处理请求
    request_id: str
    kind: str            # plan | shutdown  plan要求teammate提交计划 shutdown要求teammate停止
    teammate: str        # 这次请求发给谁
    task_id: str | None = None
    work_version: int = 0   # 创建请求时，记录 teammate 当时的工作版本。teammate 换任务时版本会增加，设计意图是避免旧计划继续适用于新任务
    created_at: float = field(default_factory=time.time)
    resolved: bool = False   # lead 是否已经对请求做出最终处理；
    approved: bool | None = None # 批准、拒绝，或尚无决定；
    detail: str = ""  # 审批反馈或处理说明

# 审批请求的状态机，只有邮箱还不够。假设 lead 收到一句“计划已提交”，它还得知道：这是对哪一次请求的回复？是不是已经处理过？发送者是否正确？
@dataclass
class ProtocolState:  # 所有请求的管理者
    """Correlation state for typed request/response exchanges."""

    pending: dict[str, PendingRequest] = field(default_factory=dict) # 可用于请求ID查请求对象
    plan_gates: dict[str, str] = field(default_factory=dict)     # 记录每个 teammate 的计划审批状态：
    plan_request_ids: dict[str, str] = field(default_factory=dict)  # 记录某个 teammate 当前应回复的计划请求 ID：
    work_versions: dict[str, int] = field(default_factory=dict)  # 记录每个 teammate 当前工作的版本：
    counter: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)
    #counter 用于生成顺序请求 ID；lock 保护多个线程同时修改这些字典和计数器。这里用可重入锁 RLock，因为 create() 持锁时还会调用也要持同一把锁的 new_request_id()。
    # 生成请求编号
    def new_request_id(self) -> str:
        with self.lock:
            self.counter += 1
            return f"req_{self.counter:06d}"
    # 创建一个新请求ID
    def create(self, kind: str, teammate: str, task_id: str | None = None) -> PendingRequest:
        with self.lock:
            request = PendingRequest(
                request_id=self.new_request_id(),
                kind=kind,
                teammate=teammate,
                task_id=task_id,
                work_version=self.work_versions.get(teammate, 0),
            )
            self.pending[request.request_id] = request
            if kind == "plan":
                self.plan_gates[teammate] = "pending"
                self.plan_request_ids[teammate] = request.request_id
            return request
    # 收到回复后核对身份
    def match_response(self, message_type: str, request_id: str, sender: str) -> PendingRequest | None:
        """Validate that a reply actually answers an outstanding request.

        Guards: the request exists, its kind matches the message type, the
        sender is the agent that was asked, and it has not already been
        resolved.  Anything else is a stale or forged reply and is ignored.
        """
        # 先由消息类型推算它应答哪类请求
        expected_kind = {PLAN_RESPONSE: "plan", SHUTDOWN_RESPONSE: "shutdown"}.get(message_type)
        if expected_kind is None or not request_id:
            return None
        with self.lock:
            request = self.pending.get(request_id)
            if request is None or request.resolved:
                return None
            if request.kind != expected_kind:
                return None
            if request.teammate != sender:
                return None
            return request
    # lead作出最终决定
    def resolve(self, request: PendingRequest, approved: bool, detail: str = "") -> None:
        with self.lock:
            request.resolved = True
            request.approved = approved
            request.detail = detail
            if request.kind == "plan":
                self.plan_gates[request.teammate] = "approved" if approved else "rejected"
    # 标记工作发生变化
    def bump_work_version(self, teammate: str) -> int:
        """Invalidate outstanding approvals when a teammate's assignment changes."""
        with self.lock:
            self.work_versions[teammate] = self.work_versions.get(teammate, 0) + 1
            return self.work_versions[teammate]
    # 询问当前能否通过计划门
    def plan_approved(self, teammate: str, work_version: int | None = None) -> bool:
        with self.lock:
            if self.plan_gates.get(teammate) == "not_required":
                return True
            if self.plan_gates.get(teammate) != "approved":
                return False
            if work_version is not None and self.work_versions.get(teammate, 0) != work_version:
                # The task changed since the plan was approved.
                return False
            return True
   # 导出状态快照
    def snapshot(self) -> dict:
        with self.lock:
            return {
                "pending": {key: asdict(value) for key, value in self.pending.items()},
                "plan_gates": dict(self.plan_gates),
                "work_versions": dict(self.work_versions),
            }


# --------------------------------------------------------------------------
# Task-bound worktrees
# --------------------------------------------------------------------------

# 校验目录名
def validate_worktree_name(name: str) -> str:
    if not isinstance(name, str) or not WORKTREE_NAME_PATTERN.fullmatch(name):
        raise TeamError(
            "Worktree names must start with a letter or digit, use only "
            "letters, digits, dot, underscore, or hyphen, and be at most 64 characters"
        )
    if ".." in name:
        raise TeamError("Worktree names may not contain '..'")
    return name


@dataclass   # 一条运行时租约    表示当前进程中谁正在使用哪个目录
class Assignment:
    teammate: str
    task_id: str
    cwd: Path
    worktree: str | None = None


class WorktreeManager:
    """Creates and tracks git worktrees bound to tasks."""

    def __init__(
        self,
        workspace: Path,        # 主仓库工作目录
        worktrees_dir: Path,    # 放其他worktree的目录
        *,
        tasks: "TaskStore | None" = None, # 共享任务看版，用于把worktree写进task文件
        enabled: bool = True, # 是否启用worktree功能
    ):
        self.workspace = Path(workspace).resolve()
        self.worktrees_dir = Path(worktrees_dir)
        self.tasks = tasks
        self.enabled = enabled
        self.assignments: dict[str, Assignment] = {}
        self._lock = threading.RLock()
    # 持久绑定
    def bound_tasks(self, name: str) -> list[Task]:
        """Tasks whose durable binding names this worktree."""
        if self.tasks is None:
            return []
        return [task for task in self.tasks.list_all() if task.worktree == name]

    def bind(self, task_id: str, worktree: str | None) -> None:
        """Record the binding on the task itself, so it survives a restart."""
        if self.tasks is None:
            return
        task = self.tasks.try_load(task_id)
        if task is None or task.worktree == worktree:
            return
        task.worktree = worktree
        self.tasks.save(task)

    # -- git plumbing -------------------------------------------------------
    # 统一执行git命令
    def _run_git(self, args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(cwd or self.workspace),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"{type(exc).__name__}: {exc}"
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output

    def is_git_repo(self) -> bool:
        ok, _ = self._run_git(["rev-parse", "--git-dir"])
        return ok

    def _path(self, name: str) -> Path:
        validate_worktree_name(name)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        path = (self.worktrees_dir / name).resolve()
        if not path.is_relative_to(self.worktrees_dir.resolve()):
            raise TeamError(f"Invalid worktree name: {name}")
        return path

    def _branch(self, name: str) -> str:
        return f"wt/{validate_worktree_name(name)}"

    def registered(self) -> dict[str, dict[str, str]]:
        ok, output = self._run_git(["worktree", "list", "--porcelain"])
        if not ok:
            return {}
        entries: dict[str, dict[str, str]] = {}
        current: dict[str, str] = {}
        for line in output.splitlines():
            if not line.strip():
                if current.get("worktree"):
                    entries[Path(current["worktree"]).name] = current
                    current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        if current.get("worktree"):
            entries[Path(current["worktree"]).name] = current
        return entries

    def is_registered(self, name: str) -> bool:
        try:
            return name in self.registered()
        except TeamError:
            return False

    # -- creation and removal ----------------------------------------------
    # 真正创建worktree
    def create(self, name: str, task_id: str) -> str:
        if not self.enabled:
            return "Error: worktrees are disabled for this session"
        try:
            name = validate_worktree_name(name)
        except TeamError as exc:
            return f"Error: {exc}"

        if not self.is_git_repo():
            return (
                "Error: the workspace is not a git repository, so no worktree "
                "could be created. Run 'git init' and commit once first."
            )

        path = self._path(name)
        if self.is_registered(name):
            return f"Worktree {name!r} already exists at {path}"
        if path.exists():
            return f"Error: {path} already exists but is not a registered worktree"

        branch = self._branch(name)
        ok, output = self._run_git(["worktree", "add", "-b", branch, str(path), "HEAD"])
        if not ok:
            return f"Error: git worktree add failed: {output}"
        self.bind(task_id, name)
        return f"Created worktree {name!r} at {path} (branch {branch})"
    # 移除worktree
    def remove(self, name: str, *, discard_changes: bool = False) -> str:
        """Host-only.  Refuses while the worktree is bound to unfinished work."""
        try:
            name = validate_worktree_name(name)
        except TeamError as exc:
            return f"Error: {exc}"

        with self._lock:
            bound = [a for a in self.assignments.values() if a.worktree == name]
            if bound:
                return (
                    f"Error: worktree {name!r} is still assigned to "
                    f"{', '.join(a.teammate for a in bound)}; release it first"
                )

        # A worktree still bound to unfinished work must not be torn down,
        # even with discard_changes -- the task would lose its checkout.
        unfinished = [
            task.id for task in self.bound_tasks(name) if task.status != COMPLETED
        ]
        if unfinished:
            return (
                f"Error: worktree {name!r} is bound to unfinished "
                f"{', '.join(unfinished)}; complete them first"
            )

        if not discard_changes:
            ok, status = self._run_git(["status", "--porcelain", "--ignored"], cwd=self._path(name))
            if ok and status.strip():
                return (
                    f"Error: worktree {name!r} has uncommitted or ignored files. "
                    "Commit them or pass discard_changes=True."
                )

        ok, output = self._run_git(["worktree", "remove", "--force", str(self._path(name))])
        if not ok:
            return f"Error: git worktree remove failed: {output}"
        # The wt/<name> branch is deliberately retained, so work can be reviewed.
        for task in self.bound_tasks(name):
            self.bind(task.id, None)
        return f"Removed worktree {name!r} (branch {self._branch(name)} retained)"

    # -- leases -------------------------------------------------------------
    # 运行时租约
    def assign(self, teammate: str, task_id: str, worktree: str) -> Path:
        """Bind a teammate to a worktree for the duration of a task."""
        try:
            path = self._path(worktree)
        except TeamError as exc:
            raise TeamError(str(exc)) from exc
        with self._lock:
            self.assignments[teammate] = Assignment(
                teammate=teammate, task_id=task_id, cwd=path, worktree=worktree
            )
        return path

    def cwd_for(self, teammate: str) -> Path:
        with self._lock:
            assignment = self.assignments.get(teammate)
        if assignment is not None and assignment.cwd.is_dir():
            return assignment.cwd
        return self.workspace

    def assignment_for(self, teammate: str) -> Assignment | None:
        with self._lock:
            return self.assignments.get(teammate)
    # 解除租约，并返回被移除的
    def release(self, teammate: str) -> Assignment | None:
        with self._lock:
            return self.assignments.pop(teammate, None)

    def active_assignments(self) -> dict[str, Assignment]:
        with self._lock:
            return dict(self.assignments)


# --------------------------------------------------------------------------
# Teammate runtime
# --------------------------------------------------------------------------

# 定义了一个teammate自己如何活着：它有哪些状态，如何启动线程，如何收到消息，如何找任务，如何调用模型，停止时如何清理
@dataclass
class TeammateState: # 记录某个工人目前的状态
    name: str
    role: str
    prompt: str
    status: str = "starting"        # starting | idle | working | waiting | stopped
    claimed_task: str | None = None
    plan: str = ""
    turns: int = 0
    last_message: str = ""          # last lifecycle event
    last_output: str = ""           # last text the teammate actually produced
    error: str = ""

# 持有状态，对话历史和线程，实际执行工作
class Teammate:
    """One colleague: its own loop, context, task claim, and mailbox."""

    def __init__(
        self,
        *,
        name: str,
        role: str,
        prompt: str,
        bus: MessageBus,
        tasks: TaskStore,
        worktrees: WorktreeManager,
        protocol: ProtocolState,
        registry: ToolRegistry,
        llm: LLMClient,
        settings: Any,
        hooks_factory: Callable[[], Hooks],
        runner: Callable[..., str],
        tool_runtime: Any = None,
        lead_name: str = "lead",
        autonomous: bool = True,
        require_plan: bool = True,
        idle_rounds: int = DEFAULT_IDLE_ROUNDS,
        on_event: Callable[[str], None] | None = None,
    ):
        self.name = name
        self.role = role
        self.prompt = prompt
        self.bus = bus
        self.tasks = tasks
        self.worktrees = worktrees
        self.protocol = protocol
        self.registry = registry
        self.llm = llm
        self.settings = settings
        self.hooks_factory = hooks_factory
        self.runner = runner
        self.tool_runtime = tool_runtime
        self.lead_name = lead_name
        self.autonomous = autonomous
        self.require_plan = require_plan
        self.idle_rounds = idle_rounds
        self.on_event = on_event

        self.state = TeammateState(name=name, role=role, prompt=prompt)
        self.messages: list[dict] = []
        self.todos = TodoList()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- logging ------------------------------------------------------------

    def _log(self, text: str) -> None:
        self.state.last_message = text
        if self.on_event:
            self.on_event(f"[{self.name}] {text}")

    # -- lifecycle ----------------------------------------------------------
    # 新建并启动一个线程
    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name=f"teammate-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout=timeout)

    # -- context ------------------------------------------------------------
    # 给teammate创建自己的工具环境
    def _tool_context(self) -> ToolContext:
        cwd = self.worktrees.cwd_for(self.name)
        runtime = self.tool_runtime
        if runtime is not None and hasattr(runtime, "for_teammate"):
            runtime = runtime.for_teammate(self)
        return ToolContext(
            settings=self.settings,
            workdir=cwd,
            owner=self.name,
            interactive=False,   # a teammate must never block on a prompt
            runtime=runtime,
            extra={"allow_outside": False, "teammate": self.name},
        )

    def system_prompt(self) -> str:
        lines = [
            f"You are {self.name!r}, a teammate on a coding team, working in "
            f"{self._tool_context().workdir}.",
            f"Your role: {self.role}.",
            "You cannot ask a human questions. If you need a decision, send a "
            f"message to {self.lead_name!r} with the send_message tool and continue "
            "with your best judgement, or stop and report.",
            f"The lead is {self.lead_name!r}. Use list_tasks, claim_task, and "
            "complete_task to work the shared task board. Exactly one task may be "
            "in progress for you at a time.",
            "Follow the lead's assigned file ownership and interfaces. Check your actual "
            "working directory and task baseline before editing. In a shared directory, "
            "modify only your assigned files; coordinate shared-file changes with the lead.",
            "Before complete_task, run the relevant checks and send the lead a delivery "
            "report with changed files, test commands/results, and any blockers. Do not mark "
            "failed or unverified work complete. If this task uses a Git worktree, commit "
            "only task-owned changes and include the branch and commit ID in the report. "
            "Do not merge into the lead branch, push remotely, or remove worktrees yourself. "
            "The lead integrates and verifies the overall project; complete_task only updates "
            "the task board. If the required baseline or Git commit is unavailable, report "
            "the blocker instead of claiming delivery.",
        ]
        if self.require_plan:
            lines.append(
                "Before doing any file modification, you must have an approved "
                "plan. Use submit_plan to create an approval request and send your "
                "plan; you do not need the lead to call request_plan first. Then "
                "stop this turn and wait for the lead's decision. If rejected, "
                "revise and call submit_plan again to request another review."
            )
        if self.state.claimed_task:
            lines.append(f"You have claimed {self.state.claimed_task}.")
        return "\n".join(lines)

    # -- mailbox handling ---------------------------------------------------

    def handle_inbox(self, received: list[Message] | None = None) -> list[str]:
        """Process waiting messages, including those consumed by an idle wait."""
        summaries: list[str] = []
        for message in self.bus.read_inbox(self.name) if received is None else received:
            if message.type == PLAN_DECISION:
                summaries.append(self._apply_plan_decision(message))
            elif message.type == SHUTDOWN_REQUEST:
                summaries.append(self._apply_shutdown_request(message))
            else:
                summaries.append(f"{message.sender}: {message.content}")
        return summaries

    def _queue_inbox(self, received: list[Message] | None = None) -> bool:
        """Process inbox messages and queue their summaries for the next turn."""
        summaries = self.handle_inbox(received)
        for summary in summaries:
            self.messages.append({"role": "user", "content": f"[team] {summary}"})
        return bool(summaries)

    def _apply_plan_decision(self, message: Message) -> str:
        approved = bool(message.metadata.get("approve"))
        detail = str(message.metadata.get("feedback", ""))
        gate = "approved" if approved else "rejected"
        self.protocol.plan_gates[self.name] = gate
        self.state.status = "working" if approved else "waiting"
        verdict = "approved" if approved else "rejected"
        return f"Lead {verdict} your plan. {detail}".strip()

    def _apply_shutdown_request(self, message: Message) -> str:
        request_id = str(message.metadata.get("request_id", ""))
        self.bus.send(
            self.name,
            self.lead_name,
            "Acknowledged shutdown.",
            type=SHUTDOWN_RESPONSE,
            metadata={"request_id": request_id, "approve": True},
        )
        self._stop.set()
        self.state.status = "stopped"
        return "Lead requested shutdown; acknowledged and stopping."

    # -- work selection -----------------------------------------------------

    def claim_task(self, task_id: str, ctx: ToolContext | None = None) -> str:
        """Synchronize a board claim with this teammate and its live tool context."""
        result = self.tasks.claim(task_id, owner=self.name)
        task = self.tasks.load(task_id)
        if task.status != IN_PROGRESS or task.owner != self.name:
            return result
        if self.state.claimed_task != task.id:
            try:
                self._bind_worktree(task)
            except TeamError as exc:
                # Never report successful isolation and then write in the lead
                # directory. A newly claimed task can be retried after repair.
                if result.startswith("Claimed"):
                    self.tasks.release(task.id, owner=self.name)
                return f"Error: could not enter task worktree: {exc}"
            self.state.claimed_task = task.id
            self.state.status = "working"
            self.todos.clear()
            self.protocol.bump_work_version(self.name)
        if ctx is not None:
            ctx.workdir = self.worktrees.cwd_for(self.name)
            ctx.extra.pop("allow_outside", None)
        return result + f"\nWorking directory: {self.worktrees.cwd_for(self.name)}"

    def complete_task(self, task_id: str, ctx: ToolContext | None = None) -> str:
        result = self.tasks.complete(task_id, owner=self.name)
        if self.state.claimed_task == task_id:
            self._release_worktree()
            self.state.claimed_task = None
            self.state.status = "idle"
            if ctx is not None:
                ctx.workdir = self.worktrees.cwd_for(self.name)
                ctx.extra.pop("allow_outside", None)
        return result

    def claim_next_task(self) -> Task | None:
        """Take the first ready task.  The store's lock makes this atomic."""
        for task in self.tasks.ready():
            result = self.claim_task(task.id)
            if result.startswith("Claimed"):
                return self.tasks.try_load(task.id)
        return None

    def _find_work(self) -> tuple[bool, bool]:
        """Return (has_task, newly_claimed) after checking the task board."""
        task = self.tasks.try_load(self.state.claimed_task) if self.state.claimed_task else None
        if task is not None and task.status == IN_PROGRESS:
            return True, False
        if task is not None:
            self._release_worktree()
        self.state.claimed_task = None

        claimed = self.claim_next_task() if self.autonomous else None
        if claimed is None:
            return False, False

        self.messages.append(
            {
                "role": "user",
                "content": (
                    f"[team] You claimed {claimed.id}: {claimed.subject}\n"
                    f"{claimed.description}"
                ),
            }
        )
        return True, True

    def _bind_worktree(self, task: Task) -> None:   #领到任务后，尝试给它一个独立目录
        """Give a claimed task its own worktree when the repo supports it."""
        if not self.worktrees.enabled:
            return
        if not self.worktrees.is_git_repo():
            return
        name = task.worktree or f"{self.name}-{task.id.removeprefix('task_')}"
        validate_worktree_name(name)
        outcome = self.worktrees.create(name, task.id)
        if not (outcome.startswith("Created") or outcome.startswith("Worktree ")):
            raise TeamError(outcome)
        expected = self.worktrees._path(name)
        registered = self.worktrees.registered().get(name, {})
        actual = registered.get("worktree")
        if not actual or Path(actual).resolve() != expected or not expected.is_dir():
            raise TeamError(f"worktree {name!r} is not registered at {expected}")
        self.worktrees.bind(task.id, name)
        self.worktrees.assign(self.name, task.id, name)
        self._log(f"working in worktree {name}")

    def _release_worktree(self) -> None:
        removed = self.worktrees.release(self.name)
        if removed is not None:
            self._log(f"released worktree {removed.worktree}")
    # 解决队友死了，还未完成任务
    def _release_assignment(self) -> None:
        """Hand abandoned work back to the board when the teammate stops.

        Without this a teammate that stops mid-task orphans it: the row stays
        `in_progress` owned by a name that will never run again, so no one can
        ever claim it.  The task file is durable, so the leak would outlive the
        process.
        """
        try:
            self._release_worktree()
        except Exception as exc:  # noqa: BLE001
            self._log(f"worktree release failed: {type(exc).__name__}: {exc}")

        task_id = self.state.claimed_task
        if not task_id:
            return
        try:
            task = self.tasks.try_load(task_id)
            if task is None:
                return
            if task.status == IN_PROGRESS and task.owner == self.name:
                self._log(self.tasks.release(task.id, owner=self.name))
        except Exception as exc:  # noqa: BLE001
            self._log(f"task release failed: {type(exc).__name__}: {exc}")

    def _plan_approved(self) -> bool:
        if not self.require_plan:
            return True
        return self.protocol.plan_approved(self.name)

    # -- the teammate loop --------------------------------------------------

    def run(self) -> None:
        self.state.status = "working"
        self._log("started")

        # Kick off with the lead's briefing.
        self.messages.append(
            {
                "role": "user",
                "content": (
                    f"You are {self.name}, role: {self.role}.\n\n{self.prompt}\n\n"
                    "Start by checking the task board with list_tasks. Claim a ready "
                    "task with claim_task, do the work, then complete_task."
                ),
            }
        )

        # The briefing itself always earns one turn.  Without this a teammate
        # given a self-contained job -- and no task on the board -- would retire
        # without ever reading what it was told to do.
        try:
            self._run_turn()
        except Exception as exc:  # noqa: BLE001
            self.state.error = f"{type(exc).__name__}: {exc}"
            self._log(f"briefing turn failed: {self.state.error}")

        idle = 0
        try:
            self._work_loop(idle)
        finally:
            # Always runs, on every exit path: normal retirement, shutdown,
            # or an unhandled error.  A leaked task is worse than a leaked
            # worktree, because the task file is durable.
            self._release_assignment()
            self.state.status = "stopped"
            self._log("stopped")

    def _work_loop(self, idle: int) -> None:
        """Receive messages, find work, run one turn, or wait without spinning."""
        # wait_for_messages consumes the mailbox. Remember that event until
        # the next iteration so a message alone can wake the model.
        message_ready = False  # 有新消息已经取出，但模型还没有处理
        # A text-only turn, or a submitted plan awaiting approval, must not
        # make the same model request again on every mailbox timeout.
        waiting_for_change = False # 上一次模型回合没有新的工具活动，或者计划正在等审批；在情况变化前，不要拿同一份输入反复问模型。
        while not self._stop.is_set():# 只要没收到停止信号，就进入下一圈
            try: # 会读取队友邮箱、处理消息，再把文字结果加入队友的 self.messages
                message_ready = self._queue_inbox() or message_ready #因为消息可能在上一圈等待时就被取出了。此刻邮箱虽然空，message_ready 仍是 True，不能把“模型尚未处理这条消息”忘掉
                if self._stop.is_set():
                    break
                # 已领取的任务仍是IN_PROGESS 或者领取到新任务了
                has_task, newly_claimed = self._find_work()
                if newly_claimed:
                    idle = 0 # 清空空闲次数
                    waiting_for_change = False

                plan_pending = self.require_plan and self.protocol.plan_gates.get(self.name) == "pending"
                # An existing task is not new input after a text-only turn.
                # Messages and newly claimed tasks can always wake the model.
                # 有新消息，模型需要读，刚领到新任务，模型需要开始做
                should_run = message_ready or newly_claimed or (
                    has_task and not waiting_for_change and not plan_pending
                )
                if should_run:
                    if plan_pending:
                        self.state.status = "waiting"
                        self.messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "[team] Your plan is awaiting the lead's decision. "
                                    "Call submit_plan again if you have not, then stop this turn."
                                ),
                            }
                        )
                    message_ready = False
                    made_progress = self._run_turn() # 发生过工具活动可能有消息
                    if self._stop.is_set():
                        break
                    plan_pending = self.require_plan and self.protocol.plan_gates.get(self.name) == "pending"
                    waiting_for_change = not made_progress or plan_pending #这轮没有工具活动 或者 正在等审批计划
                    if not waiting_for_change: # 确实有工具活动且不等审批
                        idle = 0
                        continue
                    retire_reason = "no further progress; retiring"
                else:
                    retire_reason = "no further progress; retiring" if has_task else "no work left; retiring"

                # A submitted plan is waiting on the lead, not idle work.
                # Do not retire before its decision arrives.
                if plan_pending:
                    idle = 0
                    self.state.status = "waiting"
                else:
                    idle += 1
                    if idle > self.idle_rounds:
                        self._log(retire_reason)
                        break
                    self.state.status = "idle"
                # 等邮箱消息
                received = self.bus.wait_for_messages(self.name, timeout=IDLE_SCAN_INTERVAL)
                message_ready = self._queue_inbox(received)
                if message_ready:
                    idle = 0
            except Exception as exc:  # noqa: BLE001 - a teammate must not take the process down
                self.state.error = f"{type(exc).__name__}: {exc}"
                self._log(f"error: {self.state.error}")
                message_ready = False
                time.sleep(IDLE_SCAN_INTERVAL)


    def _run_turn(self) -> bool:
        """One agent turn with the teammate's own context.

        Returns True when the turn actually did something (issued tool calls),
        which is what tells the work loop whether to continue or idle.
        """
        ctx = self._tool_context()
        self.state.status = "working"
        self.state.turns += 1

        activity = {"count": 0}

        def on_event(_text: str) -> None:
            activity["count"] += 1

        text = self.runner(
            llm=self.llm,
            registry=self.registry,
            messages=self.messages,
            system=self.system_prompt,
            ctx=ctx,
            hooks=self.hooks_factory(),
            max_turns=MAX_TEAMMATE_TURNS,
            on_event=on_event,
        )
        if text:
            self.state.last_output = text
            self._log(text[:200].replace("\n", " "))
        return activity["count"] > 0

    # -- teammate-facing tools ---------------------------------------------

    def submit_plan(self, plan: str) -> str:
        """Send a plan, opening a review request when none is outstanding."""
        with self.protocol.lock:
            request_id = self.protocol.plan_request_ids.get(self.name, "")
            request = self.protocol.pending.get(request_id)
            if (
                request is None
                or request.resolved
                or request.kind != "plan"
                or request.teammate != self.name
            ):
                request = self.protocol.create("plan", self.name, task_id=self.state.claimed_task)
            request_id = request.request_id
            self.protocol.plan_gates[self.name] = "pending"
        self.bus.send(
            self.name,
            self.lead_name,
            plan,
            type=PLAN_RESPONSE,
            metadata={
                "request_id": request_id,
                "approve": True,
                "plan": plan,
                "task_id": self.state.claimed_task,
            },
        )
        return f"Plan submitted to {self.lead_name} (request {request_id}); awaiting the decision."

    def send_message(self, to: str, content: str) -> str:
        self.bus.send(self.name, to, content)
        return f"Message sent to {to}"


# --------------------------------------------------------------------------
# Team manager
# --------------------------------------------------------------------------


class TeamManager:
    """Owns the bus, protocol state, worktrees, and the teammate roster."""

    def __init__(
        self,
        *,
        workspace: Path,
        mailbox_dir: Path,
        worktrees_dir: Path,
        tasks: TaskStore,
        registry: ToolRegistry,
        llm: LLMClient,
        settings: Any,
        hooks_factory: Callable[[], Hooks],
        runner: Callable[..., str],
        tool_runtime: Any = None,
        lead_name: str = "lead",
        require_plan: bool = True,
        enabled: bool = True,
        verbose: bool = False,
        on_event: Callable[[str], None] | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.bus = MessageBus(mailbox_dir)
        self.tasks = tasks
        self.worktrees = WorktreeManager(
            self.workspace, worktrees_dir, tasks=tasks, enabled=enabled
        )
        self.protocol = ProtocolState()
        self.registry = registry
        self.llm = llm
        self.settings = settings
        self.hooks_factory = hooks_factory
        self.runner = runner
        self.tool_runtime = tool_runtime
        self.lead_name = lead_name
        self.require_plan = require_plan
        self.enabled = enabled
        self.verbose = verbose
        self.on_event = on_event
        self.teammates: dict[str, Teammate] = {}
        self._lock = threading.RLock()

    # -- roster -------------------------------------------------------------

    def spawn(
        self,
        name: str,
        role: str,
        prompt: str,
        *,
        autonomous: bool = True,
        require_plan: bool | None = None,
    ) -> str:
        if not self.enabled:
            return "Error: teams are disabled for this session"
        if not is_valid_agent_name(name):
            return (
                f"Error: invalid teammate name {name!r}. Use letters, digits, dot, "
                "underscore, or hyphen, starting with a letter or digit."
            )
        if name.casefold() in RESERVED_AGENT_NAMES:
            return f"Error: {name!r} is reserved by the runtime"

        with self._lock:
            existing = self.teammates.get(name)
            if existing is not None and existing.alive:
                return f"Error: teammate {name!r} is already running"

            # Arm the gate *before* the thread starts, so the very first tool
            # call already sees the right answer.  An absent gate entry would
            # otherwise read as "not approved" and block a teammate that was
            # spawned with require_plan=False.
            effective_require_plan = self.require_plan if require_plan is None else require_plan
            with self.protocol.lock:
                self.protocol.plan_gates[name] = (
                    "required" if effective_require_plan else "not_required"
                )
                self.protocol.work_versions.setdefault(name, 0)

            teammate = Teammate(
                name=name,
                role=role,
                prompt=prompt,
                bus=self.bus,
                tasks=self.tasks,
                worktrees=self.worktrees,
                protocol=self.protocol,
                registry=self.registry,
                llm=self.llm,
                settings=self.settings,
                hooks_factory=self.hooks_factory,
                runner=self.runner,
                tool_runtime=self.tool_runtime,
                lead_name=self.lead_name,
                autonomous=autonomous,
                require_plan=effective_require_plan,
                on_event=self.on_event,
            )
            self.teammates[name] = teammate

        teammate.start()
        return f"Spawned teammate {name!r} ({role})"

    def get(self, name: str) -> Teammate | None:
        return self.teammates.get(name)

    def roster(self) -> str:
        if not self.teammates:
            return "No teammates."
        lines = []
        for name, teammate in sorted(self.teammates.items()):
            state = teammate.state
            detail = f" task={state.claimed_task}" if state.claimed_task else ""
            gate = self.protocol.plan_gates.get(name, "not_required")
            lines.append(
                f"- {name} ({state.role}): {state.status}{detail} "
                f"turns={state.turns} plan={gate}"
            )
            if state.error:
                lines.append(f"    error: {state.error}")
        return "\n".join(lines)

    def shutdown(self, name: str, reason: str = "") -> str:
        teammate = self.teammates.get(name)
        if teammate is None:
            return f"Error: no teammate named {name!r}"
        request = self.protocol.create("shutdown", name)
        self.bus.send(
            self.lead_name,
            name,
            reason or "Shutdown requested by the lead.",
            type=SHUTDOWN_REQUEST,
            metadata={"request_id": request.request_id},
        )
        return f"Shutdown requested from {name!r} (request {request.request_id})"

    def stop_all(self, timeout: float = 5.0) -> None:
        for name in list(self.teammates):
            teammate = self.teammates[name]
            teammate.stop()
        for name in list(self.teammates):
            self.teammates[name].join(timeout=timeout)

    # -- lead-side protocol tools ------------------------------------------

    def request_plan(self, name: str, task: str) -> str:
        """Ask a teammate to submit a plan.

        This deliberately does not require the teammate to be in the roster
        yet: the mailbox is durable, so a request sent to a not-yet-spawned (or
        currently restarting) teammate is delivered as soon as it reads its
        inbox.  Only the name has to be valid.
        """
        if not is_valid_agent_name(name):
            return f"Error: invalid teammate name {name!r}"
        request = self.protocol.create("plan", name, task_id=task or None)
        self.bus.send(
            self.lead_name,
            name,
            f"Submit a plan before making changes.\n\nTask: {task}",
            type=PLAN_REQUEST,
            metadata={"request_id": request.request_id, "task": task},
        )
        return f"Plan requested from {name!r} (request {request.request_id})"

    def review_plan(self, request_id: str, approve: bool, feedback: str = "") -> str:
        request = self.protocol.pending.get(request_id)
        if request is None:
            return f"Error: unknown request {request_id!r}"
        if request.kind != "plan":
            return f"Error: {request_id!r} is not a plan request"
        if request.resolved:
            return f"Error: request {request_id!r} was already resolved"

        self.protocol.resolve(request, approve, feedback)
        self.bus.send(
            self.lead_name,
            request.teammate,
            feedback or ("Plan approved." if approve else "Plan rejected."),
            type=PLAN_DECISION,
            metadata={"request_id": request_id, "approve": approve, "feedback": feedback},
        )
        verdict = "approved" if approve else "rejected"
        return f"Plan {request_id} {verdict} for {request.teammate!r}"

    def pending_requests(self) -> str:
        outstanding = [r for r in self.protocol.pending.values() if not r.resolved]
        if not outstanding:
            return "No outstanding requests."
        return "\n".join(
            f"- {r.request_id} {r.kind} from {r.teammate} (task={r.task_id})"
            for r in outstanding
        )

    # -- lead inbox ---------------------------------------------------------

    def consume_lead_inbox(self, *, route: bool = True) -> list[str]:
        """Read the lead's mailbox, routing protocol replies into state."""
        summaries: list[str] = []
        for message in self.bus.read_inbox(self.lead_name):
            if route and message.type == PLAN_RESPONSE:
                request_id = str(message.metadata.get("request_id", ""))
                request = self.protocol.match_response(PLAN_RESPONSE, request_id, message.sender)
                if request is None:
                    summaries.append(f"[ignored stale plan response from {message.sender}]")
                    continue
                request.resolved = False  # awaiting the lead's review
                gate = self.protocol.plan_gates.get(message.sender)
                self.protocol.plan_gates[message.sender] = "pending" if gate != "approved" else gate
                summaries.append(
                    f"{message.sender} submitted a plan for {request_id}:\n{message.content}\n"
                    f"Reply with review_plan(request_id='{request_id}', approve=..., feedback=...)."
                )
            elif route and message.type == SHUTDOWN_RESPONSE:
                request_id = str(message.metadata.get("request_id", ""))
                request = self.protocol.match_response(SHUTDOWN_RESPONSE, request_id, message.sender)
                if request is not None:
                    self.protocol.resolve(request, True, "shutdown acknowledged")
                summaries.append(f"{message.sender} acknowledged shutdown.")
            else:
                summaries.append(f"{message.sender}: {message.content}")
        return summaries

    def format_events(self, events: list[str]) -> str:
        if not events:
            return ""
        return "Team events:\n" + "\n".join(f"- {event}" for event in events)
