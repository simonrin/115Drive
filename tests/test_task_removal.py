import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from runtime_json import atomic_write_json
from sync_tasks import TaskStore, file_lock
from task_removal import detach


class Native:
    def __init__(self, root, task_id, fetch):
        self.root=Path(root);self.task_id=task_id;self.items={};self.owned=True;self.fail=None
    def registered(self):return self.owned
    def path(self, relative):return self.root/relative
    def state(self, path):return self.items.get(Path(path).relative_to(self.root).as_posix())
    def revert(self, relative):
        if self.fail==relative:raise OSError('held open')
        self.items.pop(relative)
    def discard_online(self, relative):
        if self.fail==relative:raise OSError('held open')
        if not self.items[relative]['partial']:return False
        self.path(relative).unlink();self.items.pop(relative);return True
    def unregister(self):self.owned=False


class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=TaskStore(self.root/'app')
        self.local=self.root/'local';self.local.mkdir()
        result=self.store.mutate('create',dict(revision=0,name='one',local_path=str(self.local),cloud_path='/one',policy='always_keep'))
        self.id=result['id'];self.task=self.store.read()['tasks'][0];self.directory=self.store.directory(self.id)
        self.directory.mkdir(parents=True)
        self.state=dict(version=1,task=self.id,local=str(self.local),cloud='/one',local_root_id=self.local.stat().st_ino,pending=None,items={})
        atomic_write_json(self.directory/'state.json',self.state)
        (self.directory/'history.txt').write_text('original history')
        self.native=Native(self.local,self.id,None);self.shell=[]
        for name in ['a.txt','b.txt']:
            (self.local/name).write_bytes(('content '+name).encode())
            self.native.items[name]=dict(partial=False,insync=True,modified=0,identity=dict(task=self.id,kind='file',size=(self.local/name).stat().st_size))
        self.files={p.name:p.read_bytes() for p in self.local.iterdir()}
    def remove(self,**kwargs):
        return self.store.remove(dict(id=self.id,revision=self.store.read()['revision']),
            lambda task,directory:detach(self.store.root,task,directory,native_factory=lambda *args:self.native,unregister_shell=lambda root,task:self.shell.append(task['id'])),**kwargs)
    def test_removes_only_entry_preserving_files_and_history(self):
        state=(self.directory/'state.json').read_bytes();self.remove()
        self.assertEqual(self.store.read()['tasks'],[])
        self.assertEqual({p.name:p.read_bytes() for p in self.local.iterdir()},self.files)
        self.assertEqual((self.directory/'state.json').read_bytes(),state)
        self.assertEqual((self.directory/'history.txt').read_text(),'original history')
        self.assertEqual(json.loads((self.directory/'removal.json').read_text())['task'],self.task)
        self.assertFalse(self.native.owned);self.assertEqual(self.shell,[self.id])
    def test_running_stopping_worker_and_stale_revision_rejected(self):
        with self.assertRaises(ValueError):self.remove(worker_stopped=lambda _:False)
        with file_lock(self.directory/'worker.lock'):
            with self.assertRaises(OSError):self.remove()
        self.store.mutate('resume',dict(id=self.id,revision=1))
        with self.assertRaises(ValueError):self.remove()
        with self.assertRaises(ValueError):self.store.remove(dict(id=self.id,revision=1),lambda *args:self.fail('detached'))
        self.assertEqual(len(self.native.items),2)
    def test_online_stub_removed_without_changing_downloaded_files_or_history(self):
        self.native.items['b.txt']['partial']=True
        state=(self.directory/'state.json').read_bytes();self.remove()
        self.assertEqual(self.store.read()['tasks'],[])
        self.assertFalse((self.local/'b.txt').exists());self.assertEqual((self.local/'a.txt').read_bytes(),self.files['a.txt'])
        self.assertEqual((self.directory/'state.json').read_bytes(),state)
        self.assertEqual((self.directory/'history.txt').read_text(),'original history')
    def test_dirty_partial_preflight_changes_nothing_and_allows_resume(self):
        for change in [dict(insync=False),dict(modified=1),dict(identity=dict(task=self.id,kind='file',size=999))]:
            with self.subTest(change=change):
                old=dict(self.native.items['b.txt']);self.native.items['b.txt'].update(partial=True,**change)
                with self.assertRaisesRegex(ValueError,'未同步修改'):self.remove()
                self.assertEqual(len(self.native.items),2);self.assertFalse(self.store.removing(self.id))
                self.native.items['b.txt']=old
        self.store.mutate('resume',dict(id=self.id,revision=1))
    def test_online_stub_failure_is_retryable_and_does_not_resume(self):
        self.native.items['a.txt']['partial']=True;self.native.fail='b.txt'
        with self.assertRaises(OSError):self.remove()
        self.assertTrue(self.store.removing(self.id));self.assertTrue((self.local/'b.txt').exists())
        self.assertFalse((self.local/'a.txt').exists())
        self.store.set_all_paused(False);self.assertTrue(self.store.read()['tasks'][0]['paused'])
        self.native.fail=None;self.remove()
        self.assertEqual((self.local/'b.txt').read_bytes(),self.files['b.txt'])
    def test_hydrated_while_detaching_is_retained(self):
        self.native.items['b.txt']['partial']=True
        def hydrated(relative):
            self.native.items[relative]['partial']=False
            return False
        with patch.object(self.native,'discard_online',side_effect=hydrated):self.remove()
        self.assertEqual({p.name:p.read_bytes() for p in self.local.iterdir()},self.files)
    def test_pending_work_and_foreign_files_rejected(self):
        self.state['pending']={'kind':'upload'};atomic_write_json(self.directory/'state.json',self.state)
        with self.assertRaisesRegex(ValueError,'未完成'):self.remove()
        self.state['pending']=None;atomic_write_json(self.directory/'state.json',self.state)
        self.native.items['a.txt']['identity']['task']='b'*32
        with self.assertRaisesRegex(ValueError,'其他任务'):self.remove()
        self.assertFalse(self.store.removing(self.id))
    def test_interrupted_detach_stays_paused_and_is_retryable(self):
        self.native.fail='b.txt'
        with self.assertRaises(OSError):self.remove()
        self.assertTrue(self.store.removing(self.id));self.assertNotIn('a.txt',self.native.items)
        for op,extra in [('resume',{}),('preferences',dict(policy='online_only'))]:
            with self.assertRaises(ValueError):self.store.mutate(op,dict(id=self.id,revision=1,**extra))
        self.store.set_all_paused(False);self.assertTrue(self.store.read()['tasks'][0]['paused'])
        self.native.fail=None;self.remove();self.assertEqual(self.store.read()['tasks'],[])
        self.assertEqual({p.name:p.read_bytes() for p in self.local.iterdir()},self.files)
    def test_configuration_write_failure_after_detach_can_retry(self):
        with patch('sync_tasks.atomic_write_json',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):self.remove()
        self.assertEqual(len(self.store.read()['tasks']),1);self.assertFalse(self.native.owned)
        self.remove();self.assertEqual(self.store.read()['tasks'],[])
    def test_never_started_missing_folder_is_not_created(self):
        missing=self.root/'missing';other=self.store.mutate('create',dict(revision=1,name='new',local_path=str(missing),cloud_path='/new',policy='always_keep'))
        self.store.remove(dict(id=other['id'],revision=2),lambda task,directory:detach(self.store.root,task,directory,native_factory=lambda *args:self.fail('created native'),unregister_shell=lambda *args:None))
        self.assertFalse(missing.exists());self.assertEqual(self.store.read()['tasks'],[self.task])
    def test_replaced_root_and_unfinished_remote_transaction_rejected(self):
        self.state['local_root_id']+=1;atomic_write_json(self.directory/'state.json',self.state)
        with self.assertRaisesRegex(ValueError,'替换'):self.remove()
        self.state['local_root_id']-=1;atomic_write_json(self.directory/'state.json',self.state)
        atomic_write_json(self.directory/'remote-transactions'/'x.json',dict(committed=False))
        with self.assertRaisesRegex(ValueError,'云端事务'):self.remove()
        self.assertFalse(self.store.removing(self.id))
    def test_completed_delete_history_kept_but_uncheckpointed_delete_blocks(self):
        path=self.directory/'remote-transactions'/'delete.json'
        atomic_write_json(path,dict(kind='delete',expected=dict(id='old')))
        self.state['items']={'old.txt':dict(remote=dict(id='old'))};atomic_write_json(self.directory/'state.json',self.state)
        with self.assertRaisesRegex(ValueError,'云端事务'):self.remove()
        self.state['items']={};atomic_write_json(self.directory/'state.json',self.state)
        history=path.read_bytes();self.remove();self.assertEqual(path.read_bytes(),history)
    def test_http_delete_requires_nonce_and_paused_stopped_worker(self):
        import threading,urllib.request,urllib.error,types
        from http.server import ThreadingHTTPServer
        import setup_ui as ui
        server=ThreadingHTTPServer(('127.0.0.1',0),ui.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        def post(nonce):
            request=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+'/tasks/delete',data=json.dumps(dict(id=self.id,revision=self.store.read()['revision'])).encode(),headers={'Host':'127.0.0.1:5245','X-TwoDrive-Nonce':nonce})
            return urllib.request.urlopen(request,timeout=5)
        try:
            with patch.object(ui.t,'ROOT',self.store.root),patch.object(ui,'BUSY',threading.Lock()),patch.object(ui.task_service,'_MANAGER',types.SimpleNamespace(worker_stopped=lambda _:True)),patch('task_removal.detach') as operation:
                thread.start()
                with self.assertRaises(urllib.error.HTTPError) as failure:post('invalid')
                self.assertEqual(failure.exception.code,403);operation.assert_not_called()
                self.store.mutate('resume',dict(id=self.id,revision=1))
                with self.assertRaises(urllib.error.HTTPError) as failure:post(ui.NONCE)
                self.assertEqual(failure.exception.code,400);operation.assert_not_called()
                self.store.mutate('pause',dict(id=self.id,revision=2))
                with post(ui.NONCE) as response:self.assertTrue(json.load(response)['ok'])
                operation.assert_called_once();self.assertEqual(self.store.read()['tasks'],[])
        finally:
            server.shutdown();server.server_close();thread.join(3)


if __name__=='__main__':unittest.main()
