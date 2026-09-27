"""Regression checks for one-shot team completion and mailbox delivery."""

from __future__ import annotations

import contextlib
import io
from types import SimpleNamespace
from unittest.mock import patch

from common import HarnessCase

from agent import cli
from agent.tasks import COMPLETED
from evals.project_baseline import check_idle_mailbox_delivery


class WorkerProbe:
    def __init__(self, alive: bool) -> None:
        self.alive = alive
        self.state = SimpleNamespace(
            role="tester", status="working" if alive else "stopped",
            claimed_task=None, turns=0, error="",
        )

    def stop(self) -> None:
        self.alive = False

    def join(self, timeout: float | None = None) -> None:
        pass


class TeamCompletionTests(HarnessCase):
    def test_one_shot_cli_waits_for_real_teammate_task(self):
        task_id = ""
        lead_calls = 0

        def responder(_index, messages, tools):
            nonlocal lead_calls
            names = {tool.get("name") for tool in tools}
            if "spawn_teammate" in names:
                lead_calls += 1
                if lead_calls == 1:
                    return {
                        "tool": "spawn_teammate",
                        "input": {"name": "worker", "role": "tester", "prompt": "Complete the assigned task."},
                    }
                return "Team work is complete." if lead_calls > 2 else "Delegated."
            content = "\n".join(str(item.get("content", "")) for item in messages)
            if task_id in content:
                return {"tool": "complete_task", "input": {"task_id": task_id}}
            return "Checking the board."

        runtime = self.make_runtime(responder=responder, require_plan=False)
        task = runtime.tasks.create("Small task", "Complete this task")
        task_id = task.id
        output = io.StringIO()
        with patch.object(cli.Runtime, "create", return_value=runtime):
            with contextlib.redirect_stdout(output):
                exit_code = cli.main(["--mock", "--workdir", str(self.tmp), "delegate"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(runtime.tasks.load(task.id).status, COMPLETED)
        self.assertIn("Team work is complete.", output.getvalue())

    def test_idle_wait_processes_consumed_shutdown(self):
        check = check_idle_mailbox_delivery(self.tmp / "mailbox-case")
        self.assertTrue(check["passed"], check["observed"])

    def test_timeout_reports_unfinished_task(self):
        runtime = self.make_runtime()
        task = runtime.tasks.create("Slow work", "Still running")
        runtime.teams.teammates["worker"] = WorkerProbe(alive=True)

        answer = runtime.wait_for_teammates(timeout=0, initial_answer="Lead finished")

        self.assertIn("Timed out", answer)
        self.assertIn(task.id, answer)

    def test_retired_worker_does_not_count_as_task_success(self):
        runtime = self.make_runtime()
        task = runtime.tasks.create("Unfinished work", "Not done")
        runtime.teams.teammates["worker"] = WorkerProbe(alive=False)

        answer = runtime.wait_for_teammates(timeout=1, initial_answer="Lead finished")

        self.assertIn("Unfinished tasks", answer)
        self.assertIn(task.id, answer)

    def test_completed_task_gets_final_lead_summary(self):
        runtime = self.make_runtime(script=["Team work is complete."])
        task = runtime.tasks.create("Done work", "Finished")
        task.status = COMPLETED
        task.owner = "worker"
        runtime.tasks.save(task)
        runtime.teams.teammates["worker"] = WorkerProbe(alive=True)

        answer = runtime.wait_for_teammates(timeout=1, initial_answer="Delegated")

        self.assertEqual(answer, "Team work is complete.")


if __name__ == "__main__":
    import unittest

    unittest.main()
