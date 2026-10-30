"""Verify a release offline without signing in or starting synchronization."""
from pathlib import Path
import argparse
import hashlib
import json
import marshal
import subprocess
import types
import zipfile
from artifact_audit import Audit
from PyInstaller.archive.readers import CArchiveReader


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(code):
    return code.replace(co_filename='<release>',
        co_consts=tuple(canonical(x) if isinstance(x,types.CodeType) else x for x in code.co_consts))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('release',type=Path)
    parser.add_argument('--work',type=Path,required=True,help='A new disposable directory')
    args=parser.parse_args()
    release=args.release.resolve();work=args.work.resolve()
    work.mkdir(parents=True,exist_ok=False)
    hashes={}
    for line in (release/'SHA256SUMS.txt').read_text(encoding='utf-8-sig').splitlines():
        value,name=line.split('  ',1)
        assert Path(name).name==name and name not in hashes,'Invalid checksum filename'
        assert digest((release/name).read_bytes())==value,'Release checksum mismatch: '+name
        hashes[name]=value
    names=['115Drive-Setup-0.2.1-x64.exe','115Drive-0.2.1-windows-x64.zip','115Drive-0.2.1-source.zip']
    assert all(name in hashes for name in names),'Missing release assets'
    app=work/'app'
    process=subprocess.run([str(release/names[0]),'--extract-only',str(app)],
        timeout=60,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert process.returncode==0,'Installer extraction failed'
    audit=Audit()
    audit.directory(app)
    assert audit.report()['ok'],audit.report()
    with zipfile.ZipFile(release/names[1]) as portable,zipfile.ZipFile(release/names[2]) as source:
        assert portable.testzip() is None and source.testzip() is None,'ZIP CRC mismatch'
        files={p.relative_to(app).as_posix():p for p in app.rglob('*') if p.is_file()}
        assert set(files)==set(portable.namelist()),'Installer and portable inventory differ'
        assert all(digest(p.read_bytes())==digest(portable.read(n)) for n,p in files.items()),'Installer payload differs'
        manifest=json.loads(source.read('SOURCE-MANIFEST.json'))
        assert set(source.namelist())==set(manifest)|{'SOURCE-MANIFEST.json'},'Source inventory mismatch'
        assert all(digest(source.read(n))==h for n,h in manifest.items()),'Source hash mismatch'
        source_audit=Audit()
        source_audit.content(names[2],(release/names[2]).read_bytes())
        assert source_audit.report()['ok'],source_audit.report()
        for name in ['README.md','PRIVACY.md','THIRD_PARTY_NOTICES.md','tray/Panel.xaml','tray/Settings.xaml',
                     'third_party/OpenList-4.2.6-twodrive-source.zip','third_party/OpenList-Frontend-v4.2.6-source.zip']:
            assert portable.read(name)==source.read(name),'Source/package mismatch: '+name
        frozen=CArchiveReader(str(app/'service/TwoDrive.Service.exe'))
        pyz=frozen.open_embedded_archive('PYZ.pyz')
        modules=['twodrive','setup_ui','sync_progress','transfer_recovery','cloud_scan','auth115',
                 'desktop_settings','folder_change','local_paths','sync_errors','local_watch']
        for name in modules:
            expected=compile(source.read(name+'.py'),name+'.py','exec',dont_inherit=True)
            assert canonical(expected)==canonical(pyz.extract(name)),'Frozen module differs: '+name
        expected=compile(source.read('desktop_service.py'),'desktop_service.py','exec',dont_inherit=True)
        assert canonical(expected)==canonical(marshal.loads(frozen.extract('desktop_service'))),'Frozen entry point differs'
    check=subprocess.run([str(app/'service/TwoDrive.Service.exe'),'diagnose','--root',str(app)],
        timeout=20,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    diagnostic=json.loads((app/'runtime/package-diagnostics.json').read_text(encoding='utf-8'))
    assert check.returncode==0 and diagnostic['ok'] and diagnostic['frozen'],'Frozen runtime diagnostic failed'
    result=dict(ok=True,sha256=hashes,installer_matches_portable=True,zip_crc_verified=True,
        source_manifest_verified=True,frozen_source_modules_verified=len(modules)+1,
        packaged_files=len(files),source_files=len(manifest),runtime_diagnostic=True,
        binary_privacy=audit.report(),source_privacy=source_audit.report())
    (work/'verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
