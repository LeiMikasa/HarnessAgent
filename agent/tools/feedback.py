"""Bounded, per-loop command diagnosis advice; never executes or blocks tools."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
import re
import shlex

from .result import ToolResult


COMMAND_DIAGNOSIS = (
    "[command diagnosis] A command failed. Use its exit code, stdout and stderr to identify "
    "the failing step. For a failed check, inspect the failing assertion and relevant implementation, "
    "compare expected and actual values, then make an evidence-based correction. Keep meaningful "
    "checks; do not remove or weaken them just to pass. Inspect possible side effects before "
    "rerunning. Use a workspace script for complex diagnostics."
)
REPEATED_COMMAND_FAILURE = (
    "[repeated command failure] The same command arguments and visible failure have repeated. "
    "Choose a focused inspection or a different diagnostic method before repeating it unchanged. "
    "Corrected commands and necessary verification are still allowed; respect permission denials."
)


class CommandFeedback:
    def __init__(self):
        self._last_failure: str | None = None
        self.streak = 0

    def observe(self, name, args, result: ToolResult, tool) -> str:
        if name != "bash":
            # A possible mutation changes the evidence behind a repeat warning.
            if tool is None or not tool.read_only:
                self._last_failure, self.streak = None, 0
            return ""
        if result.ok or result.error_code != "COMMAND_FAILED":
            self._last_failure, self.streak = None, 0
            return ""
        payload = json.dumps([args, result.error_code, str(result)], sort_keys=True,
                             ensure_ascii=False, default=str)
        signature = hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()
        self.streak = self.streak + 1 if signature == self._last_failure else 1
        self._last_failure = signature
        return COMMAND_DIAGNOSIS + ("\n" + REPEATED_COMMAND_FAILURE if self.streak >= 2 else "")


def _command_kinds(command: str) -> set[str]:
    """Conservative advisory labels, never a shell parser or permission decision.

    Only classify executable positions. Quoted code/echo arguments containing
    'pip install' must not be promoted into successful installation evidence.
    """
    try:
        lexer = shlex.shlex(command, posix=False, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return set()
    segments, current = [], []
    for token in tokens:
        if token and all(char in ";&|()" for char in token):
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    kinds = set()
    for segment in segments:
        program = segment[0].strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()
        program = program.removesuffix(".exe").removesuffix(".cmd")
        arguments = [token.strip("\"'") for token in segment[1:]]
        if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?|py", program):
            if program == "py" and arguments and re.fullmatch(r"-\d+(?:\.\d+)*", arguments[0]):
                arguments = arguments[1:]
            if arguments[:2] == ["-m", "pip"] and arguments[2:3] == ["install"]:
                kinds.add("dependency-install")
            if arguments[:2] in (["-m", "pytest"], ["-m", "unittest"]):
                kinds.add("test")
        elif re.fullmatch(r"pip(?:\d+(?:\.\d+)*)?", program) and arguments[:1] == ["install"]:
            kinds.add("dependency-install")
        elif program in {"npm", "pnpm", "yarn"}:
            if arguments[:1] in (["install"], ["ci"], ["add"]):
                kinds.add("dependency-install")
            action = arguments[1:2] if arguments[:1] == ["run"] else arguments[:1]
            if action and action[0] in {"test", "build", "lint", "typecheck", "check"}:
                kinds.add("test/build/check")
        elif program in {"pytest", "ruff", "mypy", "tsc", "eslint"}:
            kinds.add("test/build/check")
    return kinds


class ExecutionFeedback:
    """Small, per-loop history of observed successes; never caches tool results.

    All requested tools still execute. Read equality means identical visible
    results, not proof the whole file is unchanged. Shell success records only
    an invocation's status, not correctness of its subcommands or project.
    """

    MAX_RECORDS = 64
    MANIFEST_NAMES = {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
                      "pyproject.toml", "poetry.lock", "uv.lock", "pipfile", "pipfile.lock"}

    def __init__(self):
        self._scope = None
        self._reads = OrderedDict()
        self._checks = OrderedDict()
        self._installs = OrderedDict()
        self._repeat_reads = 0
        self._last_bash = None
        self._bash_streak = 0
        self._sandbox_unavailable = False

    def _set_scope(self, ctx, args=None):
        runtime = getattr(ctx, "runtime", None)
        sandbox = getattr(runtime, "sandbox", None)
        scope = (getattr(ctx, "owner", "agent"), str(getattr(ctx, "workdir", "")),
                 getattr(runtime, "permission_mode", None), getattr(runtime, "sandbox_enabled", False),
                 getattr(sandbox, "name", None), (args or {}).get("execution_mode", "default"))
        if scope != self._scope:
            self._scope = scope
            self._reads.clear()
            self._checks.clear()
            self._installs.clear()
            self._repeat_reads = 0
            self._last_bash = None
            self._bash_streak = 0
            self._sandbox_unavailable = False

    def _remember(self, records, key, value):
        records[key] = value
        records.move_to_end(key)
        while len(records) > self.MAX_RECORDS:
            records.popitem(last=False)

    def observe(self, name, args, result: ToolResult, tool, *, ctx):
        args = args if isinstance(args, dict) else {}
        self._set_scope(ctx, args)
        if name == "bash":
            if result.error_code == "SANDBOX_UNAVAILABLE":
                self._sandbox_unavailable = True
            elif result.ok or result.error_code == "COMMAND_FAILED":
                self._sandbox_unavailable = False
        key = hashlib.sha256(json.dumps([name, args], sort_keys=True, ensure_ascii=False,
                                        default=str).encode("utf-8")).hexdigest()
        # Count identical arguments regardless of success or output changes. This
        # is repetition evidence only, never proof that a call made no progress.
        # Read-only inspections may intervene; a different command or potential
        # mutation starts a fresh chain. Retain no raw shell arguments/output.
        if name == "bash" and isinstance(args.get("command"), str) and args["command"].strip():
            self._bash_streak = self._bash_streak + 1 if key == self._last_bash else 1
            self._last_bash = key
        elif tool is None or not tool.read_only:
            self._last_bash = None
            self._bash_streak = 0
        if tool is not None and tool.read_only:
            if result.ok:
                output = hashlib.sha256(str(result).encode("utf-8")).hexdigest()
                if self._reads.get(key) == output:
                    self._repeat_reads += 1
                self._remember(self._reads, key, output)
            else:
                self._reads.pop(key, None)
            return

        # Any potentially mutating call invalidates inspection/check freshness,
        # even if it fails with an unknown outcome. No filesystem guessing.
        self._reads.clear()
        self._checks.clear()
        self._repeat_reads = 0
        path = str(args.get("path", "")).replace("\\", "/").rsplit("/", 1)[-1].lower()
        if path in self.MANIFEST_NAMES or (path.startswith("requirements") and path.endswith(".txt")):
            self._installs.clear()
        if name == "bash":
            kinds = _command_kinds(str(args.get("command", "")))
            if not result.ok:
                if "dependency-install" in kinds:
                    self._installs.clear()
                return
            if "dependency-install" in kinds:
                self._remember(self._installs, key, True)
            if kinds - {"dependency-install"}:
                self._remember(self._checks, key, True)

    def render(self, ctx) -> str:
        self._set_scope(ctx)
        notes = []
        if self._sandbox_unavailable:
            notes.append("[sandbox unavailable] A sandbox Bash call could not start. Changing commands "
                         "does not repair Docker. Continue with available file tools and report the "
                         "execution limit; retry Bash only after concrete evidence of an environment "
                         "change or a focused recovery step. Host execution still requires permission. "
                         "This is historical failure evidence, not a permanent tool block.")
        if self._bash_streak >= 3:
            notes.append("[repeated bash call] The same bash arguments have been used "
                         f"{self._bash_streak} times without an intervening different command or potentially "
                         "mutating tool. Inspect the latest result and identify new evidence or a concrete "
                         "reason before rerunning. Use the required dedicated file tools for file "
                         "operations. Change approach if stuck, or finish when the requested work and relevant "
                         "checks are complete. Intentional polling and necessary verification remain allowed; "
                         "this reminder does not mean outputs are identical or the task is complete.")
        if self._installs:
            notes.append("An earlier shell invocation containing a recognized dependency-install command returned success. "
                         "Reuse that setup if manifests and environment are unchanged; reinstall only for a changed "
                         "dependency requirement or concrete evidence of a missing/broken dependency.")
        if self._checks:
            notes.append("A recent recognized test/build/check shell invocation returned success. "
                         "Reuse the check if its inputs/environment are unchanged; verify again after relevant changes.")
        if self._repeat_reads:
            notes.append("An inspection with identical arguments returned the same visible content more than once. "
                         "Reuse it unless fresh evidence is needed; this does not prove the entire file is unchanged.")
        if not notes:
            return ""
        return ("[execution record] Observations from this loop only; these are historical statuses, not proof of "
                "current state or task completion.\n" + "\n".join(notes))[:1600]
