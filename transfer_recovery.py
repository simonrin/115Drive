"""Short-lived transfer intents and narrowly owned scratch-file cleanup.

No version archive, whole-tree resync or age-based deletion of user files.
An interrupted run remains pending until a real bisync succeeds.
"""
from __future__ import annotations
import json
import base64
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import time


def partial_suffix(run_id):
    # rclone limits --partial-suffix to 16 characters. Keep 48 random bits
    # and the existing *.partial exclusion; the full 128-bit intent stays local.
    return base64.urlsafe_b64encode(bytes.fromhex(run_id)[:6]).decode('ascii')+'.partial'


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def ordinary(path):
    st = Path(path).lstat()
    return not stat.S_ISLNK(st.st_mode) and not (
        getattr(st, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def root_identity(path):
    path = Path(path)
    if not path.is_dir() or not ordinary(path):
        raise RuntimeError('同步目录缺失或已重定向，暂不恢复传输。')
    # Check every ancestor too; a replaced junction must not redirect cleanup.
    if any(not ordinary(p) for p in path.parents):
        raise RuntimeError('同步目录的上级路径已重定向，暂不清理临时文件。')
    st = path.stat()
    return [st.st_dev, st.st_ino]


def checked_file(file, root):
    """Recheck the full deletion path, including directories changed mid-walk."""
    file, root = Path(file), Path(root)
    root_identity(root)
    try:
        file.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (ValueError, FileNotFoundError):
        raise RuntimeError('临时文件路径已改变，已停止清理。')
    current = file
    while current != root:
        if not ordinary(current):
            raise RuntimeError('临时文件路径包含重定向，已停止清理。')
        current = current.parent
    return file


def rclone_running():
    """Fail closed on an unqueryable rclone. Never kill an orphan transfer."""
    if os.name != 'nt':
        return False  # Unit tests/local fixtures; application targets Windows.
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
         "$p=@(Get-Process -Name rclone -ErrorAction SilentlyContinue); if($p.Count){exit 3}"],
        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    if result.returncode not in (0, 3):
        raise RuntimeError('无法确认传输进程是否结束，暂不恢复。')
    return result.returncode == 3


def server_process_running(executable):
    """A closed TCP port alone does not prove an upload server has exited."""
    if os.name != 'nt':
        return False
    result = subprocess.run(
        ['powershell.exe','-NoProfile','-NonInteractive','-Command',
         "$p=@(Get-Process -Name openlist -ErrorAction SilentlyContinue); "
         "foreach($s in $p){try{$v=$s.Path}catch{exit 3}; "
         "if(-not $v -or $v -eq $env:TWODRIVE_PROCESS_IMAGE){exit 3}}; exit 0"],
        env={**os.environ,'TWODRIVE_PROCESS_IMAGE':str(Path(executable).absolute())},
        capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=15)
    if result.returncode not in (0,3):
        raise RuntimeError('无法确认上传服务是否结束，暂不清理临时文件。')
    return result.returncode == 3


class TransferRecovery:
    def __init__(self, root, cfg, workdir, alive=None):
        self.root = Path(root)
        self.local = Path(cfg['local_path']).absolute()
        self.remote = cfg['remote_path']
        self.workdir = Path(workdir).absolute()
        self.path = self.root/'runtime/private/transfer-pending.json'
        self.report = self.root/'runtime/transfer-recovery.json'
        self.alive = alive or rclone_running
        self.record = None

    def load(self):
        if not self.path.exists():
            return None
        if not ordinary(self.path) or self.path.stat().st_size > 65536:
            raise RuntimeError('中断恢复记录无效，已保留文件并暂停。')
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            valid = (data['version'] == 1 and
                     re.fullmatch('[0-9a-f]{32}', data['id']) and
                     data['local'] == str(self.local) and data['remote'] == self.remote and
                     data['workdir'] == str(self.workdir) and
                     data['local_identity'] == root_identity(self.local) and
                     data['work_identity'] == root_identity(self.workdir))
        except (KeyError, ValueError, TypeError):
            valid = False
        if not valid:
            raise RuntimeError('中断恢复记录与当前同步目录不一致，已保留文件并暂停。')
        self.record = data
        return data

    def clean_partials(self):
        # The random suffix was persisted before rclone was started. Ordinary
        # *.partial files, old generations and redirected paths are untouched.
        suffix = partial_suffix(self.record['id'])
        count = total = 0
        def fail(error):
            raise error
        for parent, dirs, files in os.walk(self.local, followlinks=False, onerror=fail):
            dirs[:] = [d for d in dirs if ordinary(Path(parent)/d)]
            for name in files:
                file = Path(parent)/name
                if name.endswith(suffix) and ordinary(file) and file.is_file():
                    if root_identity(self.local) != self.record['local_identity']:
                        raise RuntimeError('清理期间同步目录已被替换。')
                    size = file.stat().st_size
                    checked_file(file,self.local).unlink()
                    count += 1
                    total += size
        return count, total

    def prepare(self):
        if self.load() is None:
            return False
        if self.alive():
            raise RuntimeError('上次的 rclone 传输进程仍在运行，等待结束后恢复。')
        # Rclone --recover uses its saved listings. Only release this run's
        # stale lock after confirming no rclone process can still own it.
        locks = 0
        checked_locks = []
        for file in self.workdir.glob('*.lck'):
            if not ordinary(file) or not file.is_file():
                raise RuntimeError('同步锁路径异常，已停止恢复。')
            if root_identity(self.workdir) != self.record['work_identity']:
                raise RuntimeError('同步历史目录已替换，已停止恢复。')
            try:
                lock = json.loads(file.read_text())
                owned_process = (str(lock['PID']) == str(self.record['pid']) and
                                 Path(lock['Session']).absolute() == file.with_suffix('').absolute())
            except (ValueError, KeyError, TypeError):
                owned_process = False
            if file.name not in self.record.get('lock_names', []) and not owned_process:
                raise RuntimeError('发现不属于上次传输的同步锁，已停止恢复。')
            checked_locks.append(file)
        count, size = self.clean_partials()
        for file in checked_locks:
            checked_file(file,self.workdir).unlink()
            locks += 1
        save(self.report, dict(state='retrying', cleaned_files=count, cleaned_bytes=size,
                               released_locks=locks, updated_at=time.time()))
        return True

    def begin(self, args):
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.record = dict(version=1, id=secrets.token_hex(16), local=str(self.local),
                           remote=self.remote, local_identity=root_identity(self.local),
                           workdir=str(self.workdir), work_identity=root_identity(self.workdir),
                           lock_names=[], started_at=time.time())
        # Bisync derives .lck names from the listing base. Existing listing
        # names identify the authorized pair without copying/deleting history.
        self.record['lock_names'] = sorted({
            f.name.split('.path1.lst', 1)[0]+'.lck'
            for f in self.workdir.glob('*.path1.lst*') if ordinary(f)
        })
        save(self.path, self.record)
        return list(args) + ['--partial-suffix', partial_suffix(self.record['id'])]

    def started(self, process):
        self.record['pid'] = process.pid
        save(self.path, self.record)

    def complete(self):
        count, size = self.clean_partials()
        self.path.unlink()
        save(self.report, dict(state='complete', cleaned_files=count, cleaned_bytes=size,
                               updated_at=time.time()))


def prepare_openlist_scratch(private):
    """Call ONLY after proving the private server is stopped, before launch."""
    directory = Path(private)/'transfer-temp/openlist'
    marker = directory.parent/'openlist-owner.json'
    if not directory.exists():
        directory.mkdir(parents=True)
        save(marker, {'owner':'TwoDrive.OpenList','version':1,'identity':root_identity(directory)})
    identity=root_identity(directory)
    if not ordinary(marker):
        raise RuntimeError('上传临时目录异常，已停止清理。')
    if json.loads(marker.read_text()) != {'owner':'TwoDrive.OpenList','version':1,'identity':identity}:
        raise RuntimeError('上传临时目录没有有效归属记录，已停止清理。')
    count = size = 0
    for file in directory.iterdir():
        # Only OpenList's documented scratch allocator pattern, no recursion.
        if re.fullmatch(r'file-\d+',file.name) and ordinary(file) and file.is_file():
            size += file.stat().st_size
            checked_file(file,directory).unlink()
            count += 1
    return directory, dict(files=count, bytes=size)
