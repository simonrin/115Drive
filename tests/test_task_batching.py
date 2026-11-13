import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from task_scheduling import BatchCoordinator, CheckBatch, AccountChecks
from task_backend import OpenListBackend, RemoteChanged
from task_service import TaskManager
from sync_tasks import TaskStore
from test_task_scheduling import Host
import twodrive


def wait(test, timeout=3):
    deadline = time.monotonic()+timeout
    while not test():
        if time.monotonic() >= deadline: raise TimeoutError('Expected condition did not become true')
        time.sleep(.005)


class BatchTests(unittest.TestCase):
    def test_nearby_tasks_share_batch_without_completion_barrier(self):
        coordinator = BatchCoordinator(window=.15)
        ready = threading.Barrier(3)
        batches = []; slow = threading.Event(); fast_done = threading.Event()
        def run(slow_task):
            ready.wait()
            batch = coordinator.join('one', str(slow_task), threading.Event())
            batches.append(batch)
            if slow_task: slow.wait(2)
            else: fast_done.set()
        threads = [threading.Thread(target=run, args=(value,)) for value in [True, False]]
        for thread in threads: thread.start()
        try:
            ready.wait(); self.assertTrue(fast_done.wait(1))
            wait(lambda:len(batches)==2)
            self.assertTrue(threads[0].is_alive())
            self.assertIs(batches[0], batches[1])
            self.assertEqual(coordinator.metrics, dict(batches=1, tasks=2, largest_batch=2))
        finally:
            slow.set()
            for thread in threads: thread.join(2)

    def test_urgent_flushes_pending_account_without_waiting_for_window(self):
        coordinator = BatchCoordinator(window=20); results=[]
        thread = threading.Thread(target=lambda:results.append(coordinator.join('one','a',threading.Event())))
        thread.start(); wait(lambda:'one' in coordinator.pending)
        started=time.monotonic()
        urgent=coordinator.join('one','b',threading.Event(),urgent=True)
        thread.join(1)
        self.assertLess(time.monotonic()-started,1)
        self.assertEqual(results,[urgent]);self.assertEqual(coordinator.metrics['tasks'],2)

    def test_pause_drops_only_waiter_and_does_not_leave_pending_batch(self):
        coordinator=BatchCoordinator(window=20);cancel=threading.Event();errors=[]
        def run():
            try:coordinator.join('one','a',cancel)
            except InterruptedError:errors.append('cancelled')
        thread=threading.Thread(target=run);thread.start();wait(lambda:'one' in coordinator.pending)
        cancel.set();thread.join(1)
        self.assertEqual(errors,['cancelled']);self.assertEqual(coordinator.pending,{})
        coordinator.join('one','b',threading.Event(),urgent=True)
        self.assertEqual(coordinator.metrics,dict(batches=1,tasks=1,largest_batch=1))

    def test_other_accounts_and_later_rounds_get_different_batches(self):
        coordinator=BatchCoordinator()
        values=[coordinator.join(account,'a',threading.Event(),urgent=True) for account in ['one','two','one']]
        self.assertEqual(len({value.number for value in values}),3)

    def test_read_deduplicates_inflight_returns_copies_and_expires(self):
        now=[0];batch=CheckBatch(1,clock=lambda:now[0]);entered=threading.Event();release=threading.Event()
        values=[];calls=[]
        def loader():
            calls.append(1);entered.set();release.wait(2);return {'files':[1]}
        threads=[threading.Thread(target=lambda:values.append(batch.read('same',loader))) for _ in range(2)]
        threads[0].start();self.assertTrue(entered.wait(1));threads[1].start()
        wait(lambda:batch.metrics['waits']==1);release.set()
        for thread in threads:thread.join(2)
        self.assertEqual(calls,[1]);values[0]['files'].append(9);self.assertEqual(values[1],{'files':[1]})
        self.assertEqual(batch.read('same',lambda:None),{'files':[1]})
        now[0]=5;self.assertEqual(batch.read('same',lambda:{'files':[2]}),{'files':[2]})

    def test_failing_directory_does_not_poison_other_reads_or_future_retry(self):
        batch=CheckBatch(1)
        def fail():raise ValueError('incomplete page')
        with self.assertRaises(ValueError):batch.read('bad',fail)
        self.assertEqual(batch.read('good',lambda:[1]),[1])
        self.assertEqual(batch.read('bad',lambda:[2]),[2])

    def test_large_listings_and_expired_entries_are_not_retained_by_long_batches(self):
        now=[0];batch=CheckBatch(1,clock=lambda:now[0],max_entries=2,max_items=4)
        self.assertEqual(batch.read('large',lambda:list(range(8))),list(range(8)))
        self.assertEqual(batch.entries,{})
        for key in ['one','two','three']:batch.read(key,lambda:[1])
        self.assertEqual(len(batch.entries),2);self.assertNotIn('one',batch.entries)
        now[0]=5;batch.read('new',lambda:[2])
        self.assertEqual(set(batch.entries),{'new'})

    def test_cancelled_follower_does_not_cancel_shared_owner(self):
        batch=CheckBatch(1);entered=threading.Event();release=threading.Event();cancel=threading.Event();values=[];errors=[]
        def loader():entered.set();release.wait(2);return [1]
        owner=threading.Thread(target=lambda:values.append(batch.read('same',loader)))
        def follow():
            try:batch.read('same',lambda:None,cancel)
            except InterruptedError:errors.append('cancelled')
        owner.start();self.assertTrue(entered.wait(1));follower=threading.Thread(target=follow);follower.start()
        try:
            wait(lambda:batch.metrics['waits']==1);cancel.set();follower.join(1)
            self.assertEqual(errors,['cancelled']);self.assertTrue(owner.is_alive())
        finally:release.set();owner.join(2);follower.join(2)
        self.assertEqual(values,[[1]])


class BatchBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.host=Host(Path(self.tmp.name));self.connection_calls=0
        original=self.host.storages
        def storages(token):self.connection_calls+=1;return original(token)
        self.host.storages=storages
        self.one=OpenListBackend(dict(id='a'*32,cloud_path='/test'),self.host)
        self.two=OpenListBackend(dict(id='b'*32,cloud_path='/test'),self.host)
        self.batch=CheckBatch(1)

    def test_batch_shares_connection_and_validated_listing_but_not_mutation_checks(self):
        first=self.one.batch_snapshot(self.batch);second=self.two.batch_snapshot(self.batch)
        self.assertEqual(first,second);self.assertEqual(self.connection_calls,1)
        self.assertEqual(self.one.metrics['directory_lists'],3);self.assertEqual(self.two.metrics['directory_lists'],0)
        self.host.rows['branch'][0]=dict(self.host.rows['branch'][0],fid='changed')
        with self.assertRaises(RemoteChanged):self.two.expect('branch/file.bin',first['branch/file.bin'])
        self.assertEqual(self.connection_calls,2)
        self.assertEqual(self.two.snapshot()['branch/file.bin']['id'],'changed')

    def test_incomplete_or_transport_mismatched_listing_is_not_cached(self):
        self.host.bad=True
        with self.assertRaises(twodrive.IndexInvalidError):self.one.batch_snapshot(self.batch)
        self.host.bad=False
        original=self.host.api
        self.host.api=lambda *args:{'content':[]}
        with self.assertRaises(RemoteChanged):self.one.snapshot(batch=self.batch,transport=True)
        self.host.api=original
        self.assertIn('branch/file.bin',self.two.batch_snapshot(self.batch))

    def test_same_remote_path_new_root_identity_does_not_reuse_old_directory(self):
        old=self.one.batch_snapshot(self.batch)
        original=self.host.native_115_request
        self.host.rows['new-root']=copy.deepcopy(self.host.rows['root'])
        for row in self.host.rows['new-root']:row['pid']='new-root'
        def request(cookie,endpoint,query):
            return {'id':'new-root'} if endpoint=='files/getid' else original(cookie,endpoint,query)
        self.host.native_115_request=request
        previous=self.one.identity
        self.one.batch_snapshot(self.batch)
        self.assertNotEqual(self.one.identity,previous)
        self.assertTrue(any(query.get('cid')=='new-root' for _,query in self.host.calls))

    def test_connection_is_refreshed_in_later_batch_and_for_identity_sensitive_operations(self):
        self.one.batch_snapshot(self.batch);previous=self.one.identity
        def other(token):return [dict(mount_path='/115',driver='115 Cloud',remark='Managed by TwoDrive',addition=json.dumps({'cookie':'UID=456_new;'}))]
        self.host.storages=other
        self.one.connect();self.assertNotEqual(previous,self.one.identity)
        self.one.batch_snapshot(CheckBatch(2));self.assertNotEqual(previous,self.one.identity)


class ManagerTests(unittest.TestCase):
    def test_manager_reads_configuration_once_per_tick_and_workers_share_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'app';root.mkdir();store=TaskStore(root)
            for name in ['one','two']:
                task=store.mutate('create',dict(revision=store.read()['revision'],name=name,local_path=str(root.parent/name),cloud_path='/'+name,policy='online_only'))
                store.mutate('resume',dict(revision=store.read()['revision'],id=task['id']))
            from test_task_runtime import FakeEngine
            manager=TaskManager(types.SimpleNamespace(ROOT=root),backend_factory=lambda *args:None,engine_factory=FakeEngine,shell=False)
            manager.batches=AccountChecks()
            reads=[];original=manager.store.read
            def read():reads.append(1);return original()
            manager.store.read=read
            try:
                manager.tick()
                wait(lambda:len(manager.workers)==2 and all(row.get('engine') and row['engine'].checked.is_set() for row in manager.workers.values()))
                self.assertEqual(len(reads),1)
                self.assertEqual(len({row['last_batch'] for row in manager.workers.values()}),1)
                time.sleep(1.1);self.assertEqual(len(reads),1)
                manager.tick();self.assertEqual(len(reads),2)
            finally:
                manager.stop.set();manager.tick()
                for row in list(manager.workers.values()):row['thread'].join(2)


if __name__=='__main__':unittest.main()
