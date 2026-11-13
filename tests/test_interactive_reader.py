import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from task_backend import OpenListBackend, RemoteChanged
from test_task_scheduling import Host


class InteractiveReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.host=Host(self.root)
        self.backend=OpenListBackend(dict(id='a'*32,cloud_path='/test'),self.host)
        self.backend.connect()
        self.expected=dict(kind='file',id='file-id',size=4,sha1=hashlib.sha1(b'data').hexdigest())
        self.target=self.root/'hydration-stage'

    def test_reader_proceeds_while_owner_lock_is_held_and_preserves_owner_context(self):
        finished=threading.Event(); errors=[]; readers=[]
        def transfer(reader, source, target, cancel, *, kind):
            readers.append(reader); self.assertEqual(kind,'interactive'); Path(target).write_bytes(b'data')
        def run():
            try:self.backend.download_for_open('branch/file.bin',self.expected,self.target,threading.Event())
            except BaseException as error:errors.append(error)
            finally:finished.set()
        self.backend._transaction_parent='unrelated'
        with patch.object(OpenListBackend,'transfer',transfer), self.backend._lock:
            worker=threading.Thread(target=run); worker.start()
            self.assertTrue(finished.wait(2),'interactive reader waited on owner transfer lock')
        worker.join(2)
        self.assertEqual(errors,[]); self.assertEqual(self.target.read_bytes(),b'data')
        self.assertIsNot(readers[0],self.backend)
        self.assertIsNone(readers[0]._transaction_parent)
        self.assertEqual(self.backend._transaction_parent,'unrelated')
        self.assertGreater(self.backend.metrics['directory_lists'],0)
        self.assertEqual(list(self.backend.journal_dir.glob('*.json')),[])

    def test_account_switch_before_read_refused_before_transfer(self):
        self.host.storages=lambda token:[dict(mount_path='/115',driver='115 Cloud',remark='Managed by TwoDrive',addition=json.dumps({'cookie':'UID=456_test;'}))]
        with patch.object(OpenListBackend,'transfer') as transfer:
            with self.assertRaises(RemoteChanged):self.backend.download_for_open('branch/file.bin',self.expected,self.target,threading.Event())
        transfer.assert_not_called(); self.assertFalse(self.target.exists())

    def test_account_switch_after_transfer_refused_and_stage_removed(self):
        original_identity=self.backend.identity
        def transfer(reader,source,target,cancel,**kwargs):
            Path(target).write_bytes(b'data')
            self.host.storages=lambda token:[dict(mount_path='/115',driver='115 Cloud',remark='Managed by TwoDrive',addition=json.dumps({'cookie':'UID=456_test;'}))]
        with patch.object(OpenListBackend,'transfer',transfer):
            with self.assertRaises(RemoteChanged):self.backend.download_for_open('branch/file.bin',self.expected,self.target,threading.Event())
        self.assertFalse(self.target.exists()); self.assertEqual(self.backend.identity,original_identity)

    def test_root_replacement_after_transfer_refused(self):
        original=self.host.native_115_request
        def transfer(reader,source,target,cancel,**kwargs):
            Path(target).write_bytes(b'data')
            self.host.native_115_request=lambda c,e,q: {'id':'replacement'} if e=='files/getid' else original(c,e,q)
        with patch.object(OpenListBackend,'transfer',transfer):
            with self.assertRaises(RemoteChanged):self.backend.download_for_open('branch/file.bin',self.expected,self.target,threading.Event())
        self.assertFalse(self.target.exists())

    def test_path_version_change_after_transfer_refused(self):
        def transfer(reader,source,target,cancel,**kwargs):
            Path(target).write_bytes(b'data'); self.host.rows['branch'][0]['fid']='replacement'
        with patch.object(OpenListBackend,'transfer',transfer):
            with self.assertRaises(RemoteChanged):self.backend.download_for_open('branch/file.bin',self.expected,self.target,threading.Event())
        self.assertFalse(self.target.exists())

    def test_cancelled_read_removes_partial_stage_and_does_not_touch_owner_state(self):
        cancel=threading.Event()
        def transfer(reader,source,target,stop,**kwargs):
            Path(target).write_bytes(b'par'); cancel.set(); raise InterruptedError('cancelled')
        with patch.object(OpenListBackend,'transfer',transfer):
            with self.assertRaises(InterruptedError):self.backend.download_for_open('branch/file.bin',self.expected,self.target,cancel)
        self.assertFalse(self.target.exists()); self.assertIsNone(self.backend._transaction_parent)


if __name__=='__main__':unittest.main()
