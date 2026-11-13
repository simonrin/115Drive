"""Kill real provider processes at durable boundaries; never shuts down Windows."""
import hashlib,json,os,subprocess,sys,time,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_engine import TaskEngine
from task_backend import LocalBackend,sha1
from runtime_json import atomic_write_json

def engine(run, fault=None):
    task=json.loads((run/'task.json').read_text(encoding='utf-8'))
    value = TaskEngine(task,run/'state',LocalBackend(run/'remote'),fault=fault)
    value.enable_state_store()
    return value

def worker(run,point):
    def stop(step):
        if step==point:
            (run/'kill-ready').write_text(step)
            while True:time.sleep(1)
    e=engine(run,stop)
    try:e.cycle()
    finally:e.close()
    raise RuntimeError('Fault point not reached')

def main():
    directory=Path(__file__).resolve().parents[1]/'tests/runs'/('crash-acceptance-'+uuid.uuid4().hex[:12])
    directory.mkdir(parents=True);print(directory,flush=True)
    result=dict(ok=False,mode='TerminateProcess, not physical power loss',checks=[])
    points=['upload_intent','remote_uploaded','local_adopted','upload_checkpoint','upload_complete',
            'placeholder_created','remote_refreshed','local_deleted','remote_renamed','conflict_renamed','dehydrated']
    try:
        for point in points:
            run=directory/point;run.mkdir();(run/'local').mkdir();(run/'remote').mkdir()
            initial=b'original-'+os.urandom(1024*1024+71);changed=b'new-'+os.urandom(1024*1024+37)
            (run/'remote/base.bin').write_bytes(initial)
            task=dict(id=uuid.uuid4().hex,name=point,local_path=str(run/'local'),cloud_path='/'+point,
                      policy='always_keep',cache_hours=0,overrides={},paused=False)
            atomic_write_json(run/'task.json',task)
            e=engine(run);e.cycle();e.close()
            if point in points[:5]: (run/'local/new.bin').write_bytes(changed)
            elif point=='placeholder_created': (run/'remote/new.bin').write_bytes(changed)
            elif point=='remote_refreshed': (run/'remote/base.bin').write_bytes(changed)
            elif point=='local_deleted': (run/'remote/base.bin').unlink()
            elif point=='remote_renamed': (run/'local/base.bin').rename(run/'local/renamed.bin')
            elif point=='conflict_renamed':
                (run/'local/base.bin').write_bytes(changed);(run/'remote/base.bin').write_bytes(b'cloud-conflict')
            elif point=='dehydrated':
                task['policy']='online_only';atomic_write_json(run/'task.json',task)
            process=subprocess.Popen([sys.executable,__file__,'--worker',str(run),point],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                end=time.monotonic()+40
                while not (run/'kill-ready').exists() and process.poll() is None and time.monotonic()<end:time.sleep(.02)
                assert (run/'kill-ready').exists(),process.communicate(timeout=1)
                process.kill();process.communicate(timeout=10)
            finally:
                if process.poll() is None:process.kill();process.communicate()
            e=engine(run)
            try:
                e.cycle();e.cycle()
                assert e.state['pending'] is None
                if point=='local_deleted':assert not (e.root/'base.bin').exists()
                elif point=='remote_renamed':assert (run/'remote/renamed.bin').read_bytes()==initial and not (e.root/'base.bin').exists()
                elif point=='conflict_renamed':
                    payloads=[p.read_bytes() for p in e.root.iterdir() if p.is_file()]
                    assert changed in payloads and b'cloud-conflict' in payloads
                elif point=='dehydrated':assert e.native.state(e.root/'base.bin')['partial']
                else:
                    name='base.bin' if point=='remote_refreshed' else 'new.bin'
                    assert (run/'remote'/name).read_bytes()==changed
                    assert (e.root/name).read_bytes()==changed
                for name in sorted(e.state['items'],key=lambda p:p.count('/'),reverse=True):
                    e.native.hydrate(name);e.native.revert(name)
                e.close();e.native.unregister()
            finally:e.close()
            result['checks'].append(point);atomic_write_json(directory/'result.json',result,indent=2)
            print('PASS '+point,flush=True)
        result['ok']=True
    except Exception as error:result['error']=repr(error);raise
    finally:atomic_write_json(directory/'result.json',result,indent=2)

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--worker':worker(Path(sys.argv[2]),sys.argv[3])
    else:main()
