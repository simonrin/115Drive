import json,shutil,tempfile,threading,unittest,uuid
from pathlib import Path
from cloud_transactions import IdentityTransactions
from task_backend import LocalBackend,version,ConcurrentChange,sha1

class Backend(IdentityTransactions,LocalBackend):
    def __init__(self,root,state):super().__init__(root);self.bind(state)
    def transaction_rows(self):return self.snapshot()
    def upload_fresh(self,path,source,cancelled):
        with self.path(path).open('xb') as output,source.open('rb') as stream:shutil.copyfileobj(stream,output)
    def rename_identity(self,old,new,expected):
        if version(self.snapshot().get(old))!=version(expected):raise ConcurrentChange('changed identity')
        try:self.path(old).rename(self.path(new))
        except FileExistsError:raise ConcurrentChange('occupied name')
    def upload(self,path,source,expected,cancelled):return self.transaction_upload(path,source,expected,cancelled)

class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.b=Backend(self.root/'remote',self.root/'state')
        self.source=self.root/'source';self.source.write_bytes(b'version A')
        self.b.path('file').write_bytes(b'original');self.old=self.b.snapshot()['file']
        self.b.operation_id=uuid.uuid4().hex
    def hashes(self):return {row.get('sha1') for row in self.b.snapshot().values()}
    def test_restart_recovers_stage_archive_and_promote_without_reupload(self):
        for point in ('remote_staged','remote_archived','remote_promoted'):
            with self.subTest(point=point),tempfile.TemporaryDirectory() as folder:
                b=Backend(Path(folder)/'remote',Path(folder)/'state');b.path('file').write_bytes(b'original')
                old=b.snapshot()['file'];op=uuid.uuid4().hex;b.operation_id=op
                def crash(step):
                    if step==point:raise OSError('injected process death')
                b.fault=crash
                with self.assertRaises(OSError):b.upload('file',self.source,old,threading.Event())
                b=Backend(Path(folder)/'remote',Path(folder)/'state');b.operation_id=op
                result=b.upload('file',self.source,old,threading.Event())
                self.assertEqual(result['sha1'],sha1(self.source))
                self.assertEqual(len(b.snapshot()),2)
                self.assertTrue(json.loads((b.journal_dir/(op+'.json')).read_text())['committed'])
    def test_competing_writer_between_check_and_promotion_preserves_all_versions(self):
        def race(step):
            if step=='before_remote_promote':self.b.path('file').write_bytes(b'version B')
        self.b.fault=race
        with self.assertRaises(ConcurrentChange):self.b.upload('file',self.source,self.old,threading.Event())
        self.assertEqual(self.b.path('file').read_bytes(),b'version B')
        self.assertEqual(len(self.hashes()),3)
    def test_stale_writer_cannot_archive_new_remote_identity(self):
        newer=self.b.path('replacement');newer.write_bytes(b'version B');newer.replace(self.b.path('file'))
        with self.assertRaises(ConcurrentChange):self.b.upload('file',self.source,self.old,threading.Event())
        self.assertEqual(self.b.path('file').read_bytes(),b'version B')
    def test_delete_is_recoverable_identity_archive(self):
        self.b.transaction_delete('file',self.old)
        self.assertFalse(self.b.path('file').exists())
        self.assertIn(self.old['sha1'],self.hashes())

if __name__=='__main__':unittest.main()
