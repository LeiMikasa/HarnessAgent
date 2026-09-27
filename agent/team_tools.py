"""Team tools -- what the lead and the teammates can actually call.

The lead's job is coordination: spawn, hand out work, approve plans, keep an
eye on the roster.  A teammate's job is execution: claim a task, ask for plan
approval, report back.

    lead tool                 direction        teammate tool
    ------------------------  ---------------  ----------------------
    spawn_teammate            -> roster
    list_teammates            <- status
    send_message              <-> mailbox
    request_plan              -> ask           submit_plan
    review_plan               <- decide
    request_shutdown          -> stop
    create_worktree           -> isolation
    create_task/claim_task    <-> shared board
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .tasks import TaskStore
from .teams import TeamManager, Teammate
from .tools.registry import ToolContext, ToolRegistry


@dataclass
class TeammateToolRuntime:
    """The `ctx.runtime` a teammate's tools see.

    It deliberately exposes no `spawn_subagent` and no lead-side switches, so
    a teammate's blast radius stays small: it can talk to the team, work the
    shared board, and touch files in its own worktree.
    """

    teams: TeamManager
    tasks: TaskStore
    teammate: Teammate
    todos: Any = None
    skills: Any = None
    mcp: Any = None
    settings: Any = None

    def spawn_subagent(self, prompt: str, ctx: ToolContext | None = None) -> str:
        return "Error: teammates cannot spawn subagents"

    def refresh_mcp_tools(self) -> None:
        return None

    def for_teammate(self, teammate: Teammate) -> "TeammateToolRuntime":
        """A per-teammate view, so `submit_plan` reaches the right colleague."""
        return TeammateToolRuntime(
            teams=self.teams,
            tasks=self.tasks,
            teammate=teammate,
            todos=self.todos,
            skills=self.skills,
            mcp=self.mcp,
            settings=self.settings,
        )


def _manager(ctx: ToolContext) -> TeamManager | None:
    runtime = ctx.runtime
    if runtime is None:
        return None
    return getattr(runtime, "teams", None)


def _teammate(ctx: ToolContext) -> Teammate | None:
    runtime = ctx.runtime
    if runtime is None:
        return None
    return getattr(runtime, "teammate", None)


# --------------------------------------------------------------------------
# Tools available to both sides
# --------------------------------------------------------------------------


def run_list_teammates(args: dict, ctx: ToolContext) -> str:
    manager = _manager(ctx)
    if manager is None:
        return "Error: teams are not enabled for this session"
    return manager.roster()


def run_send_message(args: dict, ctx: ToolContext) -> str:
    manager = _manager(ctx)
    if manager is None:
        return "Error: teams are not enabled for this session"

    to = str(args.get("to", "")).strip()
    content = str(args.get("content", ""))
    if not to or not content:
        return "Error: 'to' and 'content' are required"

    teammate = _teammate(ctx)
    if teammate is not None:
        # A teammate writing to the lead.
        if to == manager.lead_name:
            return teammate.send_message(to, content)
        manager.bus.send(teammate.name, to, content)
        return f"Message sent to {to}"

    if to not in manager.teammates:
        return f"Error: no teammate named {to!r}. Roster:\n{manager.roster()}"
    manager.bus.send(manager.lead_name, to, content)
    return f"Message sent to {to}"


SEND_MESSAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "to": {"type": "string", "description": "Recipient name."},
        "content": {"type": "string", "description": "Message body."},
    },
    "required": ["to", "content"],
}


# --------------------------------------------------------------------------
# Lead-side tools
# --------------------------------------------------------------------------


def run_spawn_teammate(args: dict, ctx: ToolContext) -> str:
    manager = _manager(ctx)
    if manager is None:
        return "Error: teams are not enabled for this session"
    name = str(args.get("name", "")).strip()
    role = str(args.get("role", "")).strip() or "generalist"
    prompt = str(args.get("prompt", "")).strip()
    if not prompt:
        return "Error: prompt is required; the teammate cannot see this conversation"
    autonomous = args.get("autonomous")
    return manager.spawn(
        name,
        role,
        prompt,
        autonomous=True if autonomous is None else bool(autonomous),
    )


def run_request_plan(args: dict, ctx: ToolContext) -> str:
    manager = _manager(ctx)
    if manager is None:
        return "Error: teams are not enabled for this session"
    teammate = str(args.get("teammate", "")).strip()
    task = str(args.get("task", "")).strip()
    return manager.request_plan(teammate, task)


def run_review_plan(args: dict, ctx: ToolContext) -> str:
    manager = _manager(ctx)
    if manager is None:
        return "Error: teams are not enabled for this session"
    request_id = str(args.get("request_id", "")).strip()
    approve = args.get("approve")
    if not isinstance(approve, bool):
        return "Error: approve must be true or false"
    feedback = str(args.get("feedback", ""))
    return manager.review_plan(request_id, approve, feedback)


def run_pending_requests(args: dict, ctx: ToolContext) -> str:
    manager = _manager(ctx)
    if manager is None:
        return "Error: teams are not enabled for this session"
    return manager.pending_requests()


def run_request_shutdown(args: dict, ctx: ToolContext) -> str:
    manager = _manager(ctx)
    if manager is None:
        return "Error: teams are not enabled for this session"
    teammate = str(args.get("teammate", "")).strip()
    reason = str(args.get("reason", ""))
    return manager.shutdown(teammate, reason)


def run_create_worktree(args: dict, ctx: ToolContext) -> str:
    manager = _manager(ctx)
    if manager is None:
        return "Error: teams are not enabled for this session"
    name = str(args.get("name", "")).strip()
    task_id = str(args.get("task_id", "")).strip()
    if not name or not task_id:
        return "Error: name and task_id are required"
    return manager.worktrees.create(name, task_id)


def run_remove_worktree(args: dict, ctx: ToolContext) -> str:
    manager = _manager(ctx)
    if manager is None:
        return "Error: teams are not enabled for this session"
    name = str(args.get("name", "")).strip()
    if not name:
        return "Error: name is required"
    return manager.worktrees.remove(name, discard_changes=bool(args.get("discard_changes")))


def run_list_worktrees(args: dict, ctx: ToolContext) -> str:
    manager = _manager(ctx)
    if manager is None:
        return "Error: teams are not enabled for this session"
    entries = manager.worktrees.registered()
    assignments = manager.worktrees.active_assignments()
    lines = []
    for name, info in sorted(entries.items()):
        branch = info.get("branch", "")
        lines.append(f"- {name} ({branch})")
    for teammate, assignment in sorted(assignments.items()):
        lines.append(f"  leased to {teammate} for {assignment.task_id}")
    return "\n".join(lines) if lines else "No worktrees."


# --------------------------------------------------------------------------
# Teammate-side tools
# --------------------------------------------------------------------------


def run_submit_plan(args: dict, ctx: ToolContext) -> str:
    teammate = _teammate(ctx)
    if teammate is None:
        return "Error: submit_plan is only available to teammates"
    plan = str(args.get("plan", "")).strip()
    if not plan:
        return "Error: plan is required"
    return teammate.submit_plan(plan)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

SPAWN_TEAMMATE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Unique teammate name, e.g. 'researcher'."},
        "role": {"type": "string", "description": "Short role label, e.g. 'test author'."},
        "prompt": {
            "type": "string",
            "minLength": 1,
            "description": "Self-contained briefing. The teammate cannot see this conversation.",
        },
        "autonomous": {
            "type": "boolean",
            "description": "Whether it may claim tasks from the board on its own. Defaults to true.",
        },
    },
    "required": ["name", "prompt"],
}

REQUEST_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "teammate": {"type": "string"},
        "task": {"type": "string", "description": "What the teammate is about to do."},
    },
    "required": ["teammate", "task"],
}

REVIEW_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "request_id": {"type": "string", "description": "The id from the plan request."},
        "approve": {"type": "boolean"},
        "feedback": {"type": "string"},
    },
    "required": ["request_id", "approve"],
}

REQUEST_SHUTDOWN_SCHEMA = {
    "type": "object",
    "properties": {
        "teammate": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["teammate"],
}

CREATE_WORKTREE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Worktree name, e.g. 'parser-work'."},
        "task_id": {"type": "string", "description": "The task this worktree is bound to."},
    },
    "required": ["name", "task_id"],
}

REMOVE_WORKTREE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "discard_changes": {
            "type": "boolean",
            "description": "Remove even if the worktree has uncommitted or ignored files.",
        },
    },
    "required": ["name"],
}

SUBMIT_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {"type": "string", "minLength": 1, "description": "The steps you intend to take."}
    },
    "required": ["plan"],
}

_EMPTY_SCHEMA = {"type": "object", "properties": {}}


def register_lead_team_tools(registry: ToolRegistry) -> ToolRegistry:
    """Coordination tools for the lead agent."""
    registry.add(
        "spawn_teammate",
        "Start a persistent teammate with its own context, task claim, and mailbox. "
        "Use it when the work divides cleanly or would flood this conversation.",
        SPAWN_TEAMMATE_SCHEMA,
        run_spawn_teammate,
    )
    registry.add("list_teammates", "Show the team roster and their status.", _EMPTY_SCHEMA, run_list_teammates, read_only=True)
    registry.add("send_message", "Send a message to a teammate's mailbox.", SEND_MESSAGE_SCHEMA, run_send_message)
    registry.add(
        "request_plan",
        "Ask a teammate for a plan before it modifies files.",
        REQUEST_PLAN_SCHEMA,
        run_request_plan,
    )
    registry.add(
        "review_plan",
        "Approve or reject a submitted plan by request_id.",
        REVIEW_PLAN_SCHEMA,
        run_review_plan,
    )
    registry.add("pending_requests", "List outstanding plan and shutdown requests.", _EMPTY_SCHEMA, run_pending_requests, read_only=True)
    registry.add(
        "request_shutdown",
        "Ask a teammate to stop.",
        REQUEST_SHUTDOWN_SCHEMA,
        run_request_shutdown,
    )
    registry.add(
        "create_worktree",
        "Create a git worktree bound to a task so a teammate can edit in isolation.",
        CREATE_WORKTREE_SCHEMA,
        run_create_worktree,
    )
    registry.add(
        "remove_worktree",
        "Remove a worktree. Refuses while it is leased to unfinished work.",
        REMOVE_WORKTREE_SCHEMA,
        run_remove_worktree,
    )
    registry.add("list_worktrees", "List worktrees and their leases.", _EMPTY_SCHEMA, run_list_worktrees, read_only=True)
    return registry


def register_teammate_team_tools(registry: ToolRegistry) -> ToolRegistry:
    """Coordination tools a teammate may call."""
    registry.add(
        "submit_plan",
        "Submit a plan to the lead and wait for approval before modifying files.",
        SUBMIT_PLAN_SCHEMA,
        run_submit_plan,
    )
    registry.add("send_message", "Send a message to the lead or another teammate.", SEND_MESSAGE_SCHEMA, run_send_message)
    return registry
