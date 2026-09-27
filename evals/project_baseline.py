"""Deterministic checks of HarnessAgent's own team lifecycle.

These checks use the production CLI and teammate loop, but no paid model calls.
An invariant failure is recorded as data rather than making the evaluator crash.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent import cli
from agent.events import Hooks
from agent.tasks import TaskStore
from agent.teams import (
    SHUTDOWN_REQUEST,
    MessageBus,
    ProtocolState,
    Teammate,
    WorktreeManager,
)
from agent.tools.registry import ToolRegistry


def check_cli_team_lifecycle(root: Path) -> dict:
    """Does the real one-shot CLI wait before closing its runtime?"""

    class RuntimeProbe:
        def __init__(self) -> None:
            self.team_complete = False
            self.wait_calls = 0
            self.close_saw_unfinished_team = False
            self.request = ""

        def submit(self, request: str) -> str:
            self.request = request
            return "Lead turn finished; teammate is still working."

        def wait_for_teammates(self, **kwargs: object) -> str:
            self.wait_calls += 1
            self.team_complete = True
            return str(kwargs.get("initial_answer", ""))

        def close(self) -> None:
            self.close_saw_unfinished_team = not self.team_complete

    runtime = RuntimeProbe()
    output = io.StringIO()
    with patch.object(cli.Runtime, "create", return_value=runtime):
        with contextlib.redirect_stdout(output):
            exit_code = cli.main(["--mock", "--workdir", str(root), "finish team task"])

    passed = exit_code == 0 and not runtime.close_saw_unfinished_team
    return {
        "name": "one_shot_cli_waits_for_teammates",
        "passed": passed,
        "method": "Run the production CLI one-shot branch with an instrumented runtime; the lead returns while a teammate is unfinished.",
        "observed": {
            "exit_code": exit_code,
            "wait_calls": runtime.wait_calls,
            "close_saw_unfinished_team": runtime.close_saw_unfinished_team,
            "request": runtime.request,
            "stdout": output.getvalue().strip(),
        },
        "expected": "The CLI waits for teammate completion or reports the unfinished work before closing.",
    }


def check_idle_mailbox_delivery(root: Path) -> dict:
    """Does an idle teammate handle a shutdown delivered during its wait?"""
    bus = MessageBus(root / "mailbox")
    tasks = TaskStore(root / "tasks")
    teammate = Teammate(
        name="worker",
        role="tester",
        prompt="Wait for the lead.",
        bus=bus,
        tasks=tasks,
        worktrees=WorktreeManager(root, root / "worktrees", tasks=tasks, enabled=False),
        protocol=ProtocolState(),
        registry=ToolRegistry(),
        llm=None,  # No model call is reached when there is no task.
        settings=SimpleNamespace(),
        hooks_factory=Hooks,
        runner=lambda **_kwargs: "",
        autonomous=False,
        require_plan=False,
        idle_rounds=1,
    )
    original_wait = bus.wait_for_messages
    delivered = []

    def deliver_during_wait(agent: str, timeout: float = 0) -> list:
        bus.send(
            "lead", agent, "Please stop.", type=SHUTDOWN_REQUEST,
            metadata={"request_id": "baseline_shutdown"},
        )
        messages = original_wait(agent, timeout=0)
        delivered.extend(messages)
        return messages

    bus.wait_for_messages = deliver_during_wait  # type: ignore[method-assign]
    teammate._work_loop(0)
    responses = bus.peek("lead")
    passed = teammate._stop.is_set() and bool(responses)
    return {
        "name": "idle_teammate_handles_mailbox_message",
        "passed": passed,
        "method": "Run the production teammate work loop with a real mailbox; inject a shutdown request exactly when its idle wait begins.",
        "observed": {
            "wait_returned_types": [message.type for message in delivered],
            "worker_mailbox_remaining": [message.type for message in bus.peek("worker")],
            "shutdown_ack_types": [message.type for message in responses],
            "stop_flag_set": teammate._stop.is_set(),
        },
        "expected": "The teammate processes the returned message, acknowledges shutdown, and sets its stop flag.",
    }


def run(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="project-baseline-", dir=output_dir) as temp:
        root = Path(temp)
        checks = [
            check_cli_team_lifecycle(root / "cli"),
            check_idle_mailbox_delivery(root / "mailbox-check"),
        ]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Project lifecycle invariants; deterministic, offline, no model-quality claim.",
        "checks": checks,
        "passed": sum(check["passed"] for check in checks),
        "total": len(checks),
    }
    (output_dir / "summary.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path(".scratch/evals") / f"project-baseline-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
    )
    args = parser.parse_args()
    report = run(args.output_dir)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Report: {args.output_dir.resolve() / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
