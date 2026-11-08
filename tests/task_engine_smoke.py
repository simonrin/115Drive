"""Two real CfAPI roots backed by independent generated local cloud directories."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import base64
import threading
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from task_engine import TaskEngine
from task_backend import LocalBackend, sha1
from runtime_json import atomic_write_json


def external_read(path):
    p = subprocess.run([sys.executable, '-c', 'import pathlib,hashlib,sys; print(hashlib.sha1(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())', str(path)], capture_output=True, text=True, timeout=60)
    if p.returncode: raise RuntimeError(p.stderr)
    return p.stdout.strip()


def main():
    workspace = Path(__file__).resolve().parents[1]
    directory = workspace/'tests/runs'/('task-engine-' + uuid.uuid4().hex[:12])
    directory.mkdir(parents=True)
    print(str(directory), flush=True)
    engines = []
    shell_registered = set()
    result = dict(ok=False, checks=[])
    try:
        for name, policy in (('one', 'always_keep'), ('two', 'online_only')):
            backend = LocalBackend(directory/name/'remote')
            (backend.root/'cloud.txt').write_text('原始云端内容', encoding='utf-8')
            task = dict(id=uuid.uuid4().hex, name=name, local_path=str(directory/name/'local'), cloud_path='/'+name,
                        policy=policy, cache_hours=0, overrides={}, paused=False)
            root = Path(task['local_path']); root.mkdir()
            (root/'local.txt').write_text('原始本地内容', encoding='utf-8')
            engine = TaskEngine(task, directory/name/'state', backend)
            engines.append(engine)
            if '--shell' in sys.argv:
                command = ['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(workspace/'register-sync-root.ps1'),
                           '-Action','Register','-Root',str(engine.root),'-TaskId',task['id'],
                           '-DisplayName','115Drive 测试 · '+name,'-IconPath',str(workspace/'bin/TwoDriveTray/TwoDriveTray.exe'),
                           '-ContextBase64',base64.b64encode(engine.native.owner).decode('ascii')]
                shell = subprocess.run(command, capture_output=True, text=True, timeout=45)
                assert shell.returncode == 0, shell.stderr
                shell_registered.add(task['id'])
                engine.native.register()  # Shell registration must preserve ownership.
            engine.cycle()
            engine.cycle()
        one, two = engines
        assert one.native.state(one.root/'cloud.txt')['pin'] == 1
        assert not one.native.state(one.root/'cloud.txt')['partial']
        assert two.native.state(two.root/'cloud.txt')['partial']
        assert two.native.state(two.root/'local.txt')['partial']
        assert (two.backend.root/'local.txt').read_text(encoding='utf-8') == '原始本地内容'
        result['checks'].append('two_tasks_apply_distinct_defaults_to_old_and_new_files')
        assert external_read(two.root/'cloud.txt') == sha1(two.backend.root/'cloud.txt')
        assert not two.native.state(two.root/'cloud.txt')['partial']
        two.task['overrides']['cloud.txt'] = 'always_keep'
        two.cycle()
        assert two.native.state(two.root/'cloud.txt')['pin'] == 1
        result['checks'].append('file_override_keeps_content_in_online_task')
        (one.root/'local.txt').write_text('本地修改', encoding='utf-8')
        one.cycle()
        assert (one.backend.root/'local.txt').read_text(encoding='utf-8') == '本地修改'
        result['checks'].append('local_edit_uploads_and_verifies')
        (two.backend.root/'local.txt').write_text('云端修改', encoding='utf-8')
        two.cycle()
        assert two.native.state(two.root/'local.txt')['partial']
        assert external_read(two.root/'local.txt') == sha1(two.backend.root/'local.txt')
        result['checks'].append('cloud_edit_refreshes_online_placeholder')
        (one.root/'local.txt').write_text('同时修改的本地', encoding='utf-8')
        (one.backend.root/'local.txt').write_text('同时修改的云端', encoding='utf-8')
        one.cycle(); one.cycle()
        conflicts = list(one.root.glob('local.conflict-local-*.txt'))
        assert len(conflicts) == 1 and conflicts[0].read_text(encoding='utf-8') == '同时修改的本地'
        assert (one.root/'local.txt').read_text(encoding='utf-8') == '同时修改的云端'
        assert (one.backend.root/conflicts[0].name).is_file()
        result['checks'].append('simultaneous_edits_preserve_both_versions')
        (one.root/'cloud.txt').unlink(); one.cycle()
        assert not (one.backend.root/'cloud.txt').exists()
        (two.backend.root/'local.txt').unlink(); two.cycle()
        assert not (two.root/'local.txt').exists()
        result['checks'].append('deletion_propagates_separately_from_release')
        (two.backend.root/'rename.txt').write_text('不应下载的改名文件', encoding='utf-8')
        (two.backend.root/'目录').mkdir()
        (two.backend.root/'目录/内文.txt').write_text('目录改名内容', encoding='utf-8')
        two.cycle()
        (two.root/'rename.txt').rename(two.root/'renamed.txt')
        (two.root/'目录').rename(two.root/'新目录')
        two.cycle()
        assert not (two.backend.root/'rename.txt').exists()
        assert (two.backend.root/'renamed.txt').is_file()
        assert two.native.state(two.root/'renamed.txt')['partial']
        assert (two.backend.root/'新目录/内文.txt').is_file()
        assert two.native.state(two.root/'新目录/内文.txt')['partial']
        assert external_read(two.root/'renamed.txt') == sha1(two.backend.root/'renamed.txt')
        result['checks'].append('file_and_directory_rename_preserve_unhydrated_contents')
        original_upload = one.backend.upload
        def edit_during_upload(path, source, expected, cancelled):
            remote = original_upload(path, source, expected, cancelled)
            if path == 'race.txt': (one.root/'race.txt').write_text('上传期间的新修改', encoding='utf-8')
            return remote
        (one.root/'race.txt').write_text('准备上传的内容', encoding='utf-8')
        one.backend.upload = edit_during_upload
        one.cycle()
        assert (one.backend.root/'race.txt').read_text(encoding='utf-8') == '准备上传的内容'
        race_state = one.native.state(one.root/'race.txt')
        assert race_state is None or not race_state['insync']
        one.backend.upload = original_upload
        one.cycle()
        assert (one.backend.root/'race.txt').read_text(encoding='utf-8') == '上传期间的新修改'
        result['checks'].append('concurrent_local_edit_stays_dirty_and_uploads_next_cycle')
        previous = two
        previous.close()
        two = TaskEngine(previous.task, previous.directory, previous.backend)
        engines[1] = two
        if '--shell' in sys.argv:
            repeated = subprocess.run(command, capture_output=True, text=True, timeout=45)
            assert repeated.returncode == 0, repeated.stderr
        two.cycle()
        assert two.native.state(two.root/'cloud.txt')['pin'] == 1
        result['checks'].append('restart_preserves_task_state_and_explicit_policy')
        two.native.pin('cloud.txt', 2)
        two.cycle(); two.cycle()
        assert two.task['overrides']['cloud.txt'] == 'online_only'
        assert two.native.state(two.root/'cloud.txt')['partial']
        two.task['overrides']['cloud.txt'] = 'always_keep'
        two.cycle()
        assert two.native.state(two.root/'cloud.txt')['pin'] == 1
        result['checks'].append('shell_release_persists_and_later_ui_override_wins')
        (two.root/'failed.txt').write_text('不能丢失的待上传内容', encoding='utf-8')
        original_upload = two.backend.upload
        def failed_upload(*args): raise OSError('injected upload failure')
        two.backend.upload = failed_upload
        try:
            two.cycle()
            raise AssertionError('Upload failure was ignored')
        except OSError: pass
        assert (two.root/'failed.txt').read_text(encoding='utf-8') == '不能丢失的待上传内容'
        assert not two.native.state(two.root/'failed.txt')['insync']
        try:
            two.native.dehydrate('failed.txt')
            raise AssertionError('Dirty file was released')
        except RuntimeError: pass
        assert two.state['pending'] is not None
        two.backend.upload = original_upload
        two.cycle()
        assert two.state['pending'] is None and two.native.state(two.root/'failed.txt')['partial']
        result['checks'].append('upload_failure_preserves_dirty_content_and_recovers')
        original_snapshot = two.backend.snapshot
        two.backend.snapshot = lambda: {}
        before = set(two.state['items'])
        try:
            two.cycle()
            raise AssertionError('Empty unhealthy listing was accepted')
        except RuntimeError: pass
        assert set(two.state['items']) == before and (two.root/'cloud.txt').exists()
        two.backend.snapshot = original_snapshot
        result['checks'].append('missing_cloud_health_stops_deletion')
        with (two.root/'cloud.txt').open('rb') as busy:
            busy.read(1)
            two.native.pin('cloud.txt', 0)
            try:
                two.native.dehydrate('cloud.txt')
                raise AssertionError('Open file was released')
            except OSError: pass
        result['checks'].append('open_file_rejects_dehydration')
        (two.backend.root/'collision.txt').write_text('云端冲突版本', encoding='utf-8')
        (two.root/'collision.txt').write_text('本地冲突版本', encoding='utf-8')
        two.cycle(); two.cycle()
        copies = list(two.root.glob('collision.conflict-local-*.txt'))
        assert len(copies) == 1 and copies[0].read_text(encoding='utf-8') == '本地冲突版本'
        assert two.native.state(copies[0])['pin'] == 1
        assert not two.native.state(copies[0])['partial']
        result['checks'].append('conflict_copy_stays_offline_even_in_online_task')
        (two.backend.root/'cancel.txt').write_text('暂停下载测试', encoding='utf-8')
        two.cycle()
        original_download = two.backend.download
        entered = threading.Event()
        def waiting_download(path, expected, destination, cancelled):
            entered.set()
            if cancelled.wait(10): raise InterruptedError('injected pending download cancelled')
            return original_download(path, expected, destination, cancelled)
        two.backend.download = waiting_download
        reader = subprocess.Popen([sys.executable, '-c', 'import pathlib,sys;pathlib.Path(sys.argv[1]).read_bytes()',
            str(two.root/'cancel.txt')], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            assert entered.wait(3)
            two.cancel.set()
            reader.communicate(timeout=5)
            assert reader.returncode != 0
        finally:
            if reader.poll() is None: reader.kill(); reader.communicate()
            two.backend.download = original_download
        assert two.native.state(two.root/'cancel.txt')['partial']
        two.close()
        two = TaskEngine(two.task, two.directory, two.backend); engines[1] = two
        two.cycle()
        assert external_read(two.root/'cancel.txt') == sha1(two.backend.root/'cancel.txt')
        result['checks'].append('task_pause_cancels_native_read_and_resume_retries')
        for engine in engines:
            engine.task['policy'] = 'always_keep'; engine.task['overrides'] = {}
            for row in engine.state['items'].values(): row.pop('shell_policy', None)
            engine.cycle()
            for path in sorted(engine.state['items'], key=lambda p: -p.count('/')):
                if engine.native.path(path).exists(): engine.native.revert(path)
            engine.close()
            if engine.task['id'] in shell_registered:
                removed = subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(workspace/'register-sync-root.ps1'),
                    '-Action','Unregister','-Root',str(engine.root),'-TaskId',engine.task['id']],capture_output=True,text=True,timeout=30)
                assert removed.returncode == 0, removed.stderr
            else: engine.native.unregister()
        result['checks'].append('both_roots_drained_and_unregistered')
        result['ok'] = True
    except Exception as error:
        result['error'] = repr(error)
        raise
    finally:
        for engine in engines: engine.close()
        atomic_write_json(directory/'result.json', result, indent=2)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__': main()
