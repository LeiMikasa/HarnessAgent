"""Real Git cleanup checks, confined to an explicitly initialized test repository."""

import shutil
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from common import HarnessCase
from agent.tasks import COMPLETED, TaskStore
from agent.teams import WorktreeManager
from agent.team_tools import REMOVE_WORKTREE_SCHEMA, run_remove_worktree


class CleanupToolTests(unittest.TestCase):
    def test_option_is_exposed_and_forwarded(self):
        self.assertEqual(REMOVE_WORKTREE_SCHEMA['properties']['delete_branch']['type'], 'boolean')
        worktrees = Mock()
        ctx = SimpleNamespace(runtime=SimpleNamespace(teams=SimpleNamespace(worktrees=worktrees)))
        run_remove_worktree({'name': 'worker', 'delete_branch': True}, ctx)
        worktrees.remove.assert_called_once_with('worker', discard_changes=False, delete_branch=True)


@unittest.skipUnless(shutil.which('git'), 'git is not installed')
class WorktreeCleanupTests(HarnessCase):
    def git(self, *args, cwd=None):
        return subprocess.run(
            ['git', '-c', 'user.name=cleanup-test', '-c', 'user.email=cleanup@example.test', *args],
            cwd=cwd or self.tmp, check=True, capture_output=True, text=True).stdout.strip()

    def setUp(self):
        super().setUp()
        # Catch upward discovery regressions before initializing any repository.
        self.assertFalse(WorktreeManager(self.tmp, self.tmp / 'trees').is_git_repo())
        self.git('init', '-q')
        (self.tmp / '.gitignore').write_text('.agent/\nignored.tmp\n', encoding='utf-8')
        (self.tmp / 'source.txt').write_text('baseline\n', encoding='utf-8')
        self.git('add', '.')
        self.git('commit', '-qm', 'baseline')
        self.original_branch = self.git('branch', '--show-current')
        self.tasks = TaskStore(self.tmp / '.agent' / 'tasks')
        self.task = self.tasks.create('deliver feature')
        self.manager = WorktreeManager(self.tmp, self.tmp / '.agent' / 'worktrees', tasks=self.tasks)
        self.name = 'worker'
        self.path = self.tmp / '.agent' / 'worktrees' / self.name
        self.assertTrue(str(self.manager.create(self.name, self.task.id)).startswith('Created'))
        self.task = self.tasks.load(self.task.id)
        self.task.status = COMPLETED
        self.tasks.save(self.task)

    def branch_exists(self):
        return bool(self.git('branch', '--list', 'wt/worker'))

    def delivery(self):
        (self.path / 'source.txt').write_text('delivered\n', encoding='utf-8')
        self.git('add', 'source.txt', cwd=self.path)
        self.git('commit', '-qm', 'delivery', cwd=self.path)

    def test_default_retains_branch_and_unbinds_task(self):
        self.assertIn('retained', self.manager.remove(self.name))
        self.assertFalse(self.path.exists())
        self.assertTrue(self.branch_exists())
        self.assertIsNone(self.tasks.load(self.task.id).worktree)

    def test_integrated_delivery_deletes_worktree_and_branch(self):
        self.delivery()
        self.git('merge', '--ff-only', 'wt/worker')
        result = self.manager.remove(self.name, delete_branch=True)
        self.assertIn('deleted integrated branch', result)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.branch_exists())
        self.assertIsNone(self.tasks.load(self.task.id).worktree)
        self.assertEqual(self.git('branch', '--show-current'), self.original_branch)
        self.assertEqual((self.tmp / 'source.txt').read_text(), 'delivered\n')

    def test_unmerged_delivery_keeps_both(self):
        self.delivery()
        result = self.manager.remove(self.name, delete_branch=True)
        self.assertFalse(result.ok)
        self.assertIn('not verified as integrated', result)
        self.assertTrue(self.path.exists())
        self.assertTrue(self.branch_exists())
        self.assertEqual(self.tasks.load(self.task.id).worktree, self.name)

    def test_cherry_picked_delivery_is_conservatively_retained(self):
        self.delivery()
        (self.tmp / 'lead.txt').write_text('independent work\n', encoding='utf-8')
        self.git('add', 'lead.txt')
        self.git('commit', '-qm', 'lead progress')
        self.git('cherry-pick', 'wt/worker')
        self.assertEqual((self.tmp / 'source.txt').read_text(), 'delivered\n')
        self.assertFalse(self.manager.remove(self.name, delete_branch=True).ok)
        self.assertTrue(self.path.exists())
        self.assertTrue(self.branch_exists())

    def test_lease_blocks_cleanup(self):
        self.manager.assign('teammate', self.task.id, self.name)
        self.assertIn('still assigned', self.manager.remove(self.name, delete_branch=True))
        self.assertTrue(self.path.exists())

    def test_unfinished_binding_blocks_cleanup_even_with_discard(self):
        self.task.status = 'in_progress'
        self.tasks.save(self.task)
        result = self.manager.remove(self.name, discard_changes=True)
        self.assertIn('unfinished', result)
        self.assertTrue(self.path.exists())

    def test_dirty_untracked_and_ignored_files_block_cleanup(self):
        for filename in ('source.txt', 'new.txt', 'ignored.tmp'):
            with self.subTest(filename=filename):
                target = self.path / filename
                target.write_text('local work\n', encoding='utf-8')
                self.assertFalse(self.manager.remove(self.name, delete_branch=True).ok)
                self.assertTrue(target.exists())
                if filename == 'source.txt':
                    self.git('restore', 'source.txt', cwd=self.path)
                else:
                    target.unlink()

    def test_status_failure_preserves_worktree(self):
        original = self.manager._run_git
        def fail_status(args, cwd=None):
            return (False, 'cannot inspect') if args[0] == 'status' else original(args, cwd)
        with patch.object(self.manager, '_run_git', side_effect=fail_status):
            self.assertFalse(self.manager.remove(self.name).ok)
        self.assertTrue(self.path.exists())

    def test_wrong_branch_is_not_removed(self):
        self.git('switch', '-c', 'user-branch', cwd=self.path)
        self.assertFalse(self.manager.remove(self.name, delete_branch=True).ok)
        self.assertTrue(self.path.exists())

    def test_locked_worktree_is_preserved(self):
        self.git('worktree', 'lock', str(self.path))
        self.assertFalse(self.manager.remove(self.name, delete_branch=True).ok)
        self.assertTrue(self.path.exists())

    def test_discard_cannot_be_combined_with_branch_deletion(self):
        result = self.manager.remove(self.name, discard_changes=True, delete_branch=True)
        self.assertFalse(result.ok)
        self.assertTrue(self.path.exists())

    def test_partial_failure_reports_retained_branch(self):
        original = self.manager._run_git
        def fail_delete(args, cwd=None):
            return (False, 'simulated Git failure') if args[:2] == ['branch', '-d'] else original(args, cwd)
        with patch.object(self.manager, '_run_git', side_effect=fail_delete):
            result = self.manager.remove(self.name, delete_branch=True)
        self.assertFalse(result.ok)
        self.assertIn('Removed worktree', result)
        self.assertIn('branch wt/worker retained', result)
        self.assertFalse(self.path.exists())
        self.assertTrue(self.branch_exists())
        self.assertIsNone(self.tasks.load(self.task.id).worktree)

    def test_recheck_after_remove_preserves_branch_on_failure(self):
        original = self.manager._run_git
        checks = 0
        def fail_second_check(args, cwd=None):
            nonlocal checks
            if args[0] == 'merge-base':
                checks += 1
                if checks == 2:
                    return False, 'HEAD changed'
            return original(args, cwd)
        with patch.object(self.manager, '_run_git', side_effect=fail_second_check):
            result = self.manager.remove(self.name, delete_branch=True)
        self.assertFalse(result.ok)
        self.assertFalse(self.path.exists())
        self.assertTrue(self.branch_exists())

