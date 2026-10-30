import copy
import errno
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
import urllib.error
from unittest.mock import Mock, patch

import twodrive as t
from cloud_scan import CloudScan
from sync_errors import ActionRequiredError, recovery_policy
from sync_progress import SyncProgress
from tests import test_cloud_scan as cloud_tests
from tests import test_worker_retry as worker_tests
file=cloud_tests.file
Finished=worker_tests.Finished


class IndexPersistenceTests(unittest.TestCase):
    setUp=cloud_tests.CloudScanTests.setUp
    def test_idle_checks_write_receipt_only_including_after_restart(self):
        before=self.scan.path.read_bytes()
        stamp=self.scan.path.stat().st_mtime_ns
        receipt=self.t.read_json(self.scan.receipt_path)
        self.assertNotIn('checked_at',json.loads(before))
        self.assertEqual(receipt['sha256'],hashlib.sha256(before).hexdigest())
        self.assertFalse(self.scan.prepare(set(),self.p))
        self.scan.synchronized()
        restarted=CloudScan(self.t,'cloud115:115Drive')
        self.assertFalse(restarted.prepare(set(),self.p))
        self.assertEqual(before,self.scan.path.read_bytes())
        self.assertEqual(stamp,self.scan.path.stat().st_mtime_ns)
        metrics=self.t.read_json(self.t.ROOT/'runtime/scan-metrics.json')
        self.assertFalse(metrics['index_written'])

    def test_new_content_committed_pending_until_transfer_success(self):
        before=self.scan.path.read_bytes()
        self.t.dirs['2']['rows'].append(file('42','2','changed.txt'))
        self.assertTrue(self.scan.prepare(set(),self.p))
        changed=self.scan.path.read_bytes()
        self.assertNotEqual(before,changed)
        self.assertTrue(self.t.read_json(self.scan.receipt_path)['needs_sync'])
        self.scan.synchronized()
        self.assertEqual(changed,self.scan.path.read_bytes())
        self.assertFalse(self.t.read_json(self.scan.receipt_path)['needs_sync'])

    def test_torn_receipt_commit_cannot_acknowledge_new_content(self):
        writer=self.t.write_json
        self.t.dirs['2']['rows'].append(file('42','2','changed.txt'))
        def fail_receipt(path,value):
            if path==self.scan.receipt_path: raise PermissionError('receipt unavailable')
            writer(path,value)
        with patch.object(self.t,'write_json',side_effect=fail_receipt):
            with self.assertRaises(PermissionError): self.scan.prepare(set(),self.p)
        restarted=CloudScan(self.t,'cloud115:115Drive')
        self.assertTrue(restarted.index['needs_sync'])
        self.assertTrue(restarted.prepare(set(),self.p))

    def test_version_two_migrates_without_full_tree_rebuild(self):
        legacy=copy.deepcopy(self.scan.index)
        legacy['version']=2
        self.t.write_json(self.scan.path,legacy)
        restarted=CloudScan(self.t,'cloud115:115Drive')
        self.assertFalse(restarted.prepare(set(),self.p))
        self.assertEqual(self.t.read_json(self.scan.path)['version'],3)
        self.assertEqual(self.t.read_json(self.t.ROOT/'runtime/scan-metrics.json')['mode'],'incremental')

    def test_network_failure_keeps_good_index_and_recovers_incrementally(self):
        before=self.scan.path.read_bytes()
        receipt=self.scan.receipt_path.read_bytes()
        request=self.t.native_115_request
        def disconnected(cookie,path,query):
            if path=='files' and query.get('show_dir')==0:
                raise urllib.error.URLError(ConnectionResetError('disconnected'))
            return request(cookie,path,query)
        with patch.object(self.t,'native_115_request',side_effect=disconnected):
            with self.assertRaises(urllib.error.URLError): self.scan.prepare(set(),self.p)
        self.assertFalse(self.scan.force_full)
        self.assertEqual(before,self.scan.path.read_bytes())
        self.assertEqual(receipt,self.scan.receipt_path.read_bytes())
        self.assertFalse(self.scan.prepare(set(),self.p))
        self.assertEqual(self.t.read_json(self.t.ROOT/'runtime/scan-metrics.json')['mode'],'incremental')

    def test_duplicate_name_requires_action_without_rebuilding(self):
        self.t.dirs['2']['rows'].append(file('42','2','TEST.txt'))
        with self.assertRaises(ActionRequiredError): self.scan.prepare(set(),self.p)
        self.assertFalse(self.scan.force_full)


class WorkerOptimizationTests(unittest.TestCase):
    sandbox=worker_tests.RetryTests.sandbox
    def test_action_required_waits_for_explicit_request(self):
        with self.sandbox() as (root, private, local, clock):
            calls=[]
            def sync(*args,**kwargs):
                calls.append(clock.now)
                if len(calls)==1: raise ActionRequiredError('duplicate name')
                raise Finished()
            def tick():
                if clock.now==160:
                    self.assertEqual(calls,[100])
                    self.assertTrue(t.read_json(root/'runtime/status.json')['requires_action'])
                    (root/'runtime/sync-now').touch()
            clock.tick=tick
            with patch.object(t,'sync_once',side_effect=sync),self.assertRaises(Finished):t.worker_loop()
            self.assertEqual(calls,[100,160])

    def test_unchanged_snapshot_is_not_rewritten(self):
        with self.sandbox() as (root, private, local, clock):
            calls=[]
            def sync(*args,**kwargs):
                calls.append(clock.now)
                if len(calls)==3:raise Finished()
            with patch.object(t,'write_json',wraps=t.write_json) as writer,patch.object(t,'sync_once',side_effect=sync),self.assertRaises(Finished):
                t.worker_loop()
            self.assertEqual(sum(c.args[0]==private/'local-snapshot.json' for c in writer.call_args_list),1)


class StatusTimeTests(unittest.TestCase):
    def test_check_does_not_claim_sync_or_transfer(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            progress=SyncProgress(root)
            progress.data.update(last_sync_at='2026-10-27T10:00:00+08:00',last_transfer_at='2026-10-27T09:00:00+08:00')
            progress.finish(True,synchronized=False)
            self.assertTrue(progress.data['last_check_at'])
            self.assertEqual(progress.data['last_sync_at'],'2026-10-27T10:00:00+08:00')
            self.assertEqual(progress.data['last_transfer_at'],'2026-10-27T09:00:00+08:00')

    def test_only_confirmed_copy_updates_transfer_time_even_if_later_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            progress=SyncProgress(root)
            progress.feed({'object':'a.txt','msg':'Deleted'})
            self.assertIsNone(progress.data['last_transfer_at'])
            progress.feed({'object':'.twodrive-health','msg':'Copied (new)'})
            self.assertIsNone(progress.data['last_transfer_at'])
            progress.feed({'object':'a.txt','msg':'Copied (new)'})
            transfer=progress.data['last_transfer_at']
            self.assertTrue(transfer)
            progress.finish(False)
            self.assertIsNone(progress.data['last_sync_at'])
            with patch.object(t,'ROOT',root):
                t.write_json(root/'runtime/status.json',{'state':'syncing'})
                t.status('error','later file failed')
                self.assertEqual(t.read_json(root/'runtime/status.json')['last_transfer_at'],transfer)

    def test_error_categories(self):
        for error,category,retry in [
            (urllib.error.HTTPError('https://example.test',401,'',None,None),'authorization',False),
            (urllib.error.HTTPError('https://example.test',503,'',None,None),'remote_http',True),
            (OSError(errno.ENOSPC,'full'),'local_access',False),
            (TimeoutError(),'network_or_io',True),
            (RuntimeError('unknown'),'unexpected',False)]:
            with self.subTest(category=category):
                p=recovery_policy(error)
                self.assertEqual((p.category,p.retryable),(category,retry))
