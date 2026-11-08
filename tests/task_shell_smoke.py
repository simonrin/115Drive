"""Localized Explorer actions keep native hydration and safe release behavior."""
import json,sys,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sync_tasks import TaskStore
from task_backend import LocalBackend
from task_engine import TaskEngine
from task_shell import apply_policy
from task_removal import detach
from runtime_json import atomic_write_json

run=Path(__file__).resolve().parents[1]/'tests/runs'/('shell-policy-'+uuid.uuid4().hex[:10]);run.mkdir(parents=True)
store=TaskStore(run/'app');local=run/'local';backend=LocalBackend(run/'remote')
(backend.root/'folder').mkdir();(backend.root/'folder/a.txt').write_bytes(b'cloud a');(backend.root/'file.txt').write_bytes(b'cloud b')
created=store.mutate('create',dict(revision=0,name='shell',local_path=str(local),cloud_path='/shell',policy='online_only',cache_hours=24))
task=store.read()['tasks'][0];engine=TaskEngine(dict(task,paused=False),store.directory(task['id']),backend)
result=dict(ok=False,checks=[])
def action(path,policy):
    apply_policy(store,dict(path=str(local/path),policy=policy))
    engine.task=dict(store.read()['tasks'][0],paused=False);engine.cycle()
try:
    engine.cycle()
    action('','always_keep')
    assert (local/'file.txt').read_bytes()==b'cloud b' and (local/'folder/a.txt').read_bytes()==b'cloud a'
    result['checks'].append('root_keep_recursively_hydrates')
    action('file.txt','online_only')
    assert engine.native.state(local/'file.txt')['partial']
    assert not engine.native.state(local/'folder/a.txt')['partial']
    result['checks'].append('single_file_release_is_immediate_with_24_hour_cache')
    action('file.txt','always_keep')
    with (local/'file.txt').open('r+b') as stream:stream.write(b'local b')
    action('file.txt','online_only')
    assert (backend.root/'file.txt').read_bytes()==b'local b' and engine.native.state(local/'file.txt')['partial']
    result['checks'].append('dirty_local_file_uploaded_before_release')
    action('folder','online_only');assert engine.native.state(local/'folder/a.txt')['partial']
    action('folder','always_keep');assert (local/'folder/a.txt').read_bytes()==b'cloud a'
    result['checks'].append('folder_recursive_release_and_keep')
    action('','online_only');assert engine.native.state(local/'folder/a.txt')['partial']
    result['checks'].append('root_release_overrides_existing_child_pins')
    engine.close();history=engine.state_path.read_bytes()
    apply_policy(store,dict(path=str(local/'file.txt'),policy='always_keep'))
    assert engine.state_path.read_bytes()==history and engine.native.state(local/'file.txt')['partial']
    result['checks'].append('paused_task_saves_selection_without_transferring')
    store.remove(dict(id=task['id'],revision=store.read()['revision']),lambda task,directory:detach(store.root,task,directory,unregister_shell=lambda *args:None))
    result['ok']=True
except Exception as error:result['error']=repr(error);raise
finally:engine.close();atomic_write_json(run/'result.json',result,indent=2);print(run);print(json.dumps(result),flush=True)
