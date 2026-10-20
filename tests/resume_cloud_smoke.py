"""Explicit --cloud test: stop only the managed backend and our test rclone.

Requires the production worker to be paused first. Uses a new isolated cloud
subfolder; records actual WAN bytes acknowledged/reused, never credentials.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import twodrive as t
from sync_progress import SyncProgress,SyncPaused,execute


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--cloud',action='store_true')
    parser.add_argument('--overwrite',action='store_true')
    parser.add_argument('--progress',action='store_true')
    parser.add_argument('--pause',action='store_true',help='pause through the running transfer instead of killing the backend')
    args=parser.parse_args()
    if args.pause:args.progress=True
    if not args.cloud:parser.error('requires explicit --cloud')
    assert (t.ROOT/'runtime/stop').exists()
    assert t.read_json(t.CONFIG).get('recovery_enabled') is False
    run='resume-cloud-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(4)
    base=t.ROOT/'tests/runs'/run;base.mkdir()
    remote='cloud115:115Drive/.twodrive-test-'+run
    cache=t.PRIVATE/'transfer-resume'
    payload=base/'payload.bin'
    size=64*1024*1024
    payload.write_bytes(os.urandom(size))
    digest=hashlib.sha1(payload.read_bytes()).hexdigest()
    result=dict(fixture=str(base),size=size,sha1=digest,complete=False,overwrite=args.overwrite,pause=args.pause)
    t.write_json(base/'result.json',result)
    child=None
    observed={}

    class Observed(SyncProgress):
        def __init__(self,directory):
            self.samples=[]
            self.pause_requested=threading.Event()
            directory.mkdir()
            super().__init__(directory)
            self.cancelled=self.pause_requested.is_set
        def feed(self,row):
            super().feed(row)
            for item in self.data.get('active',[]):
                if item.get('progress_source')=='backend':
                    self.samples.append(dict(item))
            (self.root/'samples.json').write_text(json.dumps(self.samples,ensure_ascii=False),encoding='utf-8')

    def stop_server():
        exe=str(t.OPENLIST).replace("'","''")
        command=("$ErrorActionPreference='Stop'; "
                 "Get-Process -Name openlist -ErrorAction SilentlyContinue | "
                 f"Where-Object Path -eq '{exe}' | ForEach-Object "
                 "{Stop-Process -Id $_.Id; if(-not $_.WaitForExit(10000)){throw 'Still running'}}")
        subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                       check=True,timeout=20,creationflags=t.NO_WINDOW)

    def start_copy(source,dest,label,extra=()):
        command=[str(t.RCLONE),'copyto',str(source),str(dest),
            '--config',str(t.PRIVATE/'rclone.conf'),*t.transfer_flags(),
            '--retries','1','--low-level-retries','1','--timeout','2m',*extra]
        if args.progress:
            command += ['--log-level','INFO']
            progress=Observed(base/label)
            started=threading.Event()
            holder={}
            def record_child(process):
                holder['child']=process
                started.set()
            progress.process_started=record_child
            def run():
                try:
                    outcome=execute(command,progress,base/(label+'.log'))
                    progress.finish(outcome.returncode==0)
                except SyncPaused:
                    holder['paused']=True
                    progress.finish(None)
                except BaseException as error:
                    holder['error']=repr(error)
                    started.set()
            thread=threading.Thread(target=run);thread.start()
            if not started.wait(10) or 'error' in holder:raise RuntimeError(holder)
            observed[label]=(progress,thread,holder)
            return holder['child']
        log=open(base/(label+'.log'),'wb')
        process=subprocess.Popen(command,
            stdout=log,stderr=subprocess.STDOUT,creationflags=t.NO_WINDOW)
        log.close()
        return process

    def records(prefix):
        found={}
        for p in cache.glob(prefix+'-*.json'):
            try:
                data=t.read_json(p)
                if data.get('size')==size:found[p.name]=data
            except (OSError,ValueError):pass
        return found

    def interrupt_at_checkpoint(prefix,previous):
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            for name,data in records(prefix).items():
                if name in previous or data.get('complete'):continue
                count=len(data.get('parts') or data.get('blocks') or {})
                if count>=2:
                    if args.pause:
                        direction='upload' if prefix=='u' else 'download'
                        progress,thread,holder=observed[direction+'-interrupted']
                        started=time.monotonic()
                        progress.pause_requested.set()
                        thread.join(timeout=8)
                        assert not thread.is_alive() and holder.get('paused'),holder
                        elapsed=time.monotonic()-started
                        # Leave the backend alive. A local disconnect must cancel
                        # the real cloud HTTP request, not merely hide the UI.
                        time.sleep(1)
                        frozen=t.read_json(cache/name)
                        time.sleep(3)
                        assert t.read_json(cache/name)==frozen,'Cloud transfer continued after pause'
                        assert not frozen.get('complete') and child.poll() is not None
                        result[direction+'_pause']=dict(seconds=elapsed,backend_alive=True,checkpoint_stable=True)
                        return name,frozen
                    stop_server()
                    if child.poll() is None:child.terminate()
                    child.wait(timeout=15)
                    # Read only after backend death: next ACK may have won race.
                    frozen=t.read_json(cache/name)
                    return name,frozen
            if child.poll() is not None:raise RuntimeError('Transfer ended before checkpoint; inspect '+str(base))
            time.sleep(.1)
        raise TimeoutError('No resumable checkpoint; inspect '+str(base))

    t.rc('mkdir',remote)
    try:
        if args.overwrite:
            old=base/'old.bin'
            old.write_bytes(b'original complete version before interrupted overwrite')
            t.rc('copyto',str(old),remote+'/large.bin')
        before=records('u')
        child=start_copy(payload,remote+'/large.bin','upload-interrupted')
        name,checkpoint=interrupt_at_checkpoint('u',before)
        uploaded=len(checkpoint['parts'])*checkpoint['part_size']
        result['upload_before_restart']=dict(parts=len(checkpoint['parts']),bytes=uploaded)
        t.write_json(base/'result.json',result)
        print('Upload stopped after',uploaded,'acknowledged bytes',flush=True)
        t.ensure_server()
        if args.overwrite:
            rows=t.api('/api/fs/list',dict(path='/115/'+remote.split(':',1)[1],
                password='',page=1,per_page=0,refresh=True),t.login())['content']
            assert len(rows)==1 and rows[0]['name']=='large.bin' and rows[0]['size']==old.stat().st_size
            result['old_version_restored_after_restart']=True
        # Slow local delivery while hashing so the verifying stage is visible.
        # It does not alter cloud checkpoint identity or OSS acknowledgement.
        child=start_copy(payload,remote+'/large.bin','upload-resumed',
                         ('--bwlimit','4M') if args.progress else ())
        if child.wait(timeout=300):raise RuntimeError('Upload resume failed; inspect '+str(base))
        completed=[x for n,x in records('u').items() if n not in before and x.get('complete')]
        assert len(completed)==1,completed
        summary=completed[0]
        assert summary['reused_bytes']>=uploaded
        assert summary['sent_bytes']+summary['reused_bytes']==size
        assert summary['sent_bytes']<size
        result['upload']=dict(reused_bytes=summary['reused_bytes'],network_bytes_after_restart=summary['sent_bytes'])
        print('Upload resumed:',result['upload'],flush=True)
        t.write_json(base/'result.json',result)

        before=records('d')
        downloaded=base/'downloaded.bin'
        # Limit local delivery so the backend is killed well before completion.
        child=start_copy(remote+'/large.bin',downloaded,'download-interrupted',
                         ('--multi-thread-streams','0','--bwlimit','2M'))
        name,checkpoint=interrupt_at_checkpoint('d',before)
        fetched=checkpoint['network_bytes']
        result['download_before_restart']=dict(blocks=len(checkpoint['blocks']),bytes=fetched)
        t.write_json(base/'result.json',result)
        print('Download stopped after',fetched,'cached bytes',flush=True)
        t.ensure_server()
        child=start_copy(remote+'/large.bin',downloaded,'download-resumed',
                         ('--multi-thread-streams','0','--bwlimit','4M') if args.progress else ())
        if child.wait(timeout=300):raise RuntimeError('Download resume failed; inspect '+str(base))
        finished=t.read_json(cache/name)
        assert hashlib.sha1(downloaded.read_bytes()).hexdigest()==digest
        additional=finished['network_bytes']-fetched
        assert additional==size-fetched,(additional,fetched,size)
        assert finished['reused_bytes']>=fetched
        result['download']=dict(reused_bytes=fetched,network_bytes_after_restart=additional,sha1_verified=True)
        if args.progress:
            for _,thread,holder in observed.values():
                thread.join(timeout=10)
                assert not thread.is_alive() and 'error' not in holder,holder
            summaries={}
            for direction,baseline in [('upload',uploaded),('download',fetched)]:
                progress,_,_=observed[direction+'-resumed']
                samples=progress.samples
                resumed=[x for x in samples if x['phase']=='resuming']
                verifying=[x for x in samples if x['phase']=='verifying']
                assert resumed and resumed[0]['bytes']>=baseline,(direction,samples)
                assert verifying,(direction,samples)
                assert all(x['bytes']>=baseline for x in samples),(direction,samples)
                counts=[x['bytes'] for x in samples]
                assert counts==sorted(counts),(direction,counts)
                summaries[direction]=dict(samples=len(samples),first= samples[0],
                    first_resuming=resumed[0],last=samples[-1],monotonic=True)
            result['tray_progress']=summaries
        result['complete']=True
        result['recovery_enabled']=False
        t.write_json(base/'result.json',result)
        suffix='-pause' if args.pause else '-progress' if args.progress else '-overwrite' if args.overwrite else ''
        t.write_json(t.ROOT/('runtime/chunk-resume-cloud'+suffix+'-verification.json'),result)
        print(json.dumps(result,indent=2),flush=True)
    finally:
        if child is not None and child.poll() is None:
            child.terminate();child.wait(timeout=15)
        t.ensure_server()
        # Failed checkpoint evidence must remain for diagnosis.
        if result.get('complete'):t.rc('purge',remote)


if __name__=='__main__':main()
