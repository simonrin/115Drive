"""Opt-in real-cloud acceptance in one newly generated, disposable 115 root.

Do not run concurrently with another cloud acceptance workload. Real credentials
are read only through TWODRIVE_HOME; task state, files and journals stay below
this script's tests/runs (or the live application's tests/runs).
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import twodrive as t
from task_backend import OpenListBackend, RemoteChanged, sha1
from task_engine import TaskEngine
from task_scheduling import work_priority
from runtime_json import atomic_write_json


class FixturePrivatePaths:
    """Keep generated task journals out of the real account's task directory."""
    def __init__(self, fixture): self.fixture = fixture
    def __truediv__(self, name):
        return self.fixture/name if name == 'tasks' else t.PRIVATE/name


class CountedHTTP:
    def __init__(self, owner): self.owner = owner
    def open(self, request, *args, **kwargs):
        if request.get_method() == 'POST' and request.full_url.startswith('https://webapi.115.com/'):
            self.owner.count('identity_posts')
        return t.HTTP.open(request, *args, **kwargs)


class FixtureHost:
    def __init__(self, directory):
        self.PRIVATE = FixturePrivatePaths(directory/'private')
        self.HTTP = CountedHTTP(self)
        self.lock = threading.Lock()
        self.counts = dict(raw=0, identity_posts=0, openlist=0, login=0, account=0)
    def __getattr__(self, name): return getattr(t, name)
    def count(self, name):
        with self.lock: self.counts[name] += 1
    def snapshot(self):
        with self.lock: return dict(self.counts)
    def login(self): self.count('login'); return t.login()
    def storages(self, token): self.count('account'); return t.storages(token)
    def native_115_request(self, *args): self.count('raw'); return t.native_115_request(*args)
    def api(self, *args): self.count('openlist'); return t.api(*args)
    def transfer_flags(self): return [*t.transfer_flags(), '--bwlimit', '512K']


def delta(after, before): return {key:after[key]-before[key] for key in before}
def fingerprint(path): return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def describe():
    return dict(cloud_scope='/.twodrive-efficiency-<new UUID>',
                local_scope='tests/runs/efficiency-cloud-<new UUID>',
                stages=['bulk_vs_directory_exact_map', 'eight_guarded_deletes_and_lost_reply_retry',
                        'second_client_edit_before_delete', 'second_client_recreates_original_name',
                        'same_backend_limited_32MiB_upload_and_native_interactive_read',
                        'large_file_roundtrip_hash', 'native_drain_and_generated_root_cleanup'],
                failure_policy='save evidence, retain fixture, cancel writer, close native connection',
                credentials='read TWODRIVE_HOME only; never print or copy credentials')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-115', action='store_true')
    parser.add_argument('--describe', action='store_true')
    parser.add_argument('--keep-cloud', action='store_true')
    parser.add_argument('--runs-root', type=Path)
    args=parser.parse_args()
    if args.describe:
        print(json.dumps(describe(), indent=2)); return
    if not args.live_115: parser.error('Pass --live-115 to create only the generated cloud fixture.')
    if not os.environ.get('TWODRIVE_HOME'): parser.error('Set TWODRIVE_HOME to the existing application directory.')
    runs=(args.runs_root or Path(__file__).resolve().parent/'runs').resolve()
    allowed={Path(__file__).resolve().parent/'runs', (t.ROOT/'tests/runs').resolve()}
    if runs not in allowed: parser.error('--runs-root must be this source tests/runs or TWODRIVE_HOME/tests/runs.')
    run_id=uuid.uuid4().hex; run=runs/('efficiency-cloud-'+run_id[:12]); run.mkdir(parents=True)
    cloud='/.twodrive-efficiency-'+run_id
    assert re.fullmatch(r'/\.twodrive-efficiency-[a-f0-9]{32}',cloud)
    configured=t.PRIVATE/'sync-tasks.json'
    if configured.exists():
        for task in json.loads(configured.read_bytes()).get('tasks',[]):
            remote=task['cloud_path'].rstrip('/')
            assert remote and cloud != remote and not cloud.startswith(remote+'/') and not remote.startswith(cloud+'/'), 'Fixture intersects a configured task'
    before_config={str(path):fingerprint(path) for path in (t.CONFIG,configured)}
    result=dict(ok=False, cloud=cloud, run=str(run), checks=[], seconds={}, measurements={},
                expected_failures=[], cleaned=False, config_before=before_config)
    atomic_write_json(run/'result.json',result,indent=2)
    print(run,flush=True)
    host=FixtureHost(run); cancel=threading.Event(); engine=None; writer=None; errors=[]; uploaded=[]
    cleanup=None; cleanup_id=None

    def save(): atomic_write_json(run/'result.json',result,indent=2)
    def passed(name):
        result['checks'].append(name); save(); print('PASS '+name,flush=True)
    def expected_failure(name, error):
        entry=dict(name=name,type=type(error).__name__,message=str(error))
        result['expected_failures'].append(entry)
        atomic_write_json(run/(name+'.json'),entry,indent=2); save()
    def backend(name, state):
        task=dict(id=uuid.uuid4().hex,account='115',name=name or 'root',cloud_path=cloud+('/'+name if name else ''),
                  local_path=str(run/'local'/state),policy='online_only',cache_hours=24,overrides={},paused=False)
        value=OpenListBackend(task,host);value.bind(run/state);return value

    try:
        token=t.login()
        for name in ('','batch','race','transfer','empty','empty/nested'):
            t.api('/api/fs/mkdir',dict(path='/115'+cloud+('/'+name if name else '')),token)
        cleanup=backend('','cleanup-state'); cleanup.connect(); cleanup_id=cleanup.root_id
        result['created_root_id']=cleanup_id; save()
        seed=run/'seed'
        for folder in ('batch','race','transfer'): (seed/folder).mkdir(parents=True)
        for number in range(8): (seed/'batch'/f'{number:02}.bin').write_bytes(os.urandom(16384+number))
        for name in ('before.bin','recreate.bin'): (seed/'race'/name).write_bytes(os.urandom(16411))
        (seed/'transfer/small.bin').write_bytes(os.urandom(262157))
        t.rc('copy',str(seed),'cloud115:'+cloud)
        reader=backend('','manifest-state')
        before=host.snapshot(); started=time.monotonic()
        bulk=reader.scan_snapshot()
        result['seconds']['bulk_first']=round(time.monotonic()-started,3)
        result['measurements']['bulk_first']=delta(host.snapshot(),before)
        assert reader.metrics['bulk_snapshots']>0, 'Bulk endpoint unavailable; safe fallback is not a bulk performance pass'
        before=host.snapshot(); started=time.monotonic()
        steady=reader.scan_snapshot()
        result['seconds']['bulk_steady']=round(time.monotonic()-started,3)
        result['measurements']['bulk_steady']=delta(host.snapshot(),before)
        reference=backend('','reference-state'); reference._bulk_retry_at=float('inf')
        before=host.snapshot(); started=time.monotonic()
        authoritative=reference.snapshot(transport=False)
        result['seconds']['directory_reference']=round(time.monotonic()-started,3)
        result['measurements']['directory_reference']=delta(host.snapshot(),before)
        assert bulk==steady==authoritative, 'Bulk manifest differs from complete directory traversal'
        atomic_write_json(run/'manifest.json',bulk,indent=2)
        passed('bulk_and_directory_manifests_match_including_empty_directories')

        deletes=backend('batch','delete-state'); originals=deletes.scan_snapshot()
        assert set(originals)=={f'{n:02}.bin' for n in range(8)}
        before=host.snapshot(); started=time.monotonic()
        original_post=deletes.post_identity; lost=[False]
        def lose_last_response(endpoint, fields):
            original_post(endpoint,fields)
            if endpoint=='files/batch_rename' and str(fields.get('fid'))==originals['07.bin']['id'] and not lost[0]:
                lost[0]=True
                raise ConnectionError('Injected lost reply after identity rename')
        deletes.post_identity=lose_last_response
        for number in range(8):
            name=f'{number:02}.bin'
            try: deletes.delete(name,originals[name])
            except ConnectionError as error:
                assert number==7 and lost[0]
                expected_failure('lost-delete-reply-before-retry',error)
                posts=host.snapshot()['identity_posts']
                try: deletes.delete(name,originals[name])
                except RemoteChanged as retry_error: expected_failure('retry-refuses-already-renamed-source',retry_error)
                else: raise AssertionError('Retry accepted stale original identity')
                assert host.snapshot()['identity_posts']==posts, 'Retry submitted another identity mutation'
        deletes.post_identity=original_post
        result['seconds']['eight_deletes']=round(time.monotonic()-started,3)
        counts=delta(host.snapshot(),before); result['measurements']['eight_deletes']=counts
        assert counts['identity_posts']==8, counts
        assert counts['raw']<=24, counts
        retained=deletes.snapshot(include_internal=True,transport=False)
        proof=[]
        for name,expected in originals.items():
            assert name not in retained
            matches=[(path,row) for path,row in retained.items() if row['id']==expected['id']]
            assert len(matches)==1 and matches[0][0].startswith('_twodrive-deleted-')
            path,actual=matches[0]; assert actual==expected and actual['sha1']==sha1(seed/'batch'/name)
            proof.append(dict(original=name,archive=path,remote=actual))
        atomic_write_json(run/'retained-delete-proof.json',proof,indent=2)
        passed('eight_deletes_keep_identity_hash_and_retry_does_not_resubmit')

        first=backend('race','race-first-state'); second=backend('race','race-second-state')
        race_rows=first.scan_snapshot(); changed=run/'changed.bin'; changed.write_bytes(os.urandom(16411))
        replacement=second.upload('before.bin',changed,race_rows['before.bin'],cancel)
        posts=host.snapshot()['identity_posts']
        try:first.delete('before.bin',race_rows['before.bin'])
        except RemoteChanged as error:expected_failure('second-client-edit-blocks-delete',error)
        else:raise AssertionError('Changed second-client file was deleted')
        assert host.snapshot()['identity_posts']==posts
        current=second.snapshot(include_internal=True,transport=False)
        assert current['before.bin']['id']==replacement['id'] and current['before.bin']['sha1']==sha1(changed)
        assert any(row['id']==race_rows['before.bin']['id'] and row['sha1']==race_rows['before.bin']['sha1'] for row in current.values())
        passed('second_client_content_change_is_preserved_before_delete')

        recreated=run/'recreated.bin'; recreated.write_bytes(os.urandom(16423)); replacement_rows=[]
        original_post=first.post_identity; recreated_once=[False]
        def recreate_after_rename(endpoint, fields):
            original_post(endpoint,fields)
            if endpoint=='files/batch_rename' and str(fields.get('fid'))==race_rows['recreate.bin']['id'] and not recreated_once[0]:
                recreated_once[0]=True
                replacement_rows.append(second.upload('recreate.bin',recreated,None,cancel))
        first.post_identity=recreate_after_rename
        try:first.delete('recreate.bin',race_rows['recreate.bin'])
        except RemoteChanged as error:expected_failure('second-client-recreated-source-preserved',error)
        else:raise AssertionError('Recreated source name was not reported')
        finally:first.post_identity=original_post
        current=second.snapshot(include_internal=True,transport=False)
        assert replacement_rows and current['recreate.bin']['id']==replacement_rows[0]['id']
        assert current['recreate.bin']['sha1']==sha1(recreated)
        assert any(row['id']==race_rows['recreate.bin']['id'] and row['sha1']==race_rows['recreate.bin']['sha1'] for row in current.values())
        atomic_write_json(run/'concurrent-client-proof.json',current,indent=2)
        passed('second_client_recreated_name_and_archived_original_both_preserved')

        same=backend('transfer','same-task-state'); local=Path(same.task['local_path']); local.mkdir(parents=True)
        engine=TaskEngine(copy.deepcopy(same.task),run/'same-task-state',same)
        engine.enable_state_store()
        engine.cycle()
        assert (run/'same-task-state/state.sqlite3').is_file()
        result['guarded_sqlite_state']=True; save()
        assert engine.native.state(local/'small.bin')['partial'], 'Small file was not online-only before native read'
        large=run/'large.bin'; size=32*1024**2+17; chunk=os.urandom(1024**2)
        with large.open('wb') as stream:
            for offset in range(0,size,len(chunk)):stream.write(chunk[:min(len(chunk),size-offset)])
            stream.flush();os.fsync(stream.fileno())
        expected_large=sha1(large); entered=threading.Event()
        same.fault=lambda point:entered.set() if point=='transfer_started' else None
        def write_large():
            try:uploaded.append(same.upload('large.bin',large,None,cancel))
            except BaseException as error:errors.append(dict(type=type(error).__name__,message=str(error)))
        writer=threading.Thread(target=write_large,name='efficiency-cloud-large-upload'); writer.start()
        assert entered.wait(90), 'Large upload did not reach transfer subprocess'
        started=time.monotonic()
        read=subprocess.run([sys.executable,'-c',
            'import pathlib,hashlib,sys;print(hashlib.sha1(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())',str(local/'small.bin')],
            capture_output=True,text=True,timeout=90,creationflags=t.NO_WINDOW)
        result['seconds']['native_read_while_same_backend_uploading']=round(time.monotonic()-started,3)
        assert read.returncode==0 and read.stdout.strip()==sha1(seed/'transfer/small.bin'), 'Native interactive read failed hash check'
        assert writer.is_alive() and not errors, 'Interactive read waited until same-task upload finished'
        passed('same_task_native_read_completes_before_limited_large_upload')
        writer.join(180)
        assert not writer.is_alive() and not errors and uploaded, errors
        assert uploaded[0]['sha1']==expected_large and uploaded[0]['size']==size
        roundtrip=run/'large-roundtrip.bin'
        same.download('large.bin',uploaded[0],roundtrip,cancel)
        assert sha1(roundtrip)==expected_large
        result['large_file']=dict(size=size,sha1=expected_large,remote=uploaded[0],roundtrip_sha1=sha1(roundtrip))
        passed('large_upload_and_independent_download_full_hash_match')

        for path,row in sorted(engine.state['items'].items(),key=lambda pair:pair[0].count('/'),reverse=True):
            if row['remote']['kind']=='file':engine.native.hydrate(path)
            engine.native.revert(path)
        engine.close();engine.native.unregister();engine=None
        passed('generated_native_root_drained_and_unregistered_local_files_retained')
        result['config_after']={name:fingerprint(Path(name)) for name in before_config}
        assert result['config_after']==before_config, 'Original account/task configuration changed during acceptance'
        result['validated']=True; save()
        if not args.keep_cloud:
            cleanup.connect()
            assert cleanup.root_id==cleanup_id and re.fullmatch(r'/\.twodrive-efficiency-[a-f0-9]{32}',cloud)
            cleanup.post_identity('rb/delete',{'fid[0]':cleanup_id})
            result['cleaned']=True
        result['ok']=True; result['counts']=host.snapshot(); save()
        print('PASS fixture validated; cloud cleanup='+str(result['cleaned']),flush=True)
    except BaseException as error:
        result['error']=dict(type=type(error).__name__,message=str(error))
        result['writer_errors']=errors
        result['counts']=host.snapshot()
        atomic_write_json(run/'failure-before-cleanup.json',result,indent=2)
        save()
        raise
    finally:
        cancel.set()
        if writer is not None:writer.join(30)
        if engine is not None:engine.close()
        result['writer_alive_after_cancel']=bool(writer and writer.is_alive())
        save(); print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
