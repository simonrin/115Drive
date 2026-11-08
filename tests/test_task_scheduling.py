import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
import twodrive
from task_backend import OpenListBackend, RemoteChanged
from task_engine import TaskEngine
from task_scheduling import FairGate, IdleSchedule, METADATA, TRANSFERS


class ScheduleTests(unittest.TestCase):
    def test_idle_backoff_local_wakeup_settings_and_error_cooldown(self):
        now = [0]
        schedule = IdleSchedule('0'*32, clock=lambda:now[0])
        self.assertTrue(schedule.due())
        for changed, delay in [(True,15),(False,30),(False,60),(False,120),(False,120)]:
            schedule.complete(changed)
            self.assertEqual(schedule.last_interval,delay)
            self.assertFalse(schedule.due())
            now[0] += delay
            self.assertTrue(schedule.due())
        schedule.complete(False)
        schedule.local_change();now[0]+=1
        self.assertTrue(schedule.due())
        schedule.complete(True)
        self.assertTrue(schedule.due(settings_changed=True))
        schedule.failed();schedule.local_change()
        self.assertFalse(schedule.due(settings_changed=True))
        now[0] += 15;self.assertTrue(schedule.due())
        for _ in range(8):schedule.failed()
        self.assertEqual(schedule.last_interval,300)

    def test_tasks_are_staggered_and_never_exceed_idle_ceiling(self):
        first,second=[IdleSchedule(x*32,clock=lambda:0) for x in ('0','f')]
        for _ in range(6):first.complete(False);second.complete(False)
        self.assertLess(second.deadline,first.deadline)
        self.assertLessEqual(first.deadline,120)

    def test_gate_fifo_cancellation_and_slot_recovery(self):
        gate=FairGate();cancel=threading.Event();order=[];errors=[]
        def run(number,cancelled=None):
            try:
                with gate.slot(cancelled):order.append(number)
            except InterruptedError:errors.append(number)
        threads=[]
        with gate.slot():
            for number in range(3):
                thread=threading.Thread(target=run,args=(number,cancel if number==1 else None));threads.append(thread);thread.start()
                deadline=time.monotonic()+2
                while len(gate.waiters)<number+1 and time.monotonic()<deadline:time.sleep(.005)
                self.assertEqual(len(gate.waiters),number+1)
            cancel.set();threads[1].join(2)
            self.assertFalse(threads[1].is_alive())
        for thread in threads:thread.join(2)
        self.assertEqual(order,[0,2]);self.assertEqual(errors,[1]);self.assertEqual(gate.active,0)
        with self.assertRaises(ValueError):
            with gate.slot():raise ValueError('operation failed')
        with gate.slot():self.assertEqual(gate.active,1)

    def test_large_transfer_lane_does_not_reserve_metadata_or_open_downloads(self):
        with TRANSFERS['upload'].slot(),TRANSFERS['background'].slot():
            with METADATA.slot(),TRANSFERS['interactive'].slot():
                self.assertEqual(METADATA.active,1)
                self.assertEqual(TRANSFERS['interactive'].active,1)

    def test_two_slots_bound_concurrency_and_cancel_waiting_third(self):
        gate=FairGate(2);cancel=threading.Event();entered=threading.Event();stopped=threading.Event()
        def third():
            try:
                with gate.slot(cancel):entered.set()
            except InterruptedError:stopped.set()
        with gate.slot(),gate.slot():
            thread=threading.Thread(target=third);thread.start()
            deadline=time.monotonic()+2
            while not gate.waiters and time.monotonic()<deadline:time.sleep(.005)
            self.assertFalse(entered.is_set());self.assertEqual(gate.active,2)
            cancel.set();self.assertTrue(stopped.wait(1));thread.join(1)
        self.assertEqual(gate.peak_active,2);self.assertEqual(gate.active,0)


class StateWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.engine=TaskEngine(dict(id='a'*32,local_path=str(self.root/'local'),cloud_path='/test'),
            self.root/'state',None,native_factory=lambda *args,**kwargs:None)
        self.remote=dict(kind='file',id='1',size=4,sha1='a'*40)

    def test_unchanged_checkpoints_preserve_state_bytes_and_timestamp(self):
        self.engine.checkpoint('file',self.remote,applied_pin=1)
        path=self.engine.state_path;before=(path.read_bytes(),path.stat().st_mtime_ns)
        with patch('task_engine.atomic_write_json',side_effect=AssertionError('Unchanged state written')):
            for _ in range(20):
                self.engine.checkpoint('file',dict(self.remote),applied_pin=1)
                self.assertFalse(self.engine.save())
        self.assertEqual((path.read_bytes(),path.stat().st_mtime_ns),before)

    def test_failed_durable_write_does_not_advance_saved_state(self):
        self.engine.save();before=self.engine.state_path.read_bytes()
        self.engine.state['pending']={'kind':'upload','stage':'a'*32}
        with patch('task_engine.atomic_write_json',side_effect=OSError('blocked')):
            with self.assertRaises(OSError):self.engine.save()
        self.assertEqual(self.engine.state_path.read_bytes(),before)
        from runtime_json import atomic_write_json
        with patch('task_engine.atomic_write_json',wraps=atomic_write_json) as write:
            self.assertTrue(self.engine.save());self.assertTrue(write.call_args.kwargs['durable'])
        self.assertEqual(json.loads(self.engine.state_path.read_text())['pending'],self.engine.state['pending'])


class Host:
    def __init__(self,root):
        self.ROOT=root;self.PRIVATE=root/'private';self.calls=[];self.bad=False
        self.rows={
            'root':[dict(n='branch',cid='branch',pid='root'),dict(n='unrelated',cid='unrelated',pid='root')],
            'branch':[dict(n='file.bin',cid='branch',fid='file-id',s=4,sha=hashlib.sha1(b'data').hexdigest())],
            'unrelated':[dict(n='other.bin',cid='unrelated',fid='other-id',s=4,sha='b'*40)],
        }
        self.native_115_list=twodrive.native_115_list
    def login(self):return 'token'
    def storages(self,token):return [dict(mount_path='/115',driver='115 Cloud',remark='Managed by TwoDrive',addition=json.dumps({'cookie':'UID=123_test;'}))]
    def native_115_request(self,cookie,endpoint,query):
        self.calls.append((endpoint,dict(query)))
        if endpoint=='files/getid':return {'id':'root'}
        cid=query['cid'];rows=self.rows[cid]
        return dict(state=True,cid=cid,offset=0,count=len(rows)+(1 if self.bad else 0),data=list(rows))
    def api(self,endpoint,query,token):
        cid=query['path'].split('/')[-1]
        if cid=='test':cid='root'
        return {'content':[dict(name=r['n'],is_dir=not bool(r.get('fid')),size=int(r.get('s',0))) for r in self.rows[cid]]}


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.host=Host(Path(self.tmp.name))
        self.one=OpenListBackend(dict(id='a'*32,cloud_path='/test'),self.host)
        self.two=OpenListBackend(dict(id='b'*32,cloud_path='/test'),self.host)
        self.expected=dict(kind='file',id='file-id',size=4,sha1=hashlib.sha1(b'data').hexdigest())
    def test_target_check_skips_unrelated_subtrees_but_rejects_incomplete_listing(self):
        self.assertEqual(self.one.expect('branch/file.bin',self.expected),self.expected)
        directories=[q['cid'] for endpoint,q in self.host.calls if endpoint=='files']
        self.assertEqual(directories,['root','branch'])
        self.host.bad=True
        with self.assertRaises(twodrive.IndexInvalidError):self.one.expect('branch/file.bin',self.expected)

    def test_transaction_scope_reads_only_parent_and_restores_after_failure(self):
        with self.assertRaises(RuntimeError):
            with self.one.transaction_scope('branch/file.bin'):
                rows=self.one.transaction_rows()
                self.assertIn('branch/file.bin',rows);self.assertNotIn('unrelated/other.bin',rows)
                raise RuntimeError('interrupted transaction')
        self.assertIsNone(self.one._transaction_parent)
        self.assertIn('unrelated/other.bin',self.one.snapshot())

    def test_another_task_can_scan_while_download_is_blocked_and_postcheck_is_fresh(self):
        entered=threading.Event();release=threading.Event();finished=threading.Event();errors=[]
        def transfer(source,target,cancelled,**kwargs):
            entered.set();release.wait(3);Path(target).write_bytes(b'data')
        self.one.transfer=transfer
        def download():
            try:self.one.download('branch/file.bin',self.expected,Path(self.tmp.name)/'output',threading.Event())
            except Exception as error:errors.append(error)
        thread=threading.Thread(target=download);thread.start()
        try:
            self.assertTrue(entered.wait(2))
            other=threading.Thread(target=lambda:(self.two.snapshot(),finished.set()));other.start()
            self.assertTrue(finished.wait(1),'A transfer blocked another task metadata scan')
            self.host.rows['branch'][0]=dict(self.host.rows['branch'][0],fid='replacement')
        finally:release.set();thread.join(3)
        self.assertEqual(len(errors),1);self.assertIsInstance(errors[0],RemoteChanged)


if __name__=='__main__':unittest.main()
