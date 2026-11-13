import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

import twodrive
from remote_manifest import RecursiveManifest, ManifestUnavailable, ancestry_matches
from task_backend import OpenListBackend, RemoteChanged, ConcurrentChange


def file(fid, parent, name='file.bin', data=b'data'):
    return dict(fid=fid, cid=parent, n=name, s=len(data), sha=hashlib.sha1(data).hexdigest())


class BulkHost:
    def __init__(self, root):
        self.PRIVATE = root/'private'
        self.root_id = '10'
        self.remote = 'test'
        self.uid = '123'
        self.calls = []
        self.dirs = {'20': ('10', 'branch'), '30': ('10', 'empty')}
        self.files = {'100': file('100', '20')}
        self.page_size = 1150
        self.dir_page_size = 5000
        self.bulk_bad = False
        self.bad_legacy = False
        self.path_override = None
        self.native_115_list = twodrive.native_115_list
        self.mutation = None
        self.mutations = []

    def login(self): return 'test-token'
    def storages(self, token):
        return [dict(mount_path='/115', driver='115 Cloud', remark='Managed by TwoDrive',
                     addition=json.dumps({'cookie': 'UID=' + self.uid + '_test;'}))]

    def ancestry(self, cid):
        if self.path_override is not None: return self.path_override
        parts = []
        while cid != self.root_id:
            parent, name = self.dirs[cid]
            parts.append(dict(cid=cid, name=name)); cid = parent
        return [dict(cid='0', name='根目录'), dict(cid=self.root_id, name=self.remote)] + list(reversed(parts))

    def native_115_request(self, cookie, endpoint, query):
        self.calls.append((endpoint, copy.deepcopy(query)))
        if endpoint == 'files/getid': return {'id': self.root_id}
        if endpoint == 'files/file':
            return {'data': [dict(file_id=self.root_id, file_name=self.remote, pick_code='pick')]}
        if endpoint == 'files/downfolders':
            all_rows = [dict(fid=cid, pid=parent, fn=name) for cid, (parent, name) in self.dirs.items()]
            page = query['page']; start = (page - 1) * self.dir_page_size
            return {'data': dict(root=dict(fid=self.root_id, fn=self.remote), count=len(all_rows),
                                 list=all_rows[start:start+self.dir_page_size],
                                 has_next_page=start+self.dir_page_size < len(all_rows))}
        if endpoint == 'files':
            cid, offset = query['cid'], query.get('offset', 0)
            if query.get('cur') == 0:
                rows = list(self.files.values())
                if self.bulk_bad:
                    return dict(cid=cid, count=len(rows)+1, offset=offset, path=self.ancestry(cid), data=[])
            else:
                rows = [dict(cid=child, pid=parent, n=name) for child, (parent, name) in self.dirs.items() if parent == cid]
                rows += [row for row in self.files.values() if row['cid'] == cid]
            return dict(cid=cid, count=len(rows) + int(self.bad_legacy), offset=offset,
                        path=self.ancestry(cid), data=copy.deepcopy(rows[offset:offset+self.page_size]))
        raise AssertionError(endpoint)

    def api(self, endpoint, data, token):
        self.calls.append(('openlist:' + endpoint, copy.deepcopy(data)))
        return {'content': []}

    def post(self, endpoint, fields):
        self.mutations.append((endpoint, dict(fields)))
        row = self.files[str(fields['fid'])]
        row['n'] = fields['file_name']
        if self.mutation: self.mutation(row)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.host = BulkHost(Path(self.temp.name))

    def read(self):
        return RecursiveManifest(lambda e,q:self.host.native_115_request('',e,q),
                                 self.host.remote, self.host.root_id).read()


class ManifestTests(Fixture):
    def test_builds_complete_tree_with_empty_directories_and_multiple_pages(self):
        self.host.page_size = self.host.dir_page_size = 1
        self.host.files['101'] = file('101', '10', 'root.bin')
        rows = self.read()
        self.assertEqual(set(rows), {'branch', 'empty', 'branch/file.bin', 'root.bin'})
        self.assertEqual(rows['branch/file.bin']['sha1'], hashlib.sha1(b'data').hexdigest())
        self.assertEqual(len([q for e,q in self.host.calls if e == 'files']), 2)

    def test_same_size_content_change_and_directory_move_are_visible(self):
        before = self.read()
        self.host.files['100'] = file('100', '20', data=b'edit')
        self.host.dirs['20'] = ('30', 'branch')
        after = self.read()
        self.assertNotIn('branch/file.bin', after)
        self.assertNotEqual(before['branch/file.bin']['sha1'], after['empty/branch/file.bin']['sha1'])

    def test_topology_cycle_and_missing_parent_fail_closed(self):
        for topology in [{'20': ('30', 'branch'), '30': ('20', 'empty')}, {'20': ('missing', 'branch')}]:
            self.host.dirs = topology
            with self.assertRaises(ManifestUnavailable): self.read()

    def test_duplicate_pages_wrong_offset_count_and_root_path_rejected(self):
        original = self.host.native_115_request
        for change in [lambda r:r.update(offset=999), lambda r:r.update(count=0),
                       lambda r:r.update(path=[dict(cid='10', name='replacement')]),
                       lambda r:r.update(data=r['data']*2, count=2)]:
            def request(cookie, endpoint, query):
                result = original(cookie, endpoint, query)
                if endpoint == 'files': change(result)
                return result
            self.host.native_115_request = request
            with self.assertRaises(ManifestUnavailable): self.read()
        self.host.native_115_request = original

    def test_excluded_parent_excludes_descendants(self):
        self.host.dirs['20'] = ('10', 'ignored.part')
        self.assertEqual(set(self.read()), {'empty'})

    def test_fresh_ancestry_requires_root_path_and_every_id(self):
        path = self.host.ancestry('20')
        self.assertTrue(ancestry_matches(path, 'test', '10', 'branch', {'branch': '20'}))
        self.assertFalse(ancestry_matches(path, 'test', '10', 'branch', {'branch': '21'}))
        self.assertFalse(ancestry_matches(path, 'another/test', '10', 'branch', {'branch': '20'}))


class BackendBulkTests(Fixture):
    def setUp(self):
        super().setUp()
        self.backend = OpenListBackend(dict(id='a'*32, cloud_path='/test'), self.host)
        self.backend.post_identity = self.host.post

    def test_no_change_scan_is_bounded_by_pages_not_directory_count(self):
        self.host.dirs.update({str(1000+i): ('10', 'folder'+str(i)) for i in range(258)})
        first = self.backend.scan_snapshot()
        self.host.calls.clear()
        second = self.backend.scan_snapshot()
        self.assertEqual(first, second)
        self.assertEqual(self.backend.metrics['directory_lists'], 0)
        self.assertEqual(len(self.host.calls), 5)  # connect, root info, topology, files, root recheck
        self.assertGreater(1 - len(self.host.calls)/259, .8)

    def test_invalid_bulk_falls_back_to_complete_fresh_traversal(self):
        self.host.bulk_bad = True
        rows = self.backend.scan_snapshot()
        self.assertIn('branch/file.bin', rows)
        self.assertEqual(self.backend.metrics['bulk_fallbacks'], 1)
        self.assertEqual(self.backend.metrics['directory_lists'], 3)
        self.host.bad_legacy = True
        with self.assertRaises(twodrive.IndexInvalidError): self.backend.scan_snapshot()

    def test_internal_archives_hidden_and_unfinished_stage_blocks(self):
        archived = '_twodrive-deleted-'+'b'*32+'.bin'
        self.host.files['101'] = file('101', '20', archived)
        rows = self.backend.scan_snapshot()
        self.assertNotIn('branch/'+archived, rows)
        self.host.files['102'] = file('102', '20', '_twodrive-stage-'+'c'*32+'.bin')
        with self.assertRaises(RemoteChanged): self.backend.scan_snapshot()

    def test_changed_first_observation_must_be_confirmed(self):
        original = self.host.native_115_request; count = [0]
        def request(cookie, endpoint, query):
            if endpoint == 'files' and query.get('cur') == 0:
                count[0] += 1
                if count[0] == 2: self.host.files['100'] = file('100', '20', data=b'edit')
            return original(cookie, endpoint, query)
        self.host.native_115_request = request
        rows = self.backend.scan_snapshot()
        self.assertEqual(self.backend.metrics['bulk_fallbacks'], 1)
        self.assertEqual(rows['branch/file.bin']['sha1'], hashlib.sha1(b'edit').hexdigest())

    def test_delete_keeps_journal_and_cuts_duplicate_guard_reads(self):
        # This test isolates the initial ID-rename guard. The cross-root
        # history move and its fresh guards have separate provider-model tests.
        self.backend.migrate_delete_history = lambda ops: {op:dict(stage='moved') for op in ops}
        expected = self.backend.scan_snapshot()['branch/file.bin']
        self.host.calls.clear()
        self.backend.delete('branch/file.bin', expected)
        raw = [endpoint for endpoint,query in self.host.calls if not endpoint.startswith('openlist:')]
        self.assertEqual(raw, ['files', 'files'])
        self.assertEqual(len(self.host.mutations), 1)
        self.assertGreaterEqual(1 - (len(raw)+len(self.host.mutations))/13, .7)
        journal = json.loads(next(self.backend.journal_dir.glob('*.json')).read_bytes())
        self.assertEqual(set(journal), {'version','kind','path','expected','archive','backend'})
        self.assertEqual(journal['version'], 1)
        self.assertEqual(journal['expected'], expected)
        self.assertEqual(self.host.files['100']['sha'], expected['sha1'])

    def test_source_change_after_intent_before_submit_never_renames(self):
        expected = self.backend.scan_snapshot()['branch/file.bin']
        self.backend.fault = lambda stage:self.host.files.update({'100': file('100','20',data=b'edit')})
        with self.assertRaises(ConcurrentChange): self.backend.delete('branch/file.bin', expected)
        self.assertEqual(self.host.mutations, [])
        self.assertEqual(self.host.files['100']['n'], 'file.bin')

    def test_reused_original_name_and_target_collision_are_preserved(self):
        self.backend.migrate_delete_history = lambda ops: {op:dict(stage='moved') for op in ops}
        expected = self.backend.scan_snapshot()['branch/file.bin']
        self.host.mutation = lambda row:self.host.files.update({'101':file('101','20',data=b'new!')})
        with self.assertRaises(RemoteChanged): self.backend.delete('branch/file.bin', expected)
        self.assertEqual(self.host.files['101']['n'], 'file.bin')
        self.assertTrue(self.host.files['100']['n'].startswith('_twodrive-deleted-'))
        self.host.files = {'100':file('100','20')}
        self.host.mutation = lambda row:row.update(n=row['n']+'(1)')
        with self.assertRaises(ConcurrentChange): self.backend.delete('branch/file.bin', expected)

    def test_stale_parent_hint_replacement_cannot_delete_new_file(self):
        expected = self.backend.scan_snapshot()['branch/file.bin']
        self.host.dirs['20'] = ('10','moved')
        self.host.dirs['21'] = ('10','branch')
        self.host.files['101'] = file('101','21')
        with self.assertRaises(ConcurrentChange): self.backend.delete('branch/file.bin', expected)
        self.assertEqual(self.host.mutations, [])

    def test_account_change_rejected_before_using_hint(self):
        expected = self.backend.scan_snapshot()['branch/file.bin']
        self.host.uid = '456'
        with self.assertRaises(RemoteChanged): self.backend.delete('branch/file.bin', expected)
        self.assertEqual(self.host.mutations, [])

    def test_replaced_root_identity_rejected_even_if_original_file_moved_there(self):
        expected = self.backend.scan_snapshot()['branch/file.bin']
        self.host.root_id = '11'
        self.host.dirs = {'20': ('11', 'branch')}
        with self.assertRaises(RemoteChanged): self.backend.delete('branch/file.bin', expected)
        self.assertEqual(self.host.mutations, [])

    def test_unsupported_provider_response_falls_back_but_authentication_error_does_not(self):
        from sync_errors import TransientSyncError, ActionRequiredError
        original = self.host.native_115_request
        def request(cookie, endpoint, query):
            if endpoint == 'files/file': raise TransientSyncError('unsupported')
            return original(cookie, endpoint, query)
        self.host.native_115_request = request
        self.assertIn('branch/file.bin', self.backend.scan_snapshot())
        self.backend._bulk_retry_at = 0
        def unauthorized(cookie, endpoint, query):
            if endpoint == 'files/file': raise ActionRequiredError('expired')
            return original(cookie, endpoint, query)
        self.host.native_115_request = unauthorized
        with self.assertRaises(ActionRequiredError): self.backend.scan_snapshot()

    def test_cancelled_bulk_does_not_fall_back_or_write(self):
        self.backend.cancelled = threading.Event(); self.backend.cancelled.set()
        with self.assertRaises(InterruptedError): self.backend.scan_snapshot()
        self.assertEqual(self.backend.metrics['bulk_fallbacks'], 0)
        self.assertEqual(self.host.mutations, [])


if __name__ == '__main__': unittest.main()
