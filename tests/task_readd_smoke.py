"""Re-adding a detached folder reconciles equal files without retransmission.

The old task archive is preserved, not automatically resumed. All cloud data
in this test belongs to an isolated LocalBackend, never the logged-in account.
"""
import hashlib, json, sys, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_json import atomic_write_json
from sync_tasks import TaskStore
from task_backend import LocalBackend
from task_engine import TaskEngine
from task_removal import detach


class CountingBackend(LocalBackend):
    def __init__(self, root):
        super().__init__(root)
        self.downloads, self.uploads = [], []
    def download(self, relative, expected, destination, cancelled):
        self.downloads.append(relative)
        return super().download(relative, expected, destination, cancelled)
    def upload(self, relative, source, expected, cancelled):
        if not relative.startswith('.twodrive-health-'): self.uploads.append(relative)
        return super().upload(relative, source, expected, cancelled)


def main():
    run = Path(__file__).resolve().parents[1]/'tests/runs'/('readd-'+uuid.uuid4().hex[:12])
    run.mkdir(parents=True); print(run, flush=True)
    app, local, remote = run/'app', run/'local', run/'remote'
    local.mkdir(); remote.mkdir()
    (remote/'online.bin').write_bytes(b'cloud only')
    (remote/'cached.bin').write_bytes(b'cached body')
    (local/'original.bin').write_bytes(b'local body')
    store = TaskStore(app); result = dict(ok=False, checks=[]); history = {}; engines = []
    try:
        for number, policy in enumerate(['online_only', 'online_only', 'always_keep']):
            created = store.mutate('create', dict(revision=store.read()['revision'], name='readd',
                local_path=str(local), cloud_path='/readd', policy=policy, cache_hours=24))
            task = store.read()['tasks'][0]
            backend = CountingBackend(remote)
            engine = TaskEngine(dict(task, paused=False), store.directory(task['id']), backend)
            engines.append(engine); engine.cycle()
            if number == 0:
                engine.native.hydrate('cached.bin')
            else:
                assert not backend.uploads, backend.uploads
                assert backend.downloads == ([] if policy == 'online_only' else ['online.bin']), backend.downloads
                assert (local/'original.bin').read_bytes() == b'local body'
                assert (local/'cached.bin').read_bytes() == b'cached body'
                assert task['id'] not in history
                for old_id, digest in history.items():
                    assert hashlib.sha256((store.directory(old_id)/'state.json').read_bytes()).hexdigest() == digest
                result['checks'].append(policy+'_readd_no_upload_or_redundant_download_old_history_untouched')
            engine.close()
            history[task['id']] = hashlib.sha256(engine.state_path.read_bytes()).hexdigest()
            store.remove(dict(id=task['id'], revision=store.read()['revision']),
                lambda task, directory: detach(app, task, directory, unregister_shell=lambda *args: None))
            assert not engine.native.registered()
        result['ok'] = True
    except Exception as error:
        result['error'] = repr(error); raise
    finally:
        for engine in engines: engine.close()
        atomic_write_json(run/'result.json', result, indent=2)
        print(json.dumps(result), flush=True)


if __name__ == '__main__': main()
