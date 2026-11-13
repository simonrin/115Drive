"""Actual Windows registration creates badges once and restores owned metadata."""
import base64
import ctypes
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cloud_files import CloudFiles

run = ROOT/'tests/runs'/('folder-badge-' + uuid.uuid4().hex[:12])
app = run/'app 新安装'
(app/'tray').mkdir(parents=True)
(app/'runtime/private').mkdir(parents=True)
for name in ('register-sync-root.ps1', 'register-account-navigation.ps1', 'register-folder-badge.ps1'):
    shutil.copy2(ROOT/name, app/name)
for name in ('FolderBadge.dll', '115Drive-folder.ico'):
    shutil.copy2(ROOT/'tray'/name, app/'tray'/name)

def execute(script, *args, ok=True):
    result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                             str(app/script), *map(str, args)], capture_output=True, timeout=45)
    if ok:
        assert result.returncode == 0, result.stderr.decode(errors='replace')
    return result

def snapshot(*paths):
    return [(str(p), hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in paths]

def apply(task, native):
    return execute('register-sync-root.ps1', '-Action', 'Register', '-Root', task['local_path'],
                   '-TaskId', task['id'], '-IconPath', ROOT/'tray/115Drive.ico',
                   '-ContextBase64', base64.b64encode(native.owner).decode())

def remove(task, native):
    if native.registered():
        native.unregister()
    execute('register-sync-root.ps1', '-Action', 'Unregister', '-Root', task['local_path'], '-TaskId', task['id'])

def paths(task):
    icon = app/'runtime/shell/folder-icons'/(task['id']+'.ico')
    return Path(task['local_path'])/'desktop.ini', icon, icon.with_suffix('.json')

tasks = []
for number in range(3):
    local = run/('original folder '+str(number))
    local.mkdir()
    (local/'keep.txt').write_bytes(b'Original user data; never replace.')
    tasks.append(dict(id=uuid.uuid4().hex, account='115', name='badge '+str(number), local_path=str(local)))
config = app/'runtime/private/sync-tasks.json'
config.write_text(json.dumps(dict(tasks=tasks)), encoding='utf-8')
custom_ini = paths(tasks[0])[0]
custom_ini.write_text('[.ShellClassInfo]\nIconResource=%SystemRoot%\\system32\\shell32.dll,319\nInfoTip=Original folder tip\n[ViewState]\nFolderType=Documents\n', encoding='utf-16')
ctypes.windll.kernel32.SetFileAttributesW(str(custom_ini), 6)
ctypes.windll.kernel32.SetFileAttributesW(tasks[0]['local_path'], 0x11)
original = custom_ini.read_bytes()
natives = [CloudFiles(task['local_path'], task['id'], None) for task in tasks]
report = dict(ok=False, checks=[])
try:
    for task, native in zip(tasks, natives):
        native.register()
        result = apply(task, native)
        ini, icon, state_file = paths(task)
        assert state_file.exists(), result.stdout.decode(errors='replace')
        state = json.loads(state_file.read_text(encoding='utf-8'))
        assert state['phase'] == 'applied', state
        assert str(icon)+',0' in ini.read_text(encoding='utf-16')
        assert icon.read_bytes()[:6] == b'\0\0\1\0\x09\0'
        navigation = json.loads((app/'runtime/shell/navigation.json').read_text(encoding='utf-8'))
        link = next(e for e in navigation['entries'] if e.get('task') == task['id'])
        assert link['kind'] == 'library'
        assert str(icon).encode('utf-16le') in Path(link['path']).read_bytes()
    assert 'InfoTip=Original folder tip' in custom_ini.read_text(encoding='utf-16')
    assert 'FolderType=Documents' in custom_ini.read_text(encoding='utf-16')
    report['checks'].append('first_registration_creates_matching_root_and_navigation_icons_preserving_metadata')

    ini, icon, state_file = paths(tasks[0])
    before = snapshot(ini, icon, state_file)
    apply(tasks[0], natives[0])
    assert snapshot(ini, icon, state_file) == before
    report['checks'].append('repeat_registration_preserves_icon_bytes_and_timestamps_without_stacking')

    state = json.loads(state_file.read_text(encoding='utf-8'))
    state.update(phase='prepared', applied_hash=None)
    state_file.write_text(json.dumps(state), encoding='utf-8')
    apply(tasks[0], natives[0])
    recovered = snapshot(ini, icon)
    assert recovered[0][:2] == before[0][:2] and recovered[1] == before[1]
    assert json.loads(state_file.read_text(encoding='utf-8'))['phase'] == 'applied'
    report['checks'].append('interrupted_registration_recovers_without_regenerating_badge')

    unchanged = snapshot(*paths(tasks[1]))
    foreign = execute('register-folder-badge.ps1', '-AppRoot', app, '-Root', tasks[1]['local_path'],
                      '-TaskId', tasks[0]['id'], '-ContextBase64', base64.b64encode(natives[0].owner).decode(), ok=False)
    assert foreign.returncode != 0
    assert snapshot(*paths(tasks[1])) == unchanged
    report['checks'].append('mismatched_task_context_rejected_before_metadata_changes')

    remove(tasks[0], natives[0])
    assert custom_ini.read_bytes() == original
    assert json.loads(state_file.read_text(encoding='utf-8'))['phase'] == 'removed'
    remove(tasks[1], natives[1])
    assert not paths(tasks[1])[0].exists()
    report['checks'].append('unregister_restores_original_metadata_and_removes_only_new_metadata')

    changed_ini = paths(tasks[2])[0]
    edited = (changed_ini.read_text(encoding='utf-16')+'\n; User added note\n').encode('utf-16')
    with changed_ini.open('r+b') as stream:
        stream.write(edited); stream.truncate()
    changed = changed_ini.read_bytes()
    apply(tasks[2], natives[2])
    assert changed_ini.read_bytes() == changed
    remove(tasks[2], natives[2])
    assert changed_ini.read_bytes() == changed
    report['checks'].append('later_user_customizations_survive_reregistration_and_removal')
    assert all((Path(t['local_path'])/'keep.txt').read_bytes() == b'Original user data; never replace.' for t in tasks)
    report['ok'] = True
except Exception as error:
    report['error'] = repr(error)
    raise
finally:
    errors = []
    for task, native in zip(tasks, natives):
        try:
            remove(task, native)
        except Exception as error:
            errors.append(repr(error))
    config.write_text('{"tasks":[]}', encoding='utf-8')
    cleanup = execute('register-account-navigation.ps1', '-AppRoot', app, ok=False)
    report.update(cleanup_ok=not errors and cleanup.returncode == 0, cleanup_errors=errors, evidence=str(run))
    (run/'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
