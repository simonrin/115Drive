"""Apply Explorer's localized pin commands through the existing task policy."""
from pathlib import Path
from cloud_files import CloudFiles, check
from sync_tasks import POLICIES, placeholder_stat, relative_path


def apply_policy(store, body, *, native_factory=CloudFiles):
    policy, value = body.get('policy'), body.get('path')
    if policy not in POLICIES or not isinstance(value, str): raise ValueError('无效的文件保留策略。')
    path = Path(value)
    if not path.is_absolute() or not path.drive or path.drive.startswith('\\'):
        raise ValueError('请选择此电脑上的同步文件或文件夹。')
    relative_path('/'.join(path.parts[1:]))
    data = store.read()
    task = next((t for t in data['tasks'] if path.is_relative_to(Path(t['local_path']))), None)
    if task is None or store.removing(task['id']): raise ValueError('此位置没有可用的 115Drive 同步任务。')
    root = Path(task['local_path'])
    relative = path.relative_to(root).as_posix()
    if relative == '.': relative = ''
    native = native_factory(root, task['id'], None)
    if not native.registered(): raise ValueError('同步文件夹尚未启动，请先启动任务。')
    # Check every selected component without following another provider's link.
    for candidate in reversed([path, *path.parents]):
        if candidate == root or not candidate.is_relative_to(root): continue
        attrs = placeholder_stat(candidate)
        if attrs.st_file_attributes & 0x400:
            state = native.state(candidate)
            if not state or state['identity'].get('task') != task['id']:
                raise ValueError('所选位置包含其他任务的链接或云文件。')
    placeholder_stat(path)  # Reject moved/deleted selections before saving.
    operation = 'override' if relative else 'preferences'
    request = dict(revision=data['revision'], id=task['id'], policy=policy)
    if relative: request['path'] = relative
    result = store.mutate(operation, request)
    # Match the native Explorer action: recursive pin/unpin, no immediate
    # deletion or network transfer here. The engine verifies the cloud version
    # before dehydration and preserves unsynced local data.
    with native.handle(path, write=False) as handle:
        check(native.dll.CfSetPinState(handle, 1 if policy == 'always_keep' else 2, 1, None), '设置文件保留状态')
    return dict(result, paused=task['paused'])
