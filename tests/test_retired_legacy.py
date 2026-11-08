import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch,Mock
from sync_tasks import TaskStore,legacy_enabled
from task_service import TaskManager
from runtime_json import atomic_write_json
import twodrive as t
import setup_ui


class RetiredTests(unittest.TestCase):
    def test_reauthorization_probe_never_recreates_retired_cloud_root(self):
        from tests.smoke import cloud_test_targets
        remote,backup,prefix=cloud_test_targets(dict(legacy_enabled=False),'generated-id')
        self.assertEqual(remote,'cloud115:.twodrive-test-generated-id')
        self.assertNotIn('115Drive',backup)
        self.assertEqual(remote,prefix+'generated-id')
        self.assertEqual(cloud_test_targets({},'generated-id')[0],'cloud115:115Drive/.twodrive-test-generated-id')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'app';self.root.mkdir();self.store=TaskStore(self.root)
        self.store.private.mkdir(parents=True)
        atomic_write_json(self.root/'config.json',dict(legacy_enabled=False,local_path=str(self.root.parent/'old'),remote_path='cloud115:old'))
        atomic_write_json(self.store.private/'initialized.json',dict(legacy='preserved'))
        (self.root/'runtime/stop').touch()
        for name in ['one','two']:
            self.store.mutate('create',dict(revision=self.store.read()['revision'],name=name,local_path=str(self.root.parent/name),cloud_path='/'+name,policy='always_keep'))

    def test_legacy_hidden_and_worker_cannot_reuse_old_history(self):
        before=(self.store.private/'initialized.json').read_bytes()
        self.assertFalse(legacy_enabled(self.root));self.assertIsNone(self.store.legacy())
        with patch.object(t,'ROOT',self.root),patch.object(t,'worker_lock',side_effect=AssertionError('Retired worker acquired lock')):
            t.worker()
        with self.assertRaises(RuntimeError):t.sync_once(dict(legacy_enabled=False))
        self.assertEqual((self.store.private/'initialized.json').read_bytes(),before)

    def test_global_pause_preserves_ids_policies_and_avoids_noop_write(self):
        before=self.store.path.read_bytes();self.store.set_all_paused(True);self.assertEqual(self.store.path.read_bytes(),before)
        ids=[x['id'] for x in self.store.read()['tasks']]
        self.store.set_all_paused(False)
        self.assertTrue(all(not x['paused'] and x['policy']=='always_keep' for x in self.store.read()['tasks']))
        self.store.set_all_paused(True);self.assertEqual(ids,[x['id'] for x in self.store.read()['tasks']])

    def test_global_routes_never_resume_legacy_scheduled_task(self):
        handler=types.SimpleNamespace(valid_host=lambda:True,headers={'X-TwoDrive-Nonce':setup_ui.NONCE},send=Mock())
        manager=types.SimpleNamespace(request_check=Mock())
        with patch.object(t,'ROOT',self.root),patch.object(setup_ui.task_service,'_MANAGER',manager),patch.object(t,'run',side_effect=AssertionError('Legacy process launched')):
            handler.path='/resume';setup_ui.Handler.do_POST(handler)
            self.assertTrue(all(not x['paused'] for x in self.store.read()['tasks']))
            self.assertTrue((self.root/'runtime/stop').exists())
            handler.path='/pause';setup_ui.Handler.do_POST(handler)
            self.assertTrue(all(x['paused'] for x in self.store.read()['tasks']))
            self.assertEqual(handler.send.call_args.args[0],200)

    def test_aggregate_does_not_call_waiting_or_failed_tasks_synced(self):
        self.store.set_all_paused(False);tasks={x['id']:x for x in self.store.read()['tasks']}
        manager=TaskManager(types.SimpleNamespace(ROOT=self.root),shell=False)
        for task in tasks.values():
            atomic_write_json(self.store.directory(task['id'])/'status.json',dict(state='idle',last_check_at=123))
            manager.workers[task['id']]=dict(thread=types.SimpleNamespace(is_alive=lambda:True),ready=False)
        manager.publish_summary(tasks)
        self.assertEqual(json.loads((self.root/'runtime/tasks-status.json').read_text())['state'],'checking')
        for row in manager.workers.values():row['ready']=True
        manager.publish_summary(tasks)
        self.assertEqual(json.loads((self.root/'runtime/tasks-status.json').read_text())['state'],'idle')
        key=next(iter(tasks));atomic_write_json(self.store.directory(key)/'status.json',dict(state='error',message='failure'))
        manager.publish_summary(tasks)
        self.assertEqual(json.loads((self.root/'runtime/tasks-status.json').read_text())['state'],'error')
