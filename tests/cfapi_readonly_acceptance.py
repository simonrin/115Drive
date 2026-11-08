import json,os,sys,uuid
from pathlib import Path
w=Path(__file__).resolve().parents[1];sys.path.insert(0,str(w))
from task_engine import TaskEngine
from task_backend import LocalBackend,sha1
from runtime_json import atomic_write_json
run=w/'tests/runs'/('readonly-'+uuid.uuid4().hex[:10]);run.mkdir();local=run/'local';local.mkdir();backend=LocalBackend(run/'remote')
path=local/'readonly.txt';path.write_bytes(b'original readonly content');(backend.root/path.name).write_bytes(path.read_bytes());os.chmod(path,0o444)
task=dict(id=uuid.uuid4().hex,name='readonly',local_path=str(local),cloud_path='/readonly',policy='always_keep',cache_hours=0,overrides={},paused=False)
engine=TaskEngine(task,run/'state',backend);result=dict(ok=False,checks=[],path=str(run));original_id=path.stat().st_ino
try:
    engine.cycle();assert path.stat().st_file_attributes&1 and path.stat().st_ino==original_id
    assert engine.native.state(path)['pin']==1 and engine.native.state(path)['insync']
    result['checks'].append('readonly_adoption_and_pin_preserve_attribute_identity')
    (backend.root/path.name).write_bytes(b'cloud update of readonly content');engine.cycle()
    assert path.read_bytes()==b'cloud update of readonly content' and path.stat().st_file_attributes&1
    result['checks'].append('readonly_cloud_refresh_keeps_attribute')
    task['policy']='online_only';engine.task=task;engine.cycle();assert engine.native.state(path)['partial']
    engine.native.hydrate(path.name);assert path.read_bytes()==b'cloud update of readonly content'
    result['checks'].append('readonly_release_and_hydration')
    engine.native.revert(path.name);assert path.stat().st_file_attributes&1 and path.stat().st_ino==original_id
    result['checks'].append('readonly_revert_preserves_content_and_identity');result['ok']=True
except Exception as error:result['error']=repr(error)
finally:
    engine.close()
    if result['ok']:engine.native.unregister()
    atomic_write_json(run/'result.json',result,indent=2);print(json.dumps(result,ensure_ascii=False),flush=True)
if not result['ok']:raise SystemExit(1)
