"""Guard real mutations, races and publication failures without a model/API."""

from concurrent.futures import ThreadPoolExecutor
import os
import stat
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from common import HarnessCase, make_settings
from agent.events import Hooks
from agent.teams import Teammate
from agent.tools import filesystem
from agent.tools.basic import register_basic_tools
from agent.tools.registry import ToolContext, ToolRegistry


class FileObservationTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.registry = register_basic_tools(ToolRegistry())
        self.ctx = ToolContext(make_settings(self.tmp), self.tmp)

    def call(self, name, ctx=None, **args):
        return self.registry.dispatch(name, args, ctx or self.ctx)

    def assert_error(self, result, code):
        self.assertFalse(result.ok, result)
        self.assertEqual(result.error_code, code, result)

    def test_unread_overwrite_and_edit_are_rejected_without_changing_bytes(self):
        path = self.write("a.txt", "original")
        for name, args in (("write_file", {"content": "bad"}),
                           ("edit_file", {"old_text": "original", "new_text": "bad"})):
            result = self.call(name, path="a.txt", **args)
            self.assert_error(result, "FS_NOT_OBSERVED")
            self.assertEqual(result.execution_status, "not_executed")
            self.assertEqual(result.action, "reread")
            self.assertEqual(path.read_text(), "original")

    def test_read_then_write_and_chained_edits_update_observation(self):
        self.write("a.txt", "one")
        self.assertTrue(self.call("read_file", path="a.txt").ok)
        self.assertTrue(self.call("write_file", path="a.txt", content="two").ok)
        self.assertTrue(self.call("edit_file", path="a.txt", old_text="two", new_text="three").ok)
        self.assertTrue(self.call("edit_file", path="a.txt", old_text="three", new_text="four").ok)
        self.assertEqual((self.tmp / "a.txt").read_text(), "four")

    def test_new_file_can_be_created_and_immediately_edited_without_read(self):
        self.assertTrue(self.call("write_file", path="nested/new.txt", content="one").ok)
        self.assertTrue(self.call("edit_file", path="nested/new.txt", old_text="one", new_text="two").ok)
        self.assertEqual((self.tmp / "nested/new.txt").read_text(), "two")

    def test_stale_version_wins_over_literal_matching_and_recovers_after_read(self):
        path = self.write("a.txt", "original")
        self.call("read_file", path="a.txt")
        path.write_text("human changed it")
        for name, args in (("write_file", {"content": "bad"}),
                           ("edit_file", {"old_text": "original", "new_text": "bad"})):
            self.assert_error(self.call(name, path="a.txt", **args), "FS_STALE_VERSION")
        self.assertEqual(path.read_text(), "human changed it")
        self.call("read_file", path="a.txt")
        self.assertTrue(self.call("edit_file", path="a.txt", old_text="human", new_text="user").ok)

    def test_hash_detects_same_length_content_with_restored_mtime(self):
        path = self.write("a.txt", "before")
        self.call("read_file", path="a.txt")
        original = path.stat()
        path.write_text("after!")
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assert_error(self.call("write_file", path="a.txt", content="bad"), "FS_STALE_VERSION")
        self.assertEqual(path.read_text(), "after!")

    def test_deleted_target_requires_reread_then_guarded_creation(self):
        path = self.write("a.txt", "before")
        self.call("read_file", path="a.txt")
        path.unlink()
        self.assert_error(self.call("write_file", path="a.txt", content="after"), "FS_STALE_VERSION")
        self.assertFalse(path.exists())
        self.assert_error(self.call("read_file", path="a.txt"), "NOT_FOUND")
        self.assert_error(self.call("edit_file", path="a.txt", old_text="x", new_text="y"), "FS_NOT_FOUND")
        self.assertTrue(self.call("write_file", path="a.txt", content="after").ok)

    def test_absence_observation_does_not_authorize_overwriting_a_concurrent_creator(self):
        self.assert_error(self.call("read_file", path="new.txt"), "NOT_FOUND")
        path = self.write("new.txt", "someone else")
        self.assert_error(self.call("write_file", path="new.txt", content="bad"), "FS_STALE_VERSION")
        self.assertEqual(path.read_text(), "someone else")

    def test_partial_read_authorizes_same_version_but_grep_does_not(self):
        self.write("a.txt", "one\ntwo\n")
        self.call("grep", pattern="one", path="a.txt")
        self.assert_error(self.call("write_file", path="a.txt", content="bad"), "FS_NOT_OBSERVED")
        self.call("read_file", path="a.txt", limit=1)
        self.assertTrue(self.call("edit_file", path="a.txt", old_text="two", new_text="three").ok)

    def test_observations_do_not_leak_to_child_or_new_session_context(self):
        self.write("a.txt", "one")
        self.call("read_file", path="a.txt")
        for ctx in (self.ctx.child(owner="child"), ToolContext(make_settings(self.tmp), self.tmp)):
            self.assert_error(self.call("write_file", ctx=ctx, path="a.txt", content="bad"), "FS_NOT_OBSERVED")
        self.assertTrue(self.call("write_file", path="a.txt", content="parent").ok)

    def test_clearing_the_cli_conversation_requires_rereading(self):
        import io
        from contextlib import redirect_stdout
        from agent.cli import _command

        runtime = self.make_runtime(teams_enabled=False)
        self.write("a.txt", "one")
        self.call("read_file", ctx=runtime.lead_ctx, path="a.txt")
        with redirect_stdout(io.StringIO()):
            _command(runtime, "clear")
        self.assert_error(self.call("write_file", ctx=runtime.lead_ctx, path="a.txt", content="bad"),
                          "FS_NOT_OBSERVED")

    def test_repeated_offline_demo_reads_its_existing_output(self):
        from agent.cli import _demo_script

        self.write("greeting.py", "# existing greeting")
        runtime = self.make_runtime(script=_demo_script(self.tmp), teams_enabled=False)
        runtime.submit("demo")
        results = [b for m in runtime.messages if isinstance(m.get("content"), list)
                   for b in m["content"] if b.get("type") == "tool_result"]
        self.assertTrue(all(not b.get("is_error") for b in results), results)
        self.assertIn("Hello, world!", str(results))

    def test_absolute_and_workspace_aliases_share_an_observation(self):
        self.write("nested/a.txt", "one")
        self.ctx.runtime = SimpleNamespace(sandbox_enabled=True, permission_mode="workspace")
        self.call("read_file", path="/workspace/nested/a.txt")
        self.assertTrue(self.call("edit_file", path=str(self.tmp / "nested/a.txt"), old_text="one", new_text="two").ok)

    def test_two_sessions_with_same_observed_version_have_only_one_winner(self):
        self.write("a.txt", "before")
        contexts = [ToolContext(make_settings(self.tmp), self.tmp) for _ in range(2)]
        for ctx in contexts:
            self.call("read_file", ctx=ctx, path="a.txt")
        barrier = threading.Barrier(2)

        def change(index):
            barrier.wait(timeout=5)
            return self.call("write_file", ctx=contexts[index], path="a.txt", content=f"winner-{index}")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, range(2)))
        self.assertEqual(sum(result.ok for result in results), 1)
        self.assertEqual([r.error_code for r in results if not r.ok], ["FS_STALE_VERSION"])
        winner = next(index for index, result in enumerate(results) if result.ok)
        self.assertEqual((self.tmp / "a.txt").read_text(), f"winner-{winner}")

    def test_two_unseen_creators_do_not_overwrite_one_another(self):
        contexts = [ToolContext(make_settings(self.tmp), self.tmp) for _ in range(2)]
        barrier = threading.Barrier(2)

        def create(index):
            barrier.wait(timeout=5)
            return self.call("write_file", ctx=contexts[index], path="new.txt", content=f"creator-{index}")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, range(2)))
        self.assertEqual(sum(result.ok for result in results), 1)
        self.assertEqual([r.error_code for r in results if not r.ok], ["FS_NOT_OBSERVED"])

    def test_external_creator_between_final_check_and_link_is_preserved(self):
        path = self.tmp / "new.txt"

        def concurrent_create(source, target):
            path.write_text("external creator")
            raise FileExistsError("won the race")

        with patch("agent.tools.filesystem.os.link", side_effect=concurrent_create):
            result = self.call("write_file", path="new.txt", content="bad")
        self.assert_error(result, "FS_STALE_VERSION")
        self.assertEqual(path.read_text(), "external creator")
        self.assertEqual(list(self.tmp.glob(".minicode-*.tmp")), [])

    def test_change_during_staging_is_detected_before_replacement(self):
        path = self.write("a.txt", "original")
        self.call("read_file", path="a.txt")
        original = os.fsync

        def external_change(fd):
            original(fd)
            path.write_text("external update")

        with patch("agent.tools.filesystem.os.fsync", side_effect=external_change):
            result = self.call("write_file", path="a.txt", content="bad")
        self.assert_error(result, "FS_STALE_VERSION")
        self.assertEqual(path.read_text(), "external update")

    def test_failed_staging_keeps_original_and_cleans_up(self):
        path = self.write("a.txt", "original")
        self.call("read_file", path="a.txt")
        with patch("agent.tools.filesystem.os.fsync", side_effect=OSError("disk full")):
            result = self.call("write_file", path="a.txt", content="bad")
        self.assert_error(result, "FS_IO_ERROR")
        self.assertEqual(result.execution_status, "not_executed")
        self.assertEqual(path.read_text(), "original")
        self.assertEqual(list(self.tmp.glob(".minicode-*.tmp")), [])

    def test_uncertain_replacement_requires_a_fresh_read(self):
        path = self.write("a.txt", "original")
        self.call("read_file", path="a.txt")
        with patch("agent.tools.filesystem._replace_existing", side_effect=OSError("lost acknowledgement")):
            result = self.call("write_file", path="a.txt", content="bad")
        self.assert_error(result, "FS_IO_ERROR")
        self.assertEqual(result.execution_status, "unknown")
        self.assertEqual(path.read_text(), "original")
        self.assert_error(self.call("write_file", path="a.txt", content="bad"), "FS_NOT_OBSERVED")
        self.call("read_file", path="a.txt")
        self.assertTrue(self.call("write_file", path="a.txt", content="recovered").ok)

    def test_permission_denied_read_does_not_create_observation(self):
        path = self.write("a.txt", "original")
        with patch("agent.tools.filesystem._snapshot", side_effect=PermissionError("denied")):
            self.assert_error(self.call("read_file", path="a.txt"), "PERMISSION_DENIED")
        self.assert_error(self.call("write_file", path="a.txt", content="bad"), "FS_NOT_OBSERVED")
        self.assertEqual(path.read_text(), "original")

    def test_edit_preserves_crlf_line_endings(self):
        path = self.tmp / "windows.txt"
        path.write_bytes(b"one\r\ntwo\r\n")
        self.call("read_file", path="windows.txt")
        self.assertTrue(self.call("edit_file", path="windows.txt", old_text="one\ntwo", new_text="one\nthree").ok)
        self.assertEqual(path.read_bytes(), b"one\r\nthree\r\n")

    def test_new_file_uses_normal_creation_permissions_and_overwrite_preserves_mode(self):
        ordinary = self.write("ordinary.txt", "before")
        self.assertTrue(self.call("write_file", path="new.txt", content="new").ok)
        new = self.tmp / "new.txt"
        self.assertEqual(new.stat().st_mode, ordinary.stat().st_mode)
        before = new.stat().st_mode
        self.assertTrue(self.call("write_file", path="new.txt", content="updated").ok)
        self.assertEqual(new.stat().st_mode, before)

    def test_teammate_reuses_observations_across_its_tool_contexts(self):
        teammate = Teammate(name="worker", role="dev", prompt="change a file", bus=Mock(), tasks=Mock(),
                            worktrees=SimpleNamespace(cwd_for=lambda _: self.tmp), protocol=Mock(),
                            registry=self.registry, llm=Mock(), settings=make_settings(self.tmp),
                            hooks_factory=Hooks, runner=Mock())
        first, second = teammate._tool_context(), teammate._tool_context()
        self.assertIs(first.files, second.files)
        self.assertIsNot(first.files, self.ctx.files)
        self.write("a.txt", "original")
        self.call("read_file", ctx=first, path="a.txt")
        self.assertTrue(self.call("write_file", ctx=second, path="a.txt", content="worker").ok)

    @unittest.skipUnless(os.name == "nt", "Windows DACL test")
    def test_windows_replacement_preserves_explicit_target_dacl(self):
        import ctypes
        from ctypes import wintypes

        path = self.write("a.txt", "before")
        command = subprocess.run(["icacls", str(path), "/inheritance:d"], capture_output=True)
        self.assertEqual(command.returncode, 0, command.stderr)
        security = ctypes.WinDLL("advapi32", use_last_error=True).GetFileSecurityW
        security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID,
                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        security.restype = wintypes.BOOL

        def dacl():
            needed = wintypes.DWORD()
            security(str(path), 4, None, 0, ctypes.byref(needed))
            buffer = ctypes.create_string_buffer(needed.value)
            self.assertTrue(security(str(path), 4, buffer, len(buffer), ctypes.byref(needed)))
            return buffer.raw

        before = dacl()
        self.call("read_file", path="a.txt")
        result = self.call("write_file", path="a.txt", content="after")
        self.assertTrue(result.ok, result)
        self.assertEqual(dacl(), before)
        self.assertEqual(path.read_text(), "after")

    @unittest.skipUnless(os.name == "nt", "Windows read-only attribute test")
    def test_read_only_target_failure_leaves_no_staging_file(self):
        path = self.write("readonly.txt", "before")
        path.chmod(stat.S_IREAD)
        try:
            self.call("read_file", path="readonly.txt")
            result = self.call("write_file", path="readonly.txt", content="bad")
            self.assertFalse(result.ok, result)
            self.assertEqual(path.read_text(), "before")
            self.assertEqual(list(self.tmp.glob(".minicode-*.tmp")), [])
        finally:
            path.chmod(stat.S_IREAD | stat.S_IWRITE)
