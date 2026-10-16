"""Offline regression tests for shell output and model-facing diagnosis advice."""

from __future__ import annotations

import copy
import json
import os
import shlex
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

from common import HarnessCase, make_settings
from agent.llm import MockLLM
from agent.loop import run_loop
from agent.shell import decode_shell_output, shell_guidance, shell_name
from agent.tools.basic import MAX_TOOL_OUTPUT, register_basic_tools, run_bash
from agent.tools.feedback import CommandFeedback
from agent.tools.registry import Tool, ToolContext, ToolRegistry
from agent.tools.result import ToolResult


def python_command(path):
    args = [sys.executable, str(path)]
    return subprocess.list2cmdline(args) if sys.platform == "win32" else shlex.join(args)


class ShellOutputTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(settings=make_settings(self.tmp), workdir=self.tmp)

    def test_real_failed_command_preserves_utf8_chinese_and_separates_streams(self):
        script = self.write("check.py", "import sys\nsys.stdout.buffer.write('检查开始'.encode('utf-8'))\n"
                            "sys.stderr.buffer.write('断言失败：期望12，实际11'.encode('utf-8'))\nsys.exit(7)\n")
        result = register_basic_tools(ToolRegistry()).dispatch("bash", {"command": python_command(script)}, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "COMMAND_FAILED")
        self.assertEqual(result.execution_status, "unknown")
        self.assertFalse(result.transient)
        payload = json.loads(result.model_content())
        self.assertIn("Exit code: 7", payload["message"])
        self.assertIn("stdout:\n检查开始", payload["message"])
        self.assertIn("stderr:\n断言失败：期望12，实际11", payload["message"])

    def test_success_retains_plain_text_for_existing_callers(self):
        script = self.write("success.py", "print('检查通过')\n")
        result = run_bash({"command": python_command(script)}, self.ctx)
        self.assertEqual(result, "检查通过")

    def test_large_stdout_cannot_hide_stderr_and_overall_error_is_bounded(self):
        completed = subprocess.CompletedProcess("x", 2, stdout=b"x" * 100_000,
                                                stderr=b"critical assertion failed\n" + b"y" * 100_000)
        with patch("agent.tools.basic.subprocess.run", return_value=completed):
            result = run_bash({"command": "x"}, self.ctx)
        self.assertIn("stdout:", result)
        self.assertIn("stderr:\ncritical assertion failed", result)
        self.assertIn("truncated", result)
        self.assertLessEqual(len(result), MAX_TOOL_OUTPUT)

    def test_child_environment_is_copied_and_explicit_encoding_is_respected(self):
        completed = subprocess.CompletedProcess("x", 0, stdout=b"ok", stderr=b"")
        with patch.dict(os.environ, {"PYTHONIOENCODING": "gb18030"}), \
                patch("agent.tools.basic.subprocess.run", return_value=completed) as run:
            self.assertEqual(run_bash({"command": "x"}, self.ctx), "ok")
            kwargs = run.call_args.kwargs
            self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "gb18030")
            self.assertIsNot(kwargs["env"], os.environ)
            self.assertNotIn("text", kwargs)
            self.assertEqual(os.environ["PYTHONIOENCODING"], "gb18030")

    def test_utf8_is_preferred_over_legacy_locale(self):
        with patch("agent.shell.locale.getencoding", return_value="gb18030"):
            self.assertEqual(decode_shell_output("构建成功".encode("utf-8")), "构建成功")

    def test_legacy_locale_and_utf16_bom_are_supported(self):
        with patch("agent.shell.sys", SimpleNamespace(platform="linux")), \
                patch("agent.shell.locale.getencoding", return_value="gb18030"):
            self.assertEqual(decode_shell_output("找不到命令".encode("gb18030")), "找不到命令")
        self.assertEqual(decode_shell_output("检查失败".encode("utf-16")), "检查失败")
        self.assertEqual(decode_shell_output(None), "")
        self.assertEqual(decode_shell_output("already decoded"), "already decoded")

    def test_invalid_output_bytes_do_not_crash(self):
        with patch("agent.shell.sys", SimpleNamespace(platform="linux")), \
                patch("agent.shell.locale.getencoding", return_value="utf-8"):
            self.assertEqual(decode_shell_output(b"\xff"), "\ufffd")

    def test_windows_shell_description_matches_comspec_and_reaches_tool_pool(self):
        with patch("agent.shell.sys", SimpleNamespace(platform="win32")), \
                patch.dict(os.environ, {"COMSPEC": "C:\\Windows\\System32\\cmd.exe"}):
            runtime = self.make_runtime(teams_enabled=False)
            prompt = runtime.build_system()
            description = runtime.tools.get("bash").description
            for text in (prompt, description):
                self.assertIn("Operating system: Windows", text)
                self.assertIn("cmd.exe", text)
                self.assertIn("not PowerShell or POSIX", text)
                self.assertIn("tail", text)
                self.assertIn("script", text)

    def test_posix_shell_does_not_follow_interactive_shell_environment(self):
        with patch("agent.shell.sys", SimpleNamespace(platform="linux")), \
                patch.dict(os.environ, {"SHELL": "/bin/zsh", "COMSPEC": "cmd.exe"}):
            self.assertEqual(shell_name(), "/bin/sh")
            self.assertIn("/bin/sh", shell_guidance())
            self.assertNotIn("Use cmd syntax", shell_guidance())


class CommandFeedbackTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.feedback = CommandFeedback()
        self.bash = Tool("bash", "", {}, lambda a, c: "")
        self.read = Tool("read_file", "", {}, lambda a, c: "", read_only=True)
        self.write_tool = Tool("write_file", "", {}, lambda a, c: "")

    def failed(self, command="npm test", message="assertion failed"):
        return self.feedback.observe("bash", {"command": command},
                                     ToolResult.failure("COMMAND_FAILED", message), self.bash)

    def test_first_failure_gives_evidence_based_diagnosis_without_repeat_warning(self):
        advice = self.failed()
        self.assertIn("[command diagnosis]", advice)
        self.assertIn("expected and actual", advice)
        self.assertIn("do not remove or weaken", advice)
        self.assertNotIn("[repeated command failure]", advice)

    def test_same_command_and_visible_failure_warns_without_retaining_payload(self):
        self.failed()
        self.assertIn("[repeated command failure]", self.failed())
        self.assertEqual(self.feedback.streak, 2)
        self.assertNotIn("assertion failed", self.feedback._last_failure)

    def test_different_command_or_error_starts_new_streak(self):
        self.failed()
        self.assertNotIn("[repeated command failure]", self.failed(message="different failure"))
        self.assertNotIn("[repeated command failure]", self.failed(command="npm run build", message="different failure"))

    def test_success_and_possible_mutation_reset_the_streak(self):
        self.failed()
        self.feedback.observe("bash", {}, ToolResult("ok"), self.bash)
        self.assertNotIn("[repeated command failure]", self.failed())
        self.feedback.observe("write_file", {}, ToolResult("written"), self.write_tool)
        self.assertNotIn("[repeated command failure]", self.failed())

    def test_read_only_inspection_does_not_claim_a_mutation_happened(self):
        self.failed()
        self.feedback.observe("read_file", {}, ToolResult("source"), self.read)
        self.assertIn("[repeated command failure]", self.failed())

    def test_permissions_and_timeouts_do_not_receive_command_failure_advice(self):
        for code in ("PERMISSION_DENIED", "POLICY_BLOCKED", "TIMEOUT", "INVALID_ARGUMENT"):
            self.assertEqual(self.feedback.observe("bash", {}, ToolResult.failure(code, "blocked"), self.bash), "")

    def drive(self, registry, script):
        llm = MockLLM(script=script)
        original = llm.create
        requests = []

        def create(**kwargs):
            requests.append({"system": kwargs["system"], "messages": copy.deepcopy(kwargs["messages"])})
            return original(**kwargs)

        llm.create = create
        result = run_loop(llm=llm, registry=registry, system="sys", max_turns=12,
                          ctx=ToolContext(settings=make_settings(self.tmp), workdir=self.tmp),
                          messages=[{"role": "user", "content": "Fix the failing check"}])
        return result, requests

    def test_mock_model_receives_real_failure_then_inspects_repairs_and_finishes(self):
        path = self.write("check.py", "actual = 11\nassert actual == 12, f'expected 12, actual {actual}'\nprint('passed')\n")
        command = python_command(path)
        result, requests = self.drive(register_basic_tools(ToolRegistry()), [
            {"tool": "bash", "input": {"command": command}},
            {"tool": "read_file", "input": {"path": "check.py"}},
            {"tool": "edit_file", "input": {"path": "check.py", "old_text": "actual = 11", "new_text": "actual = 12"}},
            {"tool": "bash", "input": {"command": command}},
            "Verified and finished",
        ])
        self.assertTrue(result.ok)
        self.assertEqual((result.turns, result.tool_calls), (5, 4))
        self.assertIn("[command diagnosis]", requests[1]["system"])
        failed = requests[1]["messages"][-1]["content"][0]
        self.assertTrue(failed["is_error"])
        self.assertIn("expected 12, actual 11", json.loads(failed["content"])["message"])
        self.assertNotIn("[command diagnosis]", requests[2]["system"])
        self.assertEqual(requests[-1]["messages"][-1]["content"][0]["content"], "passed")

    def test_repeated_failures_are_not_blocked_and_corrected_command_runs(self):
        calls = []
        registry = ToolRegistry()

        def bash(args, ctx):
            calls.append(args["command"])
            return ToolResult.failure("COMMAND_FAILED", "same failure") if args["command"] == "bad" else "fixed"

        registry.add("bash", "", {"type": "object"}, bash)
        result, requests = self.drive(registry, [
            *[{"tool": "bash", "input": {"command": "bad"}}] * 3,
            {"tool": "bash", "input": {"command": "corrected"}}, "done",
        ])
        self.assertTrue(result.ok)
        self.assertEqual(calls, ["bad", "bad", "bad", "corrected"])
        self.assertEqual(len(requests), 5)
        self.assertIn("[repeated command failure]", requests[2]["system"])
        self.assertNotIn("[command diagnosis]", requests[4]["system"])

    def test_feedback_isolated_between_runs_and_bounded_for_batched_failures(self):
        registry = ToolRegistry()
        registry.add("bash", "", {"type": "object"}, lambda a, c: ToolResult.failure("COMMAND_FAILED", "x" * 80000))
        batch = [{"tool": "bash", "input": {"command": "bad"}}] * 8
        _, first = self.drive(registry, [batch, "done"])
        self.assertEqual(first[1]["system"].count("[command diagnosis]"), 1)
        self.assertLess(len(first[1]["system"]), 1500)
        _, second = self.drive(registry, [{"tool": "bash", "input": {"command": "bad"}}, "done"])
        self.assertNotIn("[repeated command failure]", second[1]["system"])
