"""Manifest-only installation, with durable program rollback and untouched user data."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid

from runtime_json import atomic_write_bytes, atomic_write_json
from sync_tasks import file_lock, relative_path, placeholder_stat

MANIFEST = 'install-manifest.json'
SINGLE_FILES = {'115Drive.exe', 'filters.txt', 'install-startup.ps1', 'install-tray.ps1',
                'set-startup.ps1', 'register-sync-root.ps1', 'register-shell-menu.ps1',
                'register-account-navigation.ps1', 'register-folder-badge.ps1',
                'README.md', 'LICENSE', 'PRIVACY.md', 'CONTRIBUTING.md', 'THIRD_PARTY_NOTICES.md'}
PREFIXES = ('service/', 'docs/', 'licenses/', 'third_party/')
OTHER_FILES = {'tray/Panel.xaml', 'tray/Settings.xaml', 'tray/keep-local.ico', 'tray/free-space.ico',
               'tray/115Drive-folder.ico', 'tray/FolderBadge.dll',
               'bin/openlist-v4.2.6/openlist.exe',
               'bin/rclone-v1.75.1/rclone-v1.75.1-windows-amd64/rclone.exe'}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def ordinary(path):
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        try: info = placeholder_stat(item)
        except FileNotFoundError: continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('安装路径不能包含联接、符号链接或云盘占位目录。')
    return path


def managed(name):
    name = relative_path(name)
    if name not in SINGLE_FILES | OTHER_FILES | {MANIFEST, 'installer-process.ps1'} and not name.startswith(PREFIXES):
        raise ValueError('安装清单包含非程序路径：' + name)
    return name


def payload_files(payload):
    payload = ordinary(payload)
    manifest = json.loads((payload/MANIFEST).read_text(encoding='utf-8-sig'))
    if manifest.get('product') != '115Drive' or manifest.get('format') != 1:
        raise ValueError('无法识别安装包。')
    files = manifest['files']
    if not isinstance(files, dict) or not {'115Drive.exe', 'service/TwoDrive.Service.exe'} <= files.keys():
        raise ValueError('安装包不完整。')
    seen = set()
    for name, checksum in files.items():
        managed(name)
        if name == MANIFEST or name.casefold() in seen: raise ValueError('安装路径重复。')
        seen.add(name.casefold())
        source = ordinary(payload/name)
        if not source.is_file() or digest(source) != checksum: raise ValueError('安装包校验失败：' + name)
    return dict(files, **{MANIFEST: digest(payload/MANIFEST)})


def recognize(root):
    root = ordinary(root)
    if root == Path(root.anchor) or str(root).startswith('\\\\'):
        raise ValueError('请选择本地磁盘中的程序文件夹。')
    if not root.exists() or not any(root.iterdir()): return False
    # Source workspaces use different entry points/startup tasks. Do not convert them implicitly.
    identity = ordinary(root/'runtime/private/installer-identity.json')
    ours = identity.exists() and json.loads(identity.read_text(encoding='utf-8')) == dict(product='115Drive',root=str(root))
    if not ours and (not (root/'115Drive.exe').is_file() or not (root/'service/TwoDrive.Service.exe').is_file()):
        raise ValueError('此目录不是安装版或便携版 115Drive。请选择原程序目录；源码工作目录不适用。')
    for name in ('config.json', 'runtime/private/sync-tasks.json'):
        path = ordinary(root/name)
        if not path.exists(): continue
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        paths = [value.get('local_path')] if name == 'config.json' else [t['local_path'] for t in value['tasks']]
        for local in filter(None, paths):
            local = Path(os.path.abspath(local))
            if local == root or local in root.parents or root in local.parents:
                raise ValueError('程序目录与同步文件夹重叠，已停止升级。')
    return True


def copy_atomic(source, target):
    ordinary(source); ordinary(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.115drive-install-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as output, Path(source).open('rb') as input_file:
            shutil.copyfileobj(input_file, output, 1024*1024)
            output.flush(); os.fsync(output.fileno())
        shutil.copystat(source, temporary)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Transaction:
    def __init__(self, root, directory, data):
        self.root, self.directory, self.data = root, directory, data
        self.marker = root/'runtime/private/installer-active.json'

    def save(self):
        atomic_write_json(self.directory/'journal.json', self.data, indent=2, durable=True)

    @classmethod
    def create(cls, root):
        directory = ordinary(root/'runtime/private/install-upgrades'/uuid.uuid4().hex)
        directory.mkdir(parents=True)
        tx = cls(root, directory, dict(format=1, root=str(root), phase='preparing', files=[], control={}))
        tx.save()
        atomic_write_json(tx.marker, {'transaction':directory.name}, durable=True)
        return tx

    @classmethod
    def pending(cls, root):
        marker = ordinary(root/'runtime/private/installer-active.json')
        if not marker.exists(): return None
        name = json.loads(marker.read_text(encoding='utf-8'))['transaction']
        if len(name) != 32 or any(c not in '0123456789abcdef' for c in name): raise ValueError('升级恢复记录损坏。')
        directory = ordinary(root/'runtime/private/install-upgrades'/name)
        data = json.loads(ordinary(directory/'journal.json').read_text(encoding='utf-8'))
        if data.get('format') != 1 or Path(data['root']) != root: raise ValueError('升级恢复目录不匹配。')
        for row in data['files']: managed(row['path'])
        return cls(root, directory, data)

    def prepare(self, payload, files):
        rows = []
        for name, checksum in sorted(files.items()):
            target = ordinary(self.root/name)
            if target.exists() and not target.is_file(): raise ValueError('程序文件位置被目录占用：' + name)
            # filters.txt is user-editable, including before the first manifest-based upgrade.
            if name == 'filters.txt' and target.exists(): continue
            old_hash = digest(target) if target.exists() else None
            if old_hash == checksum: continue
            if old_hash:
                backup = ordinary(self.directory/'old'/name)
                copy_atomic(target, backup)
                if digest(backup) != old_hash: raise OSError('旧程序备份校验失败。')
            rows.append(dict(path=name, old=old_hash, new=checksum))
        self.data.update(files=rows, phase='prepared'); self.save()

    def apply(self, payload, checkpoint=lambda event, tx: None):
        self.data['phase'] = 'applying'; self.save()
        checkpoint('before_replace', self)
        for row in self.data['files']:
            target = ordinary(self.root/row['path'])
            actual = digest(target) if target.exists() else None
            if actual != row['old']: raise OSError('升级期间程序文件发生变化，已停止：' + row['path'])
            copy_atomic(Path(payload)/row['path'], target)
            if digest(target) != row['new']: raise OSError('新程序校验失败。')
            checkpoint('after_replace', self)
        self.data['phase'] = 'committed'; self.save()
        checkpoint('after_commit', self)

    def rollback(self):
        if self.data['phase'] != 'committed':
            for row in reversed(self.data['files']):
                target = ordinary(self.root/row['path'])
                actual = digest(target) if target.exists() else None
                if actual == row['old']: continue
                if actual != row['new']: raise OSError('检测到升级后手动修改，保留文件和恢复记录：' + row['path'])
                if row['old']:
                    backup = ordinary(self.directory/'old'/row['path'])
                    if digest(backup) != row['old']: raise OSError('备份损坏，未覆盖程序。')
                    copy_atomic(backup, target)
                else: target.unlink()
            self.data['phase'] = 'rolled_back'; self.save()

    def finish(self):
        self.marker.unlink()


def install(payload, root, *, controller=None, checkpoint=lambda event, tx: None):
    payload = ordinary(payload); root = ordinary(root)
    if root == payload or root in payload.parents or payload in root.parents:
        raise ValueError('安装包临时目录不能与程序目录重叠。')
    files = payload_files(payload)  # Validate every byte before changing the old installation.
    existing = recognize(root)
    root.mkdir(parents=True, exist_ok=True)
    ordinary(root/'runtime/private/installer.lock')
    with file_lock(root/'runtime/private/installer.lock'):
        atomic_write_json(root/'runtime/private/installer-identity.json',dict(product='115Drive',root=str(root)),durable=True)
        pending = Transaction.pending(root)
        if pending:
            if controller: controller.quiet(pending, recovering=True)
            pending.rollback()
            if controller: controller.restore(pending)
            pending.finish()
            if controller: controller.remember(pending)
        tx = Transaction.create(root)
        try:
            if controller: controller.quiet(tx)
            checkpoint('after_quiet', tx)
            tx.prepare(payload, files)
            tx.apply(payload, checkpoint)
            if controller: controller.restore(tx)
            tx.finish()
        except Exception:
            tx.rollback()
            if controller: controller.restore(tx)
            tx.finish()
            if controller: controller.resume(tx)
            raise
        result = dict(ok=True, upgraded=existing, replaced=len(tx.data['files']), backup=str(tx.directory))
        if controller:
            try: controller.resume(tx)
            except Exception: result['restart_warning'] = '程序已更新，请手动打开 115Drive。'
        return result


def main(root, payload):
    from installer_process import WindowsController
    controller = WindowsController(Path(root))
    return install(payload, root, controller=controller)
