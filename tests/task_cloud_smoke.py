"""Opt-in 115 integration: generated sibling roots only; never uses live bisync."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import twodrive as t
from task_backend import OpenListBackend, sha1
from task_engine import TaskEngine
from runtime_json import atomic_write_json


def main():
    if '--live-115' not in sys.argv: raise SystemExit('Pass --live-115 to use a new dedicated cloud test folder.')
    run_id = uuid.uuid4().hex
    directory = t.ROOT/'tests/runs'/('task-cloud-' + run_id[:12])
    directory.mkdir(parents=True)
    cloud = '/.twodrive-cfapi-test-' + run_id
    result = dict(ok=False, cloud_root=cloud, checks=[])
    atomic_write_json(directory/'result.json', result, indent=2)
    print(directory, flush=True)
    engines = []
    try:
        token = t.login()
        t.api('/api/fs/mkdir', dict(path='/115' + cloud), token)
        for name, policy in [('one','always_keep'), ('two','online_only')]:
            t.api('/api/fs/mkdir', dict(path='/115' + cloud + '/' + name), token)
            root = directory/name/'local'; root.mkdir(parents=True)
            source = directory/name/'seed.bin'; source.write_bytes(os.urandom(262157))
            (root/'local.txt').write_text('115 原始本地 ' + run_id, encoding='utf-8')
            task = dict(id=uuid.uuid4().hex, name=name, cloud_path=cloud+'/'+name, local_path=str(root),
                        policy=policy, cache_hours=0, overrides={}, paused=False)
            backend = OpenListBackend(task, t)
            backend.bind(directory/name/'state')
            backend.upload('cloud.bin', source, None, threading.Event())
            engine = TaskEngine(task, directory/name/'state', backend); engines.append(engine)
            engine.cycle()
            engine.cycle()
            for path in ('cloud.bin','local.txt'):
                info = engine.native.state(root/path)
                assert info['partial'] == (policy == 'online_only'), info
                assert info['pin'] == (1 if policy == 'always_keep' else 0), info
            result['checks'].append(name + '_original_local_and_remote_policy')
            atomic_write_json(directory/'result.json', result, indent=2)
            print('PASS ' + result['checks'][-1], flush=True)
        one, two = engines
        read = subprocess.run([sys.executable,'-c',
            'import hashlib,pathlib,sys;print(hashlib.sha1(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())',
            str(two.root/'cloud.bin')], capture_output=True, text=True, timeout=90)
        assert read.returncode == 0, read.stderr
        assert read.stdout.strip() == sha1(directory/'two/seed.bin')
        result['checks'].append('115_native_callback_download_hash_matches')
        two.cycle()
        assert two.native.state(two.root/'cloud.bin')['partial']
        result['checks'].append('verified_115_file_releases_cached_content')
        # Drain before unregistering; every test file remains an ordinary local
        # copy even after the generated cloud directory is removed.
        for engine in engines:
            paths = sorted(engine.state['items'], key=lambda p:p.count('/'), reverse=True)
            for path in paths:
                if engine.state['items'][path]['remote']['kind'] == 'file': engine.native.hydrate(path)
                engine.native.revert(path)
            engine.close(); engine.native.unregister()
        cleanup=OpenListBackend(dict(id=uuid.uuid4().hex,cloud_path=cloud),t)
        cleanup.connect();cleanup.post_identity('rb/delete',{'fid[0]':cleanup.root_id})
        result['checks'].append('generated_cloud_and_native_roots_removed_local_copies_preserved')
        result['ok'] = True
    except Exception as error:
        result['error'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        for engine in engines: engine.close()
        atomic_write_json(directory/'result.json', result, indent=2)
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__': main()
