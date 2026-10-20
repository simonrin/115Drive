"""Real rclone interruption/restart in an isolated local fixture, no user data."""
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time
import threading
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import twodrive as t
from transfer_recovery import TransferRecovery, partial_suffix
from sync_progress import SyncProgress, SyncPaused, execute


def exercise(pause=False):
    base=t.ROOT/'tests/runs'/('restart-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(4))
    local=base/'local';remote=base/'remote';work=base/'state'
    local.mkdir(parents=True);remote.mkdir()
    for p in (local,remote):(p/t.SENTINEL).write_text('test marker')
    cfg={**t.read_json(t.CONFIG),'local_path':str(local),'remote_path':str(remote),
         'local_hash_cache':False,'recovery_enabled':False}
    def args(initial=False):
        values=t.bisync_args(local,str(remote),work,base/'backup',str(base/'remote-backup'),
                            initial=initial,cfg=cfg)
        values[values.index('--log-file')+1]=str(base/'initial.log')
        return values
    t.run(args(True),timeout=60)
    (local/'existing.txt').write_bytes(b'local must survive')
    payload=secrets.token_bytes(2*1024*1024)
    (remote/'download.bin').write_bytes(payload)
    unknown=local/'my-own.partial';unknown.write_text('keep me')
    recovery=TransferRecovery(base,cfg,work,alive=lambda:False)
    command=recovery.begin(args())+['--bwlimit','128k','--multi-thread-streams','0']
    signal=threading.Event()
    if pause:
        holder={};started=threading.Event()
        progress=SyncProgress(base)
        progress.cancelled=signal.is_set
        def record(process):
            holder['child']=process;recovery.started(process);started.set()
        progress.process_started=record
        def transfer():
            try:holder['result']=execute(command,progress,base/'pause.log')
            except SyncPaused:holder['paused']=True;progress.finish(None)
            except BaseException as error:holder['error']=repr(error);started.set()
        thread=threading.Thread(target=transfer);thread.start()
        assert started.wait(10) and 'error' not in holder,holder
        child=holder['child']
    else:
        child=subprocess.Popen([str(a) for a in command],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                                creationflags=t.NO_WINDOW)
        recovery.started(child)
    try:
        deadline=time.monotonic()+30
        while time.monotonic()<deadline and child.poll() is None:
            partials=list(local.glob('*'+partial_suffix(recovery.record['id'])))
            if partials and partials[0].stat().st_size>0:break
            time.sleep(.2)
        if not partials or child.poll() is not None:
            if child.poll() is None:child.kill()
            if pause:
                thread.join(timeout=15)
                stderr=repr(holder).encode()
            else:_,stderr=child.communicate(timeout=15)
            (base/'failure-stderr.txt').write_bytes(stderr)
            raise RuntimeError(stderr.decode(errors='replace')[-1500:] or 'No active partial transfer to interrupt')
        if pause:
            pause_started=time.monotonic();signal.set();thread.join(timeout=8)
            pause_seconds=time.monotonic()-pause_started
            assert not thread.is_alive() and holder.get('paused'),holder
            assert recovery.path.exists()
        else:
            child.kill();child.communicate(timeout=15)
        locks={p.name:p.read_text(errors='replace') for p in work.glob('*.lck')}
        t.write_json(base/'crash-evidence.json',dict(pid=child.pid,exit_code=child.returncode,
                    partial_names=[p.name for p in partials],locks=locks))
        restarted=TransferRecovery(base,cfg,work,alive=lambda:False)
        assert restarted.prepare()
        assert not any(p.exists() for p in partials)
        progress=SyncProgress(base)
        retry=restarted.begin(args());progress.process_started=restarted.started
        result=execute(retry,progress,base/'bisync.log')
        assert result.returncode==0,f'Recovery exit={result.returncode}'
        restarted.complete()
        assert hashlib.sha256((local/'download.bin').read_bytes()).digest()==hashlib.sha256(payload).digest()
        assert (remote/'existing.txt').read_bytes()==b'local must survive'
        assert unknown.read_text()=='keep me'
        assert not (remote/'my-own.partial').exists()
        assert not (base/'backup').exists() and not (base/'remote-backup').exists()
        assert not list(local.glob('*'+partial_suffix(restarted.record['id'])))
        assert not restarted.path.exists()
        result=dict(ok=True,real_process_killed=True,recovered_without_resync=True,
                    content_verified=True,unknown_partial_preserved=True,recovery_archive_created=False)
        if pause:result.update(pause_seconds=pause_seconds,pause_request_verified=True)
        t.write_json(base/'result.json',result)
        print(json.dumps(dict(**result,fixture=str(base)),indent=2))
        return result
    finally:
        if child.poll() is None:
            child.kill()
            if pause:thread.join(timeout=15)
            else:child.communicate(timeout=15)


if __name__=='__main__':exercise(pause='--pause' in sys.argv)
