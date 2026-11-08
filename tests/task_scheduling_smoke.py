"""Real Windows task-manager idle monitoring, wakeups and watch fallback."""
import hashlib,json,sys,threading,time,types,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_service import TaskManager
from sync_tasks import TaskStore
from task_backend import LocalBackend
from runtime_json import atomic_write_json


def main():
    run=Path(__file__).resolve().parents[1]/'tests/runs'/('scheduling-'+uuid.uuid4().hex[:12]);run.mkdir(parents=True);print(run,flush=True)
    app=run/'app';app.mkdir();store=TaskStore(app);ids=[]
    for name,policy in [('one','always_keep'),('two','online_only')]:
        remote=run/('remote-'+name);remote.mkdir();(remote/'file.txt').write_bytes(b'initial-'+name.encode())
        task=store.mutate('create',dict(revision=store.read()['revision'],name=name,local_path=str(run/name),cloud_path='/'+name,policy=policy,cache_hours=24))
        ids.append(task['id']);store.mutate('resume',dict(revision=store.read()['revision'],id=task['id']))
    manager=TaskManager(types.SimpleNamespace(ROOT=app),backend_factory=lambda task,host:LocalBackend(run/('remote-'+task['name'])),shell=False)
    service=threading.Thread(target=manager.run,daemon=True);service.start();result=dict(ok=False,checks=[])
    def wait(label,test,timeout=12):
        started=time.monotonic()
        while time.monotonic()-started<timeout:
            if test():result['checks'].append(label);print('PASS '+label,flush=True);return
            time.sleep(.1)
        raise TimeoutError(label)
    engines=[]
    try:
        wait('two_native_workers_started',lambda:len(manager.workers)==2 and all(r.get('engine') and r['engine'].metrics['cycles']>=2 for r in manager.workers.values()),30)
        engines=[manager.workers[task_id]['engine'] for task_id in ids]
        assert manager.batches.metrics['largest_batch'] >= 2, manager.batches.metrics
        result['checks'].append('native_workers_share_check_batch')
        result['batch_metrics'] = dict(manager.batches.metrics)
        time.sleep(3)
        before=[(e.state_path.read_bytes(),e.state_path.stat().st_mtime_ns,dict(e.metrics)) for e in engines]
        time.sleep(20)
        result['idle']=[dict(state_bytes_unchanged=e.state_path.read_bytes()==old[0],state_mtime_unchanged=e.state_path.stat().st_mtime_ns==old[1],
            state_writes=e.metrics['state_writes']-old[2]['state_writes'],inventory_scans=e.metrics['inventory_scans']-old[2]['inventory_scans'],
            remote_scans=e.metrics['remote_scans']-old[2]['remote_scans']) for e,old in zip(engines,before)]
        assert all(r['state_bytes_unchanged'] and r['state_mtime_unchanged'] and r['state_writes']==0 for r in result['idle']),result
        result['checks'].append('idle_state_files_untouched')
        (run/'one/file.txt').write_bytes(b'changed-local')
        wait('local_notification_wakes_before_cloud_deadline',lambda:(run/'remote-one/file.txt').read_bytes()==b'changed-local')
        store.mutate('preferences',dict(revision=store.read()['revision'],id=ids[1],policy='always_keep',cache_hours=24))
        wait('policy_change_wakes_and_hydrates',lambda:engines[1].native.state(run/'two/file.txt')['pin']==1 and not engines[1].native.state(run/'two/file.txt')['partial'])
        # The production monitor handles watch errors with polling and retry.
        class FailedWatch:
            def poll(self):raise OSError('injected watcher failure')
            def close(self):pass
        watch=engines[0].monitor;watch.watcher.close();watch.watcher=FailedWatch()
        wait('watch_error_enters_polling',lambda:watch.metrics['failures']>=1)
        (run/'one/file.txt').write_bytes(b'fallback-change')
        wait('polling_fallback_detects_edit',lambda:(run/'remote-one/file.txt').read_bytes()==b'fallback-change')
        store.mutate('pause',dict(revision=store.read()['revision'],id=ids[0]))
        wait('one_task_pause_leaves_other_running',lambda:(ids[0] not in manager.workers or not manager.workers[ids[0]]['thread'].is_alive()) and manager.workers[ids[1]]['thread'].is_alive())
        result['ok']=True
    except Exception as error:result['error']=repr(error);raise
    finally:
        atomic_write_json(run/'result.json',result,indent=2)  # Keep primary evidence even if cleanup fails.
        manager.stop.set();service.join(3);manager.tick()
        for row in list(manager.workers.values()):row['thread'].join(10)
        for engine in engines:
            engine.native.connect()
            for path in engine.state['items']:
                if engine.native.path(path).exists():
                    assert not engine.native.state(engine.native.path(path))['partial'],'Test cleanup requires downloaded data'
                    engine.native.revert(path)
            engine.native.close();engine.native.unregister()
        result['drained']=True;atomic_write_json(run/'result.json',result,indent=2);print(json.dumps(result),flush=True)


if __name__=='__main__':main()
