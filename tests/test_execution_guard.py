"""Regressions for the repeated full-suite run observed in the Web session."""

import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

from common import HarnessCase, make_settings
from agent.events import Hooks, PRE_TOOL_USE
from agent.llm import MockLLM
from agent.loop import execute_tool, run_loop
from agent.tools.basic import MAX_TOOL_OUTPUT, register_basic_tools, run_bash
from agent.tools.execution import ExecutionGuard, check_identity, execution_check, workspace_version
from agent.tools.registry import ToolContext, ToolRegistry


class ExecutionGuardTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(make_settings(self.tmp), self.tmp)
        self.registry = register_basic_tools(ToolRegistry())
        self.guard = ExecutionGuard()
        self.source = self.write("app.py", "value = 1\n")

    def call(self, command="python3 -m unittest discover -s tests -p 'test_*.py'", **kwargs):
        return execute_tool({"name": "bash", "input": {
            "command": command, "task_reason": "Verify the requested code fix", **kwargs}},
            self.registry, self.ctx, Hooks(), execution_guard=self.guard)

    def test_reason_is_required_for_checks_and_installs_before_any_process(self):
        for command in ("npm test", "pip install x", "npm ci", "npm run build",
                        "timeout 900 python3 -m unittest discover", "PYTHONPATH=. pytest tests",
                        '"C:\\Python\\python.exe" -X utf8 -m unittest discover'):
            with self.subTest(command=command), patch("agent.tools.basic.subprocess.run") as run:
                result = run_bash({"command": command}, self.ctx)
                self.assertEqual(result.error_code, "TASK_REASON_REQUIRED")
                run.assert_not_called()

    def test_masked_exit_is_rejected_instead_of_reporting_false_success(self):
        for command in ("python3 -m unittest discover > .scratch/full-test.log 2>&1; echo exit=$?; tail -3 .scratch/full-test.log",
                        "npm test | tail -30", "pytest || echo ignored",
                        "npm test && echo passed; echo done"):
            with self.subTest(command=command), patch("agent.tools.basic.subprocess.run") as run:
                result = self.call(command)
                self.assertEqual(result.error_code, "CHECK_STATUS_MASKED")
                self.assertEqual(result.execution_status, "not_executed")
                run.assert_not_called()

    def test_normalization_ignores_timeout_and_output_destinations_but_preserves_selectors(self):
        base = "python3 -m unittest discover -s tests -p 'test_*.py'"
        self.assertEqual(check_identity(base), check_identity("timeout 900 " + base + " > .scratch/a.log 2>&1"))
        self.assertNotEqual(check_identity(base), check_identity("python3 -m unittest test_core"))
        self.assertNotEqual(check_identity("A=1 pytest tests"), check_identity("A=2 pytest tests"))
        self.assertIsNone(check_identity("python custom_test.py"))
        self.assertIsNone(check_identity("cd elsewhere; npm test"))

    def test_completed_failure_cannot_be_repeated_only_to_save_log(self):
        with patch("agent.tools.basic.subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"failed", b"assertion")) as run:
            first = self.call()
            self.assertEqual(first.error_code, "COMMAND_FAILED")
            second = self.call("timeout 900 python3 -m unittest discover -s tests -p 'test_*.py' > .scratch/run.log 2>&1")
            self.assertEqual(second.error_code, "REPEATED_CHECK")
            self.assertIn("failure", second)
            self.assertIn(first.command_log, second)
            self.assertEqual(run.call_count, 1)
        logs = list((self.ctx.settings.state_dir / "command-logs").glob("*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("Exit code: 1\nstdout:\nfailed\nstderr:\nassertion", logs[0].read_text())

    def test_success_is_guarded_and_log_changes_do_not_invalidate_inputs(self):
        with patch("agent.tools.basic.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "pass", "")) as run:
            self.assertTrue(self.call().ok)
            self.write(".scratch/output.log", "different output")
            self.assertEqual(self.call().error_code, "REPEATED_CHECK")
            self.assertEqual(run.call_count, 1)

    def test_real_source_and_manifest_changes_allow_reverification(self):
        with patch("agent.tools.basic.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "pass", "")) as run:
            self.call()
            self.source.write_text("value = 2\n")
            self.assertTrue(self.call().ok)
            self.write("requirements.txt", "jsonschema\n")
            self.assertTrue(self.call().ok)
            self.assertEqual(run.call_count, 3)

    def test_unknown_mutation_or_install_invalidates_environment_evidence(self):
        for mutation in ("custom-environment-fix", "pip install missing"):
            with self.subTest(mutation=mutation), patch("agent.tools.basic.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok", "")):
                self.guard = ExecutionGuard()
                self.call()
                self.call(mutation)
                self.assertTrue(self.call().ok)

    def test_inspection_does_not_erase_previous_check_and_explicit_flake_investigation_is_allowed(self):
        with patch("agent.tools.basic.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as run:
            self.call()
            execute_tool({"name": "read_file", "input": {"path": "app.py"}}, self.registry,
                         self.ctx, Hooks(), execution_guard=self.guard)
            self.call("git status --short")
            self.assertEqual(self.call().error_code, "REPEATED_CHECK")
            self.assertTrue(self.call(rerun_reason="User requested repeated runs to reproduce an intermittent failure").ok)
            self.assertEqual(run.call_count, 3)

    def test_timeout_never_becomes_completed_check_evidence(self):
        with patch("agent.tools.basic.subprocess.run", side_effect=[
                subprocess.TimeoutExpired("check", 5, output=b"partial"), subprocess.CompletedProcess([], 0, "ok", "")]):
            self.assertEqual(self.call().error_code, "TIMEOUT")
            self.assertTrue(self.call().ok)
        logs = list((self.ctx.settings.state_dir / "command-logs").glob("*.log"))
        self.assertEqual(len(logs), 2)
        self.assertTrue(any("partial" in path.read_text() for path in logs))

    def test_log_is_full_even_when_model_output_is_clipped(self):
        with patch("agent.tools.basic.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "a" * 100_000, "failure at end")):
            result = self.call("npm test")
        self.assertLessEqual(len(result), MAX_TOOL_OUTPUT)
        self.assertEqual(result.error_code, "COMMAND_FAILED")
        log = next((self.ctx.settings.state_dir / "command-logs").glob("*.log"))
        self.assertIn("a" * 100_000, log.read_text())
        self.assertIn("failure at end", log.read_text())

    def test_log_write_failure_does_not_mask_actual_check_failure(self):
        self.write(".agent/command-logs", "a file blocks this directory")
        with patch("agent.tools.basic.subprocess.run", return_value=subprocess.CompletedProcess([], 7, "", "failed")):
            result = self.call("npm test")
        self.assertEqual(result.error_code, "COMMAND_FAILED")
        self.assertIn("Exit code: 7", result)
        self.assertIn("command log unavailable", result)

    def test_snapshot_limits_fail_open_rather_than_claiming_unchanged(self):
        with patch("agent.tools.execution.workspace_version", return_value=None), \
                patch("agent.tools.basic.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as run:
            self.call()
            self.assertTrue(self.call().ok)
            self.assertEqual(run.call_count, 2)

    def test_lazily_started_docker_is_recorded_and_replaced_container_resets_evidence(self):
        self.ctx.runtime = SimpleNamespace(permission_mode="workspace", sandbox_enabled=True, sandbox=None)
        def start(*args, **kwargs):
            self.ctx.runtime.sandbox = SimpleNamespace(name="first")
            return subprocess.CompletedProcess([], 0, "ok", "")
        self.ctx.runtime.get_sandbox = lambda _: SimpleNamespace(execute=start)
        self.assertTrue(self.call().ok)
        self.assertEqual(self.call().error_code, "REPEATED_CHECK")
        self.ctx.runtime.sandbox = SimpleNamespace(name="second")
        self.assertTrue(self.call().ok)

    def test_permission_denial_still_prevents_execution_and_creates_no_completion_record(self):
        hooks = Hooks()
        from agent.tools.result import tool_error
        hooks.register(PRE_TOOL_USE, lambda *_: tool_error("PERMISSION_DENIED", "denied"))
        block = {"name": "bash", "input": {"command": "npm test", "task_reason": "Verify the fix"}}
        with patch("agent.tools.basic.subprocess.run") as run:
            result = execute_tool(block, self.registry, self.ctx, hooks, execution_guard=self.guard)
        self.assertEqual(result.error_code, "PERMISSION_DENIED")
        self.assertFalse(self.guard.records)
        run.assert_not_called()

    def test_loop_guard_is_real_and_does_not_leak_to_next_user_turn(self):
        script = [{"tool": "bash", "input": {"command": "npm test", "task_reason": "Verify the fix"}}] * 2 + ["done"]
        for _ in range(2):
            messages = [{"role": "user", "content": "Fix and verify"}]
            with patch("agent.tools.basic.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "fail", "")) as run:
                result = run_loop(llm=MockLLM(script=script), registry=self.registry, ctx=self.ctx,
                                  messages=messages, system="sys", max_turns=4)
            self.assertTrue(result.ok)
            self.assertEqual(run.call_count, 1)
            self.assertIn("REPEATED_CHECK", json.dumps(messages))

    def test_real_failing_unittest_retains_failure_and_a_readable_log(self):
        self.write("test_sample.py", "import unittest\nclass Sample(unittest.TestCase):\n def test_bad(self): self.assertEqual(1, 2)\n")
        command = subprocess.list2cmdline([sys.executable, "-m", "unittest", "test_sample"])
        result = self.call(command)
        self.assertEqual(result.error_code, "COMMAND_FAILED")
        self.assertIn("Exit code: 1", result)
        log = next((self.ctx.settings.state_dir / "command-logs").glob("*.log"))
        self.assertIn("AssertionError: 1 != 2", log.read_text())
