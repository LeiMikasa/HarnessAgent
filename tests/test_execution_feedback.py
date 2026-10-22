"""Execution reminders preserve fresh checks and never suppress tool calls."""

import copy
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

from common import HarnessCase, make_settings
from agent.llm import MockLLM
from agent.loop import run_loop
from agent.prompt import build_system_prompt
from agent.sandbox import DockerSandbox
from agent.tools.feedback import ExecutionFeedback, _command_kinds
from agent.tools.registry import Tool, ToolContext, ToolRegistry
from agent.tools.result import ToolResult


class ExecutionFeedbackTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(settings=make_settings(self.tmp), workdir=self.tmp)
        self.feedback = ExecutionFeedback()
        self.read = Tool('read_file', '', {}, lambda a, c: '', read_only=True)
        self.write = Tool('write_file', '', {}, lambda a, c: '')
        self.bash = Tool('bash', '', {}, lambda a, c: '')

    def observe(self, name, args, text='ok', tool=None):
        tool = tool or {'read_file':self.read, 'write_file':self.write, 'bash':self.bash}[name]
        result = text if isinstance(text, ToolResult) else ToolResult(text)
        self.feedback.observe(name, args, result, tool, ctx=self.ctx)
        return self.feedback.render(self.ctx)

    def test_docker_prompt_limits_selection_rules_to_available_tools(self):
        prompt = build_system_prompt(workdir=self.tmp, tool_names=['bash'], sandbox_enabled=True)
        self.assertIn('Use bash for Git', prompt)
        self.assertNotIn('Read text files with read_file', prompt)
        self.assertNotIn('Use the grep tool', prompt)
        prompt = build_system_prompt(workdir=self.tmp, tool_names=['bash', 'read_file', 'grep'], sandbox_enabled=True)
        self.assertIn('Read text files with read_file', prompt)
        self.assertIn('Use the grep tool', prompt)
        self.assertIn('Do not bypass', prompt)
        self.assertNotIn('Use edit_file', prompt)

    def test_successful_setup_reminder_survives_inspection(self):
        self.assertIn('dependency-install', self.observe('bash', {'command':'python3 -m pip install -r requirements.txt'}))
        self.assertIn('dependency-install', self.observe('read_file', {'path':'source.py'}))

    def test_observed_success_is_not_a_claim_the_task_or_subcommands_passed(self):
        note = self.observe('bash', {'command':'npm test'})
        self.assertIn('shell invocation returned success', note)
        self.assertIn('not proof', note)

    def test_failed_setup_never_becomes_a_success_record(self):
        result = ToolResult.failure('COMMAND_FAILED', 'failed')
        self.assertEqual(self.observe('bash', {'command':'pip install x'}, result), '')

    def test_identical_read_args_and_visible_result_are_recognized(self):
        args = {'path':'source.py', 'offset':1, 'limit':5}
        self.assertEqual(self.observe('read_file', args, 'one'), '')
        self.assertIn('same visible content', self.observe('read_file', args, 'one'))
        self.assertNotIn('one', repr(self.feedback._reads.values()))

    def test_external_content_changes_do_not_trigger_read_repeat(self):
        self.observe('read_file', {'path':'source.py'}, 'before')
        self.assertEqual(self.observe('read_file', {'path':'source.py'}, 'after'), '')

    def test_known_mutation_invalidates_checks_and_read_freshness(self):
        self.observe('bash', {'command':'npm test'})
        self.observe('read_file', {'path':'source.py'}, 'same')
        self.observe('write_file', {'path':'source.py'}, 'written')
        self.assertEqual(self.observe('read_file', {'path':'source.py'}, 'same'), '')

    def test_unknown_bash_mutation_invalidates_check(self):
        self.observe('bash', {'command':'npm test'})
        self.assertEqual(self.observe('bash', {'command':'custom-mutation'}), '')

    def test_manifest_change_and_failed_reinstall_invalidate_setup(self):
        for path in ('requirements-dev.txt', 'package.json', 'uv.lock'):
            with self.subTest(path=path):
                self.observe('bash', {'command':'pip install -r requirements.txt'})
                self.assertEqual(self.observe('write_file', {'path':path}), '')
        self.observe('bash', {'command':'pip install x'})
        self.assertEqual(self.observe('bash', {'command':'pip install y'}, ToolResult.failure('COMMAND_FAILED','failed')), '')

    def test_scope_and_environment_changes_clear_history(self):
        self.observe('bash', {'command':'npm ci'})
        self.ctx.owner='other'
        self.assertEqual(self.feedback.render(self.ctx), '')
        self.ctx.runtime=SimpleNamespace(permission_mode='workspace', sandbox_enabled=True,
                                         sandbox=SimpleNamespace(name='first'))
        self.observe('bash', {'command':'npm ci'})
        self.ctx.runtime.sandbox.name='second'
        self.assertEqual(self.feedback.render(self.ctx), '')

    def test_records_and_rendering_are_bounded_without_raw_payloads(self):
        for index in range(100):
            self.observe('read_file', {'path':f'{index}.py'}, 'content'*1000)
        self.assertEqual(len(self.feedback._reads), self.feedback.MAX_RECORDS)
        note=self.observe('read_file', {'path':'99.py'}, 'content'*1000)
        self.assertLessEqual(len(note), 1600)
        self.assertNotIn('99.py', note)

    def test_command_labels_ignore_echo_and_inline_script_arguments(self):
        self.assertEqual(_command_kinds('echo "pip install x"'), set())
        self.assertEqual(_command_kinds('python -c "print(\'npm test\')"'), set())
        self.assertEqual(_command_kinds('cd /workspace && python3 -m unittest discover | tail -40'), {'test'})
        self.assertEqual(_command_kinds('npm.cmd ci && npm.cmd run build'), {'dependency-install','test/build/check'})
        self.assertEqual(_command_kinds('"C:\\project\\python.exe" -m pip install x'), {'dependency-install'})

    def test_repeated_bash_counts_successes_and_changing_outputs_without_storing_args(self):
        args = {'command': 'custom-command --private-value'}
        self.assertNotIn('[repeated bash call]', self.observe('bash', args, 'first'))
        self.assertNotIn('[repeated bash call]', self.observe('bash', args, 'second'))
        note = self.observe('bash', args, ToolResult.failure('COMMAND_FAILED', 'third'))
        self.assertIn('[repeated bash call]', note)
        self.assertIn('3 times', note)
        self.assertIn('polling', note)
        self.assertNotIn('private-value', note + str(self.feedback._last_bash))
        self.assertLessEqual(len(note), 1600)

    def test_bash_repeat_survives_inspection_but_resets_after_mutation_or_different_args(self):
        args = {'command': 'custom-check'}
        for _ in range(2):
            self.observe('bash', args)
            self.observe('read_file', {'path': 'source.py'})
        self.assertIn('[repeated bash call]', self.observe('bash', args))
        self.assertNotIn('[repeated bash call]', self.observe('bash', {'command': 'other-check'}))
        for _ in range(3):
            self.observe('bash', args)
        self.observe('write_file', {'path': 'source.py'}, ToolResult.failure('TOOL_ERROR', 'unknown'))
        self.assertNotIn('[repeated bash call]', self.observe('bash', args))

    def test_bash_repeat_resets_when_execution_scope_changes(self):
        for _ in range(3):
            self.observe('bash', {'command': 'custom-check'})
        self.ctx.owner = 'teammate'
        self.assertNotIn('[repeated bash call]', self.feedback.render(self.ctx))
        self.observe('bash', {'command': 'custom-check'})
        self.ctx.runtime = SimpleNamespace(permission_mode='read-only', sandbox_enabled=False, sandbox=None)
        self.assertNotIn('[repeated bash call]', self.feedback.render(self.ctx))

    def test_loop_reminds_repeated_successful_bash_but_executes_all_calls_and_permission_hooks(self):
        from agent.events import Hooks, PRE_TOOL_USE

        calls, permission_checks, requests = [], [], []
        registry = ToolRegistry()
        registry.add('bash', '', {'type': 'object'}, lambda a, c: calls.append(a) or f'output {len(calls)}')
        hooks = Hooks()
        hooks.register(PRE_TOOL_USE, lambda block, ctx: permission_checks.append(block['name']))
        llm = MockLLM(script=[{'tool': 'bash', 'input': {'command': 'custom-check'}}] * 4 + ['done'])
        original = llm.create
        def create(**kwargs):
            requests.append(kwargs['system'])
            return original(**kwargs)
        llm.create = create
        result = run_loop(llm=llm, registry=registry, system='sys', ctx=self.ctx, hooks=hooks,
                          messages=[{'role': 'user', 'content': 'inspect'}], max_turns=6)
        self.assertTrue(result.ok)
        self.assertEqual((result.turns, len(calls), len(permission_checks)), (5, 4, 4))
        self.assertNotIn('[repeated bash call]', requests[2])
        self.assertIn('[repeated bash call]', requests[3])
        fresh = MockLLM(script=['done'])
        fresh_create = fresh.create
        fresh_requests = []
        def create_fresh(**kwargs):
            fresh_requests.append(kwargs['system'])
            return fresh_create(**kwargs)
        fresh.create = create_fresh
        run_loop(llm=fresh, registry=registry, system='sys', ctx=self.ctx,
                 messages=[{'role': 'user', 'content': 'new task'}], max_turns=1)
        self.assertNotIn('[repeated bash call]', fresh_requests[0])

    def test_loop_injects_reminder_but_executes_every_call(self):
        calls=[]
        registry=ToolRegistry()
        registry.add('read_file','',{'type':'object'},lambda a,c: calls.append(a) or 'same',read_only=True)
        llm=MockLLM(script=[{'tool':'read_file','input':{'path':'source.py'}}]*3+['done'])
        requests=[]
        original=llm.create
        def create(**kwargs):
            requests.append(copy.deepcopy(kwargs))
            return original(**kwargs)
        llm.create=create
        result=run_loop(llm=llm,registry=registry,system='sys',ctx=self.ctx,
                        messages=[{'role':'user','content':'inspect'}],max_turns=6)
        self.assertTrue(result.ok)
        self.assertEqual(len(calls),3)
        self.assertEqual(result.turns,4)
        self.assertIn('[execution record]',requests[2]['system'])
        self.assertNotIn('[execution record]',requests[0]['system'])
        # A new task starts with no historical feedback.
        fresh=MockLLM(script=['done'])
        fresh_system=[]
        fresh_create=fresh.create
        def create_fresh(**kwargs):
            fresh_system.append(kwargs['system'])
            return fresh_create(**kwargs)
        fresh.create=create_fresh
        run_loop(llm=fresh,registry=registry,system='sys',ctx=self.ctx,
                 messages=[{'role':'user','content':'new task'}],max_turns=1)
        self.assertNotIn('[execution record]',fresh_system[0])

    def test_docker_execution_enables_pipefail_without_rewriting_command(self):
        box=DockerSandbox(str(self.tmp))
        box.started=True
        box._docker=Mock(return_value=subprocess.CompletedProcess([],7,'failed',''))
        command="some-check | tail -40"
        self.assertEqual(box.execute(command).returncode,7)
        self.assertEqual(box._docker.call_args.args[0][-5:], ['bash','-o','pipefail','-c',command])
