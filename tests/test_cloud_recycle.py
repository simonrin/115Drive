import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from cloud_history import CloudHistory, CloudRecycle
from task_backend import OpenListBackend, RemoteChanged, ConcurrentChange, version
from tests.test_cloud_history import Cloud


class RecycleCloud(Cloud):
    def __init__(self, root):
        super().__init__(root)
        self.CONFIG = root/'config.json'
        self.CONFIG.write_text(json.dumps(dict(cloud_history_mode='recycle')), encoding='utf-8')
        self.recycled = {}

    def post(self, endpoint, fields):
        if endpoint != 'rb/delete': return super().post(endpoint, fields)
        self.posts.append((endpoint, dict(fields)))
        fid = str(fields['fid[0]'])
        if fid in self.files:
            self.recycled[fid] = copy.deepcopy(self.files.pop(fid))
        else:
            assert not any(parent == fid for parent,name in self.dirs.values())
            assert not any(row['cid'] == fid for row in self.files.values())
            self.recycled[fid] = self.dirs.pop(fid)
        if self.after_post: self.after_post(endpoint, fields)


class RecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cloud = RecycleCloud(self.root)
        self.backend = self.make_backend()
        self.expected = self.backend.scan_snapshot()['branch/photo.png']

    def make_backend(self, **kwargs):
        backend = OpenListBackend(dict(id='a'*32, cloud_path='/test'), self.cloud, **kwargs)
        backend.post_identity = self.cloud.post
        def upload(relative, source, cancelled):
            self.cloud.counter += 1
            parent,name = relative.rsplit('/', 1)
            self.cloud.put(str(self.cloud.counter), self.cloud.resolve('test/'+parent), name, source.read_bytes())
        backend.upload_fresh = upload
        backend.connect()
        return backend

    def source(self):
        source = self.root/'replacement.png'; source.write_bytes(b'replacement body')
        return source

    def receipt(self, backend=None):
        backend = backend or self.backend
        operation = next(backend.journal_dir.glob('*.json')).stem
        return CloudHistory(backend).info(operation)

    def assert_no_history_root(self):
        self.assertFalse(CloudHistory(self.backend).registry_path.exists())
        self.assertFalse(any(name.startswith('115Drive 同步历史-') for parent,name in self.cloud.dirs.values()))

    def test_delete_recycles_only_proven_file_without_creating_history(self):
        self.backend.delete('branch/photo.png', self.expected)
        self.assertNotIn('100', self.cloud.files)
        self.assertEqual(self.cloud.recycled['100']['sha'], self.expected['sha1'])
        self.assertEqual(self.receipt()['stage'], 'recycled')
        self.assert_no_history_root()

    def test_successful_overwrite_cleans_old_version_after_verified_commit(self):
        source = self.source()
        result = self.backend.upload('branch/photo.png', source, self.expected, threading.Event())
        rows = self.backend.scan_snapshot()
        self.assertEqual(version(rows['branch/photo.png']), version(result))
        self.assertEqual(result['sha1'], hashlib.sha1(source.read_bytes()).hexdigest())
        self.assertEqual(self.cloud.recycled['100']['sha'], self.expected['sha1'])
        self.assertFalse(any(row['n'].startswith('_twodrive-') for row in self.cloud.files.values()))
        self.assertEqual(self.receipt()['stage'], 'recycled')
        self.assert_no_history_root()

    def test_upload_restart_preserves_old_version_until_commit(self):
        for point in ('remote_staged', 'remote_archived', 'remote_promoted'):
            with self.subTest(point=point):
                self.cloud = RecycleCloud(self.root)
                self.backend = self.make_backend()
                expected = self.backend.scan_snapshot()['branch/photo.png']
                self.backend.operation_id = {'remote_staged':'b', 'remote_archived':'c', 'remote_promoted':'d'}[point]*32
                def crash(step):
                    if step == point: raise OSError('injected interruption')
                self.backend.fault = crash
                with self.assertRaises(OSError):
                    self.backend.upload('branch/photo.png', self.source(), expected, threading.Event())
                self.assertNotIn('100', self.cloud.recycled)
                restarted = self.make_backend(); restarted.operation_id = self.backend.operation_id
                restarted.upload('branch/photo.png', self.source(), expected, threading.Event())
                self.assertIn('100', self.cloud.recycled)
                self.assertEqual(sum(endpoint == 'rb/delete' for endpoint,fields in self.cloud.posts), 1)

    def test_pause_after_staging_does_not_recycle_old_version(self):
        cancelled = threading.Event()
        self.backend.fault = lambda step:cancelled.set() if step == 'remote_staged' else None
        with self.assertRaises(InterruptedError):
            self.backend.upload('branch/photo.png', self.source(), self.expected, cancelled)
        self.assertEqual(self.cloud.files['100']['n'], 'photo.png')
        self.assertEqual(self.cloud.recycled, {})

    def test_lost_recycle_reply_recovers_without_duplicate_submission(self):
        def lose(endpoint, fields):
            if endpoint == 'rb/delete':
                self.cloud.after_post = None
                raise TimeoutError('lost recycle response')
        self.cloud.after_post = lose
        with self.assertRaises(TimeoutError): self.backend.delete('branch/photo.png', self.expected)
        self.assertEqual(self.receipt()['stage'], 'recycling')
        self.cloud.put('999', '20', 'photo.png', b'other client recreation')
        restarted = self.make_backend(); restarted.scan_snapshot()
        self.assertEqual(self.receipt(restarted)['stage'], 'recycled')
        self.assertEqual(sum(endpoint == 'rb/delete' for endpoint,fields in self.cloud.posts), 1)
        self.assertEqual(self.cloud.files['999']['n'], 'photo.png')

    def test_changed_archive_is_durable_conflict_without_recycling(self):
        def edit(step):
            if step == 'before_remote_recycle':
                row = self.cloud.files['100']
                self.cloud.put('100', row['cid'], row['n'], b'concurrent edit')
        self.backend.fault = edit
        with self.assertRaises(ConcurrentChange): self.backend.delete('branch/photo.png', self.expected)
        self.assertEqual(self.receipt()['stage'], 'conflict')
        self.assertEqual(self.cloud.recycled, {})
        with self.assertRaises(RemoteChanged): self.make_backend().scan_snapshot()
        self.assertIn('100', self.cloud.files)

    def test_cleanup_conflict_does_not_abort_committed_upload(self):
        def replace(step):
            if step == 'before_remote_recycle':
                for fid in [fid for fid,row in self.cloud.files.items() if row['n'] == 'photo.png']:
                    del self.cloud.files[fid]
                self.cloud.put('999', '20', 'photo.png', b'other writer')
        self.backend.fault = replace
        with self.assertRaises(ConcurrentChange):
            self.backend.upload('branch/photo.png', self.source(), self.expected, threading.Event())
        tx = json.loads(next(self.backend.journal_dir.glob('*.json')).read_bytes())
        self.assertTrue(tx['committed']); self.assertFalse(tx.get('aborted', False))
        self.assertIn('100', self.cloud.files); self.assertIn('999', self.cloud.files)
        self.assertEqual(self.cloud.recycled, {})

    def test_nonempty_directory_including_excluded_file_is_preserved(self):
        self.cloud.files['100']['n'] = 'desktop.ini'
        with self.assertRaises(RemoteChanged): self.backend.delete('branch', dict(kind='dir', id='20'))
        self.assertEqual(self.cloud.posts, [])

    def test_empty_directory_is_recycled(self):
        self.cloud.files.clear()
        self.backend.delete('branch', dict(kind='dir', id='20'))
        self.assertNotIn('20', self.cloud.dirs)
        self.assertIn('20', self.cloud.recycled)
        self.assert_no_history_root()

    def test_read_only_rejects_recycling_before_any_mutation(self):
        reader = self.make_backend(read_only=True)
        self.cloud.posts.clear(); self.cloud.calls.clear()
        with self.assertRaises(RuntimeError): reader.delete('branch/photo.png', self.expected)
        with self.assertRaises(RuntimeError): CloudRecycle(reader).prepare('b'*32)
        self.assertEqual((self.cloud.posts, self.cloud.calls), ([], []))

    def test_invalid_policy_does_not_silently_enable_recycling(self):
        self.cloud.CONFIG.write_text('{"cloud_history_mode":"unknown"}', encoding='utf-8')
        with self.assertRaises(ValueError): self.make_backend()
        self.assertEqual(self.cloud.recycled, {})

    def test_completed_legacy_history_does_not_require_its_cloud_root(self):
        self.cloud.CONFIG.write_text('{"cloud_history_mode":"retain"}', encoding='utf-8')
        retained = self.make_backend(); retained.delete('branch/photo.png', self.expected)
        registry = json.loads(CloudHistory(retained).registry_path.read_bytes())
        del self.cloud.dirs[registry['root']['id']]
        self.cloud.CONFIG.write_text('{"cloud_history_mode":"recycle"}', encoding='utf-8')
        # The provider model cannot enumerate orphaned history descendants;
        # ignore them as an actual provider would for a recycled parent tree.
        archived_id = self.expected['id']; del self.cloud.files[archived_id]
        self.make_backend().scan_snapshot()
        self.assertEqual(self.cloud.recycled, {})

    def test_mode_change_finishes_uncertain_legacy_move_before_retiring_history(self):
        self.cloud.CONFIG.write_text('{"cloud_history_mode":"retain"}', encoding='utf-8')
        retained = self.make_backend()
        def lose(endpoint, fields):
            if endpoint == 'files/move':
                self.cloud.after_post = None
                raise TimeoutError('lost legacy history move reply')
        self.cloud.after_post = lose
        with self.assertRaises(TimeoutError): retained.delete('branch/photo.png', self.expected)
        operation = next(retained.journal_dir.glob('*.json')).stem
        self.assertEqual(CloudHistory(retained).info(operation)['stage'], 'moving')
        self.cloud.CONFIG.write_text('{"cloud_history_mode":"recycle"}', encoding='utf-8')
        restarted = self.make_backend()
        self.assertEqual(restarted.migrate_delete_history([operation])[operation]['stage'], 'moved')
        self.assertEqual(self.cloud.recycled, {})
        self.assertEqual(sum(endpoint == 'files/move' for endpoint,fields in self.cloud.posts), 1)


if __name__ == '__main__': unittest.main()
