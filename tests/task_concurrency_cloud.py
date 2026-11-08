"""Opt-in real 115: a limited 32 MiB upload must not block another root's read."""
import hashlib,json,os,subprocess,sys,threading,time,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import twodrive as t
from task_backend import OpenListBackend,sha1
from task_engine import TaskEngine
from runtime_json import atomic_write_json


def main():
    if '--live-115' not in sys.argv:raise SystemExit('Pass --live-115 to use generated cloud test folders.')
    run=t.ROOT/'tests/runs'/('scheduling-cloud-'+uuid.uuid4().hex[:12]);run.mkdir(parents=True);print(run,flush=True)
    cloud='/.twodrive-scheduling-'+uuid.uuid4().hex
    atomic_write_json(run/'setup.json',dict(cloud=cloud))
    token=t.login()
    for path in (cloud,cloud+'/upload',cloud+'/read'):t.api('/api/fs/mkdir',dict(path='/115'+path),token)
    class LimitedHost:
        def __getattr__(self,name):return getattr(t,name)
        def transfer_flags(self):return [*t.transfer_flags(),'--bwlimit','512K']
    upload=OpenListBackend(dict(id=uuid.uuid4().hex,cloud_path=cloud+'/upload'),LimitedHost());upload.bind(run/'upload-state')
    local=run/'local';local.mkdir()
    task=dict(id=uuid.uuid4().hex,name='read',local_path=str(local),cloud_path=cloud+'/read',policy='online_only',cache_hours=24,overrides={},paused=False)
    reader=OpenListBackend(task,t);reader.bind(run/'read-state')
    small=run/'small.bin';small.write_bytes(os.urandom(262157));wanted=sha1(small)
    result=dict(ok=False,cloud=cloud,checks=[],seconds={});atomic_write_json(run/'result.json',result,indent=2)
    engine=TaskEngine(task,run/'read-state',reader);thread=None;cancel=threading.Event();errors=[];uploaded=[]
    try:
        reader.upload('small.bin',small,None,cancel);engine.cycle()
        assert engine.native.state(local/'small.bin')['partial']
        large=run/'large.bin';large.write_bytes(os.urandom(32*1024**2+17))
        entered=threading.Event()
        upload.fault=lambda point:entered.set() if point=='transfer_started' else None
        def write():
            try:uploaded.append(upload.upload('large.bin',large,None,cancel))
            except Exception as error:errors.append(repr(error))
        thread=threading.Thread(target=write);thread.start()
        assert entered.wait(90),'Large transfer did not start'
        start=time.monotonic();rows=reader.snapshot();result['seconds']['scan_during_upload']=round(time.monotonic()-start,3)
        assert rows['small.bin']['sha1']==wanted and thread.is_alive() and not errors
        result['checks'].append('other_task_scans_while_large_upload_active');print('PASS concurrent scan',flush=True)
        start=time.monotonic()
        read=subprocess.run([sys.executable,'-c','import pathlib,hashlib,sys;print(hashlib.sha1(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())',str(local/'small.bin')],capture_output=True,text=True,timeout=90)
        result['seconds']['native_download_during_upload']=round(time.monotonic()-start,3)
        assert read.returncode==0 and read.stdout.strip()==wanted,(read.stdout,read.stderr)
        assert thread.is_alive() and not errors,'Read waited for the full large upload'
        result['checks'].append('other_task_native_read_completes_before_large_upload');print('PASS concurrent native read',flush=True)
        thread.join(180);assert not thread.is_alive() and not errors,errors
        assert uploaded and uploaded[0]['sha1']==sha1(large)
        result['checks'].append('large_upload_committed_and_hash_verified')
        assert (local/'small.bin').read_bytes()==small.read_bytes()
        engine.native.revert('small.bin');engine.close();engine.native.unregister()
        cleanup=OpenListBackend(dict(id=uuid.uuid4().hex,cloud_path=cloud),t);cleanup.bind(run/'cleanup-state');cleanup.connect()
        cleanup.post_identity('rb/delete',{'fid[0]':cleanup.root_id})
        result.update(ok=True,cleaned=True,read_metrics=reader.metrics,upload_metrics=upload.metrics)
    except Exception as error:result['error']=repr(error);raise
    finally:
        cancel.set()
        if thread:thread.join(30)
        engine.close();atomic_write_json(run/'result.json',result,indent=2);print(json.dumps(result),flush=True)


if __name__=='__main__':main()
