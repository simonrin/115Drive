"""Native CfAPI detach keeps local data, cloud fixture and retained history."""
import base64,hashlib,json,os,shutil,subprocess,sys,uuid,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_engine import TaskEngine
from task_backend import LocalBackend,sha1
from task_removal import detach
from cloud_files import check
from sync_tasks import TaskStore
from runtime_json import atomic_write_json
ROOT=Path(__file__).resolve().parents[1]
run=ROOT/'tests/runs'/('removal-'+uuid.uuid4().hex[:12]);run.mkdir(parents=True);print(run,flush=True)
app=run/'app';app.mkdir();shutil.copy2(ROOT/'register-sync-root.ps1',app/'register-sync-root.ps1')
store=TaskStore(app);result=dict(ok=False,checks=[])
def manifest(folder):return {p.relative_to(folder).as_posix():sha1(p) for p in folder.rglob('*') if p.is_file()}
def remove(task):return store.remove(dict(id=task['id'],revision=store.read()['revision']),lambda task,directory:detach(app,task,directory))
engines=[]
try:
    for number,policy in enumerate(['always_keep','online_only']):
        local=run/('local'+str(number));local.mkdir();remote=run/('remote'+str(number));remote.mkdir()
        (remote/'dir').mkdir();(remote/'dir/cloud.txt').write_bytes(b'cloud data')
        (remote/'dir/cached.txt').write_bytes(b'previously downloaded cloud file')
        (remote/'dir/empty.txt').write_bytes(b'')
        (remote/'dir/readonly-cloud.txt').write_bytes(b'readonly cloud stub')
        (local/'local.txt').write_bytes(b'local data');(local/'readonly.txt').write_bytes(b'read only');os.chmod(local/'readonly.txt',0o444)
        task_id=store.mutate('create',dict(revision=store.read()['revision'],name='test'+str(number),local_path=str(local),cloud_path='/test'+str(number),policy=policy,cache_hours=24))['id']
        task=next(t for t in store.read()['tasks'] if t['id']==task_id)
        engine=TaskEngine(dict(task,paused=False),store.directory(task_id),LocalBackend(remote));engines.append(engine)
        p=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(app/'register-sync-root.ps1'),'-Action','Register','-Root',str(local),'-TaskId',task_id,'-IconPath',str(ROOT/'bin/TwoDriveTray/TwoDriveTray.exe'),'-ContextBase64',base64.b64encode(engine.native.owner).decode()],capture_output=True)
        assert p.returncode==0,p.stderr
        engine.cycle()
        if policy=='online_only':
            engine.native.hydrate('dir/cached.txt')
            os.chmod(local/'dir/readonly-cloud.txt',0o444)
        engine.close();cloud_before=manifest(remote);history=engine.state_path.read_bytes()
        (local/'local.txt').write_bytes(b'unsynced local edit must also be preserved')
        online={p.relative_to(local).as_posix() for p in local.rglob('*') if (engine.native.state(p) or {}).get('partial')}
        if policy=='online_only':
            assert online=={'dir/cloud.txt','dir/readonly-cloud.txt'},online
            assert engine.native.discard_online('dir/cached.txt') is False
            with engine.native.handle(local/'dir/cloud.txt',write=False) as handle:
                check(engine.native.dll.CfSetInSyncState(handle,0,0,None),'test mark dirty')
            try:engine.native.discard_online('dir/cloud.txt');raise AssertionError('Dirty stub discarded')
            except RuntimeError as error:assert '未同步修改' in str(error)
            try:remove(task);raise AssertionError('Dirty root detached')
            except ValueError as error:assert '未同步修改' in str(error)
            assert not store.removing(task_id) and engine.state_path.read_bytes()==history
            with engine.native.handle(local/'dir/cloud.txt',write=False) as handle:
                check(engine.native.dll.CfSetInSyncState(handle,1,0,None),'test restore clean')
            result['checks'].append('exclusive_native_guard_preserves_fully_cached_and_dirty_partial_files')
        local_before={p.relative_to(local).as_posix():sha1(p) for p in local.rglob('*') if p.is_file() and p.relative_to(local).as_posix() not in online}
        ids={p.relative_to(local).as_posix():p.stat().st_ino for p in local.rglob('*') if p.relative_to(local).as_posix() not in online}
        remove(task)
        assert manifest(local)==local_before and manifest(remote)==cloud_before
        assert ids=={p.relative_to(local).as_posix():p.stat().st_ino for p in local.rglob('*')}
        assert (local/'readonly.txt').stat().st_file_attributes&1
        assert not engine.native.registered()
        assert all(engine.native.state(p) is None for p in local.rglob('*'))
        assert engine.state_path.read_bytes()==history
        assert not any(t['id']==task_id for t in store.read()['tasks'])
        result['checks'].append(policy+'_detached_local_hash_ids_readonly_cloud_history_preserved')
        if policy=='online_only':result['checks'].append('online_stubs_removed_with_provider_disconnected_no_download_and_cached_empty_files_retained')
        print('PASS '+result['checks'][-1],flush=True)
    task_id=store.mutate('create',dict(revision=store.read()['revision'],name='never started',local_path=str(run/'never-created'),cloud_path='/never-created',policy='always_keep'))['id']
    task=next(t for t in store.read()['tasks'] if t['id']==task_id)
    remove(task);assert not (run/'never-created').exists()
    result['checks'].append('never_started_missing_shell_entry_is_removed_without_creating_folder')
    with tempfile.TemporaryDirectory(prefix='115Drive-plain-delete-') as temporary:
        ordinary=Path(temporary);sample=ordinary/'keep.txt';sample.write_bytes(b'ordinary local file');identity=sample.stat().st_ino
        task_id=store.mutate('create',dict(revision=store.read()['revision'],name='ordinary folder',local_path=str(ordinary),cloud_path='/ordinary',policy='always_keep'))['id']
        task=next(t for t in store.read()['tasks'] if t['id']==task_id)
        remove(task);assert sample.read_bytes()==b'ordinary local file' and sample.stat().st_ino==identity
    result['checks'].append('never_started_existing_plain_folder_keeps_local_body_and_identity')
    result['ok']=True
except Exception as error:
    result['error']=repr(error);raise
finally:
    for engine in engines:engine.close()
    atomic_write_json(run/'result.json',result,indent=2);print(json.dumps(result),flush=True)
