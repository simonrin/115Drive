"""Drain only generated failed native-test roots, keeping original failure evidence."""
import ctypes as c
import json
import os
from pathlib import Path
import sys
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud_files import CloudFiles, RootInfo
from runtime_json import atomic_write_json
from task_backend import sha1


def main():
    runs = Path(__file__).resolve().parents[1]/'tests/runs'
    results = []
    for run in runs.iterdir():
        if not run.is_dir() or not run.name.startswith(('cfapi-', 'task-engine-')): continue
        report = run/'result.json'
        if not report.is_file() or json.loads(report.read_text(encoding='utf-8')).get('ok'): continue
        candidates = [run/'local', run/'root', run/'one/local', run/'two/local']
        for root in candidates:
            if not root.is_dir(): continue
            assert root.resolve().is_relative_to(runs.resolve())
            native = CloudFiles(root, '0'*32, lambda *_: None)
            info = RootInfo()
            result = native.dll.CfGetSyncRootInfoByPath(str(root), 1, c.byref(info), c.sizeof(info), None)
            if result < 0 or info.identity_length == 0: continue
            owner = json.loads(bytes(info.identity[:info.identity_length]))
            if owner.get('provider') != 'Twodrive' or Path(owner['root']) != root: continue
            ordinary = []
            for parent, dirs, files in os.walk(run):
                dirs[:] = [d for d in dirs if Path(parent)/d not in candidates]
                for name in files: ordinary.append(Path(parent)/name)
            def fetch(identity, cancelled):
                for path in ordinary:
                    if path.stat().st_size == identity['size'] and sha1(path) == identity['sha1']: return path
                raise RuntimeError('No verified generated source for ' + identity['path'])
            native = CloudFiles(root, owner['task'], fetch)
            row = dict(root=str(root), ok=False)
            try:
                paths = [Path(parent)/name for parent, dirs, files in os.walk(root) for name in dirs+files]
                if any((native.state(path) or {}).get('partial') for path in paths): native.connect()
                for path in sorted(paths, key=lambda p: len(p.parts), reverse=True):
                    state = native.state(path)
                    if not state: continue
                    rel = path.relative_to(root).as_posix()
                    if state['partial']: native.hydrate(rel)
                    native.revert(rel)
                native.close(); native.unregister()
                row['ok'] = True
            except Exception as error: row['error'] = repr(error)
            finally: native.close()
            results.append(row)
    atomic_write_json(runs/('cfapi-cleanup-' + uuid.uuid4().hex[:12] + '.json'), results, indent=2)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    if any(not row['ok'] for row in results): raise SystemExit(1)


if __name__ == '__main__': main()
