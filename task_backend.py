"""Version-aware remote adapters for independent CfAPI tasks."""
from __future__ import annotations
import contextlib
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid
import json
import urllib.request
import urllib.parse
from sync_tasks import relative_path, placeholder_stat
from cloud_transactions import IdentityTransactions, internal, suffix
from process_job import owned_process
from task_scheduling import METADATA, TRANSFERS
from task_names import local_name, remote_relative, excluded


def sha1(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha1').hexdigest()


def version(item):
    if item is None: return None
    return (item['kind'], item['id'], item.get('size', 0), item.get('sha1', ''))


def content(item):
    return None if item is None else (item['kind'], item.get('size', 0), item.get('sha1', ''))


def validate_manifest(rows):
    folded = set()
    for name, item in rows.items():
        if relative_path(name) != name or name.casefold() in folded:
            raise ValueError('云端清单包含重名或不安全路径。')
        folded.add(name.casefold())
        if item.get('kind') not in ('file', 'dir') or not isinstance(item.get('id'), str) or not item['id']:
            raise ValueError('云端文件身份缺失。')
        if item['kind'] == 'file':
            digest = item.get('sha1', '')
            if type(item.get('size')) is not int or item['size'] < 0 or len(digest) != 40 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('云端文件校验信息缺失。')
        parent = name.rpartition('/')[0]
        if parent and (parent not in rows or rows[parent]['kind'] != 'dir'):
            raise ValueError('云端目录树不完整。')
    return rows


class RemoteChanged(RuntimeError): pass
class ConcurrentChange(RemoteChanged): pass


class LocalBackend:
    """Isolated integration backend. Never selected by the user-facing API."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.identity = 'local:' + str(self.root)

    def path(self, relative):
        path = self.root.joinpath(*relative_path(relative).split('/'))
        for p in (path, *path.parents):
            if p == self.root: break
            if p.exists() and getattr(placeholder_stat(p), 'st_file_attributes', 0) & 0x400:
                raise ValueError('测试后端禁止重解析点。')
        return path

    def snapshot(self):
        rows = {}
        for parent, dirs, files in os.walk(self.root):
            for name in dirs + files:
                path = Path(parent)/name
                rel = path.relative_to(self.root).as_posix()
                self.path(rel)
                st = path.stat()
                rows[rel] = dict(kind='dir' if path.is_dir() else 'file', id=str(st.st_ino))
                if path.is_file(): rows[rel].update(size=st.st_size, sha1=sha1(path))
        return validate_manifest(rows)

    def download(self, relative, expected, destination, cancelled):
        self.expect(relative, expected)
        if cancelled.is_set(): raise InterruptedError()
        shutil.copyfile(self.path(relative), destination)
        self.expect(relative, expected)
        if sha1(destination) != expected['sha1']: raise RemoteChanged('下载内容变化。')

    def expect(self, relative, expected):
        actual = self.snapshot().get(relative)
        if version(actual) != version(expected): raise RemoteChanged('云端版本已变化，重新核对后再操作。')
        return actual

    def upload(self, relative, source, expected, cancelled):
        self.expect(relative, expected)
        if cancelled.is_set(): raise InterruptedError()
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / ('.twodrive-stage-' + uuid.uuid4().hex)
        try:
            shutil.copyfile(source, temporary)
            self.expect(relative, expected)
            os.replace(temporary, target)
        finally: temporary.unlink(missing_ok=True)
        return self.snapshot()[relative]

    def mkdir(self, relative):
        self.path(relative).mkdir(parents=True, exist_ok=True)
        return self.snapshot()[relative]

    def delete(self, relative, expected):
        self.expect(relative, expected)
        path = self.path(relative)
        if path.is_dir(): path.rmdir()
        else: path.unlink()

    def rename(self, old, new, expected):
        self.expect(old, expected)
        self.expect(new, None)
        self.path(old).rename(self.path(new))


class OpenListBackend(IdentityTransactions):
    """Fresh raw 115 listings plus the existing journaled OpenList transport.

Remote root identity is persisted by the engine. A path re-created under the
same name or an account switch cannot silently establish a new baseline.
"""
    def __init__(self, task, host):
        self.t, self.task = host, task
        self.remote = task['cloud_path'].lstrip('/')
        self.identity = None
        self.root_id = None
        self.cookie = self.token = None
        self._lock = threading.RLock()
        self._transaction_parent = None
        self.metrics = dict(metadata_requests=0, directory_lists=0, full_snapshots=0, lookups=0)
        self.bind(host.PRIVATE/'tasks'/task['id'])

    def _api(self, endpoint, data):
        with METADATA.slot():
            self.metrics['metadata_requests'] += 1
            return self.t.api(endpoint, data, self.token or self.t.login())

    def _raw_request(self, cookie, endpoint, query):
        with METADATA.slot():
            self.metrics['metadata_requests'] += 1
            return self.t.native_115_request(cookie, endpoint, query)

    def _directory_rows(self, relative, cid, *, transport=False, batch=None):
        if batch is not None:
            # Only successful complete, transport-checked directory reads are shared.
            scope = hashlib.sha256(self.cookie.encode()).hexdigest()
            key = ('directory', id(self.t), scope, cid, self.remote, relative, transport)
            return batch.read(key, lambda: self._directory_rows(relative, cid, transport=transport))
        from cloud_scan import valid_names, file_key
        self.metrics['directory_lists'] += 1
        entries = self.t.native_115_list(self.cookie, cid, request=self._raw_request)
        valid_names(entries)
        if transport:
            path = '/115/' + self.remote + ('/' + remote_relative(relative) if relative else '')
            result = self._api('/api/fs/list', dict(path=path, password='', page=1, per_page=0, refresh=True))
            actual = result.get('content')
            if not isinstance(actual, list) and entries: raise ValueError('云端传输目录不完整。')
            expected_names = sorted((r['n'], not bool(r.get('fid')), int(r.get('s', 0)) if r.get('fid') else 0) for r in entries)
            actual_names = sorted((r['name'], r['is_dir'], int(r['size']) if not r['is_dir'] else 0) for r in actual or [])
            if expected_names != actual_names: raise RemoteChanged('云端目录在核验期间发生变化。')
        rows = {}
        for row in entries:
            name = local_name(row['n'])
            if excluded(name): continue
            if row.get('fid'):
                file_key(row)
                if str(row['cid']) != cid: raise ValueError('云端文件目录归属不一致。')
                if name in rows: raise ValueError('云端文件名映射后重复。')
                rows[name] = dict(kind='file', id=str(row['fid']), size=int(row['s']), sha1=row['sha'].lower())
            else:
                if str(row.get('pid', cid)) != cid: raise ValueError('云端目录归属不一致。')
                if name in rows: raise ValueError('云端目录名映射后重复。')
                rows[name] = dict(kind='dir', id=str(row['cid']))
        validate_manifest(rows)
        return {(relative + '/' if relative else '') + name: row for name, row in rows.items()}

    def _connection(self):
        with METADATA.slot():
            token = self.t.login()
            storage = next((s for s in self.t.storages(token) if s.get('mount_path') == '/115'), None)
        if not storage or storage.get('driver') != '115 Cloud' or storage.get('remark') != 'Managed by TwoDrive':
            raise RuntimeError('多任务需要当前 115 Cloud 连接。')
        import json
        cookie = json.loads(storage['addition'])['cookie']
        # UID part of the existing cookie identifies the account; never expose it.
        uid = next((x.split('=', 1)[1].strip().split('_')[0] for x in cookie.split(';') if x.strip().startswith('UID=')), None)
        if not uid: raise RuntimeError('无法确认当前网盘账号身份。')
        return token, cookie, uid

    def connect(self, *, batch=None):
        self.token, self.cookie, uid = (batch.read(('connection', id(self.t)), self._connection)
                                        if batch is not None else self._connection())
        root = self._raw_request(self.cookie, 'files/getid', {'path': self.remote}).get('id')
        if not root or str(root) == '0': raise RuntimeError('云端目录不存在，请先在网盘创建该目录。')
        self.root_id = str(root)
        self.identity = hashlib.sha256((uid + ':' + self.root_id).encode()).hexdigest()

    def snapshot(self, *, include_internal=False, transport=True, batch=None):
        with self._lock:
            self.metrics['full_snapshots'] += 1
            self.connect(batch=batch)
            if not include_internal:self.finish_aborts()
            rows, seen = {}, set()
            todo = [('', self.root_id)]
            while todo:
                relative, cid = todo.pop()
                if cid in seen: raise ValueError('云端目录重复引用。')
                seen.add(cid)
                entries = self._directory_rows(relative, cid, transport=transport, batch=batch)
                for name, row in entries.items():
                    hidden = internal(name)
                    if hidden and not include_internal and name.rsplit('/',1)[-1].startswith(('_twodrive-stage-','.twodrive-stage-')):
                        raise RemoteChanged('云端有未完成的提交；等待原任务恢复，暂不传播删除或覆盖。')
                    if hidden and not include_internal: continue
                    rows[name] = row
                    if row['kind'] == 'dir' and not hidden: todo.append((name, row['id']))
            return validate_manifest(rows)

    def batch_snapshot(self, batch):
        return self.snapshot(batch=batch)

    def _scoped_rows(self, parent):
        """Fresh ancestor/parent listings, never a cached tree for a mutation."""
        self.connect()
        cid, prefix, ancestors = self.root_id, '', {}
        if parent:
            for part in relative_path(parent).split('/'):
                name = (prefix + '/' if prefix else '') + part
                row = self._directory_rows(prefix, cid).get(name)
                if not row or row['kind'] != 'dir': return {}
                ancestors[name] = row
                prefix, cid = name, row['id']
        ancestors.update(self._directory_rows(parent, cid))
        return ancestors

    def expect(self, relative, expected):
        with self._lock:
            self.metrics['lookups'] += 1
            relative = relative_path(relative)
            actual = self._scoped_rows(relative.rpartition('/')[0]).get(relative)
            if version(actual) != version(expected): raise RemoteChanged('云端版本变化，已停止操作并保留本地内容。')
            return actual

    def remote_path(self, relative):
        return 'cloud115:' + self.remote + '/' + remote_relative(relative)

    def transfer(self, source, destination, cancelled, *, kind='upload'):
        command = [str(self.t.RCLONE), 'copyto', str(source), str(destination), '--config', str(self.t.PRIVATE/'rclone.conf'),
                   *self.t.transfer_flags(), '--local-hashes', 'SHA1', '--checkers', '1', '--transfers', '1',
                   '--retries', '1', '--low-level-retries', '2', '--contimeout', '15s', '--timeout', '2m']
        # Do not retain raw provider responses in the UI or command error text.
        with TRANSFERS[kind].slot(cancelled), subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=self.t.NO_WINDOW) as proc, owned_process(proc):
            self.transfer_pid = proc.pid
            self.transaction_fault('transfer_started')
            while proc.poll() is None:
                if cancelled.wait(.2):
                    proc.terminate(); proc.wait(timeout=15)
                    raise InterruptedError('任务已暂停，传输暂存文件保留。')
            if proc.returncode: raise RuntimeError('文件传输失败，本地内容和恢复记录已保留。')

    def download(self, relative, expected, destination, cancelled):
        return self._download(relative, expected, destination, cancelled, 'background')

    def download_for_open(self, relative, expected, destination, cancelled):
        return self._download(relative, expected, destination, cancelled, 'interactive')

    def _download(self, relative, expected, destination, cancelled, kind):
        with self._lock:
            self.expect(relative, expected)
            self.transfer(self.remote_path(relative), destination, cancelled, kind=kind)
            if Path(destination).stat().st_size != expected['size'] or sha1(destination) != expected['sha1']:
                raise RemoteChanged('下载校验失败。')
            self.expect(relative, expected)

    def upload(self, relative, source, expected, cancelled):
        with self._lock, self.transaction_scope(relative):
            self.connect()
            try:return self.transaction_upload(relative, Path(source), expected, cancelled)
            except ConcurrentChange:
                self.abort_transaction(self.last_operation_id)
                raise

    @contextlib.contextmanager
    def transaction_scope(self, relative):
        previous = self._transaction_parent
        self._transaction_parent = relative_path(relative).rpartition('/')[0]
        try: yield
        finally: self._transaction_parent = previous

    def transaction_rows(self):
        if self._transaction_parent is not None: return self._scoped_rows(self._transaction_parent)
        return self.snapshot(include_internal=True, transport=False)

    def recovery_snapshot(self):
        return {p:r for p,r in self.transaction_rows().items() if not internal(p)}

    def has_transaction(self, operation): return (self.journal_dir/(operation+'.json')).is_file()

    def upload_fresh(self, relative, source, cancelled):
        if self.transaction_rows().get(relative) is not None: raise RemoteChanged('事务暂存路径已存在。')
        self.transfer(source, self.remote_path(relative), cancelled)

    def post_identity(self, endpoint, fields):
        # Same primary provider endpoints used by the bundled 115 driver.
        with METADATA.slot():
            self.metrics['metadata_requests'] += 1
            time.sleep(1)
            request = urllib.request.Request('https://webapi.115.com/'+endpoint,
                data=urllib.parse.urlencode(fields).encode('ascii'),
                headers={'Cookie':self.cookie, 'User-Agent':'Mozilla/5.0'})
            with self.t.HTTP.open(request, timeout=30) as response: result=json.load(response)
        if result.get('state') is not True: raise RemoteChanged('按文件身份提交未成功；保留原始内容和事务。')

    def rename_identity(self, old, new, expected):
        if expected['kind']=='file' and suffix(old)!=suffix(new):
            raise RemoteChanged('115 按身份改名不能修改扩展名；已保留原文件，请使用复制后删除。')
        rows = self.transaction_rows()
        if version(rows.get(old)) != version(expected): raise ConcurrentChange('按身份操作前源版本变化。')
        parent_old, _, name_old = old.rpartition('/')
        parent_new, _, name_new = new.rpartition('/')
        name_new = remote_relative(new).rpartition('/')[2]
        fid = expected['id']
        if parent_old != parent_new:
            parent = rows.get(parent_new) if parent_new else dict(id=self.root_id,kind='dir')
            if not parent or parent['kind'] != 'dir': raise RemoteChanged('改名目标目录不存在。')
            self.post_identity('files/move', {'pid':parent['id'],'fid[0]':fid})
        self.post_identity('files/batch_rename', {'fid':fid,'file_name':name_new,'files_new_name['+fid+']':name_new})
        actual = self.transaction_rows().get(new)
        # Raw identity mutations bypass OpenList's cache invalidation hooks.
        # Refresh both directories before WebDAV can serve the new name.
        for parent_path in {parent_old,parent_new}:
            path='/115/'+self.remote+('/'+remote_relative(parent_path) if parent_path else '')
            self._api('/api/fs/list',dict(path=path,password='',page=1,per_page=0,refresh=True))
        # 115 auto-numbers collisions instead of overwriting. Verify identity,
        # report the conflict, and retain every version including our upload.
        if not actual or actual['id'] != fid or content(actual) != content(expected):
            raise ConcurrentChange('另一客户端占用了目标名称；网盘保留了独立版本，请重新核对。')

    def mkdir(self, relative):
        with self._lock:
            self._api('/api/fs/mkdir', {'path': '/115/' + self.remote + '/' + remote_relative(relative)})
            return self.snapshot()[relative]

    def delete(self, relative, expected):
        with self._lock, self.transaction_scope(relative):
            self.transaction_delete(relative, expected)

    def rename(self, old, new, expected):
        with self._lock:
            self.expect(old, expected)
            self.expect(new, None)
            self.rename_identity(old, new, expected)
            rows = self.snapshot()
            if old in rows or content(rows.get(new)) != content(expected):
                raise RemoteChanged('云端改名尚未确认。')
