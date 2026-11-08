"""Real provider restart after the old cloud version is archived, before commit."""
import json,os,subprocess,sys,time,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import twodrive as t
from task_engine import TaskEngine
from task_backend import OpenListBackend,sha1
from runtime_json import atomic_write_json

def make(run,fault=None):
    task=json.loads((run/'task.json').read_text());b=OpenListBackend(task,t);b.fault=fault
    return TaskEngine(task,run/'state',b)

if len(sys.argv)==3 and sys.argv[1]=='--worker':
    run=Path(sys.argv[2])
    def halt(step):
        if step=='remote_archived':
            (run/'ready').touch()
            while True:time.sleep(1)
    e=make(run,halt);e.cycle();raise RuntimeError('Crash gate not reached')

if '--live-115' not in sys.argv:
    raise SystemExit('Pass --live-115 to use a new dedicated cloud test folder.')

run=t.ROOT/'tests/runs'/('cloud-engine-recovery-'+uuid.uuid4().hex[:12]);run.mkdir(parents=True);print(run,flush=True)
cloud='/.twodrive-engine-recovery-'+uuid.uuid4().hex;root=run/'local';root.mkdir()
task=dict(id=uuid.uuid4().hex,name='recovery',cloud_path=cloud,local_path=str(root),policy='online_only',cache_hours=0,overrides={},paused=False)
atomic_write_json(run/'task.json',task);(root/'file.txt').write_bytes(b'old-content-'+os.urandom(2048))
t.api('/api/fs/mkdir',dict(path='/115'+cloud),t.login())
result=dict(ok=False,cloud=cloud)
e=make(run)
try:
    e.cycle();old=e.state['items']['file.txt']['remote']['sha1'];e.native.hydrate('file.txt');e.close()
    (root/'file.txt').write_bytes(b'new-content-'+os.urandom(2048));wanted=sha1(root/'file.txt')
    child=subprocess.Popen([sys.executable,__file__,'--worker',str(run)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        end=time.monotonic()+180
        while not (run/'ready').exists() and child.poll() is None and time.monotonic()<end:time.sleep(.1)
        assert (run/'ready').exists(),child.communicate(timeout=1)
        child.kill();child.communicate(timeout=10)
    finally:
        if child.poll() is None:child.kill();child.communicate()
    e=make(run);e.cycle();e.cycle()
    assert e.state['pending'] is None
    assert e.native.state(root/'file.txt')['partial']
    rows=e.backend.transaction_rows();assert rows['file.txt']['sha1']==wanted and old in {row.get('sha1') for row in rows.values()}
    e.native.hydrate('file.txt');assert sha1(root/'file.txt')==wanted
    e.native.revert('file.txt');e.close();e.native.unregister()
    e.backend.connect();e.backend.post_identity('rb/delete',{'fid[0]':e.backend.root_id})
    result.update(ok=True,checks=['restart_crosses_commit_fence','pending_transaction_committed','both_cloud_versions_preserved','verified_file_released','readback_hash','cleaned'])
except Exception as error:result['error']=repr(error);raise
finally:e.close();atomic_write_json(run/'result.json',result,indent=2);print(json.dumps(result),flush=True)
