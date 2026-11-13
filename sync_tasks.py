"""Independent sync-task configuration; no credentials or legacy-history migration."""
from __future__ import annotations
import contextlib
import copy
import ctypes
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import threading
import uuid
from runtime_json import atomic_write_json

POLICIES = ('always_keep', 'online_only')
_LOCK = threading.RLock()


def legacy_enabled(root):
    try: value = json.loads((Path(root)/'config.json').read_text(encoding='utf-8-sig')).get('legacy_enabled', True)
    except FileNotFoundError: return True
    if type(value) is not bool: raise ValueError('原同步任务启用标记损坏。')
    return value


def placeholder_stat(path):
    """Python may disguise cloud reparse tags; expose them on this thread only."""
    if os.name != 'nt': return Path(path).lstat()
    mode = ctypes.WinDLL('ntdll').RtlSetThreadPlaceholderCompatibilityMode
    mode.argtypes, mode.restype = [ctypes.c_int8], ctypes.c_int8
    old = mode(2)
    if old < 0: raise RuntimeError('无法读取云文件属性。')
    try: return Path(path).lstat()
    finally: mode(old)


@contextlib.contextmanager
def file_lock(path):
    """Nonblocking process lock; held handle, never PID files, owns the lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def relative_path(value, *, empty=False):
    if not isinstance(value, str) or (not value and not empty):
        raise ValueError('文件路径不能为空。')
    if not value and empty:
        return ''
    value = value.replace('\\', '/')
    parts = value.split('/')
    if value.startswith('/') or any(not part or part in ('.', '..') or
            part.endswith((' ', '.')) or PureWindowsPath(part).is_reserved() or
            any(ord(c) < 32 or c in '<>:"|?*' for c in part) for part in parts):
        raise ValueError('文件路径无法安全映射到 Windows。')
    return '/'.join(parts)


def cloud_path(value):
    if not isinstance(value, str) or not value.startswith('/'):
        raise ValueError('请输入云端绝对目录，例如 /资料。')
    relative = relative_path(value.rstrip('/')[1:])
    if re.fullmatch(r'115Drive 同步历史-[0-9a-f]{32}', relative.split('/')[0], re.IGNORECASE):
        raise ValueError('此目录是同步历史保留区，不能作为同步任务，以免重新同步已删除文件。')
    return '/' + relative


def local_path(value):
    if not isinstance(value, str):
        raise ValueError('请输入本地完整目录。')
    p = PureWindowsPath(value)
    if not p.is_absolute() or str(p.drive).startswith('\\') or len(p.parts) < 2:
        raise ValueError('请选择本地磁盘下的完整目录。')
    relative_path('/'.join(p.parts[1:]))
    path = Path(str(p))
    for parent in (path, *path.parents):
        if parent.exists() or parent.is_symlink():
            if placeholder_stat(parent).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                # Existing CfAPI roots are checked by the engine, not accepted as new roots.
                raise ValueError('同步目录不能包含链接、重定向或其他云盘目录。')
            if not parent.is_dir():
                raise ValueError('本地路径不是文件夹。')
    if not Path(path.anchor).is_dir():
        raise ValueError('本地磁盘不可访问。')
    return str(path.absolute())


def overlaps(a, b, *, local=False):
    cls = PureWindowsPath if local else PurePosixPath
    a, b = cls(a.casefold()), cls(b.casefold())
    return a == b or a in b.parents or b in a.parents


def validate_id(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise ValueError('无效的同步任务标识。')
    return value


def effective_policy(task, path):
    path = relative_path(path, empty=True).casefold()
    rules = {k.casefold(): v for k, v in task.get('overrides', {}).items()}
    while path:
        if path in rules:
            return rules[path]
        path = path.rpartition('/')[0]
    return task['policy']


class TaskStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.private = self.root / 'runtime/private'
        self.path = self.private / 'sync-tasks.json'

    def directory(self, task_id):
        return self.private / 'tasks' / validate_id(task_id)

    def removing(self, task_id):
        return (self.directory(task_id) / 'removal.json').exists()

    def remove(self, body, detach, *, worker_stopped=lambda task_id: True):
        with _LOCK, file_lock(self.private / 'sync-tasks-config.lock'):
            data = self.read()
            if type(body.get('revision')) is not int or body['revision'] != data['revision']:
                raise ValueError('任务设置已更新，请刷新后重试。')
            task_id = validate_id(body.get('id'))
            task = next((t for t in data['tasks'] if t['id'] == task_id), None)
            if task is None: raise ValueError('同步任务不存在。')
            if not task['paused'] or not worker_stopped(task_id):
                raise ValueError('请先暂停任务，等待暂停完成后再删除。')
            with file_lock(self.directory(task_id) / 'worker.lock'):
                # The per-task lock also excludes another process's worker.
                detach(copy.deepcopy(task), self.directory(task_id))
                data['tasks'] = [t for t in data['tasks'] if t['id'] != task_id]
                data['revision'] += 1
                atomic_write_json(self.path, data, indent=2, durable=True)
            return dict(ok=True, revision=data['revision'], id=task_id)

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            return {'version': 1, 'revision': 0, 'tasks': []}
        if not isinstance(data, dict) or data.get('version') != 1 or type(data.get('revision')) is not int or not isinstance(data.get('tasks'), list):
            raise ValueError('多任务配置损坏，已停止读取。')
        ids = set()
        for task in data['tasks']:
            if not isinstance(task, dict) or validate_id(task.get('id')) in ids:
                raise ValueError('同步任务标识重复或损坏。')
            ids.add(task['id'])
            if task.get('account') != '115' or task.get('policy') not in POLICIES or type(task.get('paused')) is not bool:
                raise ValueError('同步策略配置损坏。')
            if not isinstance(task.get('name'), str) or not task['name'].strip():
                raise ValueError('任务名称缺失。')
            candidate = PureWindowsPath(task.get('local_path', ''))
            if not candidate.is_absolute() or str(candidate.drive).startswith('\\\\') or len(candidate.parts) < 2:
                raise ValueError('任务本地目录损坏。')
            relative_path('/'.join(candidate.parts[1:]))
            if overlaps(str(candidate), str(self.root), local=True):
                raise ValueError('任务目录与程序目录重叠。')
            if cloud_path(task.get('cloud_path')) != task['cloud_path']:
                raise ValueError('任务云端目录损坏。')
            if type(task.get('cache_hours')) is not int or not 0 <= task['cache_hours'] <= 8760:
                raise ValueError('缓存时间无效。')
            if not isinstance(task.get('overrides'), dict):
                raise ValueError('文件策略损坏。')
            keys = set()
            for path, policy in task['overrides'].items():
                relative_path(path)
                if policy not in POLICIES or path.casefold() in keys:
                    raise ValueError('文件策略冲突。')
                keys.add(path.casefold())
        for i, a in enumerate(data['tasks']):
            legacy = self.legacy()
            if legacy and (overlaps(a['local_path'], legacy['local_path'], local=True) or overlaps(a['cloud_path'], legacy['cloud_path'])):
                raise ValueError('任务目录与原有同步任务重叠。')
            for b in data['tasks'][i+1:]:
                if overlaps(a['local_path'], b['local_path'], local=True) or overlaps(a['cloud_path'], b['cloud_path']):
                    raise ValueError('同步任务目录重叠。')
        return data

    def legacy(self):
        if not legacy_enabled(self.root): return None
        try:
            config = json.loads((self.root / 'config.json').read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            return None
        if not (self.private / 'initialized.json').exists():
            return None
        return {'id': 'legacy', 'name': '原有同步任务', 'local_path': config['local_path'],
                'cloud_path': '/' + config['remote_path'].split(':', 1)[1],
                'policy': 'always_keep', 'engine': 'bisync'}

    def view(self):
        data = self.read()
        rows = copy.deepcopy(data['tasks'])
        for task in rows:
            try:
                task['status'] = json.loads((self.directory(task['id']) / 'status.json').read_text(encoding='utf-8'))
            except (FileNotFoundError, ValueError):
                task['status'] = {'state': 'paused' if task['paused'] else 'waiting', 'message': '等待同步任务启动'}
        return dict(revision=data['revision'], tasks=rows, legacy=self.legacy())

    def mutate(self, operation, body):
        with _LOCK, file_lock(self.private / 'sync-tasks-config.lock'):
            data = self.read()
            if type(body.get('revision')) is not int or body['revision'] != data['revision']:
                raise ValueError('任务设置已更新，请刷新后重试。')
            if operation == 'create':
                name = body.get('name')
                if not isinstance(name, str) or not name.strip() or len(name) > 80:
                    raise ValueError('请输入 1–80 字的任务名称。')
                local, remote = local_path(body.get('local_path')), cloud_path(body.get('cloud_path'))
                if overlaps(local, str(self.root), local=True):
                    raise ValueError('同步目录不能与程序目录重叠。')
                for other in [*data['tasks'], *([self.legacy()] if self.legacy() else [])]:
                    if overlaps(local, other['local_path'], local=True) or overlaps(remote, other['cloud_path']):
                        raise ValueError('本地或云端目录与现有任务重叠；子目录请使用文件夹策略。')
                task = dict(id=uuid.uuid4().hex, account='115', name=name.strip(), local_path=local,
                            cloud_path=remote, policy=body.get('policy'), cache_hours=body.get('cache_hours', 24),
                            paused=True, overrides={})
                data['tasks'].append(task)
            else:
                task_id = validate_id(body.get('id'))
                task = next((t for t in data['tasks'] if t['id'] == task_id), None)
                if task is None:
                    raise ValueError('同步任务不存在。')
                if operation != 'pause' and self.removing(task_id):
                    raise ValueError('此任务正在解除同步管理，请重试删除任务。')
                if operation == 'preferences':
                    task['policy'] = body.get('policy')
                    task['cache_hours'] = body.get('cache_hours', task['cache_hours'])
                elif operation == 'override':
                    path = relative_path(body.get('path'))
                    for old in list(task['overrides']):
                        if old.casefold() == path.casefold(): del task['overrides'][old]
                    if body.get('policy') != 'inherit': task['overrides'][path] = body.get('policy')
                elif operation in ('pause', 'resume'):
                    if operation == 'resume' and self.removing(task_id):
                        raise ValueError('此任务正在解除同步管理，请重试删除任务。')
                    task['paused'] = operation == 'pause'
                else:
                    raise ValueError('未知的任务操作。')
            if task['policy'] not in POLICIES or type(task['cache_hours']) is not int or not 0 <= task['cache_hours'] <= 8760 or any(v not in POLICIES for v in task['overrides'].values()):
                raise ValueError('请选择有效的保留策略和缓存时间（0–8760 小时）。')
            data['revision'] += 1
            atomic_write_json(self.path, data, indent=2, durable=True)
            return dict(ok=True, revision=data['revision'], id=task['id'])

    def set_all_paused(self, paused):
        if type(paused) is not bool: raise ValueError('无效的暂停状态。')
        with _LOCK, file_lock(self.private/'sync-tasks-config.lock'):
            data = self.read()
            eligible = [task for task in data['tasks'] if paused or not self.removing(task['id'])]
            if any(task['paused'] != paused for task in eligible):
                for task in eligible: task['paused'] = paused
                data['revision'] += 1
                atomic_write_json(self.path, data, indent=2, durable=True)
            return dict(ok=True, revision=data['revision'], tasks=len(data['tasks']))

    def shell_override(self, task_id, path, policy):
        # The OS and settings page share one durable override map. A later UI
        # change, including "inherit", therefore supersedes a shell selection.
        for _ in range(5):
            data = self.read()
            try:
                self.mutate('override', dict(revision=data['revision'], id=task_id, path=path, policy=policy))
                return next(t for t in self.read()['tasks'] if t['id'] == task_id)
            except ValueError:
                if self.read()['revision'] == data['revision']: raise
        raise RuntimeError('任务策略持续变化，请稍后重试。')
