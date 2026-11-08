import contextlib
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from sync_tasks import TaskStore
from task_shell import apply_policy


class ShellPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.local=self.root/'local';self.local.mkdir()
        (self.local/'a.txt').write_bytes(b'keep');self.store=TaskStore(self.root/'app')
        self.store.mutate('create',dict(revision=0,name='shell',local_path=str(self.local),cloud_path='/shell',policy='always_keep'))
        self.calls=[]
        def pin(*args):self.calls.append(args);return 0
        self.native=types.SimpleNamespace(registered=lambda:True,handle=lambda *a,**kw:contextlib.nullcontext(42),dll=types.SimpleNamespace(CfSetPinState=pin))
    def apply(self,path,policy='online_only'):
        return apply_policy(self.store,dict(path=str(path),policy=policy),native_factory=lambda *a:self.native)
    def test_file_policy_and_native_recursive_pin(self):
        self.assertTrue(self.apply(self.local/'a.txt')['paused'])
        self.assertEqual(self.store.read()['tasks'][0]['overrides'],{'a.txt':'online_only'})
        self.assertEqual(self.calls,[(42,2,1,None)])
    def test_root_sets_default_policy(self):
        self.apply(self.local)
        self.assertEqual(self.store.read()['tasks'][0]['policy'],'online_only')
    def test_foreign_relative_missing_and_invalid_policy_rejected(self):
        for path,policy in [(self.root/'outside','online_only'),('relative','online_only'),(self.local/'missing','online_only'),(self.local/'a.txt','unknown'),(self.local/'../elsewhere','online_only')]:
            with self.subTest(path=path),self.assertRaises((ValueError,OSError)):self.apply(path,policy)
        self.assertEqual(self.store.read()['revision'],1);self.assertEqual(self.calls,[])
    def test_unregistered_and_removing_task_rejected(self):
        self.native.registered=lambda:False
        with self.assertRaises(ValueError):self.apply(self.local/'a.txt')
        self.native.registered=lambda:True
        task=self.store.read()['tasks'][0];directory=self.store.directory(task['id']);directory.mkdir(parents=True)
        (directory/'removal.json').write_text('{}')
        with self.assertRaises(ValueError):self.apply(self.local/'a.txt')
        self.assertEqual(self.calls,[])
    def test_foreign_reparse_point_rejected_before_config_write(self):
        self.native.state=lambda path:dict(identity=dict(task='foreign'))
        with patch('task_shell.placeholder_stat',return_value=types.SimpleNamespace(st_file_attributes=0x400)):
            with self.assertRaises(ValueError):self.apply(self.local/'a.txt')
        self.assertEqual(self.store.read()['revision'],1);self.assertEqual(self.calls,[])
