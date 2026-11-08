"""Real NTFS/CfAPI smoke. Only generated files under tests/runs are touched."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud_files import CloudFiles
from runtime_json import atomic_write_json


def main():
    workspace = Path(__file__).resolve().parents[1]
    folder = workspace / 'tests/runs' / ('cfapi-' + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True)
    print(str(folder), flush=True)
    source = folder/'source.bin'
    source.write_bytes(os.urandom(1024*1024+193))
    digest = hashlib.sha1(source.read_bytes()).hexdigest()
    task_id = uuid.uuid4().hex
    events = []
    slow = '--slow-fetch' in sys.argv
    def fetch(identity, cancelled):
        nonlocal slow
        assert identity['sha1'] == digest
        if slow:
            slow = False
            if cancelled.wait(65): raise InterruptedError('Cancelled slow staging')
        return source
    native = CloudFiles(folder/'root', task_id, fetch, event=lambda name, data: events.append(name))
    result = dict(ok=False, root=str(native.root), checks=[], events=events)
    identity = dict(task=task_id, path='中文测试.bin', id='seed', size=source.stat().st_size, sha1=digest)
    try:
        native.connect()
        native.create('中文测试.bin', identity)
        path = native.root/'中文测试.bin'
        initial = native.state(path)
        assert initial['partial'] and initial['on_disk'] == 0, initial
        assert path.stat().st_size == source.stat().st_size
        list(native.root.iterdir())
        assert 'fetch' not in events, events
        result['checks'].append('listing_does_not_hydrate')
        read = subprocess.run([sys.executable, '-c', 'import hashlib,pathlib,sys; print(hashlib.sha1(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())', str(path)], capture_output=True, text=True, timeout=90)
        assert read.returncode == 0, read.stderr
        assert read.stdout.strip() == digest, read.stdout
        assert 'fetch' in events and not native.state(path)['partial']
        result['checks'].append('external_read_hydrates_verified_content')
        if '--slow-fetch' in sys.argv: result['checks'].append('staging_over_60_seconds_keeps_callback_alive')
        native.pin('中文测试.bin', 1)
        assert native.state(path)['pin'] == 1
        try:
            native.dehydrate('中文测试.bin')
        except RuntimeError: pass
        else: raise AssertionError('Pinned file released')
        result['checks'].append('pinned_file_cannot_be_released')
        native.pin('中文测试.bin', 0)
        native.dehydrate('中文测试.bin')
        state = native.state(path)
        assert state['partial'] and state['on_disk'] == 0, state
        assert path.stat().st_size == source.stat().st_size
        assert hashlib.sha1(source.read_bytes()).hexdigest() == digest
        result['checks'].append('release_keeps_logical_size_and_source')
        native.hydrate('中文测试.bin')
        native.pin('中文测试.bin', 1)
        native.close()
        assert hashlib.sha1(path.read_bytes()).hexdigest() == digest
        result['checks'].append('pinned_content_reads_with_provider_stopped')
        native.connect()
        native.revert('中文测试.bin')
        native.close()
        native.unregister()
        assert path.read_bytes() == source.read_bytes()
        result['checks'].append('drain_unregister_preserves_bytes')
        result['ok'] = True
    except Exception as error:
        result['error'] = repr(error)
        raise
    finally:
        native.close()
        atomic_write_json(folder/'result.json', result, indent=2)
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__': main()
