"""Real Windows processes + local HTTP fixture; no account or production task changes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from installer_upgrade import install,digest
from installer_process import WindowsController
from tests.test_installer_upgrade import fixture,write

def main():
    run=Path(tempfile.mkdtemp(prefix='installer-native-',dir=ROOT/'tests/runs'))
    result=dict(ok=False,checks=[])
    process=None;foreign=None;server=None
    try:
        payload,root,local=fixture(run)
        source=run/'stub.cs'
        source.write_text('''using System;using System.IO;using System.Threading;
class Stub {static void Main(string[] args){if(Array.IndexOf(args,"--stay")>=0){Thread.Sleep(300000);return;}int i=Array.IndexOf(args,"--root");if(i>=0)File.WriteAllText(Path.Combine(args[i+1],"restarted.txt"),"old");}}''',encoding='utf-8')
        compiler=Path(os.environ['WINDIR'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        stub=run/'stub.exe'
        subprocess.run([str(compiler),'/nologo','/target:winexe','/out:'+str(stub),str(source)],check=True,capture_output=True)
        shutil.copyfile(stub,root/'service/TwoDrive.Service.exe');shutil.copyfile(stub,root/'115Drive.exe')
        source.write_text(source.read_text().replace('"old"','"new"'),encoding='utf-8')
        subprocess.run([str(compiler),'/nologo','/target:winexe','/out:'+str(payload/'115Drive.exe'),str(source)],check=True,capture_output=True)
        shutil.copyfile(payload/'115Drive.exe',payload/'service/TwoDrive.Service.exe')
        manifest=json.loads((payload/'install-manifest.json').read_text())
        for name in manifest['files']:manifest['files'][name]=digest(payload/name)
        write(payload/'install-manifest.json',json.dumps(manifest))
        before=(root/'runtime/private/sync-tasks.json').read_bytes()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def send(self,data):
                data=json.dumps(data).encode();self.send_response(200);self.end_headers();self.wfile.write(data)
            def do_GET(self):
                if self.path=='/desktop':return self.send(dict(root=str(root),nonce='fixture',legacy_enabled=False))
                if self.path=='/qr/status':return self.send(dict(state='idle'))
                if self.path=='/status':return self.send(dict(authorization_allowed=all(t['paused'] for t in json.loads((root/'runtime/private/sync-tasks.json').read_text())['tasks'])))
                self.send_error(404)
            def do_POST(self):
                assert self.headers['X-TwoDrive-Nonce']=='fixture'
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path=='/tasks/pause':
                    data=json.loads((root/'runtime/private/sync-tasks.json').read_text());assert data['revision']==body['revision']
                    for task in data['tasks']:
                        if task['id']==body['id']:task['paused']=True
                    data['revision']+=1;write(root/'runtime/private/sync-tasks.json',json.dumps(data))
                self.send(dict(ok=True))
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        controller=WindowsController(root,api='http://127.0.0.1:'+str(server.server_port))
        foreign=subprocess.Popen([str(root/'service/TwoDrive.Service.exe'),'server','--root',str(run/'foreign-data'),'--stay'],creationflags=subprocess.CREATE_NO_WINDOW)
        try:install(payload,root,controller=controller)
        except RuntimeError as error:assert '另一个' in str(error)
        else:raise AssertionError('Foreign data directory was not refused')
        assert foreign.poll() is None and (root/'runtime/private/sync-tasks.json').read_bytes()==before
        result['checks'].append('same_binary_other_data_directory_not_stopped')
        foreign.terminate();foreign.wait(5);foreign=None
        process=subprocess.Popen([str(root/'service/TwoDrive.Service.exe'),'server','--root',str(root),'--stay'],creationflags=subprocess.CREATE_NO_WINDOW)
        out=install(payload,root,controller=controller)
        process.wait(5)
        assert out['ok'] and (root/'runtime/private/sync-tasks.json').read_bytes()==before
        deadline=time.monotonic()+10
        while not (root/'restarted.txt').exists() and time.monotonic()<deadline:time.sleep(.1)
        assert (root/'restarted.txt').read_text()=='new'
        assert (local/'document.txt').read_bytes()==b'unchanged local content'
        assert (root/'runtime/private/bisync/history.lst').read_bytes()==b'original runtime/private/bisync/history.lst'
        result['checks']+=['owned_process_identified_and_stopped_after_pause','original_task_bytes_and_mixed_pause_states_restored','new_program_restarted','local_file_and_history_preserved']
        result['ok']=True
    finally:
        for child in (process,foreign):
            if child and child.poll() is None:child.terminate();child.wait(5)
        if server:server.shutdown();server.server_close()
        (run/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(dict(run=str(run),**result)),flush=True)
if __name__=='__main__':main()
