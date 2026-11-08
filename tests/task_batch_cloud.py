"""Opt-in: two real 115 roots share checking while an independent upload runs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import twodrive as t
from task_backend import OpenListBackend, sha1
from task_engine import TaskEngine
from task_scheduling import BatchCoordinator
from runtime_json import atomic_write_json


class CountedHost:
    def __init__(self):
        self.counts = dict(login=0, account=0, native=0, transport=0)
        self.lock = threading.Lock()
    def __getattr__(self, name): return getattr(t, name)
    def count(self, name):
        with self.lock: self.counts[name] += 1
    def login(self): self.count('login'); return t.login()
    def storages(self, token): self.count('account'); return t.storages(token)
    def native_115_request(self, *args): self.count('native'); return t.native_115_request(*args)
    def api(self, *args): self.count('transport'); return t.api(*args)


def main():
    if '--live-115' not in sys.argv: raise SystemExit('Pass --live-115 to use generated cloud test folders.')
    run = t.ROOT/'tests/runs'/('batch-cloud-'+uuid.uuid4().hex[:12]); run.mkdir(parents=True)
    print(run, flush=True)
    cloud = '/.twodrive-batch-'+uuid.uuid4().hex
    result = dict(ok=False, cloud=cloud, checks=[])
    atomic_write_json(run/'result.json', result, indent=2)
    host = CountedHost(); token=t.login()
    for path in [cloud,cloud+'/one',cloud+'/two',cloud+'/upload']:
        t.api('/api/fs/mkdir',dict(path='/115'+path),token)
    small=run/'small.bin';small.write_bytes(os.urandom(262157));wanted=sha1(small)
    engines=[];large_thread=None;cancel=threading.Event();errors=[];uploaded=[]
    coordinator=BatchCoordinator()
    try:
        for name in ['one','two']:
            t.rc('copyto',str(small),'cloud115:'+cloud+'/'+name+'/small.bin')
            local=run/name;local.mkdir()
            task=dict(id=uuid.uuid4().hex,account='115',name=name,local_path=str(local),cloud_path=cloud+'/'+name,
                      policy='online_only',cache_hours=24,overrides={},paused=False)
            engine=TaskEngine(task,run/(name+'-state'),OpenListBackend(task,host));engines.append(engine)
            engine.cycle()
            assert engine.native.state(local/'small.bin')['partial']
        before=dict(host.counts)
        for engine in engines:engine.cycle()
        result['separate_checks']={key:host.counts[key]-before[key] for key in before}

        def grouped():
            barrier=threading.Barrier(len(engines));batch_ids=[];failures=[]
            def run_engine(engine):
                try:
                    barrier.wait(5)
                    batch=coordinator.join('115',engine.task['id'],cancel)
                    batch_ids.append(batch.number)
                    engine.check_batch(batch)
                except Exception as error:failures.append(repr(error))
            threads=[threading.Thread(target=run_engine,args=(engine,)) for engine in engines]
            for thread in threads:thread.start()
            for thread in threads:thread.join(90)
            assert not any(thread.is_alive() for thread in threads) and not failures,failures
            assert len(batch_ids)==2 and len(set(batch_ids))==1,batch_ids
            return batch_ids[0]

        before=dict(host.counts);result['batch_id']=grouped()
        result['grouped_checks']={key:host.counts[key]-before[key] for key in before}
        assert result['separate_checks']['account']==2 and result['grouped_checks']['account']==1,result
        result['checks'].append('two_roots_one_batch_account_checks_2_to_1')
        print('PASS shared account validation',flush=True)

        class LimitedHost:
            def __getattr__(self,name):return getattr(t,name)
            def transfer_flags(self):return [*t.transfer_flags(),'--bwlimit','512K']
        writer=OpenListBackend(dict(id=uuid.uuid4().hex,cloud_path=cloud+'/upload'),LimitedHost());writer.bind(run/'upload-state')
        large=run/'large.bin';large.write_bytes(os.urandom(32*1024**2+17))
        entered=threading.Event()
        writer.fault=lambda point:entered.set() if point=='transfer_started' else None
        def upload():
            try:uploaded.append(writer.upload('large.bin',large,None,cancel))
            except Exception as error:errors.append(repr(error))
        large_thread=threading.Thread(target=upload);large_thread.start()
        assert entered.wait(90),'Large transfer did not start'
        started=time.monotonic();result['upload_batch_id']=grouped()
        result['batch_seconds_during_upload']=round(time.monotonic()-started,3)
        assert large_thread.is_alive() and not errors,errors
        result['checks'].append('batch_checks_finish_while_large_upload_active')
        print('PASS grouped checks during upload',flush=True)
        started=time.monotonic()
        read=subprocess.run([sys.executable,'-c','import pathlib,hashlib,sys;print(hashlib.sha1(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())',str(engines[0].root/'small.bin')],capture_output=True,text=True,timeout=90)
        assert read.returncode==0 and read.stdout.strip()==wanted,(read.stdout,read.stderr)
        result['native_read_seconds_during_upload']=round(time.monotonic()-started,3)
        assert large_thread.is_alive() and not errors,errors
        result['checks'].append('native_open_finishes_while_large_upload_active')
        print('PASS native open during upload',flush=True)
        large_thread.join(180)
        assert not large_thread.is_alive() and not errors and uploaded and uploaded[0]['sha1']==sha1(large),errors
        result['checks'].append('large_upload_hash_verified')
        for engine in engines:
            engine.native.hydrate('small.bin')
            assert sha1(engine.root/'small.bin')==wanted
            engine.native.revert('small.bin');engine.close();engine.native.unregister()
        result['checks'].append('both_native_roots_verified_and_unregistered')
        cleanup=OpenListBackend(dict(id=uuid.uuid4().hex,cloud_path=cloud),t);cleanup.bind(run/'cleanup-state');cleanup.connect()
        cleanup.post_identity('rb/delete',{'fid[0]':cleanup.root_id})
        result.update(ok=True,cleaned=True,batch_metrics=dict(coordinator.metrics))
    except Exception as error:
        result['error']=repr(error)
        raise
    finally:
        atomic_write_json(run/'result.json',result,indent=2)
        cancel.set()
        if large_thread:large_thread.join(30)
        for engine in engines:engine.close()
        print(json.dumps(result),flush=True)


if __name__=='__main__':main()
