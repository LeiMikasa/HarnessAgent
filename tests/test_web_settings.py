"""Live Web controls: real loop boundaries, offline clients and mocked Docker only."""

import importlib.util
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

from common import HarnessCase, make_settings
from agent.llm import MockLLM
from agent.permissions import CALL_PERMISSION_KEYS
from agent.runtime import Runtime
from agent.tools.result import ToolResult
from agent.web.sessions import SessionManager

WEB_INSTALLED = importlib.util.find_spec("fastapi") is not None and importlib.util.find_spec("httpx") is not None
if WEB_INSTALLED:
    from fastapi.testclient import TestClient
    from agent.web.app import create_app


@unittest.skipUnless(WEB_INSTALLED, "Install requirements-web.txt")
class LiveSettingsTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.settings = make_settings(self.project, state_dir=self.tmp / "state")

    def manager(self, script=None, responder=None, client_factory=None):
        def factory(**kwargs):
            return Runtime.create(script=script, responder=responder, **kwargs)
        options = {"client_factory": client_factory} if client_factory else {}
        manager = SessionManager(self.settings, directory=self.tmp / "sessions", runtime_factory=factory, **options)
        self.addCleanup(manager.close)
        return manager

    def wait(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("Live settings did not reach expected state")
            time.sleep(0.01)

    def test_api_confirmation_validation_and_persistence_without_model_calls(self):
        manager = self.manager()
        current = manager.create(str(self.project), "workspace", mock=True)
        url = f"/api/sessions/{current.metadata['id']}/settings"
        with TestClient(create_app(self.settings, manager=manager), base_url="http://127.0.0.1:8765") as client:
            for payload in ({"permission_mode": "full"}, {"permission_mode": "bad"},
                            {"provider": "unconfigured", "model": "a"}, {"model": "bad\nmodel"}, {"model": ""}):
                self.assertEqual(client.post(url, json=payload).status_code, 422)
            result = client.post(url, json={"permission_mode": "full", "confirm_full": True,
                                           "provider": "mock", "model": "mock-2"})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["metadata"]["permission_mode"], "full")
            self.assertEqual(result.json()["metadata"]["model"], "mock-2")
            self.assertIsNone(current.runtime)
            self.assertEqual(client.post(url, json={"permission_mode": "read-only"},
                                         headers={"Origin": "https://evil.example"}).status_code, 403)
        restored = SessionManager(self.settings, directory=manager.directory)
        self.addCleanup(restored.close)
        saved = restored.get(current.metadata["id"])
        self.assertEqual(saved.metadata["permission_mode"], "full")
        self.assertEqual(saved.metadata["model"], "mock-2")
        self.assertIsNone(saved.runtime)

    def test_current_tool_finishes_under_old_policy_next_tool_in_same_batch_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        manager = self.manager(script=[{"tool_calls": [
            {"tool": "write_file", "input": {"path": "first.txt", "content": "first"}},
            {"tool": "write_file", "input": {"path": "second.txt", "content": "second"}},
        ]}, "done"])
        current = manager.create(str(self.project), "full", mock=True)
        runtime = current._runtime()
        original = runtime.tools.get("write_file").handler
        observed = []
        def blocking_write(args, ctx):
            observed.append(runtime.permission_mode)
            entered.set()
            self.assertTrue(release.wait(5))
            observed.append(runtime.permission_mode)
            return original(args, ctx)
        runtime.tools.get("write_file").handler = blocking_write
        current.submit("write two files")
        self.assertTrue(entered.wait(3))
        result = current.update_settings(permission_mode="read-only")
        self.assertEqual(result["metadata"]["permission_mode"], "full")
        self.assertEqual(result["pending_settings"], {"permission_mode": "read-only"})
        self.assertEqual(runtime.permission_mode, "full")
        release.set()
        self.wait(lambda: not current.busy)
        self.assertEqual(observed, ["full", "full"])
        self.assertTrue((self.project / "first.txt").exists())
        self.assertFalse((self.project / "second.txt").exists())
        self.assertEqual(runtime.permissions.mode, "read-only")
        self.assertTrue(runtime.permissions.sandbox_enabled)
        self.assertEqual(current.snapshot()["pending_settings"], {})
        self.assertFalse(current.last_error)

    def test_inflight_model_finishes_then_last_selected_model_receives_same_conversation(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        (self.project / "a.txt").write_text("existing content", encoding="utf-8")
        def old_responder(index, _messages, _tools):
            if index == 0:
                entered.set()
                self.assertTrue(release.wait(5))
                return {"tool": "read_file", "input": {"path": "a.txt"}}
            return "old client should not be called again"
        clients = []
        def client_factory(settings):
            client = MockLLM(script=["new model completed"], model=settings.model)
            clients.append(client)
            return client
        manager = self.manager(responder=old_responder, client_factory=client_factory)
        current = manager.create(str(self.project), "full", mock=True)
        current.submit("keep this task and history")
        self.assertTrue(entered.wait(3))
        old_client = current.runtime.llm.client
        current.update_settings(provider="mock", model="mock-2")
        current.update_settings(provider="mock", model="mock-3")
        self.assertEqual(current.metadata["model"], "mock-1")
        self.assertIs(current.runtime.llm.client, old_client)
        self.assertTrue(all(not client.calls for client in clients))
        release.set()
        self.wait(lambda: not current.busy)
        self.assertEqual(len(old_client.calls), 1)
        self.assertEqual(len(clients[0].calls), 0)
        self.assertEqual(len(clients[1].calls), 1)
        self.assertEqual(current.runtime.settings.model, "mock-3")
        self.assertEqual(current.runtime.compactor.model, "mock-3")
        self.assertIs(current.runtime.compactor.llm.client, current.runtime.llm)
        messages = clients[1].calls[0]["messages"]
        self.assertIn("keep this task and history", json.dumps(messages))
        self.assertIn("existing content", json.dumps(messages))
        events = [json.loads(line) for line in current.runtime.diagnostics.events_path.read_text(encoding="utf-8").splitlines()]
        models = [event["model"] for event in events if event["event"] == "model_start"]
        self.assertEqual(models, ["mock-1", "mock-3"])
        self.assertFalse(current.last_error)

    def test_switch_rejects_old_waiting_approval_without_executing_it(self):
        outside = self.tmp / "outside.txt"
        manager = self.manager(script=[{"tool_calls": [
            {"tool": "write_file", "input": {"path": str(outside), "content": "outside"}},
            {"tool": "write_file", "input": {"path": "inside.txt", "content": "inside"}},
        ]}, "done"])
        current = manager.create(str(self.project), "workspace", mock=True)
        current.submit("write")
        self.wait(lambda: bool(current.snapshot()["pending_approvals"]))
        approval = current.snapshot()["pending_approvals"][0]["id"]
        current.update_settings(permission_mode="read-only")
        self.wait(lambda: not current.busy)
        with self.assertRaises(KeyError):
            current.decide(approval, True)
        self.assertFalse(outside.exists())
        self.assertFalse((self.project / "inside.txt").exists())
        self.assertEqual(current.metadata["permission_mode"], "read-only")
        self.assertEqual(current.snapshot()["pending_approvals"], [])

    def test_docker_mount_is_recreated_when_permission_mode_changes(self):
        manager = self.manager(script=["done"])
        current = manager.create(str(self.project), "workspace", mock=True)
        runtime = current._runtime()
        rw, ro = Mock(), Mock()
        with patch("agent.runtime.DockerSandbox", side_effect=[rw, ro]) as docker:
            self.assertIs(runtime.get_sandbox(self.project), rw)
            for key in CALL_PERMISSION_KEYS:
                runtime.lead_ctx.extra[key] = True
            current.update_settings(permission_mode="read-only")
            rw.close.assert_called_once()
            self.assertIsNone(runtime.sandbox)
            self.assertIs(runtime.get_sandbox(self.project), ro)
            self.assertEqual([call.kwargs["read_only"] for call in docker.call_args_list], [False, True])
            self.assertTrue(all(key not in runtime.lead_ctx.extra for key in CALL_PERMISSION_KEYS))
            current.update_settings(permission_mode="full", confirm_full=True)
            ro.close.assert_called_once()
            self.assertFalse(runtime.sandbox_enabled)
            self.assertFalse(runtime.permissions.sandbox_enabled)
            self.assertNotIn("/bin/bash", runtime.tools.get("bash").description)

    def test_model_client_construction_failure_does_not_queue_partial_permissions(self):
        factory = Mock(side_effect=RuntimeError("client unavailable"))
        manager = self.manager(client_factory=factory)
        current = manager.create(str(self.project), "workspace", mock=True)
        with self.assertRaises(RuntimeError):
            current.update_settings(permission_mode="full", confirm_full=True, model="mock-2")
        self.assertEqual(current.metadata["permission_mode"], "workspace")
        self.assertEqual(current.snapshot()["pending_settings"], {})
