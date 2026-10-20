"""Exercise actual rclone bisync; --cloud touches only a fresh test subtree."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import secrets
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import twodrive as t

def exercise(cloud=False, recovery=None):
    run_id=dt.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(4)
    base=t.ROOT/'tests/runs'/run_id
    local=base/'local';local.mkdir(parents=True)
    work=base/'state';backup=base/'backup'
    if cloud:
        remote='cloud115:115Drive/.twodrive-test-'+run_id
        remote_backup='cloud115:115Drive_Recovery_AutoSync/verification/'+run_id
    else:
        remote=str(base/'remote');Path(remote).mkdir()
        remote_backup=str(base/'remote-backup')
    results=[]
    cfg=t.read_json(t.CONFIG)
    recovery=cfg.get('recovery_enabled',False) if recovery is None else recovery
    cfg={**cfg,'recovery_enabled':recovery}
    count=0
    def put(name,data):
        staging=base/'staging';staging.mkdir(exist_ok=True)
        file=staging/'payload'
        file.write_bytes(data)
        if cloud: t.refresh_cloud_tree(remote)
        if name in listing(): t.rc('deletefile',remote+'/'+name)
        t.rc('copyto',str(file),remote+'/'+name,'--ignore-times')
    def get_at(path):
        out=base/'retrieved'
        out.unlink(missing_ok=True)
        t.rc('copyto',path,str(out))
        return out.read_bytes()
    def get(name):
        return get_at(remote+'/'+name)
    def listing():
        return {x['Path']:x for x in json.loads(t.rc('lsjson',remote,'--recursive','--hash').stdout)}
    def sync(initial=False,expect_failure=False):
        nonlocal count
        count+=1
        t.refresh_cloud_tree(remote)
        args=t.bisync_args(local,remote,work,backup/str(count),remote_backup+'/'+str(count),initial=initial,cfg=cfg)
        if not recovery:
            assert '--backup-dir1' not in args and '--backup-dir2' not in args
        result=t.run(args,timeout=240,check=False)
        if expect_failure:
            assert result.returncode!=0,'Safety check unexpectedly allowed sync'
        elif result.returncode:
            raise RuntimeError('Verification sync failed, exit='+str(result.returncode)+'; see logs/bisync.log')
        if cloud and not expect_failure:
            # WebDAV's merged cache can hide duplicate 115 names after PUT.
            t.refresh_cloud_tree(remote)
            assert not any('openlist_to_delete' in name for name in listing()), 'Upload temporary file was not cleaned'
    def passed(name):
        results.append(name)
        t.write_json(base/'result.json',dict(cloud=cloud,passed=results,complete=False))
        if cloud: t.status('testing',f'真实网盘测试已通过 {len(results)}/8 项，正在继续验证…')
    try:
        if cloud: t.rc('mkdir',remote)
        (local/t.SENTINEL).write_text('test marker',encoding='utf-8')
        put(t.SENTINEL,b'test marker')
        (local/'seed-local.txt').write_bytes(b'original-local')
        put('seed-remote.txt',b'original-remote')
        (local/'initial-collision.txt').write_bytes(b'initial-local')
        put('initial-collision.txt',b'initial-cloud')
        if cloud:
            records=listing()
            sha1=records['seed-remote.txt'].get('Hashes',{}).get('sha1','')
            assert sha1.lower()==hashlib.sha1(b'original-remote').hexdigest(), '115 SHA-1 unavailable; refusing size-only sync'
            passed('115 SHA-1 readable')
        sync(initial=True)
        assert (local/'seed-remote.txt').read_bytes()==b'original-remote'
        assert get('seed-local.txt')==b'original-local'
        assert (local/'initial-collision.txt').read_bytes()==b'initial-cloud'
        if recovery: assert (backup/'1'/'initial-collision.txt').read_bytes()==b'initial-local'
        passed('Initial merge preserves unique files and uses configured same-name priority')
        (local/'中文文件.txt').write_bytes(b'ABCD')
        (local/'empty-local').mkdir()
        put('cloud-add.txt',b'1234')
        sync()
        assert get('中文文件.txt')==b'ABCD'
        assert (local/'cloud-add.txt').read_bytes()==b'1234'
        assert 'empty-local' in listing()
        passed('Two-way additions, Unicode names, empty local directory')
        names_before_edit=set(listing())
        (local/'中文文件.txt').write_bytes(b'EFGH')
        sync()
        assert get('中文文件.txt')==b'EFGH', 'Local same-size edit did not reach cloud'
        assert set(listing())==names_before_edit, 'Overwrite left extra cloud files'
        put('cloud-add.txt',b'5678')
        sync()
        assert (local/'cloud-add.txt').read_bytes()==b'5678', 'Cloud same-size edit did not reach local'
        assert set(listing())==names_before_edit, 'Edit added unexpected files'
        passed('Same-size content edits on both sides')
        (local/'seed-local.txt').unlink()
        sync()
        assert 'seed-local.txt' not in listing()
        t.rc('deletefile',remote+'/seed-remote.txt')
        sync()
        assert not (local/'seed-remote.txt').exists()
        if recovery:
            assert (backup/str(count)/'seed-remote.txt').read_bytes()==b'original-remote'
            assert get_at(remote_backup+'/'+str(count-1)+'/seed-local.txt')==b'original-local'
        passed('Two-way deletion')
        (local/'empty-local').rename(local/'empty-renamed')
        sync()
        assert 'empty-renamed' in listing() and 'empty-local' not in listing()
        t.rc('mkdir',remote+'/empty-cloud')
        sync()
        assert (local/'empty-cloud').is_dir()
        passed('Empty directory rename and remote directory creation')
        (local/'中文文件.txt').write_bytes(b'LOCAL')
        put('中文文件.txt',b'CLOUD')
        sync()
        assert (local/'中文文件.txt').read_bytes()==b'LOCAL'
        assert get('中文文件.txt')==b'LOCAL'
        if recovery:
            assert get_at(remote_backup+'/'+str(count)+'/中文文件.txt')==b'CLOUD', 'Cloud conflict version missing from recovery'
        else:
            conflicts=[name for name in listing() if 'conflict' in name]
            assert any(get(name)==b'CLOUD' for name in conflicts), 'Concurrent cloud edit was lost'
            assert not backup.exists(), 'Disabled local archive was created'
            if not cloud: assert not Path(remote_backup).exists(), 'Disabled remote archive was created'
        passed('Concurrent edits preserve the other version')
        before=listing()
        (local/t.SENTINEL).unlink()
        sync(expect_failure=True)
        after=listing()
        assert set(before)==set(after)
        passed('Missing health marker aborts without deleting remote files')
        report=dict(cloud=cloud,recovery_enabled=recovery,passed=results,complete=True,run_id=run_id,
                    finished_at=dt.datetime.now().astimezone().isoformat())
    except Exception as error:
        t.write_json(base/'result.json',dict(cloud=cloud,recovery_enabled=recovery,
            passed=results,complete=False,error=str(error),run_id=run_id))
        raise
    finally:
        if cloud:
            assert remote.startswith('cloud115:115Drive/.twodrive-test-')
            t.rc('purge',remote)
    t.write_json(base/'result.json',report)
    t.write_json(t.ROOT/'runtime'/('cloud-test.json' if cloud else 'local-test.json'),report)
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--cloud',action='store_true')
    parser.add_argument('--no-recovery',action='store_true')
    args=parser.parse_args()
    print(json.dumps(exercise(args.cloud,False if args.no_recovery else None),ensure_ascii=False,indent=2))
