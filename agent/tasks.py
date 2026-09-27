"""Task system -- big goals break into small tasks, ordered, persisted to disk.

One JSON file per task, so the graph survives a crash and can be inspected by
anything that reads JSON:

    .agent/tasks/task_a1b2c3d4.json
    {
      "id": "task_a1b2c3d4",
      "subject": "Extract the parser",
      "description": "...",
      "status": "pending",
      "owner": null,
      "blockedBy": ["task_e5f6a7b8"]
    }

The dependency graph is the whole point.  `blockedBy` names tasks that must be
*completed* first, `can_start` answers "may I begin?", and `complete` reports
which tasks just became unblocked -- so the model always knows what to do next
without holding the graph in its head.

Concurrency: s10 itself has no locking at all, which is fine for one agent and
wrong the moment teammates exist.  This implementation takes the s13 upgrade --
a re-entrant in-process lock plus write-to-temp-then-`os.replace` -- so a
partially written task file is never observable and two claimants cannot both
"win".  (Cross-*process* safety would additionally need a file lock, which is
not portable; the in-process lock covers the threaded teammate design here.)
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .tools.registry import ToolContext, ToolRegistry

TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")
# 任务状态
PENDING = "pending"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
VALID_STATUSES = (PENDING, IN_PROGRESS, COMPLETED)

STATUS_MARKERS = {PENDING: "[ ]", IN_PROGRESS: "[>]", COMPLETED: "[x]"}

MAX_SUBJECT_CHARS = 200 # subject最多有200字符
MAX_DESCRIPTION_CHARS = 4_000 # description 最多4000字符

# 任务模块自己的异常类型
class TaskError(ValueError):
    """Raised for an invalid task operation."""

# 一个任务的数据结构
@dataclass
class Task:
    id: str       # 任务唯一id
    subject: str  # 短标题
    description: str = ""  # 任务详情
    status: str = PENDING  # 任务状态
    owner: str | None = None  # 当前领取者
    blockedBy: list[str] = field(default_factory=list) # 必须先完成的任务ID
    #: Name of the git worktree this task is bound to, if any.  Durable on
    #: purpose: the in-memory lease is lost on restart, but the binding is not,
    #: so an interrupted run can still be resolved back to its checkout.
    worktree: str | None = None # 若绑定Git worktree,保存worktree

    def marker(self) -> str:
        return STATUS_MARKERS.get(self.status, "[?]")
    # 把任务变成一行可读的文本
    def line(self) -> str:
        owner = f" @{self.owner}" if self.owner else ""
        blocked = f" (blockedBy: {', '.join(self.blockedBy)})" if self.blockedBy else ""
        tree = f" [wt:{self.worktree}]" if self.worktree else ""
        return f"{self.marker} {self.id} {self.subject}{owner}{blocked}{tree}"


class TaskStore:  # 管理磁盘上的任务
    """The on-disk task graph."""

    def __init__(self, tasks_dir: Path):
        self.tasks_dir = Path(tasks_dir)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Paths and ids
    # ------------------------------------------------------------------
    # 路径与任务ID
    def _path(self, task_id: str, *, create_root: bool = False) -> Path:
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise TaskError(f"Invalid task ID: {task_id!r}")
        if create_root:
            self.tasks_dir.mkdir(parents=True, exist_ok=True)
        path = (self.tasks_dir / f"{task_id}.json").resolve()
        if not path.is_relative_to(self.tasks_dir.resolve()):
            raise TaskError(f"Invalid task ID: {task_id!r}")
        return path
    # 查看文件是否存在
    def exists(self, task_id: str) -> bool:
        try:
            return self._path(task_id).is_file()
        except TaskError:
            return False
    #
    def _allocate_id(self) -> str:
        """Reserve an id by creating its file exclusively, so two threads
        can never hand out the same one."""
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(100):
            task_id = f"task_{uuid.uuid4().hex[:8]}"
            try:
                with self._path(task_id).open("x", encoding="utf-8"):
                    return task_id
            except FileExistsError:
                continue
        raise TaskError("Could not allocate a task ID")

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    # 从磁盘加载任务
    def load(self, task_id: str) -> Task:
        path = self._path(task_id)
        if not path.is_file():
            raise TaskError(f"No such task: {task_id}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TaskError(f"Task file {task_id} is corrupt: {exc}") from exc
        if not isinstance(data, dict):
            raise TaskError(f"Task file {task_id} is not an object")

        task = Task(
            id=str(data.get("id", task_id)),
            subject=str(data.get("subject", "")),
            description=str(data.get("description", "")),
            status=str(data.get("status", PENDING)),
            owner=data.get("owner") if isinstance(data.get("owner"), str) else None,
            blockedBy=[str(item) for item in data.get("blockedBy", []) if isinstance(item, str)],
            worktree=(
                str(data["worktree"]) if isinstance(data.get("worktree"), str) else None
            ),
        )
        if task.id != task_id:
            raise TaskError(f"Task file ID does not match {task_id}")
        if task.status not in VALID_STATUSES:
            raise TaskError(f"Invalid task status: {task.status}")
        return task

    def try_load(self, task_id: str) -> Task | None:
        try:
            return self.load(task_id)
        except TaskError:
            return None
   # 列出所有任务
    def list_all(self) -> list[Task]:
        if not self.tasks_dir.is_dir():
            return []
        tasks: list[Task] = []
        for path in sorted(self.tasks_dir.glob("task_*.json")):
            task = self.try_load(path.stem)
            if task is not None:
                tasks.append(task)
        tasks.sort(key=lambda item: item.id)
        return tasks

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    # 写入任务
    def save(self, task: Task) -> None:
        """Atomic write: temp file in the same directory, then os.replace."""
        with self._lock:
            self.tasks_dir.mkdir(parents=True, exist_ok=True)
            path = self._path(task.id)
            temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            payload = json.dumps(asdict(task), indent=2)
            try:
                temp.write_text(payload, encoding="utf-8")
                os.replace(temp, path)
            finally:
                if temp.exists():
                    try:
                        temp.unlink()
                    except OSError:
                        pass
    # 创建任务
    def create(self, subject: str, description: str = "") -> Task:
        subject = " ".join(str(subject or "").split())
        if not subject:
            raise TaskError("subject is required")
        if len(subject) > MAX_SUBJECT_CHARS:
            subject = subject[:MAX_SUBJECT_CHARS] + "..."
        description = str(description or "")[:MAX_DESCRIPTION_CHARS]

        with self._lock:
            task = Task(id=self._allocate_id(), subject=subject, description=description)
            self.save(task)
            return task

    # ------------------------------------------------------------------
    # Dependency graph
    # ------------------------------------------------------------------
    # 依赖图：防止任务循环依赖
    def _depends_on(self, task_id: str, target_id: str) -> bool:
        """Does `task_id` transitively depend on `target_id`?

        Used to reject a cycle before it is written.
        """
        seen: set[str] = set()
        pending = [task_id]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            task = self.try_load(current)
            if task is None:
                continue
            if target_id in task.blockedBy:
                return True
            pending.extend(task.blockedBy)
        return False
    # 添加依赖
    def update_dependencies(self, task_id: str, add_blocked_by: list[str]) -> Task:
        with self._lock:
            task = self.load(task_id)
            if task.status != PENDING or task.owner is not None:
                raise TaskError(
                    f"Task {task_id} dependencies can only be updated while it is "
                    f"pending and unowned (currently {task.status}, owner={task.owner})"
                )
            for dependency in add_blocked_by or []:
                if not isinstance(dependency, str):
                    raise TaskError("blockedBy entries must be task IDs")
                if dependency == task_id:
                    raise TaskError(f"Task {task_id} cannot depend on itself")
                if not self.exists(dependency):
                    raise TaskError(f"No such task: {dependency}")
                if dependency not in task.blockedBy and self._depends_on(dependency, task_id):
                    raise TaskError(f"Dependency cycle detected: {task_id} -> {dependency}")

            for dependency in add_blocked_by or []:
                if dependency not in task.blockedBy:
                    task.blockedBy.append(dependency)
            self.save(task)
            return task
    # 哪些任务可以开始
    def incomplete_dependencies(self, task: Task) -> list[str]:
        missing = []
        for dependency in task.blockedBy:
            other = self.try_load(dependency)
            if other is None or other.status != COMPLETED:
                missing.append(dependency)
        return missing
    # 只回答依赖是否都已完成
    def can_start(self, task_id: str) -> bool:
        return not self.incomplete_dependencies(self.load(task_id))

    def ready(self) -> list[Task]:
        """Pending, unowned tasks whose dependencies are all complete."""
        return [
            task
            for task in self.list_all()
            if task.status == PENDING and task.owner is None and not self.incomplete_dependencies(task)
        ]
    # 用于向模型展示为什么这些任务还不能开始
    def blocked(self) -> list[Task]:
        return [
            task
            for task in self.list_all()
            if task.status == PENDING and self.incomplete_dependencies(task)
        ]

    # ------------------------------------------------------------------
    # Ownership   # 任务领取
    # ------------------------------------------------------------------

    def _validate_owner(self, owner: str) -> str:
        if not isinstance(owner, str) or not owner.strip():
            raise TaskError("owner is required")
        return owner.strip()

    def _owner_in_progress(self, owner: str) -> Task | None:
        return next(
            (
                task
                for task in self.list_all()
                if task.owner == owner and task.status == IN_PROGRESS
            ),
            None,
        )
    # 并发安全最核心的方法
    def claim(self, task_id: str, owner: str = "agent") -> str:
        """Claim a ready task.  All six guards run under one lock."""
        owner = self._validate_owner(owner)
        with self._lock:
            task = self.load(task_id)

            if task.status == COMPLETED:
                return f"Task {task_id} is already completed"
            if task.status == IN_PROGRESS:
                if task.owner == owner:
                    return f"Task {task_id} is already claimed by {owner}"
                return f"Task {task_id} is owned by {task.owner}, not {owner}"
            if task.owner is not None and task.owner != owner:
                return f"Task {task_id} is owned by {task.owner}, not {owner}"
            # 依赖未完成
            missing = self.incomplete_dependencies(task)
            if missing:
                return (
                    f"Task {task_id} is blocked by incomplete "
                    f"{', '.join(missing)}"
                )
            # 当前owner已在做另一项任务
            active = self._owner_in_progress(owner)
            if active is not None:
                return (
                    f"{owner} is already working on {active.id} ({active.subject}); "
                    "complete it before claiming another task"
                )

            task.status = IN_PROGRESS
            task.owner = owner
            self.save(task)
            return f"Claimed {task_id} ({task.subject}) as {owner}"
    # 完成任务
    def complete(self, task_id: str, owner: str = "agent") -> str:
        owner = self._validate_owner(owner)
        with self._lock:
            task = self.load(task_id)
            if task.status != IN_PROGRESS:
                return f"Task {task_id} is {task.status}, cannot complete"
            if task.owner != owner:
                return f"Task {task_id} is owned by {task.owner}, not {owner}"

            task.status = COMPLETED
            self.save(task)

            unblocked = [
                candidate.id
                for candidate in self.list_all()
                if candidate.status == PENDING
                and candidate.blockedBy
                and not self.incomplete_dependencies(candidate)
            ]
            message = f"Completed {task_id} ({task.subject})"
            if unblocked:
                message += f"\nNow unblocked: {', '.join(unblocked)}"
            return message
    # 任务归还任务池
    def release(self, task_id: str, owner: str = "agent") -> str:
        """Return a claimed task to the pool.  Used when a teammate dies."""
        with self._lock:
            task = self.load(task_id)
            if task.status != IN_PROGRESS:
                return f"Task {task_id} is {task.status}, nothing to release"
            if task.owner != owner:
                return f"Task {task_id} is owned by {task.owner}, not {owner}"
            task.status = PENDING
            task.owner = None
            self.save(task)
            return f"Released {task_id}"

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def get_json(self, task_id: str) -> str:
        return json.dumps(asdict(self.load(task_id)), indent=2)

    def render_list(self) -> str:
        tasks = self.list_all()
        if not tasks:
            return "No tasks."
        lines = [task.line() for task in tasks]
        counts = {status: 0 for status in VALID_STATUSES}
        for task in tasks:
            counts[task.status] = counts.get(task.status, 0) + 1
        lines.append(
            f"({counts[PENDING]} pending, {counts[IN_PROGRESS]} in progress, "
            f"{counts[COMPLETED]} completed)"
        )
        ready = [task.id for task in self.ready()]
        if ready:
            lines.append(f"Ready to claim: {', '.join(ready)}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Tools  # 工具层 如何从模型调用到TaskStore
# --------------------------------------------------------------------------


def _store(ctx: ToolContext) -> TaskStore:
    runtime = ctx.runtime  # 优先从runtime拿共享任务库
    if runtime is not None and getattr(runtime, "tasks", None) is not None:
        return runtime.tasks
    if "tasks" not in ctx.extra:
        ctx.extra["tasks"] = TaskStore(ctx.settings.tasks_dir)
    return ctx.extra["tasks"]


def run_create_task(args: dict, ctx: ToolContext) -> str:
    store = _store(ctx)
    try:
        task = store.create(args.get("subject", ""), args.get("description", ""))
    except TaskError as exc:
        return f"Error: {exc}"
    blocked_by = args.get("blockedBy") or []
    if blocked_by:
        try:
            store.update_dependencies(task.id, list(blocked_by))
        except TaskError as exc:
            return f"Created {task.id} but could not set dependencies: {exc}"
    return f"Created {task.id}: {task.subject}"

#一句话：args 是“模型想做什么”，ctx 是“系统允许它以什么身份、在哪个环境里做”

def run_update_task(args: dict, ctx: ToolContext) -> str:
    store = _store(ctx)
    task_id = args.get("task_id", "")
    add = args.get("addBlockedBy") or []
    if not isinstance(add, list) or not add:
        return "Error: addBlockedBy must be a non-empty array of task IDs"
    try:
        task = store.update_dependencies(task_id, add)
    except TaskError as exc:
        return f"Error: {exc}"
    dependencies = ", ".join(task.blockedBy) or "(none)"
    return f"Updated {task.id} blockedBy: {dependencies}"


def run_list_tasks(args: dict, ctx: ToolContext) -> str:
    return _store(ctx).render_list()


def run_get_task(args: dict, ctx: ToolContext) -> str:
    try:
        return _store(ctx).get_json(args.get("task_id", ""))
    except TaskError as exc:
        return f"Error: {exc}"


def run_claim_task(args: dict, ctx: ToolContext) -> str:
    try:
        return _store(ctx).claim(args.get("task_id", ""), owner=ctx.owner)
    except TaskError as exc:
        return f"Error: {exc}"


def run_complete_task(args: dict, ctx: ToolContext) -> str:
    try:
        return _store(ctx).complete(args.get("task_id", ""), owner=ctx.owner)
    except TaskError as exc:
        return f"Error: {exc}"


_ID_SCHEMA = {"type": "string", "pattern": "^task_[0-9a-f]{8}$"}

CREATE_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string", "description": "Short imperative title."},
        "description": {"type": "string", "description": "What the task involves."},
        "blockedBy": {
            "type": "array",
            "items": _ID_SCHEMA,
            "description": "Task IDs that must finish before this one starts.",
        },
    },
    "required": ["subject"],
}

UPDATE_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": _ID_SCHEMA,
        "addBlockedBy": {"type": "array", "items": _ID_SCHEMA, "minItems": 1},
    },
    "required": ["task_id", "addBlockedBy"],
}

TASK_ID_SCHEMA = {
    "type": "object",
    "properties": {"task_id": _ID_SCHEMA},
    "required": ["task_id"],
}


def register_task_tools(registry: ToolRegistry) -> ToolRegistry:
    registry.add(
        "create_task",
        "Create a task and return its runtime-generated ID.",
        CREATE_TASK_SCHEMA,
        run_create_task,
    )
    registry.add(
        "update_task",
        "Add dependencies to a pending task using IDs returned by create_task.",
        UPDATE_TASK_SCHEMA,
        run_update_task,
    )
    registry.add(
        "list_tasks",
        "List tasks with status, owner, and dependencies.",
        {"type": "object", "properties": {}},
        run_list_tasks,
        read_only=True,
    )
    registry.add("get_task", "Get one task by ID.", TASK_ID_SCHEMA, run_get_task, read_only=True)
    registry.add(
        "claim_task",
        "Claim a pending task whose dependencies are complete.",
        TASK_ID_SCHEMA,
        run_claim_task,
    )
    registry.add(
        "complete_task",
        "Complete the task this agent has claimed.",
        TASK_ID_SCHEMA,
        run_complete_task,
    )
    return registry
