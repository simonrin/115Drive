"""4 GiB boundary, repeated hydration, concurrent readers, bounded provider memory."""
import ctypes as c,hashlib,json,os,subprocess,sys,time,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cloud_files import CloudFiles
from runtime_json import atomic_write_json

def main():
    run=Path(__file__).resolve().parents[1]/'tests/runs'/('large-acceptance-'+uuid.uuid4().hex[:12]);run.mkdir(parents=True)
    print(run,flush=True);size=4*1024**3+123
    source=run/'source.bin';digest=hashlib.sha1();block=os.urandom(8*1024**2)
    with source.open('wb') as stream:
        for offset in range(0,size,len(block)):
            data=block[:min(len(block),size-offset)];stream.write(data);digest.update(data)
        stream.flush();os.fsync(stream.fileno())
    wanted=digest.hexdigest();task=uuid.uuid4().hex;fetches=[]
    def fetch(identity,cancelled):fetches.append(time.time());return source
    events=[]
    native=CloudFiles(run/'local',task,fetch,event=lambda name,value:events.append((name,value)) if name=='fetch_error' else None)
    result=dict(ok=False,size=size,sha1=wanted,checks=[],seconds={},errors=events)
    try:
        native.connect();native.create('large.bin',dict(task=task,path='large.bin',kind='file',id='large',size=size,sha1=wanted))
        read_code='import hashlib,sys;f=open(sys.argv[1],"rb");print(hashlib.file_digest(f,"sha1").hexdigest());f.close()'
        for cycle in range(3):
            start=time.monotonic()
            readers=[subprocess.Popen([sys.executable,'-c',read_code,str(native.root/'large.bin')],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(3 if cycle==1 else 1)]
            for reader in readers:
                out,error=reader.communicate(timeout=300)
                assert reader.returncode==0,error
                assert out.strip()==wanted
            result['seconds'][str(cycle)]=round(time.monotonic()-start,3)
            assert not native.state(native.root/'large.bin')['partial']
            result['checks'].append('full_hash_cycle_'+str(cycle))
            if cycle<2:
                native.dehydrate('large.bin');state=native.state(native.root/'large.bin')
                assert state['on_disk']==0 and (native.root/'large.bin').stat().st_size==size
            atomic_write_json(run/'result.json',result,indent=2);print('PASS cycle '+str(cycle),flush=True)
        class Memory(c.Structure):
            _fields_=[('cb',c.c_ulong),('faults',c.c_ulong)]+[(n,c.c_size_t) for n in ('peak','working','ppq','pq','pnpq','npq','page','peakpage')]
        memory=Memory();memory.cb=c.sizeof(memory)
        kernel=c.WinDLL('kernel32');kernel.GetCurrentProcess.restype=c.c_void_p
        psapi=c.WinDLL('psapi');psapi.GetProcessMemoryInfo.argtypes=[c.c_void_p,c.POINTER(Memory),c.c_ulong]
        assert psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),c.byref(memory),c.sizeof(memory))
        result['peak_working_set']=memory.peak;assert memory.peak<512*1024**2
        result['fetches']=len(fetches)
        native.pin('large.bin',1);native.close()
        with (native.root/'large.bin').open('rb') as stream:assert hashlib.file_digest(stream,'sha1').hexdigest()==wanted
        native.connect();native.revert('large.bin');native.close();native.unregister()
        result['checks']+=['provider_memory_below_512_MiB','offline_full_hash','drained']
        result['ok']=True
    except Exception as error:result['error']=repr(error);raise
    finally:native.close();atomic_write_json(run/'result.json',result,indent=2);print(json.dumps(result),flush=True)

if __name__=='__main__':main()
