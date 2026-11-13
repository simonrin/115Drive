import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from task_backend import OpenListBackend, RemoteChanged
from task_engine import TaskEngine
from task_scheduling import AccountChecks, CheckBatch
from test_task_scheduling import Host


class AccountRoundTests(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        self.groups = AccountChecks(clock=lambda:self.now[0])
        self.cancel = threading.Event()
        self.tasks = [dict(id=x,account='one',paused=False) for x in ('small','large')]
        self.groups.configure(self.tasks)
    def take(self, task, **kwargs): return self.groups.take('one',task,self.cancel,**kwargs)
    def finish(self, task, batch, **kwargs): self.groups.finished('one',task,batch,**kwargs)

    def test_small_directory_waits_for_same_round_large_scan(self):
        small=self.take('small');large=self.take('large')
        self.assertIs(small,large)
        self.finish('small',small)
        self.now[0]=200
        self.assertIsNone(self.take('small'))
        self.finish('large',large)
        self.now[0]+=30
        following=self.take('small')
        self.assertIsNot(following,small)
        self.assertIs(self.take('large'),following)

    def test_transfer_not_scan_completion_is_excluded_from_next_round(self):
        small=self.take('small');large=self.take('large')
        self.groups.scanned('one','large',large,True)
        self.finish('small',small)
        self.now[0]=15
        following=self.take('small')
        self.assertIsNotNone(following)
        self.finish('small',following)
        self.assertIsNone(self.groups.accounts['one']['round'])
        self.finish('large',large)
        self.now[0]+=30
        third=self.take('large')
        self.assertIsNotNone(third)

    def test_local_change_can_run_without_restarting_siblings(self):
        first=self.take('small');large=self.take('large')
        self.finish('small',first)
        urgent=self.take('small',urgent=True)
        self.assertIsNotNone(urgent);self.assertIsNot(urgent,large)
        self.finish('small',urgent,changed=True)
        self.assertEqual(self.groups.accounts['one']['round']['pending'],{'large'})

    def test_pause_failed_start_and_retry_do_not_hold_group(self):
        first=self.take('small');self.finish('small',first)
        self.groups.unavailable('one','large',retry_seconds=300)
        self.now[0]=15
        following=self.take('small');self.assertIsNotNone(following)
        self.finish('small',following)
        self.assertIsNone(self.take('large',urgent=True))
        self.tasks[1]['paused']=True;self.groups.configure(self.tasks)
        self.now[0]=400
        self.assertIsNone(self.take('large'))
        self.tasks[1]['paused']=False;self.groups.configure(self.tasks)
        self.assertIsNotNone(self.take('large',urgent=True))

    def test_separate_account_and_cancel_are_independent(self):
        self.groups.configure(self.tasks+[dict(id='other',account='two',paused=False)])
        first=self.take('small')
        other=self.groups.take('two','other',self.cancel)
        self.assertIsNot(first,other)
        self.cancel.set()
        with self.assertRaises(InterruptedError):self.take('large')


class LargeTreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.host=Host(self.root)
        self.host.rows={'root':[]}
        for number in range(258):
            name='folder'+str(number)
            self.host.rows['root'].append(dict(n=name,cid=name,pid='root'))
            self.host.rows[name]=[dict(n='file.txt',cid=name,fid='file'+str(number),s=4,sha=hashlib.sha1(b'data').hexdigest())]
        self.api_calls=[];original=self.host.api
        def api(endpoint,body,token):
            self.api_calls.append((endpoint,dict(body)))
            return original(endpoint,body,token)
        self.host.api=api
        self.task=dict(id='a'*32,local_path=str(self.root/'local'),cloud_path='/test',account='115',policy='always_keep',cache_hours=24,overrides={})
        self.backend=OpenListBackend(self.task,self.host)

    def test_one_full_scan_instead_of_two_and_no_unrelated_transport_refresh(self):
        old_start=self.backend.recovery_snapshot();old_check=self.backend.snapshot()
        old_lists=sum(endpoint=='files' for endpoint,_ in self.host.calls)
        old_transport=len(self.api_calls)
        self.host.calls.clear();self.api_calls.clear()
        start=self.backend.startup_snapshot()
        self.assertEqual(sum(endpoint=='files' for endpoint,_ in self.host.calls),1)
        self.assertEqual(len(start),258)
        new_check=self.backend.batch_snapshot(CheckBatch(1))
        new_lists=sum(endpoint=='files' for endpoint,_ in self.host.calls)
        self.assertEqual(old_start,old_check);self.assertEqual(old_check,new_check)
        self.assertEqual((old_lists,new_lists,old_transport,len(self.api_calls)),(518,260,259,0))

    def test_startup_keeps_history_and_rejects_changed_health_before_native_connect(self):
        local=Path(self.task['local_path']);local.mkdir()
        connected=[]
        native=type('Native',(),{'connect':lambda _:connected.append(True)})()
        engine=TaskEngine(self.task,self.root/'state',self.backend,native_factory=lambda *a,**k:native)
        self.backend.connect()
        marker='health-proof'
        engine.state.update(backend=self.backend.identity,health=marker,local_root_id=local.stat().st_ino)
        engine.save();before=engine.state_path.read_bytes()
        health=dict(n=engine.health_name,cid='root',fid='health',s=len(marker),sha=hashlib.sha1(marker.encode()).hexdigest())
        self.host.rows['root'].append(health)
        self.host.calls.clear();engine.start()
        self.assertEqual(connected,[True]);self.assertEqual(engine.state_path.read_bytes(),before)
        self.assertEqual(sum(endpoint=='files' for endpoint,_ in self.host.calls),1)
        connected.clear();self.host.rows['root'][-1]=dict(health,sha='f'*40)
        with self.assertRaisesRegex(RuntimeError,'健康标记'):engine.start()
        self.assertEqual(connected,[]);self.assertEqual(engine.state_path.read_bytes(),before)

    def test_transport_mismatch_blocks_download_before_transfer(self):
        expected=self.backend.scan_snapshot()['folder1/file.txt']
        self.host.api=lambda *args:{'content':[]}
        with patch.object(self.backend,'transfer') as transfer:
            with self.assertRaises(RemoteChanged):
                self.backend.download('folder1/file.txt',expected,self.root/'download',threading.Event())
            transfer.assert_not_called()

    def test_transport_refresh_only_reads_ancestors_and_target_for_transfer(self):
        expected=self.backend.scan_snapshot()['folder1/file.txt']
        self.host.calls.clear();self.api_calls.clear()
        with patch.object(self.backend,'transfer',side_effect=lambda source,target,*a,**k:Path(target).write_bytes(b'data')):
            self.backend.download('folder1/file.txt',expected,self.root/'download',threading.Event())
        self.assertEqual({query['cid'] for endpoint,query in self.host.calls if endpoint=='files'},{'root','folder1'})
        self.assertEqual([body['path'] for endpoint,body in self.api_calls],['/115/test','/115/test/folder1'])

    def test_pending_upload_looks_up_only_its_parent_not_entire_tree(self):
        self.backend.recovery_entry('folder1/file.txt')
        self.assertEqual([q['cid'] for endpoint,q in self.host.calls if endpoint=='files'],['root','folder1'])

    def test_scan_cancellation_stops_before_reading_next_directory(self):
        cancel=threading.Event();self.backend.cancelled=cancel
        original=self.host.native_115_request
        def request(cookie,endpoint,query):
            result=original(cookie,endpoint,query)
            if endpoint=='files':cancel.set()
            return result
        self.host.native_115_request=request
        with self.assertRaises(InterruptedError):self.backend.scan_snapshot()
        self.assertEqual(sum(endpoint=='files' for endpoint,_ in self.host.calls),1)
