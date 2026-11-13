"""Real CfAPI + SQLite recovery using generated local backends only.

The provider process is terminated at durable upload boundaries. This is not a
physical power-loss or real cloud test. No existing user root is accepted.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_json import atomic_write_json
from sync_state_store import load_state
from task_backend import LocalBackend, sha1
from task_engine import TaskEngine


WORKSPACE = Path(__file__).resolve().parents[1]
RUNS = (WORKSPACE/'tests/runs').resolve()


def owned(run):
    run = Path(run).resolve()
    if RUNS not in run.parents or not any(p.name.startswith('state-store-smoke-') for p in [run, *run.parents]):
        raise ValueError('Only this script generated test roots are permitted')
    return run


class CountedBackend(LocalBackend):
    def __init__(self, run):
        self.run = owned(run)
        super().__init__(self.run/'remote')

    def record(self, kind, path):
        with (self.run/'operations.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(kind=kind, path=path))+'\n')
            stream.flush(); os.fsync(stream.fileno())

    def upload(self, relative, source, expected, cancelled):
        result = super().upload(relative, source, expected, cancelled)
        self.record('upload', relative)
        return result

    def download(self, relative, expected, destination, cancelled):
        super().download(relative, expected, destination, cancelled)
        self.record('download', relative)


def engine(run, fault=None):
    run = owned(run)
    task = json.loads((run/'task.json').read_text(encoding='utf-8'))
    if Path(task['local_path']).resolve() != run/'local': raise ValueError('Test root identity differs')
    return TaskEngine(task, run/'state', CountedBackend(run), fault=fault)


def records(run):
    path = run/'operations.jsonl'
    return [json.loads(row) for row in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def worker(run, point):
    def fault(step):
        if step == point:
            atomic_write_json(run/'kill-ready.json', dict(step=step), durable=True)
            while True: time.sleep(1)
    e = engine(run, fault)
    e.enable_state_store()
    if not e.state_store.guard_legacy: raise RuntimeError('Production JSON guard is not enabled')
    try: e.cycle()
    finally: e.close()
    raise RuntimeError('Fault point was not reached')


def drain(run):
    """Revert only generated test placeholders, keeping test bodies/evidence."""
    run = owned(run)
    e = engine(run)
    try:
        e.start()
        for path in sorted(e.root.rglob('*'), key=lambda p: len(p.parts), reverse=True):
            relative = path.relative_to(e.root).as_posix()
            state = e.native.state(path)
            if state is not None:
                if state['identity'].get('task') != e.task['id']: raise RuntimeError('Foreign test placeholder')
                if state['partial'] and path.is_file(): e.native.hydrate(relative)
                e.native.revert(relative)
        e.close()
        e.native.unregister()
    finally: e.close()


def main():
    directory = RUNS/('state-store-smoke-'+uuid.uuid4().hex[:12])
    directory.mkdir(parents=True)
    result = dict(ok=False, mode='Real CfAPI; generated LocalBackend; TerminateProcess, not physical power loss', checks=[])
    atomic_write_json(directory/'result.json', result, indent=2)
    print(directory, flush=True)
    cases = []
    try:
        for point in ['upload_intent', 'remote_uploaded', 'upload_checkpoint', 'upload_complete']:
            run = directory/point; cases.append(run)
            (run/'local').mkdir(parents=True); (run/'remote').mkdir()
            old_body = b'initial-generated-content-'+os.urandom(32768)
            new_body = b'new-generated-content-'+os.urandom(65536)
            (run/'remote/original.bin').write_bytes(old_body)
            task = dict(id=uuid.uuid4().hex, name='SQLite '+point, local_path=str(run/'local'), cloud_path='/'+point,
                        policy='always_keep', cache_hours=24, overrides={}, paused=False)
            atomic_write_json(run/'task.json', task)
            e = engine(run)
            try: e.cycle(); e.cycle()
            finally: e.close()
            legacy = (run/'state/state.json').read_bytes()
            assert json.loads(legacy)['version'] == 1
            e = engine(run)
            try:
                before = e.state.copy()
                e.enable_state_store()
                assert e.state_store.guard_legacy
                exported = json.loads(e.state_path.read_bytes())
                assert exported.pop('storage_format') == 'sqlite-v1'
                assert exported['version'] == 2
                exported['version'] = 1
                assert exported == before
            finally: e.close()
            guarded = (run/'state/state.json').read_bytes()
            (run/'local/new.bin').write_bytes(new_body)
            process = subprocess.Popen([sys.executable, '-X', 'utf8', __file__, '--worker', str(run), point],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                deadline = time.monotonic()+40
                while process.poll() is None and not (run/'kill-ready.json').exists() and time.monotonic()<deadline:
                    time.sleep(.02)
                assert (run/'kill-ready.json').exists(), 'Upload fault boundary not reached'
                process.kill()
                stdout, stderr = process.communicate(timeout=10)
            finally:
                if process.poll() is None: process.kill(); stdout, stderr = process.communicate(timeout=10)
                else:
                    stdout, stderr = process.communicate(timeout=10)
                (run/'worker.stdout').write_bytes(stdout); (run/'worker.stderr').write_bytes(stderr)
            assert (run/'state/state.json').read_bytes() == guarded, 'Active cycle unexpectedly rewrote compatibility snapshot'
            canonical = load_state(run/'state')
            assert canonical['pending'] is None if point == 'upload_complete' else canonical['pending']['path'] == 'new.bin'
            assert json.loads(guarded)['version'] != 1, 'Old engine could accept stale JSON'
            e = engine(run)
            try:
                e.cycle(); e.cycle()
                assert e.state['pending'] is None
                assert sha1(run/'remote/new.bin') == hashlib.sha1(new_body).hexdigest()
                assert (run/'local/new.bin').read_bytes() == new_body
                assert (run/'local/original.bin').read_bytes() == old_body
                e.cycle(); e.cycle()  # Settle real file-open/access notifications.
                uploaded = [r for r in records(run) if r == dict(kind='upload', path='new.bin')]
                assert len(uploaded) == 1, 'Acknowledged upload repeated'
                before_sql = e.state_store.connection.total_changes
                before_json = e.state_path.read_bytes(), e.state_path.stat().st_mtime_ns
                before_ops = records(run)
                before_state = e.metrics['state_writes']
                for _ in range(3): e.cycle()
                assert e.state_store.connection.total_changes == before_sql, 'Idle database write'
                assert (e.state_path.read_bytes(), e.state_path.stat().st_mtime_ns) == before_json, 'Idle JSON rewrite'
                assert records(run) == before_ops, 'Idle transfer repeated'
                assert e.metrics['state_writes'] == before_state
                assert load_state(run/'state') == e.state
                result['checks'].append(dict(point=point, upload_count=1, idle_database_writes=0,
                                             idle_json_writes=0, original_history_preserved=True))
            finally: e.close()
            drain(run)
            result['checks'][-1]['test_root_unregistered'] = True
            atomic_write_json(directory/'result.json', result, indent=2)
            print('PASS '+point, flush=True)
        result['ok'] = True
    except Exception as error:
        result['error'] = repr(error)
        raise
    finally:
        for run in cases:
            if not any(c['point'] == run.name and c.get('test_root_unregistered') for c in result['checks']):
                try: drain(run)
                except Exception as error: result.setdefault('cleanup_errors', []).append(dict(case=run.name, error=repr(error)))
        atomic_write_json(directory/'result.json', result, indent=2)
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker': worker(owned(sys.argv[2]), sys.argv[3])
    else: main()
