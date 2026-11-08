"""1 GiB+17 bytes through live 115 upload, release, CfAPI download and offline read."""
import hashlib,json,os,subprocess,sys,time,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import twodrive as t
from task_backend import OpenListBackend
from task_engine import TaskEngine
from runtime_json import atomic_write_json

if '--live-115' not in sys.argv:
    raise SystemExit('Pass --live-115 to use a new dedicated cloud test folder.')

run=t.ROOT/'tests/runs'/('cloud-large-'+uuid.uuid4().hex[:12]);run.mkdir(parents=True);print(run,flush=True)
cloud='/.twodrive-large-test-'+uuid.uuid4().hex
root=run/'local';root.mkdir();size=1024**3+17;digest=hashlib.sha1();block=os.urandom(8*1024**2)
with (root/'large.bin').open('wb') as stream:
    for offset in range(0,size,len(block)):
        data=block[:min(len(block),size-offset)];stream.write(data);digest.update(data)
    stream.flush();os.fsync(stream.fileno())
wanted=digest.hexdigest()
task=dict(id=uuid.uuid4().hex,name='large-acceptance',local_path=str(root),cloud_path=cloud,
          policy='always_keep',cache_hours=0,overrides={},paused=False)
atomic_write_json(run/'task.json',task)
result=dict(ok=False,cloud=cloud,size=size,sha1=wanted,checks=[],seconds={})
atomic_write_json(run/'result.json',result,indent=2)
t.api('/api/fs/mkdir',dict(path='/115'+cloud),t.login())
backend=OpenListBackend(task,t);engine=TaskEngine(task,run/'state',backend)
try:
    start=time.monotonic();engine.cycle();result['seconds']['upload']=round(time.monotonic()-start,3)
    assert backend.snapshot()['large.bin']['sha1']==wanted
    assert engine.native.state(root/'large.bin')['pin']==1
    result['checks'].append('1_GiB_upload_verified');print('PASS upload',flush=True)
    atomic_write_json(run/'result.json',result,indent=2)
    engine.task['policy']='online_only';engine.cycle()
    state=engine.native.state(root/'large.bin');assert state['partial'] and state['on_disk']==0
    result['checks'].append('release_keeps_64_bit_logical_size');assert (root/'large.bin').stat().st_size==size
    start=time.monotonic()
    read=subprocess.run([sys.executable,'-c','import hashlib,sys;f=open(sys.argv[1],"rb");print(hashlib.file_digest(f,"sha1").hexdigest())',str(root/'large.bin')],capture_output=True,text=True,timeout=1800)
    assert read.returncode==0,read.stderr
    assert read.stdout.strip()==wanted
    result['seconds']['download']=round(time.monotonic()-start,3);result['checks'].append('1_GiB_native_download_full_hash');print('PASS download',flush=True)
    engine.native.pin('large.bin',1);engine.close()
    with (root/'large.bin').open('rb') as stream:assert hashlib.file_digest(stream,'sha1').hexdigest()==wanted
    result['checks'].append('offline_full_hash')
    engine.native.connect();engine.native.revert('large.bin');engine.native.close();engine.native.unregister()
    backend.connect();backend.post_identity('rb/delete',{'fid[0]':backend.root_id})
    result['checks'].append('generated_cloud_root_removed_local_copy_retained');result['ok']=True
except Exception as error:result['error']=repr(error);raise
finally:engine.close();atomic_write_json(run/'result.json',result,indent=2);print(json.dumps(result),flush=True)
