"""Bounded, per-loop command diagnosis advice; never executes or blocks tools."""

from __future__ import annotations

import hashlib
import json

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
