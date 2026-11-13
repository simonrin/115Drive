"""Exercise the actual EXE on a generated old installation, never the running app."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import uuid
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cloud_files import CloudFiles

def write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False,indent=2).encode('utf-8'))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('installer',type=Path);parser.add_argument('old_portable',type=Path);parser.add_argument('--work',type=Path,required=True)
    args=parser.parse_args();work=args.work.resolve();work.mkdir(parents=True,exist_ok=False)
    app=work/'原有 程序';app.mkdir();local=work/'本机文件';local.mkdir()
    with zipfile.ZipFile(args.old_portable) as archive:archive.extractall(app)
    write(app/'config.json',dict(legacy_enabled=False,recovery_enabled=False,start_with_windows=False,local_path=str(local),remote_path='cloud115:115Drive'))
    task_ids=[uuid.uuid4().hex for _ in range(6)]
    write(app/'runtime/private/sync-tasks.json',dict(version=1,revision=41,tasks=[dict(id=task_ids[i],account='115',name='目录'+str(i),local_path=str(work/('同步'+str(i))),cloud_path='/目录'+str(i),policy='online_only' if i%2 else 'always_keep',paused=bool(i%3),cache_hours=24,overrides={'子目录':'online_only'}) for i in range(6)]))
    for name in ('runtime/private/initialized.json','runtime/private/admin.json','runtime/private/rclone.conf','runtime/private/bisync/old.path1.lst','runtime/private/bisync/old.path2.lst','runtime/private/transfer-resume/saved.part','tray/settings.json','filters.txt','filters.txt.md5','保留此文件.txt','logs/history.log','recovery/previous.txt'):
        write(app/name,('generated preserved content '+name).encode())
    for task_id in task_ids:write(app/'runtime/private/tasks'/task_id/'state.json',dict(version=1,items={'保留文件':dict(size=10,sha1='0'*40)},pending=None))
    write(local/'已下载.txt','generated local content'.encode());os.chmod(local/'已下载.txt',stat.S_IREAD)
    databases=[]
    for database in (app/'runtime/private/openlist/data.db',app/'runtime/private/tasks'/task_ids[0]/'state.sqlite3'):
        database.parent.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(database);db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE preserved(value TEXT)');db.execute('INSERT INTO preserved VALUES (?)',('original',));db.commit()
        databases.append(db)
    fetches=[]
    def no_fetch(identity,cancel):fetches.append(identity);raise AssertionError('Installer must not hydrate files')
    native=CloudFiles(work/'仅联机',uuid.uuid4().hex,no_fetch)
    result=dict(ok=False,checks=[])
    try:
        native.connect();native.create('未下载.bin',dict(task=native.task_id,kind='file',path='未下载.bin',id='fixture',size=16*1024**2,sha1='0'*40))
        online=native.root/'未下载.bin';before_online=native.state(online);online_id=online.stat().st_ino
        originals={p:(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_ino,p.stat().st_mtime_ns,p.stat().st_file_attributes) for p in [*app.glob('config.json'),app/'filters.txt',app/'filters.txt.md5',app/'保留此文件.txt',*list((app/'runtime/private').rglob('*')),app/'tray/settings.json',app/'logs/history.log',app/'recovery/previous.txt',local/'已下载.txt'] if p.is_file()}
        for iteration in range(2):
            completed=subprocess.run([str(args.installer.resolve()),'--install-to',str(app)],timeout=240,creationflags=subprocess.CREATE_NO_WINDOW)
            error=Path(str(app)+'.error.txt')
            assert completed.returncode==0,error.read_text(encoding='utf-8-sig') if error.exists() else completed.returncode
            for path,expected in originals.items():
                actual=(hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_ino,path.stat().st_mtime_ns,path.stat().st_file_attributes)
                assert actual==expected,str(path)
            after=native.state(online)
            assert after['partial'] and after['on_disk']==0 and online.stat().st_ino==online_id and not fetches
            result['checks'].append('old_install_upgrade_preserves_data' if iteration==0 else 'repeat_install_preserves_data')
        for db in databases:
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert db.execute('SELECT value FROM preserved').fetchone()[0]=='original'
        fresh=work/'全新安装'
        completed=subprocess.run([str(args.installer.resolve()),'--install-to',str(fresh)+os.sep],timeout=240,creationflags=subprocess.CREATE_NO_WINDOW)
        assert completed.returncode==0,Path(str(fresh)+'.error.txt').read_text() if Path(str(fresh)+'.error.txt').exists() else completed.returncode
        assert (fresh/'115Drive.exe').is_file() and not (fresh/'config.json').exists()
        result['checks'].append('fresh_exe_install_accepts_trailing_separator')
        manifest=json.loads((app/'install-manifest.json').read_text())
        for name,checksum in manifest['files'].items():
            if name!='filters.txt':assert hashlib.sha256((app/name).read_bytes()).hexdigest()==checksum,name
        result.update(ok=True,protected_files=len(originals),online_hydration_count=len(fetches),version=manifest['version'])
        result['checks']+=['six_task_ids_policies_pause_states_and_revision_unchanged','open_sqlite_wal_and_history_unchanged','downloaded_file_identity_attributes_unchanged','online_placeholder_identity_and_zero_cached_bytes_unchanged','all_program_files_match_new_manifest']
    finally:
        for db in databases:db.close()
        try:
            native.discard_online('未下载.bin');native.close();native.unregister()
        finally:
            native.close();(work/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
