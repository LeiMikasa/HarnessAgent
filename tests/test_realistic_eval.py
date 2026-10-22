"""Independent checks of rebuilt fixtures, grading and Codex JSON transport."""

from collections import Counter
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common import HarnessCase
from evals.codex_llm import CodexLLM
from evals.realistic_cases import FAMILIES, build_specs, grade, load_specs, materialize
from evals.recall_real import select_specs


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
