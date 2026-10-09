"""The permission pipeline -- three gates before any tool runs.

    Gate 1  deny list    hard-coded, never negotiable, no prompt
    Gate 2  rules        context-dependent: escaping the workspace, destructive
                         shell, unvetted external tools
    Gate 3  approval     pause and ask the human -- or apply a non-interactive
                         policy when there is no human (teammates, tests)

The pipeline is exposed two ways, so it can be used with or without the hook
system:

    manager.check(tool, args, ctx) -> Decision        direct, used by tests
    manager.check_hook(block, ctx)   -> str | None     PreToolUse hook shape

Design note: this module is the *only* place that decides whether an action is
allowed.  Tools never re-implement policy; they just refuse to run when the
runtime tells them the call was denied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# --------------------------------------------------------------------------
# Gate 1: the deny list
# --------------------------------------------------------------------------

DENY_LIST: tuple[str, ...] = (
    "rm -rf /",
    "sudo",
    "shutdown",
    "reboot",
    "mkfs",
    "dd if=",
    "> /dev/sda",
    ":(){:|:&};:",
)

# Matches `rm` / `del` only in command position, so that `charm` or
# `firmware` never trip the rule.
#这个正则用来匹配命令位置的 rm 或 del。
DESTRUCTIVE_COMMAND_WORD = re.compile(r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])")

DESTRUCTIVE_COMMAND_KEYWORDS: tuple[str, ...] = (
    "rm ",
    "> /etc/",
    "chmod 777",
    "format ",
    "reg delete",
    "Remove-Item -Recurse -Force",
)


def contains_destructive_command(command: str) -> bool:
    """True when the command invokes rm/del in command position."""
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command or ""))


# --------------------------------------------------------------------------
# Decisions 决策
# --------------------------------------------------------------------------

ALLOW = "allow"
DENY = "deny"
ASK = "ask"


@dataclass
class Decision:
    """The verdict for one proposed tool call."""

    allowed: bool
    reason: str = ""
    gate: str = "ok"          # ok | deny_list | rule | approval
    ask: bool = False          # a human still needs to confirm
    allow_outside: bool = False  # the approval was specifically for path escape

    @property
    def denied(self) -> bool:
        return not self.allowed

    def as_tool_result(self) -> str:
        from .tools.result import ToolResult
        message = f"Permission denied: {self.reason}" if self.reason else "Permission denied."
        return ToolResult.failure("PERMISSION_DENIED", message)


ALLOWED = Decision(allowed=True)


@dataclass
class Rule:
    """Gate 2: a predicate over (tool_name, args) that requires action.

    `action` is ALLOW, DENY, or ASK:
      DENY  stop immediately, no prompt
      ASK   ask the human (or apply the non-interactive policy)
      ALLOW let it through, but mark `allow_outside` when asked to
    """

    name: str
    tools: Iterable[str] | None      # None means "any tool"
    predicate: Callable[[str, dict], bool]
    message: str
    action: str = ASK
    reason_is_path_escape: bool = False

    def applies(self, tool_name: str, args: dict) -> bool:
        if self.tools is not None and tool_name not in self.tools:
            return False
        try:
            return bool(self.predicate(tool_name, args))
        except Exception:  # a broken predicate must not crash the loop
            return False


# --------------------------------------------------------------------------
# Default rules
# --------------------------------------------------------------------------

PATH_TOOLS = ("read_file", "write_file", "edit_file", "glob", "grep")


def _escapes_workspace(tool_name: str, args: dict) -> bool:
    """True when any path-like argument resolves outside the cwd.

    The workspace root is read from the module-level `current_workdir` set by
    the manager for the duration of a check, which keeps the rule signature
    simple while still being per-call accurate inside worktrees.
    """
    root = _ACTIVE_WORKDIR[0] # 读取当前工作目录
    if root is None:
        return False
    for key in ("path", "file", "root", "directory"): # 检查参数里有没有 path、file、root、directory 这些键
        raw = args.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            candidate = (root / raw).resolve()
        except (OSError, ValueError, RuntimeError):
            return True
        if candidate != root and not candidate.is_relative_to(root):
            return True
    return False


def _destructive_shell(tool_name: str, args: dict) -> bool:
    command = args.get("command", "")
    if not isinstance(command, str):
        return False
    if contains_destructive_command(command):
        return True
    return any(keyword in command for keyword in DESTRUCTIVE_COMMAND_KEYWORDS)


_ACTIVE_WORKDIR: list[Path | None] = [None]


def default_rules() -> list[Rule]:
    """The rules every session starts with."""
    return [
        Rule(
            name="workspace-escape",
            tools=PATH_TOOLS,
            predicate=_escapes_workspace,
            message="path resolves outside the workspace",
            action=ASK,
            reason_is_path_escape=True,
        ),
        Rule(
            name="destructive-shell",
            tools=("bash",),
            predicate=_destructive_shell,
            message="potentially destructive shell command",
            action=ASK,
        ),
    ]


# --------------------------------------------------------------------------
# The manager
# --------------------------------------------------------------------------


class PermissionManager:
    """Runs the three gates and produces a `Decision`."""

    def __init__(
        self,
        *,
        workdir: Path,   # 工作根目录
        approval: str = ASK, # 没有人类时默认策略
        approver: Callable[[str, dict, str], bool] | None = None, #批准者
        deny_list: Iterable[str] = DENY_LIST,# 拒绝列表
        rules: Iterable[Rule] | None = None, # 规则列表
        external_policies: dict[str, str] | None = None, # MCP工具发每个工具策略
    ):
        """
        approval   default policy when a human is not available:
                   ALLOW / DENY / ASK.  ASK falls back to `approver`.
        approver   callable(tool_name, args, reason) -> bool.  When omitted and
                   policy is ASK, the interactive prompt is used if a TTY is
                   attached, otherwise the call is denied (fail closed).
        external_policies
                   per-tool policy for MCP tools, e.g. {"mcp__docs__search":
                   "allow", "mcp__deploy__trigger": "confirm"}.
        """
        self.workdir = Path(workdir).resolve()
        self.approval = approval
        self.approver = approver
        self.deny_list = tuple(deny_list)
        self.rules = list(default_rules() if rules is None else rules)
        self.external_policies = dict(external_policies or {})
        # Decisions recorded during this session, for the Stop summary.
        self.log: list[dict] = []

    # -- gate 1 -------------------------------------------------------------

    def check_deny_list(self, tool_name: str, args: dict) -> str | None:
        if tool_name != "bash":
            return None
        command = args.get("command", "")
        if not isinstance(command, str):
            return None
        for pattern in self.deny_list:
            if pattern in command:
                return f"'{pattern}' is on the deny list"
        return None

    # -- gate 2 -------------------------------------------------------------

    def check_rules(self, tool_name: str, args: dict) -> Rule | None:
        for rule in self.rules:
            if rule.applies(tool_name, args):
                return rule
        # External tools default to confirm unless host policy says otherwise.
        if tool_name.startswith("mcp__"):
            policy = self.external_policies.get(tool_name, "confirm")
            if policy != "allow":
                return Rule(
                    name="external-tool",
                    tools=None,
                    predicate=lambda *_: True,
                    message=f"external tool {tool_name} requires confirmation",
                    action=ASK,
                )
        return None

    # -- gate 3 -------------------------------------------------------------

    def _ask(self, tool_name: str, args: dict, reason: str, interactive: bool = True) -> bool:
        if self.approver is not None:
            return bool(self.approver(tool_name, args, reason))
        if not interactive: #区分主线程 和 后台线程
            # A teammate runs on its own thread.  Prompting a human from there
            # would deadlock the terminal, so it fails closed instead and the
            # teammate is expected to ask the lead over the mailbox.
            return self.approval == ALLOW
        if self.approval == ALLOW:
            return True
        if self.approval == DENY:
            return False
        return _interactive_prompt(tool_name, args, reason)

    # -- the pipeline -------------------------------------------------------

    def check(
        self,
        tool_name: str,
        args: dict,
        *,
        workdir: Path | None = None,
        interactive: bool = True,
    ) -> Decision:
        """Run all three gates and return the verdict."""
        _ACTIVE_WORKDIR[0] = Path(workdir).resolve() if workdir else self.workdir
        try:
            decision = self._check_inner(tool_name, args, interactive)
        finally:
            _ACTIVE_WORKDIR[0] = None
        self.log.append(
            {
                "tool": tool_name,
                "gate": decision.gate,
                "allowed": decision.allowed,
                "reason": decision.reason,
            }
        )
        return decision

    def _check_inner(self, tool_name: str, args: dict, interactive: bool = True) -> Decision:
        # Gate 1 -- no prompt, no override.
        reason = self.check_deny_list(tool_name, args)
        if reason:
            return Decision(allowed=False, reason=reason, gate="deny_list")

        # Gate 2 -- match a rule.
        rule = self.check_rules(tool_name, args)
        if rule is None:
            return ALLOWED

        if rule.action == DENY:
            return Decision(allowed=False, reason=rule.message, gate="rule")

        if rule.action == ALLOW:
            return Decision(
                allowed=True,
                reason=rule.message,
                gate="rule",
                allow_outside=rule.reason_is_path_escape,
            )

        # Gate 3 -- ask.
        if self._ask(tool_name, args, rule.message, interactive):
            return Decision(
                allowed=True,
                reason=rule.message,
                gate="approval",
                ask=True,
                allow_outside=rule.reason_is_path_escape,
            )
        return Decision(allowed=False, reason=rule.message, gate="approval", ask=True)

    # -- hook adapter -------------------------------------------------------

    def check_hook(self, block: Any, ctx: Any = None) -> str | None:
        """PreToolUse hook: return a denial string to veto the call.

        On approval this also stamps `ctx.extra["allow_outside"]`, which is how
        an approved out-of-workspace write gets past `safe_path`.
        """
        from .llm import block_input, block_name

        tool_name = block_name(block)
        args = block_input(block)
        workdir = getattr(ctx, "workdir", None)
        interactive = bool(getattr(ctx, "interactive", True))
        decision = self.check(tool_name, args, workdir=workdir, interactive=interactive)

        if decision.allowed:
            if decision.allow_outside and ctx is not None:
                extra = getattr(ctx, "extra", None)
                if isinstance(extra, dict):
                    extra["allow_outside"] = True
            return None
        return decision.as_tool_result()

    # -- introspection ------------------------------------------------------

    def summary(self) -> str:
        if not self.log:
            return "permissions: no tool calls attempted"
        denied = sum(1 for entry in self.log if not entry["allowed"])
        asked = sum(1 for entry in self.log if entry["gate"] == "approval")
        return (
            f"permissions: {len(self.log)} calls, {asked} required approval, "
            f"{denied} denied"
        )


def _interactive_prompt(tool_name: str, args: dict, reason: str) -> bool:
    """Ask the human on the terminal.  Fails closed without a TTY."""
    import sys

    if not (sys.stdin and sys.stdin.isatty()):
        return False
    print(f"\n\033[33m[permission] {reason}\033[0m")
    print(f"   Tool: {tool_name}({_preview(args)})")
    try:
        choice = input("   Allow? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return choice in ("y", "yes")


def _preview(args: dict, limit: int = 160) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:3])
    return text[:limit] + ("..." if len(text) > limit else "")
