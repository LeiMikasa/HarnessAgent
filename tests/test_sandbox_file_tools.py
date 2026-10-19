"""Docker file-tool behavior without starting a container or calling a model."""

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

from common import HarnessCase, make_settings
from agent import sandbox_file_tools as worker
from agent.sandbox import DockerSandbox
from agent.tools import basic
from agent.tools.registry import ToolContext, ToolRegistry
from agent.tools.result import ToolResult


class SandboxFileToolTests(HarnessCase):
    def execute(self, tool, **args):
        return ToolResult(**worker.execute(tool, args, self.tmp))

    def test_edit_exact_text_and_replace_all(self):
        path = self.write("nested/config.txt", "端口=8080\nname=A\nname=A")
        result = self.execute("edit_file", path="nested/config.txt",
                              old_text="8080", new_text="8081")
        self.assertTrue(result.ok, result)
        self.assertIn("1 replacement", result)
        result = self.execute("edit_file", path="nested/config.txt",
                              old_text="name=A", new_text="name=B", replace_all=True)
        self.assertTrue(result.ok, result)
        self.assertIn("2 replacements", result)
        self.assertEqual(path.read_text(encoding="utf-8"), "端口=8081\nname=B\nname=B")

    def test_rejected_edits_leave_file_unchanged(self):
        path = self.write("a.txt", "same same")
        for old in ("absent", "same"):
            result = self.execute("edit_file", path="a.txt", old_text=old, new_text="new")
            self.assertEqual(result.error_code, "PRECONDITION_FAILED")
            self.assertEqual(path.read_text(encoding="utf-8"), "same same")
        result = self.execute("edit_file", path="a.txt", old_text="", new_text="x")
        self.assertEqual(result.error_code, "INVALID_ARGUMENT")
        self.assertEqual(self.execute("edit_file", path="missing.txt", old_text="x", new_text="y").error_code,
                         "NOT_FOUND")

    def test_partial_write_is_unknown_and_needs_state_inspection(self):
        self.write("a.txt", "before")
        original = worker.Path.write_text

        def partial(path, content, **kwargs):
            original(path, content[:2], **kwargs)
            raise OSError("simulated disk failure")

        with patch.object(worker.Path, "write_text", partial):
            result = self.execute("edit_file", path="a.txt", old_text="before", new_text="after")
        self.assertFalse(result.ok)
        self.assertEqual(result.execution_status, "unknown")
        self.assertEqual(result.action, "inspect_state")
        self.assertEqual((self.tmp / "a.txt").read_text(encoding="utf-8"), "af")

    def test_glob_nested_paths_sorting_and_no_matches(self):
        self.write("src/z.txt", "z")
        self.write("src/nested/a.txt", "a")
        self.write(".hidden.txt", "hidden")
        self.assertEqual(self.execute("glob", pattern="**/*.txt"), "src/nested/a.txt\nsrc/z.txt")
        self.assertEqual(self.execute("glob", pattern="*.missing"), "(no matches)")

    def test_glob_match_limit(self):
        for i in range(worker.GLOB_MAX_MATCHES + 1):
            self.write(f"files/{i:03}.txt", "x")
        result = self.execute("glob", pattern="files/*.txt")
        self.assertEqual(len(result.splitlines()), worker.GLOB_MAX_MATCHES + 1)
        self.assertIn("1 more matches", result)

    def test_grep_regex_include_single_file_and_line_numbers(self):
        self.write("src/config.txt", "skip\nport=8081\n")
        self.write("src/other.log", "port=9999")
        args = {"pattern": r"port=\d+", "path": "src", "include": "*.txt"}
        self.assertEqual(self.execute("grep", **args), "src/config.txt:2:port=8081")
        args["path"] = "src/config.txt"
        self.assertEqual(self.execute("grep", **args), "src/config.txt:2:port=8081")
        self.assertEqual(self.execute("grep", pattern="no-hit"), "(no matches)")
        self.assertEqual(self.execute("grep", pattern="[bad").error_code, "INVALID_ARGUMENT")
        self.assertEqual(self.execute("grep", pattern="x", path="missing").error_code, "NOT_FOUND")

    def test_grep_limits_and_keeps_deep_match_visible(self):
        self.write("long.txt", "x" * 6000 + "needle" + "z" * 6000)
        result = self.execute("grep", pattern="needle", path="long.txt")
        self.assertIn("needle", result)
        self.assertLessEqual(len(result.split(":", 2)[2]), worker.GREP_LINE_LIMIT)
        self.write("many.txt", "needle\n" * (worker.GREP_MAX_MATCHES + 1))
        result = self.execute("grep", pattern="needle", path="many.txt")
        self.assertEqual(len(result.splitlines()), worker.GREP_MAX_MATCHES + 1)
        self.assertIn("results limited", result)
        self.write("wide.txt", ("needle" + "x" * 1100 + "\n") * 100)
        result = self.execute("grep", pattern="needle", path="wide.txt")
        self.assertLessEqual(len(result), worker.MAX_TOOL_OUTPUT)
        self.assertIn("results limited", result)

    def test_grep_skips_binary_and_large_files(self):
        self.write("binary.txt", "\x00needle")
        self.write("huge.txt", "needle" + "x" * worker.GREP_MAX_FILE_BYTES)
        self.assertEqual(self.execute("grep", pattern="needle"), "(no matches)")

    def test_paths_and_patterns_cannot_escape_workspace(self):
        cases = [
            ("edit_file", {"path": "../outside.txt", "old_text": "x", "new_text": "y"}),
            ("grep", {"path": "../", "pattern": "x"}),
            ("glob", {"pattern": "../*"}),
            ("glob", {"pattern": (self.tmp.parent / "*.txt").as_posix()}),
            ("grep", {"pattern": "x", "include": "../*"}),
        ]
        for tool, args in cases:
            with self.subTest(tool=tool, args=args):
                result = self.execute(tool, **args)
                self.assertEqual(result.error_code, "PERMISSION_DENIED", result)

    def test_search_caps_match_host_tools(self):
        for name in ("MAX_TOOL_OUTPUT", "GLOB_MAX_MATCHES", "GREP_MAX_MATCHES",
                     "GREP_MAX_FILE_BYTES", "GREP_LINE_LIMIT"):
            self.assertEqual(getattr(worker, name), getattr(basic, name))

    def test_enabled_routes_to_container_without_host_file_access(self):
        box = Mock()
        runtime = SimpleNamespace(sandbox_enabled=True, get_sandbox=Mock(return_value=box))
        ctx = ToolContext(make_settings(self.tmp), self.tmp, runtime=runtime)
        calls = [(basic.run_edit_file, "edit_file", {"path": "x", "old_text": "a", "new_text": "b"}),
                 (basic.run_glob, "glob", {"pattern": "**/*"}),
                 (basic.run_grep, "grep", {"pattern": "needle"})]
        with patch.object(basic, "safe_path", side_effect=AssertionError("host path access")), \
             patch.object(basic.globlib, "glob", side_effect=AssertionError("host search")):
            for handler, name, args in calls:
                getattr(box, name).return_value = ToolResult("container result")
                self.assertEqual(handler(args, ctx), "container result")
                getattr(box, name).assert_called_once_with(args)

    def test_request_json_preserves_regex_unicode_and_does_not_mutate_args(self):
        box = DockerSandbox(str(self.tmp))
        box.started = True
        box._docker = Mock(return_value=subprocess.CompletedProcess([], 0, '{"text":"ok"}', ""))
        args = {"path": str(self.tmp / "nested" / "你好.txt"), "pattern": r"port=\d+"}
        self.assertEqual(box.grep(args), "ok")
        positional, keyword = box._docker.call_args
        self.assertEqual(positional[0][:3], ["exec", "-i", box.name])
        request = json.loads(keyword["input_text"])
        self.assertEqual(request, {"path": "nested/你好.txt", "pattern": r"port=\d+"})
        self.assertEqual(args["path"], str(self.tmp / "nested" / "你好.txt"))
        compile(positional[0][6], "<container-worker>", "exec")
        self.assertEqual(positional[0][-1], "grep")

    def test_docker_error_never_falls_back_to_host(self):
        self.write("a.txt", "before")
        box = DockerSandbox(str(self.tmp))
        box.started = True
        box._docker = Mock(return_value=subprocess.CompletedProcess([], 1, "", "exec failed"))
        runtime = SimpleNamespace(sandbox_enabled=True, get_sandbox=lambda _: box)
        ctx = ToolContext(make_settings(self.tmp), self.tmp, runtime=runtime)
        registry = basic.register_basic_tools(ToolRegistry(), sandbox_enabled=True)
        result = registry.dispatch("edit_file", {"path": "a.txt", "old_text": "before", "new_text": "after"}, ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.execution_status, "unknown")
        self.assertEqual((self.tmp / "a.txt").read_text(encoding="utf-8"), "before")

    def test_timeout_closes_only_current_container(self):
        box = DockerSandbox(str(self.tmp))
        box.started = True
        box._docker = Mock(side_effect=[subprocess.TimeoutExpired("docker", 30),
                                       subprocess.CompletedProcess([], 0, "", "")])
        with self.assertRaises(TimeoutError):
            box.grep({"pattern": "needle"})
        self.assertEqual(box._docker.call_args.args[0], ["rm", "--force", box.name])
        self.assertFalse(box.started)
