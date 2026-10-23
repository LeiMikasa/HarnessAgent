"""Conservative execution checks, scoped to one model loop.

This recognizes common direct test runners, not arbitrary shell scripts. It is
an efficiency guard, never a permission boundary or a proof of task relevance.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from collections import OrderedDict
from pathlib import Path

from .feedback import _command_kinds
from .result import ToolResult, tool_error
from .search import IGNORED_DIRECTORIES


def command_parts(command: str) -> list[tuple[list[str], str]]:
    try:
        lexer = shlex.shlex(command, posix=False, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return []
    parts, current = [], []
    for token in tokens:
        if token in {";", "&&", "||", "|", "&", "(", ")"}:
            if current:
                parts.append((current, token))
            current = []
        else:
            current.append(token)
    if current:
        parts.append((current, ""))
    return parts


def _runner(tokens: list[str]) -> list[str]:
    tokens = [token.strip("\"'") for token in tokens]
    # Shell output destinations, including fd duplication, do not change a
    # test's identity. Do not strip options or test selectors.
    clean, index = [], 0
    while index < len(tokens):
        token = tokens[index]
        if token in {">", ">>", "<", ">&", "<&", "&>"}:
            if clean and clean[-1].isdigit():
                clean.pop()
            index += 2
            continue
        clean.append(token)
        index += 1
    # Assignments affect the environment and MUST remain in the identity.
    prefix = []
    while clean and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", clean[0]):
        prefix.append(clean.pop(0))
    if clean and clean[0] == "timeout":
        clean.pop(0)
        if clean and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", clean[0]):
            clean.pop(0)
        else:
            return []  # Unsupported timeout options: do not guess.
    return prefix + clean


def _kinds(tokens: list[str]) -> set[str]:
    runner = _runner(tokens)
    while runner and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", runner[0]):
        runner.pop(0)
    return _command_kinds(" ".join(runner))


def check_identity(command: str) -> str | None:
    """Normalize only known wrappers; ambiguous compound commands fail open."""
    directory, checks = ".", []
    for tokens, separator in command_parts(command):
        runner = _runner(tokens)
        if not runner:
            return None
        if runner[0] == "cd" and len(runner) == 2 and separator == "&&":
            directory = directory + "/" + runner[1]
        elif _kinds(tokens) - {"dependency-install"}:
            checks.append((directory, runner))
        elif runner[0] not in {"echo", "tail", "head", "mkdir"}:
            return None
    return json.dumps(checks, ensure_ascii=False) if checks else None


def execution_check(args: dict) -> ToolResult | None:
    command = args.get("command", "")
    kinds = _command_kinds(command)
    # The labels also recognize runners behind timeout and env assignments.
    parts = command_parts(command)
    kinds |= set().union(*(_kinds(tokens)
                           for tokens, _ in parts))
    if not kinds:
        return None
    if not str(args.get("task_reason", "")).strip():
        return tool_error(
            "TASK_REASON_REQUIRED",
            "Explain in task_reason why this test, build or dependency installation is necessary "
            "for the user's current request. For an overview/explanation, finish from inspected "
            "code and disclose unverified behavior instead of expanding into environment repair.",
            field="task_reason", action="correct_arguments")
    for index, (tokens, separator) in enumerate(parts):
        if _kinds(tokens) - {"dependency-install"}:
            if any(sep in {";", "||", "|", "&"} for _, sep in parts[index:]):
                return tool_error(
                    "CHECK_STATUS_MASKED",
                    "Run the check as a direct command; a pipeline, fallback or following ';' "
                    "can hide its exit status. Check output is saved automatically; do not rerun "
                    "a suite just to obtain a log. Read the saved log with file tools.",
                    field="command", action="correct_arguments")
    return None


def workspace_version(root: Path) -> str | None:
    """Bounded content snapshot; never follow links or claim freshness on errors.

    Runtime/dependency/build directories and log files are excluded. Unknown
    shell mutations separately invalidate history (including dependency setup).
    """
    ignored = IGNORED_DIRECTORIES | {".agent", ".scratch", ".pytest_cache", ".mypy_cache",
                                     ".memory", ".tasks", ".transcripts", ".task_outputs"}
    digest, count, total = hashlib.sha256(), 0, 0
    try:
        def unreadable(error):
            raise error
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
            dirs[:] = sorted(name for name in dirs if name not in ignored)
            if any((Path(directory) / name).is_symlink() for name in dirs):
                return None
            for name in sorted(files):
                if name.endswith((".log", ".pyc", ".tsbuildinfo")):
                    continue
                path = Path(directory) / name
                if path.is_symlink():
                    return None
                count += 1
                total += path.stat().st_size
                if count > 4000 or total > 16_000_000:
                    return None
                before = path.stat()
                data = path.read_bytes()
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    return None
                digest.update(str(path.relative_to(root)).encode("utf-8"))
                digest.update(str(after.st_mode).encode("ascii"))
                digest.update(b"\0" + hashlib.sha256(data).digest())
    except OSError:
        return None
    return digest.hexdigest()


class ExecutionGuard:
    """Remember completed recognized checks, including failures, for one loop."""

    def __init__(self):
        self.records = OrderedDict()
        self.pending = None

    def before(self, name, args, tool, ctx) -> ToolResult | None:
        self.pending = None
        if name != "bash" or getattr(tool.handler, "__module__", "") != "agent.tools.basic":
            if not tool.read_only and name not in {"write_file", "edit_file", "todo_write"}:
                self.records.clear()
            return None
        invalid = execution_check(args)
        if invalid is not None:
            return invalid
        command = args.get("command", "")
        identity = check_identity(command)
        if identity is None:
            # Preserve history across simple Git inspections, not arbitrary
            # shell mutations or installation commands.
            if not re.fullmatch(r"git (?:status|diff|log|show)(?: [^;&|]*)?", command.strip()):
                self.records.clear()
            return None
        runtime = ctx.runtime
        key = (ctx.owner, str(ctx.workdir), getattr(runtime, "permission_mode", None),
               getattr(runtime, "sandbox_enabled", False),
               getattr(getattr(runtime, "sandbox", None), "name", None),
               args.get("execution_mode", "default"), identity)
        version = workspace_version(Path(ctx.workdir))
        previous = self.records.get(key)
        if version is not None and previous and previous[0] == version and not args.get("rerun_reason", "").strip():
            return tool_error(
                "REPEATED_CHECK",
                "This check already completed with unchanged workspace inputs in this loop. "
                f"Previous result: {previous[1]}. Saved output: {previous[2] or 'unavailable'}. "
                "Reuse its saved output, inspect failures and fix "
                "the relevant code first. If repeated execution itself is necessary (for example "
                "investigating flakiness), provide a concrete rerun_reason.", action="inspect")
        self.pending = (key, version)
        return None

    def after(self, output: ToolResult, ctx):
        if self.pending and (output.ok or output.error_code == "COMMAND_FAILED"):
            key, version = self.pending
            # Docker is started lazily by the first command. Record the actual
            # container after dispatch rather than its previous None identity.
            key = key[:4] + (getattr(getattr(ctx.runtime, "sandbox", None), "name", None),) + key[5:]
            if version is not None:
                self.records[key] = (version, "success" if output.ok else "failure", output.command_log)
                self.records.move_to_end(key)
                while len(self.records) > 64:
                    self.records.popitem(last=False)
        self.pending = None
