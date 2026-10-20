"""Verified local-folder relocation. The source and bisync listings are retained."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import threading
import time

from local_paths import configure_mapping


class Cancelled(RuntimeError):
    pass


def atomic_bytes(path, data):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(4) + '.tmp')
    temporary.write_bytes(data)
    os.replace(temporary, path)


def ordinary_path(host, path):
    for part in [path, *path.parents]:
        if part.exists() and (host.is_reparse(part) or part.is_symlink()):
            raise ValueError('不能使用符号链接或目录联接，请选择普通本地文件夹。')


def inventory(host, root):
    ordinary_path(host, root)
    result = {}
    def fail(error):
        raise error
    for parent, dirs, files in os.walk(root, onerror=fail, followlinks=False):
        for name in dirs + files:
            path = Path(parent) / name
            if host.is_reparse(path) or path.is_symlink():
                raise ValueError('目录中含有符号链接或目录联接，无法完整复制：' + str(path))
            st = path.stat()
            if not path.is_dir() and not path.is_file():
                raise ValueError('目录含有不支持的文件：' + str(path))
            result[path.relative_to(root).as_posix()] = ('dir', 0, 0) if path.is_dir() else ('file', st.st_size, st.st_mtime_ns)
    return result


class FolderChanger:
    def __init__(self, host):
        self.t = host
        self.cancel_event = threading.Event()
        self.state_lock = threading.Lock()
        self.committing = False

    @property
    def guard(self):
        return self.t.PRIVATE / 'folder-change-active.json'

    def view(self):
        try:
            return self.t.read_json(self.t.ROOT / 'runtime/folder-change.json')
        except (OSError, ValueError):
            return dict(state='idle', message='尚未更改同步文件夹')

    def publish(self, state, message, **extra):
        self.t.write_json(self.t.ROOT / 'runtime/folder-change.json', dict(state=state, message=message, **extra))

    def checked(self, target):
        cfg = self.t.read_json(self.t.CONFIG)
        marker = self.t.read_json(self.t.PRIVATE / 'initialized.json')
        if (marker['local'], marker['remote']) != (cfg['local_path'], cfg['remote_path']):
            raise ValueError('当前配置与同步历史不一致，请先修复后更改目录。')
        if not cfg.get('local_hash_cache'):
            raise ValueError('当前同步历史不支持直接切换目录。')
        history = self.t.PRIVATE / 'bisync-cached'
        stem = 'localhash_115Drive..cloud115_115Drive'
        if not all((history / (stem + suffix)).is_file() for suffix in ('.path1.lst', '.path2.lst')):
            raise ValueError('同步历史尚未就绪，请先完成一次同步。')
        if list(history.glob('*.lck')):
            raise ValueError('本轮同步尚未结束，请稍后检查目标。')
        # Rclone retains older error listings even after a successful recovery.
        latest_pair = min((history / (stem + suffix)).stat().st_mtime_ns for suffix in ('.path1.lst', '.path2.lst'))
        if any(p.stat().st_mtime_ns > latest_pair for p in history.glob(stem+'*.lst-err')):
            raise ValueError('同步历史需要恢复，请先完成一次正常同步再更改目录。')
        raw = str(target).strip()
        target = Path(raw)
        if not raw or not target.is_absolute() or not target.drive or target.drive.startswith('\\'):
            raise ValueError('请输入本地磁盘的完整路径，例如 D:\\115Drive。')
        if any(ord(c) < 32 or c in '<>"|?*' for c in raw) or ':' in raw[2:]:
            raise ValueError('文件夹路径含有无效字符。')
        if any(part.endswith((' ', '.')) for part in target.parts[1:]) or target.is_reserved():
            raise ValueError('文件夹名称不能以空格或点结尾，也不能使用系统保留名称。')
        ordinary_path(self.t, target)
        target = target.resolve()
        source = Path(cfg['local_path']).resolve()
        if target == Path(target.anchor):
            raise ValueError('请选择磁盘下的独立文件夹，不要选择磁盘根目录。')
        for protected in (source, self.t.ROOT.resolve()):
            if target == protected or target.is_relative_to(protected) or protected.is_relative_to(target):
                raise ValueError('新目录不能与当前同步目录或程序目录重叠。')
        if not source.is_dir() or not (source / self.t.SENTINEL).is_file():
            raise ValueError('当前同步目录或健康标记缺失，不能切换。')
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ValueError('请选择空文件夹或尚未创建的文件夹；不会合并或覆盖已有内容。')
        parent = target
        while not parent.exists():
            if parent == parent.parent:
                raise ValueError('目标磁盘不可访问。')
            parent = parent.parent
        if not parent.is_dir():
            raise ValueError('目标路径的上级不是文件夹。')
        entries = inventory(self.t, source)
        size = sum(item[1] for item in entries.values())
        free = shutil.disk_usage(parent).free
        if free < size + 16 * 1024 * 1024:
            raise ValueError('目标磁盘剩余空间不足，复制时需要保留完整旧目录。')
        return cfg, source, target, entries, free

    def preview(self, target):
        cfg, source, target, entries, free = self.checked(target)
        return dict(source=str(source), target=str(target), files=sum(x[0]=='file' for x in entries.values()),
                    bytes=sum(x[1] for x in entries.values()), free_bytes=free, remote='/115Drive')

    def cancel(self):
        with self.state_lock:
            if not self.guard.exists() or self.committing:
                return False
            self.cancel_event.set()
            return True

    def check_cancel(self):
        if self.cancel_event.is_set():
            raise Cancelled('已取消更改，继续使用原同步文件夹。')

    @contextmanager
    def quiesce(self, operation):
        # A running worker acknowledges only AFTER its active sync has ended.
        # If no worker owns the lock, retain it for the entire transaction.
        deadline = time.monotonic() + 600
        while True:
            self.check_cancel()
            lock = self.t.worker_lock()
            try:
                lock.__enter__()
            except RuntimeError as error:
                if 'already running' not in str(error):
                    raise
            else:
                try:
                    yield
                finally:
                    lock.__exit__(None, None, None)
                return
            try:
                ack = self.t.read_json(self.t.PRIVATE / 'folder-change-ack.json')
            except (OSError, ValueError):
                ack = {}
            if ack.get('id') == operation:
                yield
                return
            if time.monotonic() > deadline:
                raise RuntimeError('等待当前传输结束超时，原同步目录未更改；稍后可以重试。')
            time.sleep(.25)

    def config_files(self):
        return {'config.json': self.t.CONFIG, 'rclone.conf': self.t.PRIVATE / 'rclone.conf',
                'initialized.json': self.t.PRIVATE / 'initialized.json',
                'local-snapshot.json': self.t.PRIVATE / 'local-snapshot.json'}

    def restore(self, folder, journal):
        for name, path in self.config_files().items():
            if name in journal['saved']:
                atomic_bytes(path, (folder / name).read_bytes())
            else:
                path.unlink(missing_ok=True)

    def recover(self):
        """After a console/system crash, the worker remains gated until recovery."""
        if not self.guard.exists():
            return
        guard = self.t.read_json(self.guard)
        operation = guard['id']
        if len(operation) != 32 or any(c not in '0123456789abcdef' for c in operation):
            raise RuntimeError('无效的目录切换记录；同步保持暂停。')
        folder = self.t.PRIVATE / 'folder-changes' / operation
        journal = self.t.read_json(folder / 'journal.json')
        if journal['stage'] == 'committing':
            self.restore(folder, journal)
        completed = journal['stage'] == 'committed'
        self.publish('complete' if completed else 'error',
                     '目录切换已完成。' if completed else '上次更改意外中断，已恢复原目录；已复制的文件保留在目标目录。')
        self.guard.unlink()

    def change(self, target):
        if self.guard.exists():
            raise RuntimeError('已有未完成的目录切换，请重新打开控制台恢复。')
        self.cancel_event.clear()
        self.committing = False
        operation = secrets.token_hex(16)
        folder = self.t.PRIVATE / 'folder-changes' / operation
        folder.mkdir(parents=True)
        journal = dict(stage='waiting', saved=[])
        self.t.write_json(folder / 'journal.json', journal)
        self.t.write_json(self.guard, dict(id=operation))
        self.publish('running', '等待当前同步安全结束…', step='waiting', cancellable=True)
        try:
            with self.quiesce(operation):
                cfg, source, target, entries, _ = self.checked(target)
                for name, path in self.config_files().items():
                    if path.exists():
                        shutil.copy2(path, folder / name)
                        journal['saved'].append(name)
                # Read-only backup, never rename/delete the working bisync history.
                shutil.copytree(self.t.PRIVATE / 'bisync-cached', folder / 'bisync-cached')
                journal.update(stage='copying', source=str(source), target=str(target))
                self.t.write_json(folder / 'journal.json', journal)
                target.mkdir(parents=True, exist_ok=True)
                ordinary_path(self.t, target)
                if any(target.iterdir()):
                    raise ValueError('目标文件夹不再为空，请重新选择。')
                hashes = {}
                total = sum(item[1] for item in entries.values())
                done = 0
                last_update = 0
                for name, item in entries.items():
                    self.check_cancel()
                    src, dest = source / name, target / name
                    ordinary_path(self.t, src)
                    ordinary_path(self.t, dest)
                    if item[0] == 'dir':
                        dest.mkdir(exist_ok=True)
                        continue
                    digest = hashlib.sha256()
                    with src.open('rb') as reader, dest.open('xb') as writer:
                        while chunk := reader.read(4 * 1024 * 1024):
                            self.check_cancel()
                            writer.write(chunk)
                            digest.update(chunk)
                            done += len(chunk)
                            if time.monotonic() - last_update > .25:
                                self.publish('running', '正在复制到新目录…', step='copying', name=name,
                                             bytes=done, total_bytes=total, cancellable=True)
                                last_update = time.monotonic()
                        writer.flush()
                        os.fsync(writer.fileno())
                    shutil.copystat(src, dest)
                    hashes[name] = digest.hexdigest()
                self.publish('running', '正在校验源文件和目标文件内容…', step='verifying', cancellable=True)
                for name, expected in hashes.items():
                    self.check_cancel()
                    for path in (source / name, target / name):
                        ordinary_path(self.t, path)
                        digest = hashlib.sha256()
                        with path.open('rb') as stream:
                            while chunk := stream.read(4 * 1024 * 1024):
                                self.check_cancel()
                                digest.update(chunk)
                        if digest.hexdigest() != expected:
                            raise RuntimeError('复制期间文件发生变化或校验失败，请关闭正在编辑的文件后重试：' + name)
                if inventory(self.t, source) != entries or inventory(self.t, target) != entries:
                    raise RuntimeError('复制期间目录内容发生变化，已保留原目录，请稍后重试。')
                with self.state_lock:
                    self.check_cancel()
                    self.committing = True
                journal['stage'] = 'committing'
                self.t.write_json(folder / 'journal.json', journal)
                self.publish('running', '校验通过，正在切换同步目录…', step='committing', cancellable=False)
                new_cfg = dict(cfg, local_path=str(target), local_namespace='virtual_v1', local_cache_id=operation)
                mapping = configure_mapping((folder / 'rclone.conf').read_text(encoding='utf-8-sig'), new_cfg, self.t.ROOT)
                atomic_bytes(self.t.PRIVATE / 'rclone.conf', mapping.encode('utf-8'))
                marker = self.t.read_json(folder / 'initialized.json')
                marker['local'] = str(target)
                self.t.write_json(self.t.PRIVATE / 'initialized.json', marker)
                if 'local-snapshot.json' in journal['saved']:
                    snapshot = self.t.read_json(folder / 'local-snapshot.json')
                    snapshot['local'] = str(target)
                    self.t.write_json(self.t.PRIVATE / 'local-snapshot.json', snapshot)
                self.t.write_json(self.t.CONFIG, new_cfg)
                journal['stage'] = 'committed'
                self.t.write_json(folder / 'journal.json', journal)
                self.publish('complete', '已切换到新目录，原目录保留为副本。', source=str(source), target=str(target))
        except Exception as error:
            if journal['stage'] == 'committed':
                raise  # Configuration committed; a reporting failure must not undo it.
            if journal['stage'] == 'committing':
                self.restore(folder, journal)
            self.publish('cancelled' if isinstance(error, Cancelled) else 'error',
                         str(error) + ' 已复制的目标文件会保留，原目录未删除。')
            journal['stage'] = 'rolled_back'
            self.t.write_json(folder / 'journal.json', journal)
        finally:
            # Restoration errors leave the guard in place for startup recovery.
            if journal['stage'] in ('committed', 'rolled_back'):
                self.guard.unlink(missing_ok=True)
            self.committing = False
