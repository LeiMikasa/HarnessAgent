"""Task-tool claims must enter worktrees before the next tool in a batch."""

import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from common import HarnessCase
from agent.tasks import IN_PROGRESS, PENDING, COMPLETED, run_claim_task, run_complete_task
from agent.teams import Teammate


@unittest.skipUnless(shutil.which("git"), "requires Git")
class TeammateClaimContextTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.write("README.md", "baseline\n")
        self.write(".gitignore", ".agent/\n__pycache__/\n")
        self.git("init")
        self.git("add", "README.md", ".gitignore")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "baseline")

    def git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.tmp, capture_output=True, text=True, errors="replace", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def worker(self, script=None):
        runtime = self.make_runtime(script=script, require_plan=False)
        teams = runtime.teams
        teams.protocol.plan_gates["worker"] = "not_required"
        teammate = Teammate(
            name="worker", role="writer", prompt="Implement the task",
            bus=teams.bus, tasks=runtime.tasks, worktrees=teams.worktrees,
            protocol=teams.protocol, registry=teams.registry, llm=runtime.llm,
            settings=runtime.settings, hooks_factory=teams.hooks_factory,
            runner=runtime.loop_runner, tool_runtime=teams.tool_runtime,
            autonomous=True, require_plan=False, idle_rounds=0,
        )
        self.addCleanup(teammate._release_assignment)
        return runtime, teammate

    def test_claim_then_write_in_same_batch_uses_prebound_worktree(self):
        runtime, worker = self.worker()
        task = runtime.tasks.create("Write feature.py", "Own feature.py")
        self.assertTrue(runtime.teams.worktrees.create("feature", task.id).startswith("Created"))
        runtime.llm.script = [
            [{"tool": "claim_task", "input": {"task_id": task.id}},
             {"tool": "write_file", "input": {"path": "feature.py", "content": "value = 42\n"}}],
            {"tool": "bash", "input": {"command": f'"{sys.executable}" -c "from pathlib import Path; print(Path.cwd())"'}},
            "Ready for verification",
        ]
        worker._run_turn()
        path = runtime.settings.worktrees_dir / "feature"
        self.assertEqual((path / "feature.py").read_text(), "value = 42\n")
        self.assertFalse((self.tmp / "feature.py").exists())
        self.assertEqual(worker.state.claimed_task, task.id)
        self.assertEqual(runtime.tasks.load(task.id).worktree, "feature")
        self.assertIn(str(path), runtime.llm.calls[1]["system"])
        self.assertIn(task.id, runtime.llm.calls[1]["system"])
        outputs = [block["content"] for message in worker.messages
                   if isinstance(message.get("content"), list)
                   for block in message["content"] if block.get("type") == "tool_result"]
        self.assertEqual(outputs[-1], str(path))
        self.assertEqual(len(runtime.teams.worktrees.registered()), 2)

    def test_tool_claim_creates_worktree_when_task_has_no_binding(self):
        runtime, worker = self.worker()
        task = runtime.tasks.create("New task")
        ctx = worker._tool_context()
        result = run_claim_task({"task_id": task.id}, ctx)
        expected = runtime.settings.worktrees_dir / f"worker-{task.id.removeprefix('task_')}"
        self.assertTrue(result.startswith("Claimed"))
        self.assertEqual(ctx.workdir, expected)
        self.assertTrue(expected.is_dir())
        self.assertEqual(worker.state.claimed_task, task.id)
        self.assertEqual(runtime.tasks.load(task.id).status, IN_PROGRESS)
        self.assertIn(str(expected), result)

    def test_automatic_claim_reuses_prebound_worktree(self):
        runtime, worker = self.worker()
        task = runtime.tasks.create("Auto task")
        runtime.teams.worktrees.create("prepared", task.id)
        self.assertEqual(worker._find_work(), (True, True))
        self.assertEqual(worker._tool_context().workdir, runtime.settings.worktrees_dir / "prepared")
        self.assertEqual(worker.state.claimed_task, task.id)

    def test_reclaim_is_idempotent_and_does_not_clear_plan(self):
        runtime, worker = self.worker()
        task = runtime.tasks.create("Task")
        ctx = worker._tool_context()
        run_claim_task({"task_id": task.id}, ctx)
        worker.todos.replace([{"content": "current step"}])
        version = worker.protocol.work_versions[worker.name]
        result = run_claim_task({"task_id": task.id}, ctx)
        self.assertIn("already claimed", result)
        self.assertEqual(worker.protocol.work_versions[worker.name], version)
        self.assertEqual(worker.todos.total, 1)

    def test_failed_claim_does_not_change_context_or_worker(self):
        runtime, worker = self.worker()
        task = runtime.tasks.create("Taken")
        runtime.tasks.claim(task.id, owner="other")
        ctx = worker._tool_context()
        result = run_claim_task({"task_id": task.id}, ctx)
        self.assertIn("owned by other", result)
        self.assertIsNone(worker.state.claimed_task)
        self.assertEqual(ctx.workdir, self.tmp)
        self.assertIsNone(worker.worktrees.assignment_for(worker.name))

    def test_complete_releases_directory_and_next_claim_switches_it(self):
        runtime, worker = self.worker()
        first = runtime.tasks.create("First")
        second = runtime.tasks.create("Second")
        ctx = worker._tool_context()
        run_claim_task({"task_id": first.id}, ctx)
        first_path = ctx.workdir
        result = run_complete_task({"task_id": first.id}, ctx)
        self.assertTrue(result.startswith("Completed"))
        self.assertEqual(runtime.tasks.load(first.id).status, COMPLETED)
        self.assertIsNone(worker.state.claimed_task)
        self.assertIsNone(worker.worktrees.assignment_for(worker.name))
        self.assertEqual(ctx.workdir, self.tmp)
        run_claim_task({"task_id": second.id}, ctx)
        self.assertNotEqual(ctx.workdir, first_path)
        self.assertEqual(worker.state.claimed_task, second.id)

    def test_exit_returns_tool_claim_to_board(self):
        runtime, worker = self.worker()
        task = runtime.tasks.create("Unfinished")
        run_claim_task({"task_id": task.id}, worker._tool_context())
        worker._release_assignment()
        self.assertEqual(runtime.tasks.load(task.id).status, PENDING)
        self.assertIsNone(runtime.tasks.load(task.id).owner)
        self.assertIsNone(worker.worktrees.assignment_for(worker.name))

    def test_worktree_failure_does_not_leave_hidden_claim(self):
        runtime, worker = self.worker()
        task = runtime.tasks.create("Broken isolation")
        ctx = worker._tool_context()
        with patch.object(worker.worktrees, "create", return_value="Error: git failed"):
            result = run_claim_task({"task_id": task.id}, ctx)
        self.assertIn("could not enter task worktree", result)
        self.assertEqual(runtime.tasks.load(task.id).status, PENDING)
        self.assertIsNone(worker.state.claimed_task)
        self.assertEqual(ctx.workdir, self.tmp)

    def test_no_git_keeps_shared_directory_but_tracks_claim(self):
        runtime, worker = self.worker()
        task = runtime.tasks.create("Shared task")
        ctx = worker._tool_context()
        with patch.object(worker.worktrees, "is_git_repo", return_value=False):
            result = run_claim_task({"task_id": task.id}, ctx)
        self.assertTrue(result.startswith("Claimed"))
        self.assertEqual(ctx.workdir, self.tmp)
        self.assertEqual(worker.state.claimed_task, task.id)
