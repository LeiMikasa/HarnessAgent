"""Local Web API, persistence and approval integration with free scripted models."""

import json
import threading
import time
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock

from common import HarnessCase, make_settings
from agent.runtime import Runtime
from agent.tools.result import ToolResult
from agent.ui_events import UIEvent
from agent.web.sessions import SessionManager

WEB_INSTALLED = importlib.util.find_spec("fastapi") is not None and importlib.util.find_spec("httpx") is not None
if WEB_INSTALLED:
    from fastapi.testclient import TestClient
    from agent.web.app import create_app


@unittest.skipUnless(WEB_INSTALLED, "Install requirements-web.txt to run Web integration tests")
class WebTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.settings = make_settings(self.project, state_dir=self.tmp / "state")
        self.directory = self.tmp / "sessions"
        self.manager = SessionManager(self.settings, directory=self.directory)
        self.addCleanup(self.manager.close)

    def client(self, manager=None, static_dir=None, **kwargs):
        return TestClient(create_app(self.settings, manager=manager or self.manager,
                                     static_dir=static_dir, **kwargs), base_url="http://127.0.0.1:8765")

    def test_folder_picker_returns_absolute_directory_and_cancel_preserves_client_path(self):
        picker = Mock(side_effect=[str(self.project), None])
        with self.client(directory_picker=picker) as client:
            selected = client.post("/api/workspaces/pick", json={"initial_path": str(self.tmp)})
            self.assertEqual(selected.json(), {"path": str(self.project.resolve())})
            picker.assert_called_once_with(str(self.tmp))
            self.assertEqual(client.post("/api/workspaces/pick", json={}).json(), {"path": None})
            self.assertEqual(self.manager.list(), [])

    def test_folder_picker_errors_and_untrusted_origins_do_not_open_dialog(self):
        picker = Mock(side_effect=RuntimeError("无法打开系统选择窗口"))
        with self.client(directory_picker=picker) as client:
            result = client.post("/api/workspaces/pick", json={}, headers={"Origin": "https://evil.example"})
            self.assertEqual(result.status_code, 403)
            picker.assert_not_called()
            self.assertEqual(client.post("/api/workspaces/pick", json={}).status_code, 503)
        with self.client(directory_picker=lambda _: str(self.tmp / "missing")) as client:
            self.assertEqual(client.post("/api/workspaces/pick", json={}).status_code, 422)

    def test_folder_picker_allows_only_one_native_dialog(self):
        entered, release = threading.Event(), threading.Event()
        def picker(_initial):
            entered.set()
            release.wait(5)
            return None
        with self.client(directory_picker=picker) as client:
            worker = threading.Thread(target=lambda: client.post("/api/workspaces/pick", json={}))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(client.post("/api/workspaces/pick", json={}).status_code, 409)
            finally:
                release.set()
                worker.join(3)

    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("Web session did not reach expected state")
            time.sleep(0.01)

    def factory(self, script):
        box = Mock()
        box.read_file.return_value = ToolResult("container content")
        def create(**kwargs):
            runtime = Runtime.create(script=script, **kwargs)
            runtime.get_sandbox = Mock(return_value=box)
            return runtime
        manager = SessionManager(self.settings, directory=self.directory, runtime_factory=create)
        self.addCleanup(manager.close)
        return manager, box

    def test_config_does_not_expose_credentials_or_call_a_model(self):
        self.settings.api_key = "private-api-key-fixture"
        with self.client() as client:
            response = client.get("/api/config")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("private-api-key-fixture", response.text)
            self.assertEqual(len(response.json()["permission_modes"]), 3)
            self.assertEqual(self.manager.list(), [])

    def test_host_and_origin_guard_reject_cross_site_control(self):
        with self.client() as client:
            for origin in ("https://evil.example", "http://localhost:9999", "http://localhost:bad"):
                result = client.post("/api/sessions", headers={"Origin": origin},
                                     json={"workdir": str(self.project)})
                self.assertEqual(result.status_code, 403)
            self.assertEqual(client.get("/api/config", headers={"Host": "evil.example"}).status_code, 403)
            self.assertEqual(client.get("/api/config", headers={"Origin": "http://127.0.0.1:5173"}).status_code, 200)
            self.assertEqual(self.manager.list(), [])

    def test_create_validates_workspace_mode_and_missing_session(self):
        with self.client() as client:
            for data in ({"workdir": str(self.tmp / "missing")},
                         {"workdir": str(self.project), "permission_mode": "invalid"}):
                self.assertEqual(client.post("/api/sessions", json=data).status_code, 422)
            self.assertEqual(client.get("/api/sessions/missing").status_code, 404)
            response = client.post("/api/sessions", json={"workdir": str(self.project),
                                                         "permission_mode": "read-only", "mock": True})
            self.assertEqual(response.status_code, 201)
            session = self.manager.get(response.json()["metadata"]["id"])
            self.assertIsNone(session.runtime)
            self.assertFalse(session.busy)

    def test_mock_runs_real_loop_and_persists_without_model_fees(self):
        current = self.manager.create(str(self.project), "full", mock=True)
        current.submit("免费验证")
        self.wait(lambda: not current.busy)
        snapshot = current.snapshot()
        self.assertEqual(snapshot["last_error"], "")
        self.assertTrue(any(entry["kind"] == "tool" for entry in snapshot["timeline"]))
        self.assertTrue(any("没有调用真实模型" in entry["text"] for entry in snapshot["timeline"]))
        self.assertIsNone(snapshot["metrics"]["token_totals"]["input_tokens"])
        self.assertTrue(all(todo["status"] == "completed" for todo in snapshot["todos"]))
        restored = SessionManager(self.settings, directory=self.directory)
        self.addCleanup(restored.close)
        session = restored.get(current.metadata["id"])
        self.assertEqual(session.messages, current.runtime.messages)
        self.assertIsNone(session.runtime)
        self.assertEqual(session.snapshot()["timeline"], snapshot["timeline"])
        calls = snapshot["metrics"]["counts"]["model_calls"]
        session.submit("继续免费验证")
        self.wait(lambda: not session.busy)
        self.assertEqual(session.snapshot()["metrics"]["counts"]["model_calls"], calls * 2)

    def test_assistant_update_precedes_tool_and_stream_finalization_does_not_duplicate_it(self):
        preamble = "我先读取项目说明，确认启动方式和已有功能，然后再按你的需求修改。" * 3
        (self.project / "README.md").write_text("启动说明", encoding="utf-8")
        manager, _ = self.factory([
            {"text": preamble, "tool": "read_file", "input": {"path": "README.md"}},
            "已读取项目说明，确认完成。",
        ])
        current = manager.create(str(self.project), "full", mock=True)
        current.submit("检查项目说明")
        self.wait(lambda: not current.busy)
        snapshot = current.snapshot()
        self.assertEqual(snapshot["last_error"], "")
        entries = snapshot["timeline"]
        self.assertEqual([entry["kind"] for entry in entries], ["message", "message", "tool", "message"])
        self.assertEqual(entries[1]["role"], "assistant")
        self.assertEqual(entries[1]["text"], preamble)
        self.assertEqual(entries[1]["status"], "done")
        self.assertEqual(entries[2]["name"], "read_file")
        self.assertEqual(entries[2]["text"], "启动说明")
        self.assertEqual(entries[3]["text"], "已读取项目说明，确认完成。")
        self.assertEqual(snapshot["metrics"]["counts"]["model_calls"], 2)
        self.assertEqual(snapshot["metrics"]["counts"]["tool_calls"], 1)
        events = current.events_after(0)
        entry_events = [event["data"] for event in events if event["kind"] == "entry"]
        update_index = next(i for i, entry in enumerate(entry_events) if entry.get("role") == "assistant")
        tool_index = next(i for i, entry in enumerate(entry_events) if entry.get("kind") == "tool")
        self.assertLess(update_index, tool_index)

    def test_approval_endpoint_allows_one_host_read_then_returns_to_container(self):
        outside = self.write("outside.txt", "external content")
        script = [{"tool": "read_file", "input": {"path": str(outside)}},
                  {"tool": "read_file", "input": {"path": "a.txt"}}, "done"]
        manager, box = self.factory(script)
        with self.client(manager) as client:
            response = client.post("/api/sessions", json={"workdir": str(self.project), "mock": True})
            identity = response.json()["metadata"]["id"]
            current = manager.get(identity)
            self.assertEqual(client.post(f"/api/sessions/{identity}/messages", json={"text": "read"}).status_code, 202)
            self.wait(lambda: bool(current.snapshot()["pending_approvals"]))
            request = current.snapshot()["pending_approvals"][0]
            self.assertTrue(request["host_execution"])
            self.assertEqual(box.read_file.call_count, 0)
            result = client.post(f"/api/sessions/{identity}/approvals/{request['id']}", json={"allowed": True})
            self.assertEqual(result.status_code, 200)
            self.wait(lambda: not current.busy)
            texts = [entry["text"] for entry in current.snapshot()["timeline"]]
            self.assertIn("external content", texts)
            self.assertIn("container content", texts)
            self.assertEqual(box.read_file.call_count, 1)
            self.assertNotIn("host_execution_approved", current.runtime.lead_ctx.extra)
            self.assertEqual(client.post(f"/api/sessions/{identity}/approvals/{request['id']}",
                                         json={"allowed": True}).status_code, 409)

    def test_thinking_only_tool_turn_is_displayed_separately_and_restores_after_restart(self):
        thoughts = "这是模型返回的思考内容。" * 12
        manager, _ = self.factory([[
            {"type": "thinking", "thinking": thoughts, "signature": "never-display-this-signature"},
            {"type": "redacted_thinking", "data": "never-display-this-redacted-data"},
            {"tool": "read_file", "input": {"path": "README.md"}},
        ], "任务完成"])
        current = manager.create(str(self.project), "workspace", mock=True)
        current.submit("检查项目")
        self.wait(lambda: not current.busy)
        snapshot = current.snapshot()
        self.assertEqual(snapshot["last_error"], "")
        entries = snapshot["timeline"]
        self.assertEqual([entry["kind"] for entry in entries], ["message", "message", "tool", "message"])
        self.assertEqual(entries[1]["thinking"], thoughts)
        self.assertEqual(entries[1]["thinking_status"], "done")
        self.assertEqual(entries[1]["text"], "")
        self.assertEqual(entries[3]["text"], "任务完成")
        self.assertNotIn("never-display-this", json.dumps(snapshot))
        self.assertEqual(snapshot["metrics"]["counts"]["model_calls"], 2)
        restored = SessionManager(self.settings, directory=manager.directory)
        self.addCleanup(restored.close)
        self.assertEqual(restored.get(current.metadata["id"]).snapshot()["timeline"], entries)

    def test_thinking_stream_failure_is_marked_interrupted_and_display_size_is_bounded(self):
        current = self.manager.create(str(self.project), "full", mock=True)
        current.receive(UIEvent("thinking_delta", "lead", "a", "思" * 200_001))
        entry = current.snapshot()["timeline"][0]
        self.assertEqual(len(entry["thinking"]), 200_000)
        self.assertEqual(entry["thinking_status"], "streaming")
        self.assertEqual(entry["text"], "")
        current.receive(UIEvent("model_failed", "lead", "a", "ConnectionError"))
        self.assertEqual(current.snapshot()["timeline"][0]["status"], "interrupted")
        self.assertEqual(current.snapshot()["timeline"][0]["thinking_status"], "interrupted")

    def test_restored_partial_thinking_is_not_shown_as_still_generating(self):
        current = self.manager.create(str(self.project), "full", mock=True)
        current.receive(UIEvent("thinking_delta", "lead", "a", "尚未完成"))
        with current.lock:
            current._save()
        restored = SessionManager(self.settings, directory=self.directory)
        self.addCleanup(restored.close)
        entry = restored.get(current.metadata["id"]).snapshot()["timeline"][0]
        self.assertEqual(entry["thinking_status"], "interrupted")

    def test_stop_releases_approval_and_prevents_host_mutation(self):
        outside = self.tmp / "outside-write.txt"
        manager, _ = self.factory([{"tool": "write_file", "input": {"path": str(outside), "content": "x"}}, "done"])
        current = manager.create(str(self.project), "workspace", mock=True)
        current.submit("write")
        self.wait(lambda: bool(current.snapshot()["pending_approvals"]))
        with self.client(manager) as client:
            self.assertEqual(client.post(f"/api/sessions/{current.metadata['id']}/stop").status_code, 200)
        self.wait(lambda: not current.busy)
        self.assertFalse(outside.exists())
        self.assertEqual(current.snapshot()["pending_approvals"], [])

    def test_deny_approval_never_executes_mutation(self):
        outside = self.tmp / "outside-write.txt"
        manager, _ = self.factory([{"tool": "write_file", "input": {"path": str(outside), "content": "x"}}, "done"])
        current = manager.create(str(self.project), "workspace", mock=True)
        current.submit("write")
        self.wait(lambda: bool(current.snapshot()["pending_approvals"]))
        request = current.snapshot()["pending_approvals"][0]
        current.decide(request["id"], False)
        self.wait(lambda: not current.busy)
        self.assertFalse(outside.exists())
        self.assertTrue(any(item.get("kind") == "tool" and item.get("status") == "failed"
                            and "Permission denied" in item["text"] for item in current.timeline))

    def test_busy_session_rejects_second_submit(self):
        outside = self.write("outside.txt", "content")
        manager, _ = self.factory([{"tool": "read_file", "input": {"path": str(outside)}}, "done"])
        current = manager.create(str(self.project), "workspace", mock=True)
        current.submit("first")
        self.wait(lambda: bool(current.snapshot()["pending_approvals"]))
        with self.client(manager) as client:
            self.assertEqual(client.post(f"/api/sessions/{current.metadata['id']}/messages",
                                         json={"text": "second"}).status_code, 409)
        self.wait(lambda: not current.busy)

    def test_event_replay_resnapshots_when_history_ring_overflows(self):
        current = self.manager.create(str(self.project), "full", mock=True)
        with current.lock:
            for index in range(2050):
                current._publish("state", {"index": index})
        events = current.events_after(0)
        self.assertEqual(events[0]["kind"], "snapshot")
        self.assertEqual(events[0]["seq"], current.seq)

    def test_missing_build_and_static_routes(self):
        static = self.tmp / "frontend"
        with self.client(static_dir=static) as client:
            self.assertEqual(client.get("/").status_code, 503)
            self.assertEqual(client.get("/api/unknown").status_code, 404)
            static.mkdir()
            (static / "index.html").write_text("<h1>Harness</h1>", encoding="utf-8")
            self.assertEqual(client.get("/").text, "<h1>Harness</h1>")

    def test_unknown_token_usage_stays_unknown(self):
        current = self.manager.create(str(self.project), "full", mock=True)
        self.assertEqual(current.snapshot()["metrics"], {})


if __name__ == "__main__":
    import unittest
    unittest.main()
