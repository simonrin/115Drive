import copy
import json
from pathlib import Path
import tempfile
import unittest
from sync_tasks import TaskStore, effective_policy, relative_path, overlaps, file_lock


class TaskConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = TaskStore(self.root/'app')
        self.store.root.mkdir()

    def create(self, **fields):
        body = dict(revision=self.store.read()['revision'], name='任务一', local_path=str(self.root/'one'),
                    cloud_path='/一号', policy='always_keep')
        body.update(fields)
        return self.store.mutate('create', body)

    def test_independent_defaults_and_paused_creation(self):
        a = self.create()
        b = self.create(name='任务二', local_path=str(self.root/'two'), cloud_path='/二号', policy='online_only')
        data = self.store.read()
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual([t['policy'] for t in data['tasks']], ['always_keep', 'online_only'])
        self.assertTrue(all(t['paused'] for t in data['tasks']))
        self.assertFalse((self.root/'one').exists())
        self.assertNotEqual(self.store.directory(a['id']), self.store.directory(b['id']))

    def test_nested_local_or_cloud_paths_rejected(self):
        self.create()
        for fields in (dict(local_path=str(self.root/'one/child'), cloud_path='/other'),
                       dict(local_path=str(self.root/'two'), cloud_path='/一号/child')):
            with self.assertRaises(ValueError): self.create(**fields)
        self.assertEqual(len(self.store.read()['tasks']), 1)

    def test_task_scope_protects_legacy_root(self):
        (self.store.root/'config.json').write_text(json.dumps(dict(local_path=str(self.root/'legacy'), remote_path='cloud115:115Drive')))
        self.store.private.mkdir(parents=True)
        (self.store.private/'initialized.json').write_text('{}')
        with self.assertRaises(ValueError): self.create(cloud_path='/115Drive/child')
        with self.assertRaises(ValueError): self.create(local_path=str(self.root/'legacy/child'))

    def test_policy_precedence_and_default_change_preserves_exceptions(self):
        task_id = self.create()['id']
        self.store.mutate('override', dict(revision=1, id=task_id, path='资料', policy='online_only'))
        self.store.mutate('override', dict(revision=2, id=task_id, path='资料/重要.txt', policy='always_keep'))
        task = self.store.read()['tasks'][0]
        self.assertEqual(effective_policy(task, '其他.txt'), 'always_keep')
        self.assertEqual(effective_policy(task, '资料/a.txt'), 'online_only')
        self.assertEqual(effective_policy(task, '资料/重要.txt'), 'always_keep')
        self.store.mutate('preferences', dict(revision=3, id=task_id, policy='online_only'))
        self.assertEqual(effective_policy(self.store.read()['tasks'][0], '资料/重要.txt'), 'always_keep')

    def test_invalid_policy_does_not_change_config(self):
        task_id = self.create()['id']
        before = self.store.path.read_bytes()
        with self.assertRaises(ValueError): self.store.mutate('preferences', dict(revision=1, id=task_id, policy='erase'))
        self.assertEqual(before, self.store.path.read_bytes())

    def test_stale_revision_cannot_overwrite_another_edit(self):
        task_id = self.create()['id']
        self.store.mutate('resume', dict(id=task_id, revision=1))
        with self.assertRaises(ValueError): self.store.mutate('pause', dict(id=task_id, revision=1))
        self.assertFalse(self.store.read()['tasks'][0]['paused'])

    def test_corrupt_state_is_not_reset(self):
        self.create()
        self.store.path.write_text('[]')
        with self.assertRaises(ValueError): self.store.read()
        self.assertEqual(self.store.path.read_text(), '[]')

    def test_unsafe_paths_and_task_ids(self):
        for name in ('../x', '/x', 'x:stream', 'a//b', 'con', 'a./b', 'a/../b'):
            with self.assertRaises(ValueError, msg=name): relative_path(name)
        with self.assertRaises(ValueError): self.store.directory('../other')
        self.assertFalse(overlaps('/a', '/ab'))

    def test_process_lock_rejects_second_owner(self):
        with file_lock(self.root/'lock'):
            with self.assertRaises(OSError):
                with file_lock(self.root/'lock'): pass

    def test_shell_and_settings_share_persistent_override(self):
        task_id = self.create(policy='online_only')['id']
        task = self.store.shell_override(task_id, 'keep.txt', 'always_keep')
        self.assertEqual(effective_policy(task, 'keep.txt'), 'always_keep')
        self.store.mutate('override', dict(id=task_id, revision=2, path='keep.txt', policy='inherit'))
        self.assertEqual(effective_policy(self.store.read()['tasks'][0], 'keep.txt'), 'online_only')

    def test_manually_edited_config_cannot_claim_program_directory(self):
        self.create()
        data = self.store.read(); data['tasks'][0]['local_path'] = str(self.store.root/'child')
        self.store.path.write_text(json.dumps(data))
        with self.assertRaises(ValueError): self.store.read()


if __name__ == '__main__': unittest.main()
