"""Runtime -- the one place every mechanism is wired together.

The loop is constant.  What changes is which collaborators it is handed.  This
module builds the tool pool, installs the hooks, and owns the session state, so
that `cli.py` can stay a REPL and `loop.py` can stay a loop.

    Runtime.create(settings)
        |
        +-- llm            provider or offline mock
        +-- hooks          PreToolUse/PostToolUse/Stop wiring
        +-- permissions    the three-gate pipeline, installed as a hook
        +-- tools          basic + todo + skill + task + subagent + mcp + team
        +-- skills         catalog in the prompt, bodies on demand
        +-- todos          the session plan
        +-- tasks          the file-backed task graph
        +-- memory         selection, extraction, consolidation
        +-- compactor      five-stage context compaction
        +-- mcp            external capability routed into the same pool
        +-- teams          mailbox, protocols, worktrees, teammates

Reactive compaction lives inside the loop; memory extraction lives on the Stop
hook; team traffic is injected by `before_call`.  None of them required the
loop to grow a special case beyond the hooks it already had.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import Settings, load_settings
from .context import ContextCompactor, register_compaction_tools
from .diagnostics import ObservedLLM, RunTrace
from .events import (
    POST_TOOL_USE,
    PRE_TOOL_USE,
    STOP,
    USER_PROMPT_SUBMIT,
    Hooks,
    StopDirective,
)
from .goal import GoalController, GoalEvaluator, PromptGoalEvaluator
from .llm import LLMClient, SwitchableLLM, build_client
from .loop import LoopResult, run_loop
from .mcp import MCPManager, register_mcp_tools
from .memory import MemoryStore
from .permissions import ALLOW, ASK, DENY, FULL, READ_ONLY, WORKSPACE, PERMISSION_MODES, PermissionManager
from .prompt import build_system_prompt
from .skills import SkillLoader, register_skill_tools
from .subagent import build_subagent_registry, register_subagent_tools, run_subagent_loop
from .tasks import COMPLETED, TaskStore, register_task_tools
from .team_tools import TeammateToolRuntime, register_lead_team_tools
from .teams import PLAN_GATED_TOOLS, TeamManager
from .todo import TodoList, register_todo_tools
from .tools.basic import register_basic_tools, shell_hint
from .tools.registry import ToolContext, ToolRegistry
from .ui_events import UIEvent
from .sandbox import DockerSandbox
MASKED_TOOL_OUTPUT_WARN = 100_000


@dataclass  # 计数器，不控制流程
class RuntimeStats:
    turns: int = 0     # 统计主agent的模型循环轮数
    tool_calls: int = 0 # 统计工具调用次数
    requests: int = 0  # requests统计submit()收到多少用户请求
    denied: int = 0


class Runtime:
    """A fully assembled harness session."""

    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        *,
        approval: str = ASK,
        approver: Callable[[str, dict, str], bool] | None = None,
        teams_enabled: bool = True,
        memory_enabled: bool = True,
        compaction_enabled: bool = True,
        planner_enabled: bool = True,
        require_plan: bool = True,
        verbose: bool = False,
        on_event: Callable[[str], None] | None = None,
        on_progress: Callable[[str], None] | None = None,
        goal_evaluator: GoalEvaluator | None = None,
        diagnostics_enabled: bool = True,
        on_ui_event: Callable[[UIEvent], None] | None = None,
        sandbox_enabled: bool = False,
        permission_mode: str | None = None,
    ):
        self.settings = settings
        self.llm = llm = SwitchableLLM(llm)
        self.verbose = verbose
        self.on_event = on_event
        self.on_progress = on_progress
        self.on_ui_event = on_ui_event
        self.cancel_event = None
        self.stats = RuntimeStats()
        self.notes: list[str] = []
        if permission_mode is not None and permission_mode not in PERMISSION_MODES:
            raise ValueError(f"Unknown permission mode: {permission_mode}")
        if permission_mode == FULL and sandbox_enabled:
            raise ValueError("完全权限在宿主机执行，不能同时启用 Docker 沙箱")
        # None preserves the pre-mode Python API; CLI always supplies a mode.
        self.permission_mode = permission_mode
        self.sandbox_enabled = (permission_mode != FULL if permission_mode is not None else sandbox_enabled)
        if self.sandbox_enabled and teams_enabled:
            raise ValueError("当前 Docker 沙箱尚不支持团队工作树，请使用 --no-teams")
        self.sandbox: DockerSandbox | None=None
        settings.ensure_dirs()
        self.diagnostics = (RunTrace(settings.state_dir / "runs", provider=settings.provider,
                                     model=settings.model) if diagnostics_enabled else None)

        # -- the tool pool --------------------------------------------------
        # 工具注册
        self.tools = ToolRegistry()
        register_basic_tools(self.tools,sandbox_enabled=self.sandbox_enabled)
        register_todo_tools(self.tools)
        register_skill_tools(self.tools)
        register_task_tools(self.tools)
        register_subagent_tools(self.tools)
        register_mcp_tools(self.tools)
        if compaction_enabled:
            # Lets the model ask for room itself, rather than waiting for the
            # threshold.  Useless without a compactor, so it is not offered.
            register_compaction_tools(self.tools)
        if teams_enabled:
            register_lead_team_tools(self.tools)

        # -- stateful mechanisms -------------------------------------------
        self.skills = SkillLoader(settings.skill_dirs)  # 创建共享服务
        self.todos = TodoList()
        self.tasks = TaskStore(settings.tasks_dir)
        self.memory = MemoryStore(
            ObservedLLM(llm, self.diagnostics, "memory") if self.diagnostics else llm,
            settings.model,
            settings.memory_dir,
            settings.workdir,
            enabled=memory_enabled,
            verbose=verbose,
        )
        self.compactor = ContextCompactor(
            ObservedLLM(llm, self.diagnostics, "summary") if self.diagnostics else llm,
            settings.model,
            settings.transcripts_dir,
            settings.state_dir / "tool-results",
            enabled=compaction_enabled,
            verbose=verbose,
        )
        if goal_evaluator is None:
            judge_settings = replace(settings, model=settings.goal_evaluator_model or settings.model)
            judge_client = build_client(judge_settings)
            if self.diagnostics:
                judge_client = ObservedLLM(judge_client, self.diagnostics, "goal_evaluation")
            goal_evaluator = PromptGoalEvaluator(judge_client)
        self.goal = GoalController(goal_evaluator, settings.goal_block_cap)
        self.mcp = MCPManager(
            retry_safe_tools={("docs", "search"), ("docs", "get_version"), ("docs", "list_topics")},
            policy={("docs", "search"): "allow", ("docs", "get_version"): "allow",
                    ("docs", "list_topics"): "allow", ("deploy", "trigger"): "confirm"},
            verbose=verbose,
        )

        # -- permissions ----------------------------------------------------
        self.permissions = PermissionManager(
            workdir=settings.workdir,
            approval=approval,
            approver=approver,
            sandbox_enabled=self.sandbox_enabled,
            mode=permission_mode or WORKSPACE,
            tool_lookup=self.tools.get,
        )

        # -- hooks ----------------------------------------------------------
        self.hooks = Hooks()
        self._install_hooks() # 会把权限检查等函数挂到正确的实际
        # 创建主agent的消息和ToolContext
        # -- lead context ---------------------------------------------------
        self.messages: list[dict] = []
        self.active_request = ""
        #: Memory selection costs a model call, so it is cached for the length
        #: of one user turn.  `build_system` runs before *every* model call,
        #: and selection must not run that often.
        self._memory_sections: list[str] | None = None
        self.lead_ctx = ToolContext(
            settings=settings,
            workdir=settings.workdir,
            owner="lead",
            interactive=True,
            runtime=self,
        )
        if self.diagnostics:
            self.lead_ctx.extra["diagnostics"] = self.diagnostics

        # -- teams (needs the lead context above to exist first) ------------
        self.teams: TeamManager | None = None
        if teams_enabled:
            self.teams = TeamManager(
                workspace=settings.workdir,
                mailbox_dir=settings.mailbox_dir,
                worktrees_dir=settings.worktrees_dir,
                tasks=self.tasks,  # lead和teammate使用同一个任务板
                registry=self._teammate_registry(), # 给teammate一套专门的工具
                llm=llm, #
                settings=settings,
                hooks_factory=self._teammate_hooks,
                runner=self.loop_runner, #
                require_plan=require_plan,
                verbose=verbose,
                on_event=self._emit,
            )
            self.teams.tool_runtime = TeammateToolRuntime(
                teams=self.teams,
                tasks=self.tasks,
                teammate=None,  # replaced per teammate by for_teammate()
                skills=self.skills,
                mcp=self.mcp,
                settings=settings,
            )

        self._emit(f"runtime ready: {settings.describe()}")

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    # 先准备配置和模型
    @classmethod
    def create(
        cls,
        settings: Settings | None = None,
        *,
        script: list[Any] | None = None,
        responder: Callable[[int, list[dict], list[dict]], Any] | None = None,
        env_file: str | Path | None = None,
        sandbox_enabled: bool = False,
        **overrides: Any
    ) -> "Runtime":
        """Build a runtime from the environment, or from explicit overrides.

        `script` and `responder` only affect the offline mock provider; they are
        how the test suite drives a deterministic conversation.
        """
        if settings is None:
            settings = load_settings(env_file, **overrides)
        approval = overrides.pop("approval", None) or os.getenv("AGENT_APPROVAL", ASK)
        if approval not in (ALLOW, ASK, DENY):
            approval = ASK
        llm = build_client(settings, script=script, responder=responder)
        # `script` and settings overrides are not Runtime concerns.
        for key in ("provider", "model", "base_url", "api_key", "workdir", "state_dir", "skill_dirs", "goal_evaluator_model", "goal_block_cap"):
            overrides.pop(key, None)
        return cls(settings, llm, approval=approval, **overrides,sandbox_enabled=sandbox_enabled)

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def _install_hooks(self) -> None:
        self.hooks.register(PRE_TOOL_USE, self.permissions.check_hook)
        self.hooks.register(PRE_TOOL_USE, self._log_tool_hook)
        self.hooks.register(POST_TOOL_USE, self._large_output_hook)
        self.hooks.register(STOP, self._goal_stop_hook)
        self.hooks.register(STOP, self._memory_stop_hook)
        self.hooks.register(USER_PROMPT_SUBMIT, self._user_prompt_hook)

    def _teammate_hooks(self) -> Hooks:
        """A fresh hook set for a teammate thread.

        It gets the plan gate, permission enforcement, and logging, but no
        memory extraction: teammates do not own the session's long-term memory.
        """
        hooks = Hooks()
        # The plan gate runs first, so a gated teammate is told "submit a plan"
        # rather than being sent down the permission path for an action it is
        # not allowed to take at all yet.
        hooks.register(PRE_TOOL_USE, self._plan_gate_hook)
        hooks.register(PRE_TOOL_USE, self.permissions.check_hook)
        if self.verbose:
            hooks.register(PRE_TOOL_USE, self._log_tool_hook)
        return hooks

    def _plan_gate_hook(self, block: Any, ctx: ToolContext | None = None) -> str | None:
        """Block a teammate's workspace writes until its plan is approved.

        Reading stays open: a teammate has to be able to investigate the
        repository in order to write a plan worth approving.  The gate is keyed
        off `ctx.owner`, which is the teammate's name, so one hook serves the
        whole roster.
        """
        if self.teams is None or ctx is None:
            return None
        owner = getattr(ctx, "owner", "")
        if not owner or owner == self.teams.lead_name:
            return None

        from .llm import block_name

        tool_name = block_name(block)
        if tool_name not in PLAN_GATED_TOOLS:
            return None

        protocol = self.teams.protocol
        if protocol.plan_approved(owner):
            return None

        gate = protocol.plan_gates.get(owner, "not_required")
        return (
            f"Blocked: plan status is {gate}. Submit a plan with submit_plan and "
            "wait for the lead's approval before changing the workspace."
        )

    def _log_tool_hook(self, block: Any, ctx: ToolContext | None = None) -> None:
        if not self.verbose:
            return None
        from .llm import block_input, block_name

        args = block_input(block)
        preview = ", ".join(f"{key}={value!r}" for key, value in args.items())
        if len(preview) > 160:
            preview = preview[:160] + "..."
        print(f"\033[90m[hook] {block_name(block)}({preview})\033[0m")
        return None

    def _large_output_hook(self, block: Any, output: str, ctx: ToolContext | None = None) -> None:
        if self.verbose and len(str(output)) > MASKED_TOOL_OUTPUT_WARN:
            from .llm import block_name

            print(f"\033[33m[hook] large output from {block_name(block)}: {len(str(output))} chars\033[0m")
        return None

    def _user_prompt_hook(self, query: str) -> None:
        return None

    def _goal_stop_hook(self, messages: list[dict]) -> StopDirective | None:
        # One-off subagents inherit these hooks, but never own the lead goal.
        if messages is not self.messages or not self.goal.active:
            return None
        outcome = self.goal.judge(messages, background_running=self._goal_background_running())
        if outcome in ("allow", "achieved"):
            return None
        if outcome == "continue":
            return StopDirective(
                "continue",
                "[goal check] The condition is not met yet: " + self.goal.reason
                + "\nContinue work and gather verifiable evidence before stopping.",
            )
        return StopDirective("return", self.goal.reason, f"goal_{outcome}")

    def _goal_background_running(self) -> bool:
        if self.teams is None:
            return False
        owners = {name for name, teammate in self.teams.teammates.items() if teammate.alive}
        return any(task.owner in owners and task.status != COMPLETED for task in self.tasks.list_all())

    def _memory_stop_hook(self, messages: list) -> None:
        """Runs when the model proposes to stop: harvest durable knowledge."""
        if not self.memory.enabled:
            return None
        try:
            if self.memory.extract(messages):
                self.memory.consolidate()
        except Exception as exc:  # noqa: BLE001 - memory must never break a turn
            self._emit(f"memory failure: {type(exc).__name__}: {exc}")
        return None

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    def build_system(self) -> str:
        roster = self.teams.roster() if self.teams else ""

        # Rebuilt per call so the roster and plan stay fresh -- but memory
        # selection is cached, because it is the only part that costs a model
        # call and its inputs do not change within a turn.
        memory_sections: list[str] = []
        if self.memory.enabled:
            if self._memory_sections is None:
                try:
                    self._memory_sections = self.memory.system_sections(self.messages)
                except Exception as exc:  # noqa: BLE001
                    self._emit(f"memory selection failed: {type(exc).__name__}: {exc}")
                    self._memory_sections = []
            memory_sections = self._memory_sections

        prompt = build_system_prompt(
            workdir=self.settings.workdir,
            tool_names=self.tools.names(),
            shell="/bin/bash in Docker" if self.sandbox_enabled else shell_hint(),
            sandbox_enabled=self.sandbox_enabled,
            skill_catalog=self.skills.catalog(),
            memory_sections=memory_sections,
            mcp_servers=self.mcp.names(),
            team_roster=roster,
            teams_enabled=self.teams is not None,
            team_requires_plan=self.teams.require_plan if self.teams else False,
            team_worktrees_enabled=self.teams.worktrees.enabled if self.teams else False,
            # Keep actionable plan text visible even after old tool results are archived.
            todo_summary=self.todos.render() if self.todos.items else "",
        )
        if self.permission_mode:
            prompt += (
                f"\n\nPermission mode: {self.permission_mode}. "
                "In read-only mode only inspection and todo bookkeeping are permitted by default; "
                "ordinary shell commands and mutations are blocked. In workspace mode bash runs in Docker; "
                "file tools run on the host with workspace path checks. In full mode all basic tools run on the host. "
                "For one exceptional unrestricted call in read-only/workspace mode, set execution_mode='host' "
                "and provide justification; a human must approve that exact invocation. "
                "Never repeat a denied request unchanged."
            )
        if self.goal.active:
            prompt += (
                "\n\nActive goal condition: " + self.goal.condition
                + "\nKeep working until evidence shows this condition is met. "
                "A separate judge checks the transcript when you stop."
            )
        return prompt

    # ------------------------------------------------------------------
    # MCP
    # ------------------------------------------------------------------

    def refresh_mcp_tools(self) -> None:
        """Re-assemble the pool so newly connected MCP tools become callable.

        The loop re-reads `registry.definitions()` before every model call, so
        a tool registered here is offered to the model on the very next
        iteration of the same turn -- no restart required.
        """
        reserved = [
            name
            for name, tool in self.tools.tools().items()
            if not tool.source.startswith("mcp__")
        ]
        try:
            tools, policies = self.mcp.assemble_tool_pool(reserved_names=reserved)
        except Exception as exc:  # noqa: BLE001 - a bad server must not break the turn
            self._emit(f"MCP assembly failed: {type(exc).__name__}: {exc}")
            return
        # Assembly raised before this point, so a failure leaves the pool intact.
        for name in [n for n, t in self.tools.tools().items() if t.source.startswith("mcp__")]:
            self.tools.unregister(name)
        for name, tool in tools.items():
            self.tools.register(tool, replace=True)
        self.permissions.external_policies.update(policies)
        self._emit(f"tool pool now has {len(self.tools)} tools")

    # ------------------------------------------------------------------
    # Tool execution and delegation
    # ------------------------------------------------------------------

    def execute_tool(self, block: Any) -> str:
        from .loop import execute_tool

        return execute_tool(block, self.tools, self.lead_ctx, self.hooks)

    def spawn_subagent(self, prompt: str, ctx: ToolContext | None = None) -> str:
        """The `task` tool's implementation: a nested loop, fresh context."""
        parent = ctx or self.lead_ctx
        registry = build_subagent_registry(self.tools)
        self._emit(f"subagent: {prompt[:80]}")
        result = run_subagent_loop(
            llm=self.llm,
            registry=registry,
            prompt=prompt,
            ctx=parent.child(owner=f"{parent.owner}/sub"),
            max_turns=self.settings.subagent_max_turns,
            hooks=self.hooks,
            on_event=self._emit,
            on_progress=self.on_progress,
        )
        self.stats.tool_calls += 1
        return result

    def loop_runner(
        self,
        *,
        llm: LLMClient,
        registry: ToolRegistry,
        messages: list[dict],
        system: Any,
        ctx: ToolContext,
        hooks: Hooks,
        max_turns: int,
        on_event: Callable[[str], None] | None = None,
    ) -> str:
        """The callable teammates use to run their own turns."""
        if self.diagnostics:
            ctx.extra["diagnostics"] = self.diagnostics
        if self.on_ui_event:
            ctx.extra["ui_events"] = self.on_ui_event
        if self.cancel_event is not None:
            ctx.extra["cancel_event"] = self.cancel_event
        result = run_loop(
            llm=llm,
            registry=registry,
            messages=messages,
            system=system,
            ctx=ctx,
            hooks=hooks,
            max_turns=max_turns,
            on_event=on_event,
        )
        return result.text if result.ok else f"(turn ended: {result.stop_reason}: {result.error})"

    def _teammate_registry(self) -> ToolRegistry:
        """The tool pool a teammate gets.

        Division of authority is enforced by *which tools exist*, not by runtime
        checks: a teammate can work the board (`list_tasks`, `claim_task`,
        `complete_task`) but cannot mint tasks, change dependencies, or create
        and destroy worktrees.  It also cannot spawn anything, so recursion is
        structurally impossible rather than merely discouraged.
        """
        from .team_tools import register_teammate_team_tools
        from .tools.basic import register_basic_tools as _basic

        registry = ToolRegistry()
        _basic(registry)
        register_todo_tools(registry)
        register_task_tools(registry)
        register_teammate_team_tools(registry)
        for lead_only in ("create_task", "update_task", "get_task"):
            registry.unregister(lead_only)
        return registry

    # ------------------------------------------------------------------
    # Message injection
    # ------------------------------------------------------------------

    @staticmethod
    def _inject(messages: list[dict], text: str) -> None:
        """Append harness-authored text without breaking role alternation.

        A `user` turn may carry both tool results and text, so merging is both
        legal and cheaper than a second message.
        """
        if not messages:
            messages.append({"role": "user", "content": text})
            return
        last = messages[-1]
        if last.get("role") == "user":
            content = last.get("content")
            if isinstance(content, list):
                content.append({"type": "text", "text": text})
                return
            if isinstance(content, str):
                last["content"] = f"{content}\n\n{text}"
                return
        messages.append({"role": "user", "content": text})

    def _inject_team_events(self, messages: list[dict]) -> None:
        if self.teams is None:
            return
        try:
            events = self.teams.consume_lead_inbox()
        except Exception as exc:  # noqa: BLE001
            self._emit(f"mailbox failure: {type(exc).__name__}: {exc}")
            return
        if events:
            self._inject(messages, self.teams.format_events(events))

    # ------------------------------------------------------------------
    # The turn
    # ------------------------------------------------------------------

    def run_turn(self, messages: list[dict] | None = None) -> LoopResult:
        """Run the loop to completion for the current messages."""
        target = self.messages if messages is None else messages
        if self.on_ui_event:
            self.lead_ctx.extra["ui_events"] = self.on_ui_event
        if self.cancel_event is not None:
            self.lead_ctx.extra["cancel_event"] = self.cancel_event
        result = run_loop(
            llm=self.llm,
            registry=self.tools,
            messages=target,
            system=self.build_system,
            ctx=self.lead_ctx,
            hooks=self.hooks,
            max_turns=self.settings.max_turns,
            compactor=self.compactor,
            active_request=self.active_request,
            before_call=self._inject_team_events,
            on_event=self._emit,
            on_progress=self.on_progress,
            on_diagnostic=self._emit,
        )
        self.stats.turns += result.turns
        self.stats.tool_calls += result.tool_calls
        return result

    def submit(self, request: str) -> str:
        """Handle one user request end to end and return the final text."""
        stripped = request.strip()
        if stripped == "/goal" or stripped.startswith("/goal ") or stripped == ":goal" or stripped.startswith(":goal "):
            return self.goal_command(stripped[5:].strip())
        self.stats.requests += 1
        goal_was_active = self.goal.active
        self.goal.begin_query()
        self.active_request = request
        # A new turn means new inputs: re-select memory exactly once here.
        self._memory_sections = None
        self.hooks.trigger(USER_PROMPT_SUBMIT, request)
        self.messages.append({"role": "user", "content": request})
        result = self.run_turn()
        self._collect_notes()
        return self._present_result(result, goal_was_active=goal_was_active)

    def goal_command(self, argument: str = "") -> str:
        """Show, replace, clear, or immediately pursue the session goal."""
        argument = argument.strip()
        if not argument:
            return self.goal.summary()
        if argument.lower() in ("clear", "off", "cancel"):
            self.goal.clear()
            return "Goal cleared."
        self.goal.set(argument)
        return self.submit(argument)

    def _present_result(self, result: LoopResult, *, goal_was_active: bool = False) -> str:
        if result.stop_reason.startswith("goal_"):
            return f"[goal: {result.stop_reason[5:]}] {result.error}" + (f"\n\n{result.text}" if result.text else "")
        if goal_was_active and self.goal.status == "achieved":
            return f"[goal: achieved] {self.goal.reason}" + (f"\n\n{result.text}" if result.text else "")
        if self.goal.active and not result.ok:
            return f"[goal: active] {result.stop_reason}: {result.error}" + (f"\n\n{result.text}" if result.text and result.text != result.error else "")
        return result.text or f"({result.stop_reason}: {result.error})"

    def _collect_notes(self) -> None:
        self.notes.extend(self.compactor.drain_notes())
        self.notes.extend(self.memory.drain_notes())
        self.notes.extend(self.mcp.drain_notes())
        for note in self.notes:
            self._emit(note)
        self.notes = []

    # ------------------------------------------------------------------
    # Teammates
    # ------------------------------------------------------------------

    def wait_for_teammates(
        self, poll: float = 0.25, timeout: float = 120.0, initial_answer: str = ""
    ) -> str:
        """Finish a one-shot team request before the CLI closes the session.

        The polling deadline does not reset when messages arrive. A teammate
        may retire without completing its task, so task status is checked too.
        """
        if self.teams is None or not self.teams.teammates:
            return initial_answer

        deadline = time.monotonic() + max(0.0, timeout)
        answer = initial_answer
        while True:
            if self.cancel_event is not None and self.cancel_event.is_set():
                return answer
            events = self.teams.consume_lead_inbox()
            if events:
                self._inject(self.messages, self.teams.format_events(events))
                goal_was_active = self.goal.active
                result = self.run_turn()
                answer = self._present_result(result, goal_was_active=goal_was_active)

            tasks = self.tasks.list_all()
            unfinished = [task for task in tasks if task.status != COMPLETED]
            alive = [teammate for teammate in self.teams.teammates.values() if teammate.alive]
            teammate_names = set(self.teams.teammates)
            team_tasks = [task for task in tasks if task.owner in teammate_names]

            if not alive or (team_tasks and not unfinished):
                if unfinished:
                    names = ", ".join(task.id for task in unfinished)
                    return f"{answer}\n\n[team] Unfinished tasks: {names}."
                self._inject(
                    self.messages,
                    "[team] Teammate work has ended. Summarize the completed work "
                    "for the user; do not claim any unverified result.",
                )
                goal_was_active = self.goal.active
                result = self.run_turn()
                return self._present_result(result, goal_was_active=goal_was_active) if result.text or result.stop_reason.startswith("goal_") else answer

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                names = ", ".join(task.id for task in unfinished) or "unknown"
                return f"{answer}\n\n[team] Timed out waiting for teammates; unfinished tasks: {names}."
            time.sleep(min(max(0.01, poll), remaining))

    def summon_report(self) -> str:
        if self.teams is None:
            return "Teams are disabled."
        return self.teams.roster()

    # ------------------------------------------------------------------
    # Diagnostics and teardown
    # ------------------------------------------------------------------

    def status(self) -> str:
        lines = [
            f"settings:   {self.settings.describe()}",
            f"tools:      {len(self.tools)} ({', '.join(self.tools.names())})",
            f"skills:     {len(self.skills.skills)}",
            f"tasks:      {len(self.tasks.list_all())}",
            f"memories:   {len(self.memory.list_records())}",
            f"mcp:        {', '.join(self.mcp.names()) or 'none'}",
            f"turns:      {self.stats.turns}, tool calls: {self.stats.tool_calls}",
            f"permissions:{self.permissions.summary()}",
            f"permission mode: {self.permission_mode or 'legacy API (application checks only unless sandbox enabled)'}",
            f"goal:       {self.goal.summary()}",
            f"features:   teams={'on' if self.teams is not None else 'off'}, "
            f"memory={'on' if self.memory.enabled else 'off'}",
            f"trace:      {self.diagnostics.events_path if self.diagnostics else 'disabled'}",
        ]
        if self.teams is not None:
            lines.append(f"team:       {len(self.teams.teammates)} teammate(s)")
        return "\n".join(lines)
#----------------------------------------------------------------------
# sandbox
#----------------------------------------------------------------------
    def get_sandbox(self,workdir: Path)->DockerSandbox:
        """获取当前工作区的沙箱，第一次使用时创建"""
        if not self.sandbox_enabled:
            raise RuntimeError("当前没有开启沙箱")
        # 第一版只支持Runtime的固定工作区
        # 队友切换到其他worktree时，需要单独分配容器
        if Path(workdir).resolve()!=self.settings.workdir.resolve():
            raise ValueError("当前沙箱尚不支持切换工作目录")
        if self.sandbox is None:
            sandbox=DockerSandbox(str(workdir), read_only=self.permission_mode == READ_ONLY)
            sandbox.start()
            self.sandbox=sandbox
        return self.sandbox

    def change_permission_mode(self, mode: str) -> None:
        """Execution-thread only: call between tools, never during a handler/approval."""
        if mode not in PERMISSION_MODES:
            raise ValueError("无效的权限模式")
        if mode != FULL and self.teams is not None:
            raise ValueError("当前 Docker 沙箱尚不支持团队工作树")
        if mode == self.permission_mode:
            return
        # A previous RW mount must never survive a switch to read-only.
        if self.sandbox is not None:
            self.sandbox.close()
            self.sandbox = None
        self.permission_mode = self.permissions.mode = mode
        self.sandbox_enabled = self.permissions.sandbox_enabled = mode != FULL
        for key in ("allow_outside", "host_execution_approved"):
            self.lead_ctx.extra.pop(key, None)
        descriptions = ToolRegistry()
        register_basic_tools(descriptions, sandbox_enabled=self.sandbox_enabled)
        self.tools.get("bash").description = descriptions.get("bash").description

    def change_model(self, client: LLMClient, *, provider: str, model: str) -> None:
        """Execution-thread only: retain messages, tools and all diagnostic counters."""
        self.llm.client = client
        self.settings.provider, self.settings.model = provider, model
        self.compactor.model = self.memory.model = model
        if not self.settings.goal_evaluator_model and isinstance(self.goal.evaluator, PromptGoalEvaluator):
            self.goal.evaluator.llm = (ObservedLLM(self.llm, self.diagnostics, "goal_evaluation")
                                       if self.diagnostics else self.llm)

    def close(self) -> None:
        try:
            if self.teams is not None:
                self.teams.stop_all()
            self.mcp.close_all()
        finally:
            try:
                if self.sandbox is not None:
                    self.sandbox.close()
            finally:
                if self.diagnostics:
                    self.diagnostics.flush()

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------

    def _emit(self, text: str) -> None:
        if self.on_event:
            self.on_event(text)
