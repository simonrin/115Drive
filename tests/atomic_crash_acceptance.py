"""Process death immediately before/after durable JSON replacement."""
import json,subprocess,sys,time,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime_json import atomic_write_json
if len(sys.argv)>1:
    import runtime_json
    run=Path(sys.argv[1]);point=sys.argv[2];replace=runtime_json.os.replace
    def gate(source,destination):
        if point=='after':replace(source,destination)
        (run/'ready').touch()
        while True:time.sleep(1)
    runtime_json.os.replace=gate
    atomic_write_json(run/'state.json',dict(version='new',data='N'*4*1024**2),durable=True)
else:
    run=Path(__file__).resolve().parents[1]/'tests/runs'/('atomic-crash-'+uuid.uuid4().hex[:12]);run.mkdir(parents=True)
    result=dict(ok=False,checks=[])
    for point in ('before','after'):
        case=run/point;case.mkdir();atomic_write_json(case/'state.json',dict(version='old',data='O'*4*1024**2),durable=True)
        child=subprocess.Popen([sys.executable,__file__,str(case),point])
        try:
            end=time.monotonic()+10
            while not (case/'ready').exists() and time.monotonic()<end:time.sleep(.02)
            assert (case/'ready').exists();child.kill();child.wait(timeout=5)
            data=json.loads((case/'state.json').read_text());expected='old' if point=='before' else 'new'
            assert data==dict(version=expected,data=expected[0].upper()*4*1024**2)
            result['checks'].append(point+'_replace_retains_complete_'+expected)
        finally:
            if child.poll() is None:child.kill();child.wait()
    result['ok']=True;atomic_write_json(run/'result.json',result,indent=2);print(run);print(json.dumps(result))
