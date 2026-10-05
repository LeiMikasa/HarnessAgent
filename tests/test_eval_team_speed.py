"""The benchmark's mock path must wait for actual code and task completion."""

from __future__ import annotations

from common import HarnessCase

from agent.prompt import build_system_prompt
from evals.team_speed import run_worker, verify


class TeamSpeedEvalTests(HarnessCase):
    def test_plan_prompt_uses_teammate_submissions_as_the_default(self):
        prompt = build_system_prompt(
            workdir=self.tmp,
            tool_names=["create_task", "spawn_teammate", "request_plan", "review_plan"],
            teams_enabled=True,
            team_requires_plan=True,
        )
        self.assertIn("create approval requests automatically", prompt)
        self.assertIn("request_plan is optional", prompt)
        self.assertNotIn("-> request_plan ->", prompt)

    def test_prompt_does_not_advertise_disabled_workflows(self):
        prompt = build_system_prompt(
            workdir=self.tmp,
            tool_names=["create_task", "spawn_teammate", "write_file"],
            teams_enabled=True,
            team_requires_plan=False,
            team_worktrees_enabled=False,
        )
        self.assertIn("share this workspace", prompt)
        self.assertNotIn("request_plan", prompt)
        self.assertNotIn("connect_mcp", prompt)
        self.assertNotIn("call todo_write", prompt)

    def test_fixture_starts_failing(self):
        import shutil

        from evals.team_speed import FIXTURE

        workspace = self.tmp / "fixture"
        shutil.copytree(FIXTURE, workspace)
        passed, detail = verify(workspace)
        self.assertFalse(passed)
        self.assertIn("NotImplementedError", detail)

    def test_mock_single_and_team_reach_verified_completion(self):
        single_dir = self.tmp / "single"
        team_dir = self.tmp / "team"
        single_dir.mkdir()
        team_dir.mkdir()
        single = run_worker("single", single_dir, "mock", "mock-1", 20)
        team = run_worker("team", team_dir, "mock", "mock-1", 20)

        self.assertEqual(single["status"], "success")
        self.assertEqual(team["status"], "success")
        self.assertFalse(single["team_used"])
        self.assertTrue(team["team_used"])
        self.assertTrue(all(task["status"] == "completed" for task in team["tasks"]))
        self.assertGreater(team["model_calls"], single["model_calls"])
        self.assertTrue(verify(team_dir / "workspace")[0])
