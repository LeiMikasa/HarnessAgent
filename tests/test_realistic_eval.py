"""Independent checks of rebuilt fixtures, grading and Codex JSON transport."""

from collections import Counter
import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common import HarnessCase, make_settings
from evals.codex_llm import CodexLLM
from evals.realistic_cases import FAMILIES, build_specs, grade, load_specs, materialize
from evals.recall_real import main, select_specs
from evals.recall50 import CheckpointLLM, SuiteMockLLM, run_worker
from agent.llm import LLMResponse


class RealisticCasesTests(HarnessCase):
    def test_versioned_dataset_is_deterministic_and_balanced(self):
        specs = load_specs()
        self.assertEqual(specs, build_specs())
        self.assertEqual(len({s['id'] for s in specs}), 50)
        self.assertEqual(Counter(s['family'] for s in specs), dict.fromkeys(FAMILIES, 10))
        self.assertEqual({s['pressure'] for s in specs}, {'medium', 'high'})
        for spec in specs:
            first = materialize(spec)
            self.assertEqual(first, materialize(spec))
            self.assertEqual(len(first['expected']), 8)
            self.assertEqual(len(first['absent_keys']), 2)

    def case(self):
        return materialize(next(s for s in load_specs() if s['id'] == 'live-files-orders'))

    def prepare_answer(self, case):
        for relative, text in case['files'].items():
            self.write(relative, text)
        answer = {**case['expected'], **dict.fromkeys(case['absent_keys'])}
        self.write('answer.json', json.dumps(answer, ensure_ascii=False))

    def test_current_file_fields_replace_historical_values(self):
        case = self.case()
        current = json.loads(case['files']['config/production.json'])['settings']
        self.assertEqual({key: case['expected'][key] for key in current}, current)
        self.assertIn('config/production.json', json.dumps(case['history']))
        self.prepare_answer(case)
        result = grade(self.tmp, case)
        self.assertTrue(result['passed'])
        self.assertEqual(result['correct_items'], 8)
        self.assertEqual(result['correct_abstentions'], 2)

    def test_wrong_types_and_guessed_unknowns_fail(self):
        case = self.case()
        self.prepare_answer(case)
        answer = json.loads((self.tmp/'answer.json').read_text(encoding='utf-8'))
        answer['api_port'] = str(answer['api_port'])
        answer['incident_commander'] = 'guessed'
        self.write('answer.json', json.dumps(answer))
        result = grade(self.tmp, case)
        self.assertFalse(result['passed'])
        self.assertEqual(result['correct_items'], 7)
        self.assertEqual(result['correct_abstentions'], 1)

    def test_changing_source_files_does_not_pass(self):
        case = self.case()
        self.prepare_answer(case)
        self.write('config/production.json', '{}')
        result = grade(self.tmp, case)
        self.assertFalse(result['passed'])
        self.assertEqual(result['modified_input_files'], ['config/production.json'])

    def test_selector_defaults_to_one_pair_and_samples_families(self):
        self.assertEqual(len(select_specs()), 1)
        self.assertEqual(len(select_specs(all_cases=True)), 50)
        self.assertEqual({s['family'] for s in select_specs(limit=5)}, set(FAMILIES))
        with self.assertRaises(ValueError):
            select_specs(ids='does-not-exist')


class DeepSeekEvaluationTests(HarnessCase):
    def settings(self, **overrides):
        settings = make_settings(self.tmp, provider="deepseek", model="deepseek-flash",
                                 api_key="fixture-secret", base_url="https://fixture.test/anthropic")
        return replace(settings, **{key: value for key, value in overrides.items() if value is not None})

    def invoke(self, *args):
        output = io.StringIO()
        with patch("sys.argv", ["recall_real", *args]), contextlib.redirect_stdout(output):
            result = main()
        return result, output.getvalue()

    def test_deepseek_plan_resolves_configured_model_without_calls_or_codex_login(self):
        with patch("evals.recall_real.load_settings", side_effect=self.settings), \
                patch("evals.recall_real.ensure_plan_auth", side_effect=AssertionError("No Codex login")), \
                patch("evals.recall50.build_client", side_effect=AssertionError("No API client")):
            result, output = self.invoke("--provider", "deepseek", "--limit", "5", "--plan")
        config = json.loads(output.split("\n", 1)[1])
        self.assertEqual(result, 0)
        self.assertEqual(config["model"], "deepseek-flash")
        self.assertEqual(config["base_url"], "https://fixture.test/anthropic")
        self.assertEqual(len(config["case_ids"]), 5)
        self.assertNotIn("fixture-secret", output)
        self.assertFalse(list(self.tmp.glob("realistic-*")))

    def test_explicit_model_and_existing_codex_default_are_preserved(self):
        with patch("evals.recall_real.load_settings", side_effect=self.settings):
            _, output = self.invoke("--provider", "deepseek", "--model", "another-model", "--plan")
        self.assertEqual(json.loads(output.split("\n", 1)[1])["model"], "another-model")
        _, output = self.invoke("--provider", "codex", "--plan")
        self.assertEqual(json.loads(output.split("\n", 1)[1])["model"], "gpt-6.1-sol")

    def test_missing_key_stops_before_creating_a_paid_batch(self):
        with patch("evals.recall_real.load_settings", return_value=self.settings(api_key="")), \
                patch("evals.recall50.build_client", side_effect=AssertionError("No API calls")):
            with self.assertRaisesRegex(ValueError, "API key is missing"):
                self.invoke("--provider", "deepseek", "--output", str(self.tmp))
        self.assertFalse(list(self.tmp.glob("realistic-*")))

    def test_native_provider_pair_runs_real_loop_and_scores_without_network(self):
        case = materialize(next(s for s in load_specs() if s["id"] == "live-files-orders"))
        clients = []

        class UsageFixture(SuiteMockLLM):
            def create(self, **kwargs):
                response = super().create(**kwargs)
                response.usage = {"input_tokens": 100, "output_tokens": 20,
                                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
                return response

        def client(settings):
            clients.append(settings)
            return UsageFixture(case)

        with patch("evals.recall_real.load_settings", side_effect=self.settings), \
                patch("evals.recall50.load_settings", side_effect=self.settings), \
                patch("evals.recall50.build_client", side_effect=client), \
                patch("evals.recall_real.ensure_plan_auth", side_effect=AssertionError("No Codex")):
            result, _ = self.invoke("--provider", "deepseek", "--ids", "live-files-orders",
                                    "--output", str(self.tmp))
        self.assertEqual(result, 0)
        self.assertEqual([s.model for s in clients], ["deepseek-flash"] * 2)
        batch = next(self.tmp.glob("realistic-*"))
        summary = json.loads((batch / "summary.json").read_text(encoding="utf-8"))
        self.assertIn("native tool calls", summary["measurement_note"])
        self.assertNotIn("Fresh ephemeral Codex", summary["measurement_note"])
        for mode in ("full", "compact"):
            record = json.loads((batch / "live-files-orders" / mode / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(record["correct_items"], 8)
            self.assertEqual(record["correct_abstentions"], 2)
            self.assertTrue(record["token_usage_complete"])
            self.assertEqual(record["model"], "deepseek-flash")
        self.assertNotIn("fixture-secret", (batch / "config.json").read_text())
        # Resume must reuse saved results, not issue more model requests.
        with patch("evals.recall_real.load_settings", side_effect=self.settings), \
                patch("evals.recall_real.run_worker", side_effect=AssertionError("No rerun")):
            result, _ = self.invoke("--provider", "deepseek", "--ids", "live-files-orders", "--resume", str(batch))
        self.assertEqual(result, 0)
        # A Ctrl+C result is retained as evidence, but is not treated as a
        # completed trial during resume.
        interrupted = batch / "live-files-orders" / "compact" / "result.json"
        saved = json.loads(interrupted.read_text(encoding="utf-8"))
        interrupted.write_text(json.dumps({**saved, "status": "interrupted", "success": False}))
        with patch("evals.recall_real.load_settings", side_effect=self.settings), \
                patch("evals.recall50.load_settings", side_effect=self.settings), \
                patch("evals.recall50.build_client", side_effect=client):
            result, _ = self.invoke("--provider", "deepseek", "--ids", "live-files-orders", "--resume", str(batch))
        self.assertEqual(result, 0)
        self.assertEqual(len(clients), 3)
        self.assertEqual(len(list((batch / "live-files-orders").glob("compact-interrupted-*"))), 1)

    def test_expired_budget_does_not_issue_a_model_request(self):
        from unittest.mock import Mock
        inner = Mock()
        llm = CheckpointLLM(inner, self.tmp / "telemetry.json", deadline=0)
        with self.assertRaisesRegex(TimeoutError, "deadline"):
            llm.create(system="rules", messages=[], tools=[])
        inner.create.assert_not_called()
        self.assertEqual(llm.calls, 0)

    def test_native_sdk_wait_is_capped_and_retries_disabled_only_for_evaluation(self):
        from unittest.mock import Mock
        sdk = Mock()
        inner = SimpleNamespace(provider="fixture", model="fixture", _client=sdk)
        inner.create = lambda **kwargs: LLMResponse([{"type": "text", "text": "ok"}], "end_turn")
        with patch("evals.recall50.time.monotonic", return_value=10):
            llm = CheckpointLLM(inner, self.tmp / "telemetry.json", deadline=20)
            llm.create(system="rules", messages=[], tools=[])
        sdk.with_options.assert_called_once_with(timeout=10, max_retries=0)
        self.assertIs(inner._client, sdk)

    def test_worker_missing_key_never_constructs_client(self):
        spec = select_specs()[0]
        run_dir = self.tmp / "missing-key"
        run_dir.mkdir()
        with patch("evals.recall50.load_settings", return_value=self.settings(api_key="")), \
                patch("evals.recall50.build_client", side_effect=AssertionError("No client")):
            result = run_worker(spec, "full", run_dir, "deepseek", "deepseek-flash", 10, 4000,
                                case=materialize(spec), grader=grade)
        self.assertEqual(result["status"], "runtime_error")
        self.assertIn("API key is missing", result["error"])

    def test_authentication_failure_stops_batch_without_running_remaining_cases(self):
        from agent.llm import LLMError
        from unittest.mock import Mock
        client = Mock(provider="fixture", model="deepseek-flash")
        client.create.side_effect = LLMError("AuthenticationError: status code 401")
        with patch("evals.recall_real.load_settings", side_effect=self.settings), \
                patch("evals.recall50.load_settings", side_effect=self.settings), \
                patch("evals.recall50.build_client", return_value=client):
            result, _ = self.invoke("--provider", "deepseek", "--limit", "5", "--output", str(self.tmp))
        self.assertEqual(result, 1)
        self.assertEqual(client.create.call_count, 1)
        stopped = json.loads(next(self.tmp.glob("realistic-*/stopped.json")).read_text())
        self.assertEqual(stopped["completed_runs"], 1)
        self.assertEqual(stopped["planned_runs"], 10)
        self.assertIn("deepseek", stopped["reason"])


class CodexTransportTests(HarnessCase):
    def client(self):
        with patch('evals.codex_llm.shutil.which', return_value='codex.exe'):
            return CodexLLM('fixture-model', self.tmp/'transport')

    def output(self, calls=None, usage=None, extra=None):
        events = [
            {'type': 'thread.started', 'thread_id': 'fixture'},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(
                {'text': 'done', 'tool_calls': calls or []})}},
            {'type': 'turn.completed', 'usage': usage},
        ]
        if extra:
            events.insert(1, extra)
        return '\n'.join(json.dumps(event) for event in events)

    def test_usage_preserves_raw_totals_without_double_counting_cache(self):
        usage = {'input_tokens': 110, 'cached_input_tokens': 80, 'output_tokens': 15}
        result = self.client().parse_output(self.output(usage=usage), [])
        self.assertEqual(result.usage['input_tokens'], 110)
        self.assertEqual(result.usage['cache_read_input_tokens'], 80)
        self.assertEqual(result.usage['raw_codex_usage'], usage)
        self.assertEqual(result.stop_reason, 'end_turn')

    def test_usage_missing_is_unknown(self):
        self.assertIsNone(self.client().parse_output(self.output(), []).usage)

    def test_json_tool_request_becomes_minicode_tool_block(self):
        calls = [{'name': 'read_file', 'arguments_json': '{"path":"config/production.json"}'}]
        result = self.client().parse_output(self.output(calls), [{'name':'read_file'}])
        self.assertEqual(result.stop_reason, 'tool_use')
        self.assertEqual(result.content[-1]['input'], {'path':'config/production.json'})

    def test_builtin_tool_activity_invalidates_trial(self):
        extra = {'type':'item.started', 'item':{'type':'command_execution', 'command':'pwd'}}
        with self.assertRaisesRegex(RuntimeError, 'built-in tool'):
            self.client().parse_output(self.output(extra=extra), [])

    def test_failed_or_incomplete_turn_is_not_accepted(self):
        for output in ('', '{"type":"turn.failed"}', self.output() + '\n{"type":"error"}'):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                self.client().parse_output(output, [])

    def test_unknown_tools_and_non_object_arguments_are_rejected(self):
        for name, arguments, offered in (('bash', '{}', []), ('read_file', '[]', [{'name':'read_file'}])):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.client().parse_output(self.output([{'name': name, 'arguments_json': arguments}]), offered)

    def test_request_uses_fresh_empty_cwd_and_removes_api_key_overrides(self):
        client = self.client()
        completed = SimpleNamespace(returncode=0, stdout=self.output(), stderr='')
        with patch.dict('os.environ', {'OPENAI_API_KEY':'fixture-secret', 'CODEX_API_KEY':'fixture-secret'}), \
                patch('evals.codex_llm.subprocess.run', return_value=completed) as run:
            client.create(system='rules', messages=[{'role':'user','content':'fixture'}], tools=[], max_tokens=40)
        command = run.call_args.args[0]
        self.assertIn('--ephemeral', command)
        self.assertIn('--ignore-user-config', command)
        self.assertIn('features.shell_tool=false', command)
        self.assertNotIn('OPENAI_API_KEY', run.call_args.kwargs['env'])
        self.assertNotIn('CODEX_API_KEY', run.call_args.kwargs['env'])
        self.assertEqual(list((self.tmp/'transport/call-001/empty').iterdir()), [])
        self.assertIn('fixture', run.call_args.kwargs['input'])
