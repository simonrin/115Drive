import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
from sync_tasks import TaskStore
from task_service import TaskManager, authorization_allowed
from task_backend import validate_manifest
from task_engine import TaskEngine, LinkedCancellation


class FakeEngine:
    def __init__(self, task, directory, backend, **kwargs):
        self.task = task
        self.cancel = threading.Event()
        self.checked = threading.Event()
        self.closed = False
    def start(self, registrar=None):
        if registrar: registrar()
    def cycle(self): self.checked.set()
    def status(self, *args, **kwargs): pass
    def close(self): self.closed = True


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'app'; self.root.mkdir()
        self.store = TaskStore(self.root)

    def create(self, name):
        return self.store.mutate('create', dict(revision=self.store.read()['revision'], name=name,
            local_path=str(self.root.parent/name), cloud_path='/'+name, policy='online_only'))['id']

    def test_independent_workers_pause_and_account_reauthorization_guard(self):
        ids = [self.create('one'), self.create('two')]
        for task_id in ids:
            self.store.mutate('resume', dict(id=task_id, revision=self.store.read()['revision']))
        manager = TaskManager(types.SimpleNamespace(ROOT=self.root), backend_factory=lambda *args:None,
                              engine_factory=FakeEngine, shell=False)
        try:
            manager.tick()
            for row in manager.workers.values():
                for _ in range(100):
                    if row.get('engine'): break
                    threading.Event().wait(.01)
                self.assertTrue(row['engine'].checked.wait(4))
            self.assertFalse(authorization_allowed(self.root))
            self.store.mutate('pause', dict(id=ids[0], revision=self.store.read()['revision']))
            manager.tick()
            first, second = manager.workers[ids[0]], manager.workers[ids[1]]
            first['thread'].join(2)
            self.assertFalse(first['thread'].is_alive())
            self.assertTrue(first['engine'].closed)
            self.assertTrue(second['thread'].is_alive())
            self.assertFalse(second['engine'].cancel.is_set())
            self.store.mutate('pause', dict(id=ids[1], revision=self.store.read()['revision']))
            with patch('task_service._MANAGER', manager):
                self.assertFalse(authorization_allowed(self.root))  # Wait for actual worker exit.
        finally:
            manager.stop.set(); manager.tick()
            for row in list(manager.workers.values()): row['thread'].join(2)

    def test_engine_refuses_corrupt_baseline_without_overwriting_it(self):
        task_id = self.create('one'); task = self.store.read()['tasks'][0]
        directory = self.store.directory(task_id); directory.mkdir(parents=True)
        state = directory/'state.json'; state.write_text('[]')
        with self.assertRaises(RuntimeError):
            TaskEngine(task, directory, None, native_factory=lambda *args, **kwargs:None)
        self.assertEqual(state.read_text(), '[]')

    def test_manifest_rejects_case_collision_missing_parent_and_hash(self):
        good = dict(kind='file', id='1', size=1, sha1='a'*40)
        for rows in ({'A':good, 'a':good}, {'dir/file':good}, {'bad':dict(good,sha1='')},
                     {'../bad':good}):
            with self.assertRaises(ValueError): validate_manifest(rows)

    def test_pause_cancels_native_download_without_waiting_for_windows(self):
        native, pause = threading.Event(), threading.Event()
        linked = LinkedCancellation(native, pause)
        self.assertFalse(linked.wait(0))
        pause.set()
        self.assertTrue(linked.wait(1))
        pause.clear(); native.set()
        self.assertTrue(linked.is_set())


if __name__ == '__main__': unittest.main()
