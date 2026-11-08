"""Native local contents survive stale batch deletion/update candidates."""
import copy
import json
from pathlib import Path
import sys
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_backend import LocalBackend, RemoteChanged
from task_engine import TaskEngine
from task_scheduling import CheckBatch
from runtime_json import atomic_write_json


def main():
    run=Path(__file__).resolve().parents[1]/'tests/runs'/('batch-safety-'+uuid.uuid4().hex[:12]);run.mkdir(parents=True)
    print(run,flush=True)
    backend=LocalBackend(run/'remote');(backend.root/'file.txt').write_bytes(b'original-local-and-cloud');(backend.root/'empty').mkdir()
    task=dict(id=uuid.uuid4().hex,name='safety',local_path=str(run/'local'),cloud_path='/safety',policy='always_keep',cache_hours=24,overrides={},paused=False)
    engine=TaskEngine(task,run/'state',backend);result=dict(ok=False,checks=[])
    try:
        engine.cycle();engine.cycle()
        good=backend.snapshot();saved=copy.deepcopy(engine.state['items'])
        for number,kind in enumerate(['missing_file','missing_directory','outdated_replacement'],1):
            stale=copy.deepcopy(good)
            if kind=='missing_file':del stale['file.txt']
            elif kind=='missing_directory':del stale['empty']
            else:
                engine.task['policy']='online_only'
                stale['file.txt'].update(id='stale-version',sha1='a'*40,size=7)
            backend.batch_snapshot=lambda batch:copy.deepcopy(stale)
            try:engine.check_batch(CheckBatch(number))
            except RemoteChanged:pass
            else:raise AssertionError('Stale batch was accepted: '+kind)
            assert (engine.root/'file.txt').read_bytes()==b'original-local-and-cloud'
            assert not engine.native.state(engine.root/'file.txt')['partial']
            assert (engine.root/'empty').is_dir()
            assert engine.state['items']['file.txt']['remote']==saved['file.txt']['remote']
            result['checks'].append(kind+'_rechecked_and_preserved')
        result['ok']=True
    except Exception as error:result['error']=repr(error);raise
    finally:
        atomic_write_json(run/'result.json',result,indent=2)
        for path in sorted(engine.state['items'],key=lambda value:value.count('/'),reverse=True):
            if engine.native.path(path).exists():
                assert not engine.native.state(engine.native.path(path))['partial']
                engine.native.revert(path)
        engine.close();engine.native.unregister()
        result['drained']=True;atomic_write_json(run/'result.json',result,indent=2)
        print(json.dumps(result),flush=True)


if __name__=='__main__':main()
