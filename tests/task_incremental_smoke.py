"""Real CfAPI incremental inventory, stale absence and bounded work acceptance."""
import json
from pathlib import Path
import sys
import time
import uuid
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from task_engine import TaskEngine
from task_backend import LocalBackend
from runtime_json import atomic_write_json


def main():
    run = Path(__file__).resolve().parents[1]/'tests/runs'/('incremental-'+uuid.uuid4().hex[:12])
    run.mkdir(parents=True); print(run, flush=True)
    backend = LocalBackend(run/'remote')
    for n in range(24): (backend.root/f'{n:03}.txt').write_text('original '+str(n))
    task = dict(id=uuid.uuid4().hex, name='incremental', local_path=str(run/'local'), cloud_path='/incremental',
                policy='always_keep', cache_hours=24, overrides={}, paused=False)
    engine = TaskEngine(task, run/'state', backend); result = dict(ok=False, checks=[])
    try:
        engine.root.mkdir(); engine.enable_state_store(); engine.start(); engine.enable_watch(); engine.cycle()
        for _ in range(3): engine.cycle()
        before = engine.metrics['inventory_scans']
        reconciled = engine.metrics.get('reconciled_items', 0)
        for _ in range(5): engine.cycle()
        assert engine.metrics['inventory_scans'] == before
        assert engine.metrics['reconciled_items'] == reconciled
        result['checks'].append('unchanged_cycles_skip_full_inventory_and_per_file_reconciliation')

        (engine.root/'000.txt').write_text('local edited')
        with patch.object(engine, 'inventory', side_effect=AssertionError('Unexpected full scan')):
            engine.cycle()
        assert (backend.root/'000.txt').read_text() == 'local edited'
        result['checks'].append('single_file_edit_uploads_without_full_local_scan')

        (engine.root/'001.txt').unlink(); engine.monitor.poll()
        original_snapshot = backend.snapshot
        def restore_before_remote_read():
            (engine.root/'001.txt').write_text('recreated while remote checking')
            return original_snapshot()
        with patch.object(backend, 'snapshot', side_effect=restore_before_remote_read): engine.cycle()
        assert (engine.root/'001.txt').read_text() == 'recreated while remote checking'
        assert (backend.root/'001.txt').read_text() == 'recreated while remote checking'
        result['checks'].append('cached_local_absence_never_deletes_recreated_file')

        engine.cycle(); engine.cycle()
        (engine.root/'002.txt').write_text('retry this edit')
        original_upload = backend.upload
        with patch.object(backend, 'upload', side_effect=OSError('temporary transport failure')):
            try: engine.cycle()
            except OSError: pass
            else: raise AssertionError('Injected failure was not reached')
        engine.cycle()
        assert (backend.root/'002.txt').read_text() == 'retry this edit'
        result['checks'].append('failed_cycle_retries_dirty_file_and_keeps_pending_upload')

        engine.cycle(); engine.cycle()
        for n in range(3, 24): (engine.root/f'{n:03}.txt').unlink()
        deadline = time.monotonic()+3
        while any(f'{n:03}.txt' in engine.monitor.poll() for n in range(3, 24)):
            if time.monotonic() > deadline: raise AssertionError('Delete notifications incomplete')
            time.sleep(.01)
        deleted, trace = [], []
        original_delete = backend.delete
        def deleting(path, expected):
            original_delete(path, expected); deleted.append(path); trace.append(('delete', path))
            if len(deleted) == 2: (engine.root/'urgent.txt').write_text('new user file')
            time.sleep(.02)  # Let the asynchronous Windows notification complete during simulated remote I/O.
        def uploading(path, source, expected, cancelled):
            trace.append(('upload', path)); return original_upload(path, source, expected, cancelled)
        with patch.object(backend, 'delete', side_effect=deleting), patch.object(backend, 'upload', side_effect=uploading):
            engine.cycle()
            assert engine.more_work and len(deleted) < 21, (engine.more_work, len(deleted), engine.monitor.metrics)
            cut = len(trace); engine.cycle()
            assert trace[cut] == ('upload', 'urgent.txt'), trace[cut:cut+3]
        assert (backend.root/'urgent.txt').read_text() == 'new user file'
        assert len(deleted) == 21
        result['checks'].append('new_file_preempts_same_task_bulk_delete_at_safe_boundary')
        engine.cycle(); engine.cycle()
        result['state_store'] = dict(engine.state_store.metrics)
        state = engine.state_path.read_bytes(); stamp = engine.state_path.stat().st_mtime_ns
        for _ in range(4): engine.cycle()
        assert (engine.state_path.read_bytes(), engine.state_path.stat().st_mtime_ns) == (state, stamp)
        result['checks'].append('unchanged_history_zero_writes')
        result['ok'] = True
    except BaseException as error:
        result['error'] = repr(error); raise
    finally:
        atomic_write_json(run/'result.json', result, indent=2)
        for path in sorted(engine.inventory(), key=lambda p: p.count('/'), reverse=True):
            state = engine.native.state(engine.native.path(path))
            if state:
                if state['partial']: engine.native.hydrate(path)
                engine.native.revert(path)
        engine.close(); engine.native.unregister()
        result['drained'] = True; atomic_write_json(run/'result.json', result, indent=2)
        print(json.dumps(result), flush=True)


if __name__ == '__main__': main()
