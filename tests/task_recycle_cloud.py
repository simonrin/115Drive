"""Opt-in recycle-policy acceptance in one generated cloud/native fixture."""
import argparse
from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-115', action='store_true')
    parser.add_argument('--runtime-root', type=Path)
    args = parser.parse_args()
    if not args.live_115:
        print(json.dumps(dict(cloud_access=False, policy='recycle',
            checks=['native_new_overwrite_bidirectional_delete', 'pause_and_restart',
                    'lost_recycle_reply', 'download_sha1', 'no_cloud_history_root']))); return
    if args.runtime_root is None: parser.error('--runtime-root is required')
    workspace = Path(__file__).resolve().parents[1]
    os.environ['TWODRIVE_HOME'] = str(args.runtime_root.resolve(strict=True))
    sys.path.insert(0, str(workspace))
    import twodrive as real
    from cloud_history import CloudHistory
    from runtime_json import atomic_write_json
    from task_backend import OpenListBackend, version
    from task_engine import TaskEngine
    assert real.read_json(real.ROOT/'runtime/tasks-status.json')['state'] == 'paused'
    nonce = uuid.uuid4().hex
    run = workspace/'tests/runs'/('recycle-'+nonce); run.mkdir(parents=True)
    private = run/'private'; private.mkdir()
    config = private/'config.json'
    atomic_write_json(config, dict(cloud_history_mode='recycle'))
    cloud = '/.twodrive-recycle-'+nonce
    submitted = Counter()
    began = time.monotonic()
    result = dict(ok=False, cloud_root=cloud, checks=[], cloud_cleaned=False, native_unregistered=False,
        source_sha256={name:hashlib.sha256((workspace/name).read_bytes()).hexdigest()
                       for name in ('cloud_history.py', 'task_backend.py')})
    engine = None
    cancelled = threading.Event()

    class Host:
        PRIVATE = private
        CONFIG = config
        def __getattr__(self, name): return getattr(real, name)
        def transfer_flags(self):
            flags = real.transfer_flags()
            flags[flags.index('--cache-dir')+1] = str(private/'rclone-cache')
            return flags
    host = Host()

    class Backend(OpenListBackend):
        def transfer(self, source, destination, event, *, kind='upload'):
            delegate = copy.copy(self)
            class TransferHost:
                PRIVATE = real.PRIVATE
                def __getattr__(self, name): return getattr(host, name)
            delegate.t = TransferHost()
            return OpenListBackend.transfer(delegate, source, destination, event, kind=kind)
        def post_identity(self, endpoint, fields):
            submitted[(endpoint, str(fields.get('fid[0]', fields.get('fid', ''))))] += 1
            super().post_identity(endpoint, fields)
            hook = getattr(self, 'after_post', None)
            if hook: hook(endpoint, fields)

    def backend(path, identity=None):
        item = Backend(dict(id=identity or uuid.uuid4().hex, cloud_path=path), host)
        item.connect(); return item
    def save():
        result['seconds'] = round(time.monotonic()-began, 3)
        atomic_write_json(run/'result.json', result, indent=2, durable=True)
    def passed(name):
        result['checks'].append(name); save(); print('PASS '+name+' elapsed='+str(result['seconds']), flush=True)
    def source(name, body):
        path = run/name; path.write_bytes(body); return path
    def rows(item): return item.snapshot(include_internal=True, transport=False)
    def no_history(item):
        assert not CloudHistory(item).registry_path.exists()
        assert not any(name.rsplit('/', 1)[-1].startswith('_twodrive-') for name in rows(item))
    def drain():
        if engine is None: return
        assert engine.root.resolve().is_relative_to(run.resolve())
        if engine.native.registered():
            for path in sorted(engine.root.rglob('*'), key=lambda p:len(p.parts), reverse=True):
                info = engine.native.state(path)
                if info:
                    relative = path.relative_to(engine.root).as_posix()
                    if info.get('partial'): assert engine.native.discard_online(relative)
                    else: engine.native.revert(relative)
            engine.close(); engine.native.unregister()
        else: engine.close()
        assert not engine.native.registered()
        result['native_unregistered'] = True

    print(run, flush=True); save()
    try:
        host.api('/api/fs/mkdir', dict(path='/115'+cloud), host.login())
        root = backend(cloud)
        owner_name = 'fixture-owner-'+nonce+'.txt'
        owner = source('owner.txt', ('115Drive recycle fixture '+nonce).encode())
        owner_row = root.upload(owner_name, owner, None, cancelled)
        result['owner'] = dict(root_id=root.root_id, name=owner_name, identity=owner_row); save()
        host.api('/api/fs/mkdir', dict(path='/115'+cloud+'/native'), host.login())
        native = backend(cloud+'/native')
        cloud_source = source('remote.txt', b'cloud body '+nonce.encode())
        remote_row = native.upload('remote.txt', cloud_source, None, cancelled)
        local = run/'local'; local.mkdir()
        (local/'local.txt').write_bytes(b'first local '+nonce.encode())
        task = dict(id=native.task['id'], name='Recycle fixture', cloud_path=native.task['cloud_path'],
            local_path=str(local), policy='always_keep', cache_hours=24, overrides={}, paused=False)
        engine = TaskEngine(task, private/'tasks'/task['id'], native); engine.enable_state_store()
        engine.cycle()
        assert (local/'remote.txt').read_bytes() == cloud_source.read_bytes()
        first = rows(native)['local.txt']
        passed('native_new_upload_and_download_sha1')
        replacement = b'replaced local '+nonce.encode()
        (local/'local.txt').write_bytes(replacement); engine.cycle()
        updated = rows(native)['local.txt']
        assert updated['id'] != first['id'] and updated['sha1'] == hashlib.sha1(replacement).hexdigest()
        assert not any(row['id'] == first['id'] for row in rows(native).values())
        no_history(native)
        passed('overwrite_recycles_old_version_after_commit_without_history_root')
        native.post_identity('rb/delete', {'fid[0]':remote_row['id']}); engine.cycle()
        assert not (local/'remote.txt').exists()
        (local/'local.txt').unlink(); engine.cycle()
        assert 'local.txt' not in rows(native)
        assert 'local.txt' not in engine.state['items']
        no_history(native)
        passed('native_bidirectional_delete_uses_recycle_bin')
        drain()

        original = root.upload('interrupt.txt', source('old.txt', b'old body '+nonce.encode()), None, cancelled)
        new_source = source('new.txt', b'new body '+nonce.encode())
        operation = uuid.uuid4().hex; root.operation_id = operation
        paused = threading.Event()
        root.fault = lambda step:paused.set() if step == 'remote_staged' else None
        try: root.upload('interrupt.txt', new_source, original, paused)
        except InterruptedError: pass
        else: raise AssertionError('Pause did not interrupt staged upload')
        tx = json.loads((root.journal_dir/(operation+'.json')).read_bytes())
        staged_id = tx['uploaded']['id']
        assert version(rows(root)['interrupt.txt']) == version(original)
        assert submitted[('rb/delete', original['id'])] == 0
        passed('pause_keeps_original_and_durable_staged_input')
        resumed = backend(cloud, root.task['id']); resumed.operation_id = operation
        def crash(step):
            if step == 'remote_archived': raise OSError('Injected interruption after archive')
        resumed.fault = crash
        try: resumed.upload('interrupt.txt', new_source, original, cancelled)
        except OSError: pass
        else: raise AssertionError('Archive interruption was not exercised')
        tx = json.loads((resumed.journal_dir/(operation+'.json')).read_bytes())
        assert not tx['committed'] and rows(resumed)[tx['archive']]['id'] == original['id']
        assert submitted[('rb/delete', original['id'])] == 0
        passed('interruption_before_commit_keeps_old_version')
        resumed = backend(cloud, root.task['id']); resumed.operation_id = operation
        actual = resumed.upload('interrupt.txt', new_source, original, cancelled)
        assert actual['id'] == staged_id and submitted[('rb/delete', original['id'])] == 1
        downloaded = run/'downloaded.txt'
        resumed.download('interrupt.txt', actual, downloaded, cancelled)
        assert hashlib.sha1(downloaded.read_bytes()).hexdigest() == actual['sha1']
        no_history(resumed)
        passed('restart_promotes_same_staged_id_then_recycles_old_version')
        def lose(endpoint, fields):
            if endpoint == 'rb/delete':
                resumed.after_post = None
                raise TimeoutError('Injected lost recycle response')
        resumed.after_post = lose
        before = set(resumed.journal_dir.glob('*.json'))
        try: resumed.delete('interrupt.txt', actual)
        except TimeoutError: pass
        else: raise AssertionError('Lost response did not surface')
        deletion = next(iter(set(resumed.journal_dir.glob('*.json'))-before)).stem
        assert resumed.history_archive_info(deletion)['stage'] == 'recycling'
        cold = backend(cloud, root.task['id']); cold.scan_snapshot()
        assert cold.history_archive_info(deletion)['stage'] == 'recycled'
        assert submitted[('rb/delete', actual['id'])] == 1
        assert 'interrupt.txt' not in rows(cold)
        no_history(cold)
        recycled = cold._raw_request(cold.cookie, 'rb', dict(aid=7, cid=0, format='json', offset=0, limit=100))
        tx = json.loads((cold.journal_dir/(deletion+'.json')).read_bytes())
        matches = [row for row in recycled['data'] if row.get('file_name') == tx['archive']
            and int(row.get('file_size', -1)) == actual['size']]
        assert len(matches) == 1
        passed('lost_recycle_reply_recovers_once_and_item_exists_in_provider_bin')
        assert version(rows(cold)[owner_name]) == version(owner_row)
        assert cold.root_id == result['owner']['root_id']
        cold.post_identity('rb/delete', {'fid[0]':cold.root_id})
        assert str(cold._raw_request(cold.cookie, 'files/getid', {'path':cloud.lstrip('/')}).get('id')) == '0'
        result['cloud_cleaned'] = True; result['ok'] = True
        passed('only_owned_fixture_removed_and_no_history_root_created')
    except BaseException as error:
        result['error'] = type(error).__name__+': '+str(error); raise
    finally:
        drain(); save(); print(json.dumps(result, ensure_ascii=True), flush=True)


if __name__ == '__main__': main()
