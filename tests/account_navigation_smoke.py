"""Windows Shell integration: arrow-free entries, migration and owned cleanup."""
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import winreg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud_files import CloudFiles

ROOT = Path(__file__).resolve().parents[1]
run = ROOT/'tests/runs'/('navigation-' + uuid.uuid4().hex[:12])
app = run/'app'
app.mkdir(parents=True)
config = app/'runtime/private/sync-tasks.json'
config.parent.mkdir(parents=True)
for name in ('register-sync-root.ps1', 'register-account-navigation.ps1'):
    shutil.copy2(ROOT/name, app/name)
icon = ROOT/'bin/TwoDriveTray/TwoDriveTray.exe'
tasks = []
for number, account in enumerate(('115', '115', 'second-account')):
    local = run/('local' + str(number))
    local.mkdir()
    (local/'keep.txt').write_bytes(('original data ' + str(number)).encode())
    tasks.append(dict(id=uuid.uuid4().hex, account=account, name='folder ' + str(number), local_path=str(local)))
tasks[0]['name'] = '附件 & 测试'

(app/'probe.ps1').write_text('''param([string]$Manifest,[string]$Output)
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject Shell.Application
$entries = @((Get-Content -LiteralPath $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json).entries | Where-Object {$_.kind -eq 'library'})
$result = @($entries | ForEach-Object {
    $item = $shell.Namespace((Split-Path $_.path)).ParseName((Split-Path $_.path -Leaf))
    [pscustomobject]@{Task=$_.task;Name=$item.Name;IsLink=$item.IsLink;IsFolder=$item.IsFolder;Items=@($item.GetFolder.Items() | ForEach-Object {$_.Path})}
})
[IO.File]::WriteAllText($Output,($result | ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
''', encoding='utf-8-sig')
(app/'legacy.ps1').write_text('''param([string]$Path,[string]$Target,[string]$Icon)
$link = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $Path 'target.lnk'))
$link.TargetPath=$Target
$link.IconLocation=$Icon+',0'
$link.Save()
''', encoding='utf-8-sig')

def save(rows):
    config.write_text(json.dumps(dict(tasks=rows)), encoding='utf-8')

def command(script, *args, ok=True):
    result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                             str(app/script), *map(str, args)], capture_output=True)
    if ok and result.returncode:
        raise AssertionError(result.stderr.decode('utf-8', errors='replace'))
    return result

def update(*args, **kwargs):
    return command('register-account-navigation.ps1', '-AppRoot', app, '-IconPath', icon, *args, **kwargs)

def manifest():
    return json.loads((app/'runtime/shell/navigation.json').read_text(encoding='utf-8'))

def snapshots(folder):
    return {str(p.relative_to(folder)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in folder.rglob('*') if p.is_file()}

def class_exists(clsid):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Software\\Classes\\CLSID\\' + clsid): return True
    except FileNotFoundError: return False

report = dict(ok=False, checks=[])
registered = None
try:
    save(tasks)
    originals = {t['id']: (Path(t['local_path'])/'keep.txt').read_bytes() for t in tasks}
    update()
    first = manifest()
    assert len([e for e in first['entries'] if e['kind']=='account']) == 2
    assert len([e for e in first['entries'] if e['kind']=='library']) == 3
    report['checks'].append('same_account_grouped_other_account_separate')
    probe = run/'shell-items.json'
    command('probe.ps1', app/'runtime/shell/navigation.json', probe)
    for item in json.loads(probe.read_text(encoding='utf-8')):
        task = next(t for t in tasks if t['id'] == item['Task'])
        assert item['Name'] == task['name'], item
        assert item['IsFolder'] and not item['IsLink'], item
        assert item['Items'] == [str(Path(task['local_path'])/'keep.txt')], item
    report['checks'].append('shell_entries_have_no_link_attribute_and_browse_original_files')
    before = snapshots(app/'runtime/shell')
    update()
    assert snapshots(app/'runtime/shell') == before, 'No-op registration rewrote shortcut or manifest'
    report['checks'].append('repeat_registration_does_not_rewrite_shortcuts_or_manifest')
    update('-SkipTaskId', tasks[0]['id'])
    assert len([e for e in manifest()['entries'] if e['kind']=='library']) == 2
    assert all((Path(t['local_path'])/'keep.txt').read_bytes() == originals[t['id']] for t in tasks)
    report['checks'].append('removing_task_removes_only_its_navigation_link')
    update()
    migrated = manifest()
    entry = next(e for e in migrated['entries'] if e.get('task')==tasks[0]['id'])
    Path(entry['path']).unlink()
    entry['path'] = str(Path(entry['path']).with_suffix(''))
    entry['kind'] = 'link'
    legacy = Path(entry['path'])
    legacy.mkdir()
    (legacy/'desktop.ini').write_text('[.ShellClassInfo]\r\nCLSID2={0AFACED1-E828-11D1-9187-B532F1E9575D}\r\nFlags=2\r\n', encoding='utf-16')
    ctypes.windll.kernel32.SetFileAttributesW(str(legacy/'desktop.ini'), 6)
    ctypes.windll.kernel32.SetFileAttributesW(str(legacy), 0x11)
    command('legacy.ps1', legacy, tasks[0]['local_path'], icon)
    migrated['version'] = 2
    (app/'runtime/shell/navigation.json').write_text(json.dumps(migrated), encoding='utf-8')
    unexpected = Path(entry['path'])/'user-content.txt'
    unexpected.write_bytes(b'never remove unexpected content')
    blocked = update('-SkipTaskId', tasks[0]['id'], ok=False)
    assert blocked.returncode and unexpected.read_bytes() == b'never remove unexpected content'
    unexpected.unlink()
    report['checks'].append('unexpected_content_is_retained_and_cleanup_refused')
    update()
    assert not legacy.exists()
    assert next(e for e in manifest()['entries'] if e.get('task')==tasks[0]['id'])['kind']=='library'
    report['checks'].append('legacy_folder_shortcut_migrates_without_touching_original_data')
    # Reproduce CfAPI detach followed by WinRT unregister: the path mapping is
    # already gone, but the per-user namespace shortcut can still be present.
    task = tasks[0]
    native = CloudFiles(task['local_path'], task['id'], None)
    registered = task
    command('register-sync-root.ps1', '-Action', 'Register', '-Root', task['local_path'],
            '-TaskId', task['id'], '-ContextBase64', base64.b64encode(native.owner).decode())
    # Find only this generated task's class, never another application's keys.
    root_key = 'SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Explorer\\SyncRootManager'
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root_key) as parent:
        keys = [winreg.EnumKey(parent,i) for i in range(winreg.QueryInfoKey(parent)[0])]
    task_key = next(k for k in keys if k.startswith('115Drive!') and k.endswith('!115-' + task['id']))
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root_key+'\\'+task_key) as key:
        clsid = winreg.QueryValueEx(key, 'NamespaceCLSID')[0]
    wrong = command('register-sync-root.ps1', '-Action', 'Unregister', '-Root', tasks[1]['local_path'],
                    '-TaskId', task['id'], ok=False)
    assert wrong.returncode and native.registered()
    native.unregister()
    command('register-sync-root.ps1', '-Action', 'Unregister', '-Root', task['local_path'], '-TaskId', task['id'])
    registered = None
    assert not class_exists(clsid)
    assert (Path(task['local_path'])/'keep.txt').read_bytes() == originals[task['id']]
    report['checks'].append('wrong_root_rejected_and_cfapi_orphan_shell_class_removed')
    save([])
    update()
    assert all(not class_exists(e['clsid']) for e in first['entries'] if e['kind']=='account')
    assert not list((app/'runtime/shell/accounts').iterdir())
    assert all((Path(t['local_path'])/'keep.txt').read_bytes() == originals[t['id']] for t in tasks)
    report['checks'].append('last_task_removes_account_entry_and_preserves_all_original_files')
    report['ok'] = True
except Exception as error:
    report['error'] = repr(error)
    raise
finally:
    if registered:
        command('register-sync-root.ps1','-Action','Unregister','-Root',registered['local_path'],
                '-TaskId',registered['id'],ok=False)
    save([])
    cleanup = update(ok=False)
    report['cleanup_ok'] = cleanup.returncode == 0
    (run/'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(dict(report, evidence=str(run)), ensure_ascii=False), flush=True)
