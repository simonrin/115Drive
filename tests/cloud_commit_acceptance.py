"""Real 115 identity transactions, process crashes, and independent writer races."""
import ctypes as c,hashlib,json,os,subprocess,sys,threading,time,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import twodrive as t
from task_backend import OpenListBackend,ConcurrentChange,RemoteChanged,sha1
from runtime_json import atomic_write_json

def backend(run):
    task=json.loads((run/'task.json').read_text());b=OpenListBackend(task,t);b.bind(run/'state');return b

def worker(run,point):
    if point=='transfer_started':
        original_flags=t.transfer_flags
        t.transfer_flags=lambda *args,**kwargs:[*original_flags(*args,**kwargs),'--bwlimit','1M']
    b=backend(run);b.operation_id=json.loads((run/'operation.json').read_text())['id']
    expected=json.loads((run/'expected.json').read_text())
    def hook(step):
        if step==point:
            atomic_write_json(run/'ready.json',{'point':point,'child_pid':getattr(b,'transfer_pid',None)})
            while not (run/'release').exists():time.sleep(.05)
    b.fault=hook
    try:
        if point=='before_remote_delete':b.delete('document.bin',expected)
        else:b.upload('document.bin',run/'new.bin',expected,threading.Event())
        atomic_write_json(run/'worker-result.json',{'ok':True})
    except ConcurrentChange as error:atomic_write_json(run/'worker-result.json',{'conflict':True,'error':str(error)})

def main():
    if '--live-115' not in sys.argv:
        raise SystemExit('Pass --live-115 to use a new dedicated cloud test folder.')
    run=t.ROOT/'tests/runs'/('cloud-commit-'+uuid.uuid4().hex[:12]);run.mkdir(parents=True);print(run,flush=True)
    cloud='/.twodrive-commit-test-'+uuid.uuid4().hex
    atomic_write_json(run/'setup.json',{'cloud':cloud})
    t.api('/api/fs/mkdir',{'path':'/115'+cloud},t.login())
    result={'ok':False,'cloud':cloud,'checks':[]}
    points=['remote_staged','remote_archived','remote_promoted','race_staged','race_promote','before_remote_delete','transfer_started']
    try:
        for point in points:
            case=run/point;case.mkdir();t.api('/api/fs/mkdir',{'path':'/115'+cloud+'/'+point},t.login())
            atomic_write_json(case/'task.json',dict(id=uuid.uuid4().hex,cloud_path=cloud+'/'+point))
            atomic_write_json(case/'operation.json',dict(id=uuid.uuid4().hex))
            (case/'old.bin').write_bytes(b'old-'+os.urandom(2063));(case/'new.bin').write_bytes(b'new-'+os.urandom(32*1024**2 if point=='transfer_started' else 2057))
            b=backend(case);old=b.upload('document.bin',case/'old.bin',None,threading.Event())
            atomic_write_json(case/'expected.json',old)
            gate={'race_staged':'remote_staged','race_promote':'before_remote_promote'}.get(point,point)
            process=subprocess.Popen([sys.executable,__file__,'--worker',str(case),gate],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                end=time.monotonic()+180
                while not (case/'ready.json').exists() and process.poll() is None and time.monotonic()<end:time.sleep(.1)
                assert (case/'ready.json').exists(),process.communicate(timeout=1)
                if point in ('remote_staged','remote_archived'):
                    try:b.snapshot()
                    except RemoteChanged:pass
                    else:raise AssertionError('In-flight commit was exposed as a complete listing')
                if point.startswith('race_') or point=='before_remote_delete':
                    # A second process has a separate account lock and journal.
                    other=case/'client-b';other.mkdir()
                    atomic_write_json(other/'task.json',dict(id=uuid.uuid4().hex,cloud_path=cloud+'/'+point))
                    atomic_write_json(other/'operation.json',dict(id=uuid.uuid4().hex))
                    (other/'new.bin').write_bytes(b'independent-client-B-'+os.urandom(2061))
                    atomic_write_json(other/'expected.json',backend(other).recovery_snapshot().get('document.bin'))
                    other_process=subprocess.run([sys.executable,__file__,'--worker',str(other),'never'],capture_output=True,text=True,timeout=180)
                    assert other_process.returncode==0,other_process.stderr
                    assert json.loads((other/'worker-result.json').read_text()).get('ok')
                    (case/'release').touch();out,error=process.communicate(timeout=120)
                    assert process.returncode==0,error
                    assert json.loads((case/'worker-result.json').read_text()).get('conflict')
                    rows=b.transaction_rows();hashes={v.get('sha1') for v in rows.values()}
                    assert sha1(other/'new.bin') in hashes and sha1(case/'old.bin') in hashes
                    if point!='before_remote_delete':assert sha1(case/'new.bin') in hashes
                    assert rows['document.bin']['sha1']==sha1(other/'new.bin')
                else:
                    child=None
                    if point=='transfer_started':
                        pid=json.loads((case/'ready.json').read_text())['child_pid']
                        kernel=c.WinDLL('kernel32',use_last_error=True);kernel.OpenProcess.argtypes=[c.c_uint32,c.c_int,c.c_uint32];kernel.OpenProcess.restype=c.c_void_p
                        kernel.WaitForSingleObject.argtypes=[c.c_void_p,c.c_uint32];kernel.CloseHandle.argtypes=[c.c_void_p]
                        child=kernel.OpenProcess(0x100000,0,pid);assert child
                    process.kill();process.communicate(timeout=10)
                    if child:
                        assert kernel.WaitForSingleObject(child,10000)==0,'Orphaned transfer survived provider death'
                        kernel.CloseHandle(child)
                    b=backend(case);b.operation_id=json.loads((case/'operation.json').read_text())['id']
                    new=b.upload('document.bin',case/'new.bin',old,threading.Event())
                    assert new['sha1']==sha1(case/'new.bin')
                    rows=b.transaction_rows();assert sha1(case/'old.bin') in {v.get('sha1') for v in rows.values()}
                result['checks'].append(point);atomic_write_json(run/'result.json',result,indent=2);print('PASS '+point,flush=True)
            finally:
                if process.poll() is None:process.kill();process.communicate()
        # Only this recorded generated tree is removed, by its verified identity.
        cleanup=OpenListBackend(dict(id=uuid.uuid4().hex,cloud_path=cloud),t);cleanup.connect()
        cleanup.post_identity('rb/delete',{'fid[0]':cleanup.root_id})
        result['cleaned']=True;result['ok']=True
    except Exception as error:result['error']=repr(error);raise
    finally:atomic_write_json(run/'result.json',result,indent=2);print(json.dumps(result),flush=True)

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--worker':worker(Path(sys.argv[2]),sys.argv[3])
    else:main()
