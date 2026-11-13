"""Native bidirectional bulk deletion with per-file concurrent-change guards.

Optional --count 1..1000 and --sqlite exercise the production state store using
only generated CfAPI roots. --scoped-checks uses fresh target checks instead of
the default LocalBackend's repeated whole-tree hashes; it never caches versions.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import stat
import sys
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_backend import LocalBackend, RemoteChanged, sha1, version
from task_engine import TaskEngine
from runtime_json import atomic_write_json


class ScopedCheckLocalBackend(LocalBackend):
    """Test-only fresh path/identity/content checks; snapshots remain unchanged."""
    def expect(self,relative,expected):
        path=self.path(relative)  # Keep ancestor and reparse-point guards.
        try:
            before=path.stat()
            if stat.S_ISDIR(before.st_mode):actual=dict(kind='dir',id=str(before.st_ino))
            elif stat.S_ISREG(before.st_mode):
                digest=sha1(path)
                after=self.path(relative).stat()
                fields=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns)
                if fields(before)!=fields(after):raise RemoteChanged('测试后端读取期间文件版本变化。')
                actual=dict(kind='file',id=str(before.st_ino),size=before.st_size,sha1=digest)
            else:raise RemoteChanged('测试后端遇到不支持的文件类型。')
        except FileNotFoundError:actual=None
        if version(actual)!=version(expected):raise RemoteChanged('云端版本已变化，重新核对后再操作。')
        return actual


def verify_scoped_checks(run):
    backend=ScopedCheckLocalBackend(run/'expect-probe')
    (backend.root/'empty').mkdir()
    (backend.root/'nested').mkdir()
    (backend.root/'nested/file.txt').write_bytes(b'abcd')
    rows=backend.snapshot()
    assert all(backend.expect(path,row)==row for path,row in rows.items())
    assert backend.expect('missing.txt',None) is None
    old=rows['nested/file.txt']
    (backend.root/'nested/file.txt').write_bytes(b'wxyz')
    try:backend.expect('nested/file.txt',old)
    except RemoteChanged:pass
    else:raise AssertionError('Same-size edit was not rejected')
    current=backend.snapshot()['nested/file.txt']
    (backend.root/'nested/file.txt').rename(backend.root/'nested/former.txt')
    (backend.root/'nested/file.txt').write_bytes(b'wxyz')
    try:backend.expect('nested/file.txt',current)
    except RemoteChanged:pass
    else:raise AssertionError('Same-content path replacement was not rejected')
    for path,row in backend.snapshot().items():assert backend.expect(path,row)==row
    return dict(full_snapshot_items=len(rows),same_size_edit_rejected=True,
                same_content_new_identity_rejected=True,missing_item_checked=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count',type=int,default=75)
    parser.add_argument('--sqlite',action='store_true')
    parser.add_argument('--scoped-checks',action='store_true',help='Use test-only fresh target checks, retaining complete snapshots')
    args=parser.parse_args()
    if not 1 <= args.count <= 1000:parser.error('--count must be between 1 and 1000')
    started=time.monotonic()
    workspace=Path(__file__).resolve().parents[1]
    run=workspace/'tests/runs'/('bulk-delete-'+uuid.uuid4().hex[:12]);run.mkdir(parents=True)
    print(run,flush=True)
    backend=(ScopedCheckLocalBackend if args.scoped_checks else LocalBackend)(run/'remote')
    task=dict(id=uuid.uuid4().hex,account='115',name='bulk',local_path=str(run/'local'),cloud_path='/bulk',
              policy='always_keep',cache_hours=24,overrides={},paused=False)
    engine=TaskEngine(task,run/'state',backend)
    result=dict(ok=False,count=args.count,sqlite=args.sqlite,scoped_checks=args.scoped_checks,checks=[],phases=[],
                scope='Real CfAPI with generated '+type(backend).__name__+' only; not a real cloud throughput benchmark')
    calls={name:Counter() for name in ('remote_delete','local_delete','upload','download')}
    last_progress=[time.monotonic()]
    def count(kind,method):
        def counted(path,*values,**kwargs):
            calls[kind][path]+=1
            if time.monotonic()-last_progress[0]>=20:
                print('PROGRESS '+kind+' '+str(sum(calls[kind].values())),flush=True)
                last_progress[0]=time.monotonic()
            return method(path,*values,**kwargs)
        return counted
    backend.delete=count('remote_delete',backend.delete)
    backend.upload=count('upload',backend.upload)
    backend.download=count('download',backend.download)
    engine.native.remove=count('local_delete',engine.native.remove)
    def save():
        result['elapsed_seconds']=round(time.monotonic()-started,3)
        atomic_write_json(run/'result.json',result,indent=2)
    def seed(prefix):
        print('SEED '+prefix+' '+str(args.count),flush=True)
        began=time.monotonic()
        names=[prefix+'-'+str(i).zfill(4)+'.txt' for i in range(args.count)]
        for name in names:(backend.root/name).write_bytes(('original-'+name).encode())
        engine.cycle()
        result['phases'].append(dict(name=prefix,count=args.count,seed_seconds=round(time.monotonic()-began,3)))
        save()
        return names
    def remove(root,names):
        assert root.resolve().is_relative_to(run.resolve())
        for name in names:
            target=root/name
            assert target.resolve().is_relative_to(root.resolve())
            target.unlink()
    def verify_once(names,expected,kind,seconds):
        assert all(calls[kind][name]==1 for name in expected), 'Missing or repeated '+kind
        assert all(calls[kind][name]==0 for name in set(names)-set(expected)), 'Unexpected '+kind
        before={kind:dict(values) for kind,values in calls.items()}
        engine.cycle()
        assert before=={kind:dict(values) for kind,values in calls.items()}, 'Idle check repeated a submission or transfer'
        result['phases'][-1].update(sync_seconds=round(seconds,3),deletions=len(expected),
                                    unique_submissions=len(expected),duplicate_submissions=0,
                                    repeat_check_submissions=0)
        save()
    try:
        if args.scoped_checks:
            result['scoped_check_consistency']=verify_scoped_checks(run)
            result['checks'].append('scoped_expect_matches_full_snapshot_and_rejects_changes')
        if args.sqlite:engine.enable_state_store()
        (backend.root/'keep.txt').write_bytes(b'untouched')
        names=seed('local-delete')
        keep=dict(engine.state['items']['keep.txt']['remote'])
        remove(engine.root,names);began=time.monotonic();engine.cycle()
        seconds=time.monotonic()-began
        assert all(not (backend.root/n).exists() and n not in engine.state['items'] for n in names)
        verify_once(names,names,'remote_delete',seconds)
        result['checks'].append(str(args.count)+'_local_deletions_propagate_to_remote')
        print('PASS local-delete '+str(args.count),flush=True)
        names=seed('cloud-delete')
        remove(backend.root,names);began=time.monotonic();engine.cycle()
        seconds=time.monotonic()-began
        assert all(not (engine.root/n).exists() and n not in engine.state['items'] for n in names)
        verify_once(names,names,'local_delete',seconds)
        result['checks'].append(str(args.count)+'_cloud_deletions_propagate_to_local')
        print('PASS cloud-delete '+str(args.count),flush=True)
        names=seed('concurrent-change')
        remove(engine.root,names)
        changed=names[-1];(backend.root/changed).write_bytes(b'changed-remotely')
        began=time.monotonic();engine.cycle();seconds=time.monotonic()-began
        assert (engine.root/changed).read_bytes()==(backend.root/changed).read_bytes()==b'changed-remotely'
        assert all(not (backend.root/n).exists() for n in names[:-1])
        verify_once(names,names[:-1],'remote_delete',seconds)
        result['checks'].append('concurrent_remote_edit_survives_bulk_local_deletion')
        print('PASS concurrent-change '+str(args.count),flush=True)
        assert (engine.root/'keep.txt').read_bytes()==(backend.root/'keep.txt').read_bytes()==b'untouched'
        assert engine.state['items']['keep.txt']['remote']==keep
        result['checks'].append('unrelated_file_and_baseline_preserved')
        result['ok']=True
    except Exception as error:
        result['error']=repr(error);raise
    finally:
        result['operations']={kind:dict(total=sum(values.values()),unique_paths=len(values),
            **({'duplicate_deletions':sum(value-1 for value in values.values() if value>1)}
               if kind in ('remote_delete','local_delete') else {})) for kind,values in calls.items()}
        save()
        for path in sorted(engine.state['items'],key=lambda value:value.count('/'),reverse=True):
            if engine.native.path(path).exists():
                assert not engine.native.state(engine.native.path(path))['partial']
                engine.native.revert(path)
        engine.close();engine.native.unregister()
        result['drained']=True;save()
        print(json.dumps(result),flush=True)


if __name__=='__main__':main()
