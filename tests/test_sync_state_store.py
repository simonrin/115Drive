import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sync_state_store import DATABASE, StateStore, StateStoreError, load_state
from sync_tasks import file_lock


def baseline():
    return dict(version=1, task='1'*32, local=r'F:\state-store-test', cloud='/测试',
                backend='account-root-identity', local_root_id=123, health='health-identity',
                pending=None, items={
                    'one.txt':dict(remote=dict(id='1', kind='file', size=1, sha1='a'*40), usn=17),
                    'two.txt':dict(remote=dict(id='2', kind='file', size=2, sha1='b'*40), usn=18)})


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1]/'runtime/state-store-tests'
        parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=parent)
        self.directory = Path(self.temporary.name)
        self.path = self.directory/'state.json'
        self.original = baseline()
        self.raw = json.dumps(self.original, ensure_ascii=False, indent=4).encode('utf-8')+b'\n'
        self.path.write_bytes(self.raw)
        self.stores = []

    def tearDown(self):
        for store in self.stores: store.close(export=False)
        self.temporary.cleanup()

    def store(self, **kwargs):
        store = StateStore(self.directory, **kwargs)
        self.stores.append(store)
        return store

    def sql(self, statement, values=()):
        connection = sqlite3.connect(self.directory/DATABASE)
        try:
            with connection: return connection.execute(statement, values).fetchall()
        finally: connection.close()

    def test_initial_import_preserves_exact_legacy_bytes_and_metadata(self):
        stamp = self.path.stat().st_mtime_ns
        store = self.store()
        self.assertEqual(store.load(), self.original)
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertFalse(store.export())
        self.assertEqual(self.path.stat().st_mtime_ns, stamp)
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(store.connection.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.assertEqual(store.connection.execute('PRAGMA synchronous').fetchone()[0], 2)

    def test_read_helper_without_database_has_no_migration_side_effect(self):
        self.assertEqual(load_state(self.directory), self.original)
        self.assertFalse((self.directory/DATABASE).exists())

    def test_changed_row_and_tombstone_only(self):
        store = self.store(); previous = store.load()
        statements = []
        store.connection.set_trace_callback(statements.append)
        current = copy.deepcopy(previous)
        current['items']['one.txt']['usn'] += 1
        current['items'].pop('two.txt')
        current['pending'] = dict(path='three.txt', stage='op-3')
        self.assertTrue(store.save(current, previous))
        self.assertEqual(store.metrics['item_upserts'], 1)
        self.assertEqual(store.metrics['item_deletes'], 1)
        self.assertEqual(sum(s.startswith('INSERT INTO items') for s in statements), 1)
        self.assertEqual(sum(s.startswith('DELETE FROM items') for s in statements), 1)
        self.assertEqual(load_state(self.directory), current)
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_no_change_performs_no_sql_mutation_or_json_write(self):
        store = self.store(); previous = store.load()
        trace = []
        store.connection.set_trace_callback(trace.append)
        before = store.connection.total_changes
        stamp = self.path.stat().st_mtime_ns
        for _ in range(4):
            self.assertFalse(store.save(copy.deepcopy(previous), previous))
            self.assertFalse(store.export())
        self.assertEqual(store.connection.total_changes, before)
        self.assertFalse(any(s.startswith(('INSERT','UPDATE','DELETE','COMMIT')) for s in trace))
        self.assertEqual(self.path.stat().st_mtime_ns, stamp)

    def test_close_exports_acknowledged_state(self):
        store = self.store(); previous = store.load()
        current = copy.deepcopy(previous); current['items'].pop('one.txt')
        store.save(current, previous)
        store.close()
        self.assertEqual(json.loads(self.path.read_bytes()), current)
        self.assertEqual(load_state(self.directory), current)

    def test_crash_before_transaction_commit_rolls_back(self):
        def fault(step):
            if step == 'before_state_commit': raise RuntimeError('injected failure')
        store = self.store(fault=fault); previous = store.load()
        current = copy.deepcopy(previous); current['items'].clear()
        with self.assertRaisesRegex(RuntimeError, 'injected'): store.save(current, previous)
        self.assertEqual(load_state(self.directory), previous)
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_failure_after_commit_does_not_lose_acknowledged_state(self):
        def fault(step):
            if step == 'after_state_commit': raise RuntimeError('injected failure')
        store = self.store(fault=fault); previous = store.load()
        current = copy.deepcopy(previous); current['items'].pop('one.txt')
        with self.assertRaisesRegex(RuntimeError, 'injected'): store.save(current, previous)
        store.close(export=False)
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(load_state(self.directory), current)

    def test_export_replace_before_hash_commit_recovers_exact_canonical_content(self):
        def fault(step):
            if step == 'after_export_replace': raise RuntimeError('injected failure')
        store = self.store(fault=fault); previous = store.load()
        current = copy.deepcopy(previous); current['items'].clear()
        store.save(current, previous)
        with self.assertRaisesRegex(RuntimeError, 'injected'): store.export()
        store.close(export=False)
        self.assertEqual(json.loads(self.path.read_bytes()), current)
        self.assertEqual(load_state(self.directory), current)
        recovered = self.store(); self.assertEqual(recovered.load(), current)
        stamp = self.path.stat().st_mtime_ns
        self.assertFalse(recovered.export())
        self.assertEqual(self.path.stat().st_mtime_ns, stamp)
        exported = self.sql('SELECT export_hash FROM control')[0][0]
        self.assertEqual(exported, hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_before_export_replace_keeps_prior_json_and_canonical_database(self):
        def fault(step):
            if step == 'before_export_replace': raise RuntimeError('injected failure')
        store = self.store(fault=fault); previous = store.load()
        current = copy.deepcopy(previous); current['items'].clear()
        store.save(current, previous)
        with self.assertRaisesRegex(RuntimeError, 'injected'): store.export()
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(load_state(self.directory), current)

    def test_external_json_edit_never_overwrites_database(self):
        store = self.store(); previous = store.load()
        external = copy.deepcopy(previous); external['items'].clear()
        self.path.write_text(json.dumps(external), encoding='utf-8')
        for action in (lambda: load_state(self.directory), store.load, store.export,
                       lambda: store.save(previous, previous)):
            with self.assertRaises(StateStoreError): action()
        self.assertEqual(self.sql('SELECT count(*) FROM items')[0][0], 2)
        self.assertEqual(json.loads(self.path.read_bytes()), external)

    def test_external_edit_with_preserved_mtime_is_detected(self):
        store = self.store(); previous = store.load()
        stat = self.path.stat()
        self.path.write_bytes(self.raw.replace(b'"usn": 17', b'"usn": 99'))
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with self.assertRaises(StateStoreError): store.save(previous, previous)

    def test_missing_export_after_import_fails_closed(self):
        store = self.store(); store.load(); self.path.unlink()
        with self.assertRaises(StateStoreError): load_state(self.directory)
        with self.assertRaises(StateStoreError): store.export()

    def test_new_task_without_json_is_durable_and_exportable(self):
        self.path.unlink()
        store = self.store()
        state = baseline(); state['backend'] = None; state.pop('health'); state.pop('local_root_id')
        self.assertFalse(store.save(state))  # Initial import already committed it.
        self.assertEqual(load_state(self.directory), state)
        self.assertTrue(store.export())
        self.assertEqual(json.loads(self.path.read_bytes()), state)

    def test_root_identity_changes_are_rejected(self):
        store = self.store(); previous = store.load()
        values = dict(task='2'*32, local=r'F:\another-root', cloud='/其他', backend='another-account',
                      local_root_id=456, health='another-health', version=2)
        for key, value in values.items():
            current = copy.deepcopy(previous); current[key] = value
            with self.subTest(key=key), self.assertRaises(StateStoreError): store.save(current, previous)
        self.assertEqual(load_state(self.directory), previous)

    def test_new_root_identity_can_bind_once(self):
        state = copy.deepcopy(self.original)
        state['backend'] = None; state.pop('local_root_id'); state.pop('health')
        self.path.write_text(json.dumps(state), encoding='utf-8')
        store = self.store(); previous = store.load()
        self.assertTrue(store.save(self.original, previous))
        with self.assertRaises(StateStoreError): store.save(state, self.original)
        self.assertEqual(load_state(self.directory), self.original)

    def test_stale_previous_and_concurrent_writer_fail_closed(self):
        first, second = self.store(), self.store()
        old = first.load(); self.assertEqual(second.load(), old)
        new = copy.deepcopy(old); new['items'].pop('one.txt')
        first.save(new, old)
        with self.assertRaises(StateStoreError): second.save(old, old)
        with self.assertRaises(StateStoreError): first.save(new, old)
        self.assertEqual(load_state(self.directory), new)

    def test_database_corruption_never_falls_back_to_json(self):
        store = self.store(); store.load(); store.close(export=False)
        (self.directory/DATABASE).write_bytes(b'corrupt database')
        with self.assertRaises(StateStoreError): load_state(self.directory)
        with self.assertRaises(StateStoreError): self.store().load()
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_item_payload_corruption_is_detected(self):
        store = self.store(); store.load()
        self.sql("UPDATE items SET payload='{}' WHERE path='one.txt'")
        with self.assertRaises(StateStoreError): load_state(self.directory)

    def test_missing_item_row_is_detected(self):
        store = self.store(); store.load()
        self.sql("DELETE FROM items WHERE path='one.txt'")
        with self.assertRaises(StateStoreError): load_state(self.directory)

    def test_unknown_database_schema_is_not_recreated(self):
        store = self.store(); store.load(); store.close(export=False)
        self.sql('PRAGMA user_version=999')
        with self.assertRaises(StateStoreError): load_state(self.directory)
        with self.assertRaises(StateStoreError): self.store().load()
        self.assertEqual(self.sql('PRAGMA user_version')[0][0], 999)

    def test_invalid_legacy_state_does_not_create_database(self):
        for bad in [dict(self.original, version=2), dict(self.original, local='relative'),
                    dict(self.original, items={'../bad':dict(remote={})})]:
            self.path.write_text(json.dumps(bad), encoding='utf-8')
            with self.assertRaises(StateStoreError): self.store().load()
            self.assertFalse((self.directory/DATABASE).exists())

    def test_interrupted_initial_import_keeps_original_and_can_retry(self):
        def fault(step):
            if step == 'before_import_replace': raise RuntimeError('injected failure')
        with self.assertRaisesRegex(RuntimeError, 'injected'): self.store(fault=fault).load()
        self.assertFalse((self.directory/DATABASE).exists())
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(self.store().load(), self.original)

    def removal_fixture(self):
        from tests.test_task_removal import Native
        local = self.directory/'local'; local.mkdir()
        state = copy.deepcopy(self.original)
        state['local'], state['local_root_id'] = str(local), local.stat().st_ino
        self.path.write_text(json.dumps(state), encoding='utf-8')
        task = dict(id=state['task'], local_path=str(local), cloud_path=state['cloud'])
        native = Native(local, task['id'], None)
        path = local/'one.txt'; path.write_bytes(b'local body')
        native.items['one.txt'] = dict(partial=False, insync=True, modified=0,
                                      identity=dict(task=task['id'], kind='file', size=10))
        return state, task, native, path

    def test_detach_checks_canonical_pending_operation_not_stale_json(self):
        from task_removal import detach
        state, task, native, path = self.removal_fixture()
        store = self.store(); previous = store.load()
        state['pending'] = dict(path='one.txt', stage='unfinished')
        store.save(state, previous); store.close(export=False)
        with self.assertRaisesRegex(ValueError, '未完成'):
            detach(self.directory, task, self.directory, native_factory=lambda *args:self.fail('native accessed'),
                   unregister_shell=lambda *args:self.fail('shell changed'))
        self.assertEqual(path.read_bytes(), b'local body')

    def test_detach_exports_committed_delete_history_and_keeps_local_body(self):
        from task_removal import detach
        state, task, native, path = self.removal_fixture()
        store = self.store(); previous = store.load()
        state['items'].pop('one.txt')
        store.save(state, previous); store.close(export=False)
        journal = self.directory/'remote-transactions'; journal.mkdir()
        record = journal/'operation.json'
        record.write_text(json.dumps(dict(kind='delete', expected=dict(id='1'))), encoding='utf-8')
        original = record.read_bytes(); calls = []
        detach(self.directory, task, self.directory, native_factory=lambda *args:native,
               unregister_shell=lambda *args:calls.append(True))
        self.assertEqual(path.read_bytes(), b'local body')
        self.assertEqual(record.read_bytes(), original)
        self.assertEqual(json.loads(self.path.read_bytes()), state)
        self.assertEqual(load_state(self.directory), state)
        self.assertEqual(calls, [True])

    def test_hard_process_exit_before_and_after_commit(self):
        script = '''import copy,os,sys
from sync_state_store import StateStore
def fault(step):
 if step == sys.argv[2]: os._exit(23)
s=StateStore(sys.argv[1],fault=fault); old=s.load(); new=copy.deepcopy(old)
new['items'].pop('one.txt');s.save(new,old);s.export()
'''
        for step in ['before_state_commit', 'after_state_commit', 'after_export_replace',
                     'before_export_commit', 'after_export_commit']:
            case = self.directory/step; case.mkdir(); (case/'state.json').write_bytes(self.raw)
            process = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, str(case), step],
                                     cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
            self.assertEqual(process.returncode, 23, process.stderr.decode(errors='replace'))
            expected = copy.deepcopy(self.original)
            if step != 'before_state_commit': expected['items'].pop('one.txt')
            self.assertEqual(load_state(case), expected)
            recovered = StateStore(case)
            try:
                self.assertEqual(recovered.load(), expected)
                recovered.export()
            finally: recovered.close()

    def assert_old_reader_refuses(self):
        from task_engine import TaskEngine
        task = dict(id=self.original['task'], local_path=self.original['local'], cloud_path=self.original['cloud'])
        # Exercise the actual constructor's legacy v1 validation while replacing
        # only the new canonical reader with the previous JSON-only reader.
        with patch('task_engine.load_state', side_effect=lambda directory:json.loads((directory/'state.json').read_bytes())):
            with self.assertRaisesRegex(RuntimeError, '状态损坏'):
                TaskEngine(task, self.directory, None, native_factory=lambda *args, **kwargs:object())

    def test_guarded_enable_changes_only_export_format_before_mutations(self):
        store = self.store(guard_legacy=True); previous = store.load()
        self.assertFalse(store.save(previous, previous))
        exported = json.loads(self.path.read_bytes())
        self.assertEqual(exported.pop('storage_format'), 'sqlite-v1')
        self.assertEqual(exported['version'], 2)
        exported['version'] = 1
        self.assertEqual(exported, previous)
        self.assertEqual(load_state(self.directory), previous)
        self.assert_old_reader_refuses()
        before = self.path.read_bytes()
        current = copy.deepcopy(previous); current['items'].pop('one.txt')
        store.save(current, previous)
        self.assertEqual(self.path.read_bytes(), before)  # Guarded snapshot can lag safely.
        self.assertEqual(load_state(self.directory), current)
        self.assert_old_reader_refuses()

    def test_guard_survives_restart_and_detach_style_default_store(self):
        store = self.store(guard_legacy=True); previous = store.load(); store.save(previous, previous)
        store.close()
        recovered = self.store(); self.assertEqual(recovered.load(), previous)
        self.assertTrue(recovered.guard_legacy)
        current = copy.deepcopy(previous); current['items'].clear()
        recovered.save(current, previous); recovered.export()
        self.assertEqual(json.loads(self.path.read_bytes())['version'], 2)
        self.assert_old_reader_refuses()

    def test_missing_database_with_guarded_export_cannot_be_imported(self):
        exported = dict(self.original, version=2, storage_format='sqlite-v1')
        self.path.write_text(json.dumps(exported), encoding='utf-8')
        with self.assertRaises(StateStoreError): self.store().load()
        self.assertFalse((self.directory/DATABASE).exists())

    def test_unknown_guard_format_is_rejected(self):
        store = self.store(guard_legacy=True); previous = store.load(); store.save(previous, previous)
        exported = json.loads(self.path.read_bytes())
        for changes in [dict(storage_format='unknown-format'), dict(version=2.0), dict(version=3)]:
            self.path.write_text(json.dumps(dict(exported, **changes)), encoding='utf-8')
            with self.assertRaises(StateStoreError): load_state(self.directory)
            with self.assertRaises(StateStoreError): store.export()

    def test_guard_export_failure_prevents_canonical_mutation(self):
        def fault(step):
            if step == 'before_export_replace': raise RuntimeError('guard failure')
        store = self.store(guard_legacy=True, fault=fault); previous = store.load()
        current = copy.deepcopy(previous); current['items'].clear()
        with self.assertRaisesRegex(RuntimeError, 'guard failure'): store.save(current, previous)
        self.assertEqual(load_state(self.directory), previous)
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_guard_rename_before_flag_commit_is_recovered_without_downgrade(self):
        def fault(step):
            if step == 'after_export_replace': raise RuntimeError('guard flag failure')
        store = self.store(guard_legacy=True, fault=fault); previous = store.load()
        current = copy.deepcopy(previous); current['items'].clear()
        with self.assertRaisesRegex(RuntimeError, 'guard flag failure'): store.save(current, previous)
        store.close(export=False)
        self.assertEqual(load_state(self.directory), previous)
        self.assert_old_reader_refuses()
        recovered = self.store(); self.assertEqual(recovered.load(), previous)
        self.assertTrue(recovered.guard_legacy)
        recovered.save(current, previous); recovered.export()
        self.assertEqual(load_state(self.directory), current)
        self.assert_old_reader_refuses()

    def test_explicit_legacy_export_requires_latest_snapshot_and_stopped_worker(self):
        store = self.store(guard_legacy=True); previous = store.load(); store.save(previous, previous)
        current = copy.deepcopy(previous); current['items'].clear(); store.save(current, previous)
        with self.assertRaisesRegex(StateStoreError, '最新'): store.export_legacy()
        # Failed rollback handoff closes its handle; reopen explicitly.
        store = self.store(); store.load(); store.export()
        with file_lock(self.directory/'worker.lock'):
            with self.assertRaises(OSError): store.export_legacy()
        self.assertEqual(store.export_legacy(), current)
        self.assertEqual(json.loads(self.path.read_bytes()), current)
        with self.assertRaisesRegex(StateStoreError, '回退'): load_state(self.directory)

    def test_hard_exit_keeps_legacy_guard_before_and_after_state_commit(self):
        script = '''import copy,os,sys
from sync_state_store import StateStore
armed=False
def fault(step):
 if armed and step==sys.argv[2]:os._exit(24)
s=StateStore(sys.argv[1],guard_legacy=True,fault=fault);old=s.load();s.save(old,old)
armed=True;new=copy.deepcopy(old);new['items'].pop('one.txt');s.save(new,old);s.export()
'''
        for step in ['before_state_commit', 'after_state_commit', 'after_export_replace']:
            case = self.directory/('guard-'+step); case.mkdir(); (case/'state.json').write_bytes(self.raw)
            process = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, str(case), step],
                                     cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
            self.assertEqual(process.returncode, 24, process.stderr.decode(errors='replace'))
            raw = json.loads((case/'state.json').read_bytes())
            self.assertEqual((raw['version'], raw['storage_format']), (2, 'sqlite-v1'))
            expected = copy.deepcopy(self.original)
            if step != 'before_state_commit': expected['items'].pop('one.txt')
            self.assertEqual(load_state(case), expected)
            recovered = StateStore(case)
            try: self.assertEqual(recovered.load(), expected)
            finally: recovered.close()

    def test_program_upgrade_and_rollback_preserve_guarded_database_and_wal(self):
        from installer_upgrade import install
        from tests.test_installer_upgrade import fixture
        payload, app, local = fixture(self.directory/'installer')
        task_dir = app/'runtime/private/tasks'/('1'*32)
        task_dir.mkdir(parents=True)
        initial = baseline(); initial['local'] = str(local); initial['local_root_id'] = local.stat().st_ino
        (task_dir/'state.json').write_text(json.dumps(initial), encoding='utf-8')
        store = StateStore(task_dir, guard_legacy=True)
        try:
            old = store.load(); store.save(old, old)
            latest = copy.deepcopy(old); latest['items'].pop('one.txt'); store.save(latest, old)
            protected = {path:(path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
                         for path in task_dir.iterdir() if path.is_file() and path.name != 'state.sqlite3-shm'}
            def assert_preserved():
                for path, expected in protected.items():
                    self.assertEqual((path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns), expected)
                self.assertEqual(load_state(task_dir), latest)
                self.assertEqual(json.loads((task_dir/'state.json').read_bytes())['version'], 2)
            def failure(event, transaction):
                if event == 'after_replace': raise OSError('interrupted replacement')
            with self.assertRaisesRegex(OSError, 'interrupted replacement'):
                install(payload, app, checkpoint=failure)
            assert_preserved()
            self.assertTrue(install(payload, app)['upgraded'])
            assert_preserved()
            self.assertEqual(install(payload, app)['replaced'], 0)
            assert_preserved()
        finally: store.close()


if __name__ == '__main__': unittest.main()
