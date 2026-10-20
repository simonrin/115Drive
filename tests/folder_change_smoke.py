"""Real rclone, isolated local endpoints; never touches the user's sync folder/cloud."""
import hashlib
import json
from pathlib import Path
import secrets
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import twodrive as t
from tests.test_folder_change import Fixture


def exercise(cross_drive=False):
    base=t.ROOT/'tests/runs'/('folder-change-'+secrets.token_hex(6))
    f=Fixture(base)
    if cross_drive:
        f.target=Path(tempfile.mkdtemp(prefix='twodrive-folder-test-'))/'新的 文件夹'
        assert f.target.drive.lower()!=f.source.drive.lower(), 'Cross-drive fixture must use another volume'
    (f.ROOT/'logs').mkdir()
    (f.ROOT/'filters.txt').write_bytes((t.ROOT/'filters.txt').read_bytes())
    remote=base/'remote/115Drive'
    remote.mkdir(parents=True)
    (remote/t.SENTINEL).write_text('test marker')
    (remote/'cloud-seed.txt').write_text('cloud original')
    results=[]
    counter=0
    def sync(initial=False):
        nonlocal counter
        counter+=1
        cfg=f.read_json(f.CONFIG)
        with patch.object(t,'ROOT',f.ROOT),patch.object(t,'PRIVATE',f.PRIVATE),patch.object(t,'CONFIG',f.CONFIG):
            if initial:
                # Seed the same logical endpoint identity as production before migration.
                args=t.bisync_args('localhash:115Drive','cloud115:115Drive',f.history,
                    'localhash:app/recovery/'+str(counter),'cloud115:backups/'+str(counter),initial=True,cfg=cfg)
            else:
                args=t.bisync_args(cfg['local_path'],'cloud115:115Drive',f.history,
                    f.ROOT/'recovery'/str(counter),'cloud115:backups/'+str(counter),cached_local=True,cfg=cfg)
            result=t.run(args,check=False,timeout=120)
            if result.returncode:
                raise AssertionError(f'rclone step {counter} exit {result.returncode}; log: {f.ROOT / "logs/bisync.log"}')
            if not initial: assert '--resync' not in args
    sync(initial=True)
    before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in f.history.glob('*.lst')}
    f.changer.change(str(f.target))
    assert f.changer.view()['state']=='complete',f.changer.view()
    assert before=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in f.history.glob('*.lst')}
    results.append('migration retained exact bisync listings and old source')
    (f.target/'local-new.txt').write_text('local new')
    (remote/'remote-new.txt').write_text('remote new')
    sync()
    assert (remote/'local-new.txt').read_text()=='local new'
    assert (f.target/'remote-new.txt').read_text()=='remote new'
    results.append('two-way additions after switching without resync')
    (f.target/'子目录/测试.txt').write_text('edited locally')
    (remote/'cloud-seed.txt').write_text('edited remotely')
    sync()
    assert (remote/'子目录/测试.txt').read_text()=='edited locally'
    assert (f.target/'cloud-seed.txt').read_text()=='edited remotely'
    assert (f.ROOT/'recovery'/str(counter)/'cloud-seed.txt').read_text()=='cloud original'
    results.append('two-way edits and local recovery through combine mapping')
    (f.target/'local-new.txt').unlink()
    (remote/'remote-new.txt').unlink()
    sync()
    assert not (remote/'local-new.txt').exists()
    assert not (f.target/'remote-new.txt').exists()
    assert (f.ROOT/'recovery'/str(counter)/'remote-new.txt').read_text()=='remote new'
    assert (base/'remote/backups'/str(counter)/'local-new.txt').read_text()=='local new'
    assert (f.source/'子目录/测试.txt').read_text()=='original content'
    results.append('two-way deletions with both recovery copies; original folder intact')
    report=dict(complete=True,fixture=str(base),target=str(f.target),passed=results,real_cloud=False,cross_drive=cross_drive)
    t.write_json(t.ROOT/('runtime/folder-change-cross-drive.json' if cross_drive else 'runtime/folder-change-smoke.json'),report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__': exercise('--cross-drive' in sys.argv)
