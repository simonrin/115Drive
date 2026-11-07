"""Regression probes for worker liveness and non-authoritative telemetry."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import cloud_scan
import runtime_json as r
import sync_progress as p
import transfer_recovery as recovery
import twodrive as t
from sync_errors import recovery_policy
from tests.test_cloud_scan import Fake115
from tests.test_runtime_json import deny_delete
from tests import test_worker_retry as worker_tests
from tests.test_folder_change import Fixture


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_socket_permission_is_retryable_but_local_permission_is_not(self):
        error = PermissionError(13, 'socket denied')
        error.winerror = 10013
        self.assertTrue(recovery_policy(error).retryable)
        self.assertFalse(recovery_policy(PermissionError(13, 'file denied')).retryable)

    def test_malformed_optional_history_cannot_block_a_new_run(self):
        directory = self.root/'runtime'
        directory.mkdir()
        (directory/'activity.json').write_text('[null, 5, {}, {"name":"ok.txt","action":"upload"}]')
        (directory/'status.json').write_text('[]')
        (directory/'progress.json').write_text('null')
        progress = p.SyncProgress(self.root)
        self.assertEqual([row['name'] for row in progress.activities], ['ok.txt'])
        with patch.object(t, 'ROOT', self.root):
            t.status('checking', 'fresh')
        self.assertEqual(t.read_json(directory/'status.json')['state'], 'checking')

    def test_malformed_progress_rows_do_not_abort_transfer(self):
        progress = p.SyncProgress(self.root)
        for row in [None, [], {'msg': None}, {'object': []},
                    {'stats': {'bytes': 'bad', 'transferring': [None, 42, {'name': 3}]}},
                    {'stats': {'transferring': {'bad': True}}}]:
            progress.feed(row)
        progress.feed({'stats': {'bytes': 42, 'transferring': []}})
        self.assertEqual(progress.data['bytes'], 42)

    def test_unexpected_consumer_error_reaps_only_owned_transfer(self):
        progress = p.SyncProgress(self.root)
        children = []
        progress.process_started = children.append
        try:
            with patch.object(progress, 'feed', side_effect=RuntimeError('consumer failed')):
                with self.assertRaisesRegex(RuntimeError, 'consumer failed'):
                    p.execute([sys.executable, '-c',
                               'import sys,time; print("{}",file=sys.stderr,flush=True); time.sleep(30)'],
                              progress, self.root/'transfer.log')
            self.assertIsNotNone(children[0].poll())
            self.assertTrue(children[0].stderr.closed)
        finally:
            # Do not leave the intentionally reproduced old-code orphan behind.
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()
                child.stderr.close()

    def test_optional_scan_metrics_failure_does_not_invalidate_index(self):
        owner = Fake115(self.root)
        real_write = owner.write_json
        def write(path, value):
            if Path(path).name in ('scan-metrics.json', 'scan-audit.json'):
                raise PermissionError('metrics locked')
            real_write(path, value)
        owner.write_json = write
        scan = cloud_scan.CloudScan(owner, 'cloud115:115Drive')
        with patch.object(r, 'atomic_write_json', side_effect=PermissionError('metrics locked')):
            self.assertTrue(scan.prepare(None, Mock(cancelled=lambda: False)))
            scan.synchronized()
            scan.audit_step()
        self.assertFalse(scan.force_full)
        self.assertFalse(scan.index['needs_sync'])

    def test_optional_recovery_report_cannot_fail_completed_transfer(self):
        local = self.root/'local'; local.mkdir()
        work = self.root/'work'; work.mkdir()
        manager = recovery.TransferRecovery(self.root, dict(local_path=str(local), remote_path='test:'), work,
                                            alive=lambda: False)
        manager.begin(['bisync'])
        real_save = recovery.save
        def save(path, value):
            if path == manager.report:
                raise PermissionError('report locked')
            return real_save(path, value)
        with patch.object(recovery, 'save', side_effect=save), \
             patch.object(r, 'atomic_write_json', side_effect=PermissionError('report locked')):
            manager.complete()
        self.assertFalse(manager.path.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics')
    def test_recovery_intent_retries_short_reader_lock_and_fsyncs(self):
        path = self.root/'intent.json'
        recovery.save(path, {'pid': 1})
        with deny_delete(path) as release:
            timer = threading.Timer(.06, release); timer.start()
            try:
                with patch.object(os, 'fsync', wraps=os.fsync) as flush:
                    recovery.save(path, {'pid': 2})
                    flush.assert_called_once()
            finally:
                timer.join()
        self.assertEqual(json.loads(path.read_text()), {'pid': 2})

    def test_status_lock_does_not_kill_retry_or_bypass_pause(self):
        with worker_tests.RetryTests().sandbox() as (root, private, local, clock):
            clock.tick = lambda: (root/'runtime/stop').touch() if clock.now >= 103 else None
            with patch.object(t, 'status', side_effect=PermissionError('status locked')):
                t.wait_for_retry(OSError('offline'), 1)
            self.assertEqual(clock.now, 103)

    def test_paused_worker_survives_status_lock_without_syncing(self):
        with worker_tests.RetryTests().sandbox() as (root, private, local, clock):
            (root/'runtime/stop').touch()
            def stop():
                if clock.now >= 103: raise worker_tests.Finished()
            clock.tick = stop
            with patch.object(t, 'status', side_effect=PermissionError('status locked')), \
                 patch.object(t, 'sync_once') as sync, self.assertRaises(worker_tests.Finished):
                t.worker_loop()
            sync.assert_not_called()

    def test_server_still_exiting_is_retryable(self):
        with patch.object(t, 'login', side_effect=ConnectionRefusedError()), \
             patch.object(t.socket, 'socket') as sock, \
             patch.object(recovery, 'server_process_running', return_value=True):
            sock.return_value.connect_ex.return_value = 1
            try:
                t._ensure_server()
            except Exception as error:
                self.assertTrue(recovery_policy(error).retryable)
            else:
                self.fail('still-exiting server must block a second launch')

    def test_occupied_server_preserves_auth_vs_network_policy(self):
        from sync_errors import ActionRequiredError
        for error, retry in [(TimeoutError('not ready'), True),
                             (ActionRequiredError('login rejected'), False)]:
            with self.subTest(retry=retry), patch.object(t, 'login', side_effect=error), \
                 patch.object(t.socket, 'socket') as sock, patch.object(t.subprocess, 'Popen') as launch:
                sock.return_value.connect_ex.return_value = 0
                with self.assertRaises(type(error)) as caught:
                    t._ensure_server()
                self.assertEqual(recovery_policy(caught.exception).retryable, retry)
                launch.assert_not_called()

    def test_long_checkpoint_lock_is_retryable_without_advancing_data(self):
        path = self.root/'checkpoint.json'
        t.write_json(path, {'committed': 1})
        denied = PermissionError(13, 'replace locked'); denied.winerror = 32
        with patch.object(r.os, 'replace', side_effect=denied), patch.object(r.time, 'sleep'):
            with self.assertRaises(r.AtomicReplaceBusyError) as caught:
                t.write_json(path, {'committed': 2})
        self.assertTrue(recovery_policy(caught.exception).retryable)
        self.assertEqual(json.loads(path.read_text()), {'committed': 1})
        self.assertFalse(list(self.root.glob('*.tmp')))
        t.write_json(path, {'committed': 2})
        self.assertEqual(json.loads(path.read_text()), {'committed': 2})

    def test_bad_raw_pagination_is_rejected_before_sync(self):
        cases = [
            [{'cid':'1', 'count':0, 'data':None}],
            [{'cid':'1', 'count':0, 'data':[{'n':'x'}]}],
            [{'cid':'1', 'count':2, 'data':[{'n':'x','fid':'9'}]},
             {'cid':'1', 'count':2, 'data':[{'n':'x','fid':'9'}]}],
            [{'cid':'1', 'count':2, 'data':[{'n':'x'}]},
             {'cid':'1', 'count':1, 'data':[]}],
            [{'cid':'1', 'count':1, 'offset':5, 'data':[{'n':'x'}]}],
        ]
        for pages in cases:
            with self.subTest(pages=pages), patch.object(t, 'native_115_request', side_effect=pages):
                with self.assertRaises(RuntimeError): t.native_115_list('fixture','1')

    def test_folder_display_lock_does_not_strand_transaction(self):
        fixture = Fixture(self.root)
        real_write = r.atomic_write_json
        def write(path, data, **kwargs):
            if Path(path).name == 'folder-change.json': raise PermissionError('display locked')
            return real_write(path, data, **kwargs)
        with patch.object(r, 'atomic_write_json', side_effect=write):
            fixture.changer.change(str(fixture.target))
        self.assertEqual(t.read_json(fixture.CONFIG)['local_path'], str(fixture.target))
        self.assertFalse(fixture.changer.guard.exists())
        self.assertEqual((fixture.source/'子目录/测试.txt').read_bytes(),
                         (fixture.target/'子目录/测试.txt').read_bytes())


if __name__ == '__main__':
    unittest.main()
