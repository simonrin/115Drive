"""Detach locally: retain downloaded files/history, discard clean cloud stubs."""
import json
import os
from pathlib import Path
import subprocess
import time
from cloud_files import CloudFiles
from runtime_json import atomic_write_json
from sync_tasks import placeholder_stat, relative_path
from sync_state_store import DATABASE, StateStore, load_state


def shell_unregister(root, task):
    completed = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', str(Path(root)/'register-sync-root.ps1'), '-Action', 'Unregister',
        '-Root', task['local_path'], '-TaskId', task['id']], capture_output=True,
        timeout=45, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if completed.returncode:
        raise RuntimeError('资源管理器同步入口尚未解除，请稍后重试删除任务。')


def detach(root, task, directory, *, native_factory=CloudFiles, unregister_shell=shell_unregister):
    directory, local = Path(directory), Path(task['local_path'])
    state_path, journal = directory/'state.json', directory/'removal.json'
    state = load_state(directory) if state_path.exists() or (directory/DATABASE).exists() else None
    if state is not None:
        if state.get('task') != task['id'] or state.get('local') != str(local) or state.get('cloud') != task['cloud_path']:
            raise ValueError('任务同步基线与目录不一致，不能删除任务。')
        if state.get('pending'):
            raise ValueError('任务有未完成的传输，请恢复并完成处理后，再暂停删除任务。')
        if not local.is_dir() or local.stat().st_ino != state.get('local_root_id'):
            raise ValueError('本地同步目录已丢失或被替换，不能解除原目录管理。')
    for path in (directory/'remote-transactions').glob('*.json'):
        tx = json.loads(path.read_text(encoding='utf-8'))
        if tx.get('kind') == 'delete':
            expected_id = tx.get('expected', {}).get('id')
            finished = bool(state and expected_id and not any(row.get('remote', {}).get('id') == expected_id for row in state.get('items', {}).values()))
        else:
            finished = tx.get('committed') or tx.get('aborted') and tx.get('abort_complete')
        if not finished:
            raise ValueError('任务仍有未完成的云端事务，请恢复处理完成后再暂停删除。')
    if (directory/DATABASE).exists():
        # Paused task removal must preserve the current canonical history even
        # if the last process exited before its compatibility export.
        store = StateStore(directory)
        try: store.load(); store.export()
        finally: store.close(export=False)
    native = native_factory(local, task['id'], None) if local.exists() else None
    root_id = local.stat().st_ino if native else None
    owned = native.registered() if native else False

    def scan():
        if local.stat().st_ino != root_id: raise ValueError('本地目录身份发生变化，已停止解除同步。')
        result = []
        def failed(error): raise error
        for parent, dirs, files in os.walk(local, followlinks=False, onerror=failed):
            for name in dirs + files:
                path = Path(parent)/name
                relative = relative_path(path.relative_to(local).as_posix())
                attrs = placeholder_stat(path)
                # An ordinary folder on a volume without a registered root can
                # return ERROR_INVALID_FUNCTION for CfGetPlaceholderInfo.
                # Its non-reparse files are already ordinary; do not query them.
                item = native.state(path) if owned or getattr(attrs, 'st_file_attributes', 0) & 0x400 else None
                if item:
                    if not owned or item['identity'].get('task') != task['id']:
                        raise ValueError('目录含其他任务的云文件，不能解除同步。')
                    if item['partial'] and (not item['insync'] or item['modified'] or
                            item['identity'].get('kind') != 'file' or item['identity'].get('size') != attrs.st_size):
                        raise ValueError('未完整下载的文件含未同步修改，已保留本机数据，请处理后再删除任务。')
                    result.append(relative)
                elif getattr(attrs, 'st_file_attributes', 0) & 0x400:
                    raise ValueError('目录含其他重解析点，不能安全解除同步。')
        return result

    entries = scan() if native else []
    # This durable marker prevents resume after an interruption midway through detach.
    record = json.loads(journal.read_text(encoding='utf-8')) if journal.exists() else dict(version=1, task=task, started=time.time())
    if record.get('task') != task: raise ValueError('任务删除记录与当前设置不一致。')
    atomic_write_json(journal, dict(record, phase='detaching'), indent=2, durable=True)
    for relative in sorted(entries, key=lambda p: p.count('/'), reverse=True):
        if local.stat().st_ino != root_id: raise ValueError('本地目录身份发生变化。')
        item = native.state(native.path(relative))
        if item is None: continue
        if item['identity'].get('task') != task['id']: raise ValueError('云文件身份发生变化。')
        # No provider is connected: never hydrate a cloud stub to detach it.
        # The native operation rechecks ownership/dirty state on the exact
        # exclusive handle it deletes. If hydrated meanwhile, preserve it.
        if item['partial'] and native.discard_online(relative): continue
        native.revert(relative)
    if native:
        if scan(): raise ValueError('仍有云文件尚未解除，请重试删除任务。')
        # Do not let Windows silently skip failures or delete unknown placeholders.
        if owned: native.unregister()
    unregister_shell(root, task)
    atomic_write_json(journal, dict(record, phase='detached', finished=time.time()), indent=2, durable=True)
