"""Version-aware remote adapters for independent CfAPI tasks."""
from __future__ import annotations
import contextlib
import copy
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
from cloud_transactions import IdentityTransactions, internal, suffix, INTERNAL
from process_job import owned_process
from task_scheduling import METADATA, TRANSFERS
from task_names import local_name, remote_relative, excluded
from remote_manifest import RecursiveManifest, ManifestUnavailable, ancestry_matches
from runtime_json import atomic_write_json
from cloud_history import CloudHistory


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
    def __init__(self, task, host, *, read_only=False):
        self.t, self.task = host, task
        self.read_only = read_only
        self.remote = task['cloud_path'].lstrip('/')
        self.identity = None
        self.account_key = None
        self.root_id = None
        self.cookie = self.token = None
        self._lock = threading.RLock()
        self._metrics_lock = threading.Lock()
        self._required_identity = None
        self._transaction_parent = None
        self._transaction_authenticated = False
        self._directory_hints = {}
        self._bulk_previous = None
        self._bulk_retry_at = 0
        self._history_seeded = False
        self._history_pending = {}
        self._history_known = {}
        self._history_retry_at = 0
        self.metrics = dict(metadata_requests=0, directory_lists=0, full_snapshots=0, lookups=0,
                            bulk_snapshots=0, bulk_fallbacks=0, scoped_hint_reads=0)
        self.cancelled = None
        self.progress = None
        self.bind(host.PRIVATE/'tasks'/task['id'])

    def _metric(self, name, amount=1):
        with self._metrics_lock: self.metrics[name] += amount

    def _api(self, endpoint, data):
        if self.read_only and endpoint != '/api/fs/list': raise RuntimeError('只读会话禁止修改云端文件。')
        with METADATA.slot(self.cancelled, account=self.account_key):
            self._metric('metadata_requests')
            return self.t.api(endpoint, data, self.token or self.t.login())

    def _raw_request(self, cookie, endpoint, query):
        with METADATA.slot(self.cancelled, account=self.account_key):
            self._metric('metadata_requests')
            return self.t.native_115_request(cookie, endpoint, query)

    def _directory_rows(self, relative, cid, *, transport=False, batch=None, verify_path=False):
        if batch is not None:
            # Only successful complete reads are shared. Transport-checked reads
            # have a separate key; mutation/transfer guards bypass the batch.
            scope = hashlib.sha256(self.cookie.encode()).hexdigest()
            key = ('directory', id(self.t), scope, cid, self.remote, relative, transport)
            return batch.read(key, lambda: self._directory_rows(relative, cid, transport=transport), self.cancelled)
        from cloud_scan import valid_names, file_key
        self._metric('directory_lists')
        def request(cookie, endpoint, query):
            response = self._raw_request(cookie, endpoint, query)
            if verify_path and not ancestry_matches(response.get('path'), self.remote, self.root_id,
                                                     relative, self._directory_hints):
                raise ManifestUnavailable('云端目录路径身份已变化，重新逐级核验。')
            return response
        entries = self.t.native_115_list(self.cookie, cid, request=request if verify_path else self._raw_request)
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
        with METADATA.slot(self.cancelled, account=self.account_key):
            token = self.t.login()
            storage = next((s for s in self.t.storages(token) if s.get('mount_path') == '/115'), None)
        if not storage or storage.get('driver') != '115 Cloud' or storage.get('remark') != 'Managed by TwoDrive':
            raise RuntimeError('多任务需要当前 115 Cloud 连接。')
        import json
        cookie = json.loads(storage['addition'])['cookie']
        # UID part of the existing cookie identifies the account; never expose it.
        uid = next((x.split('=', 1)[1].strip().split('_')[0] for x in cookie.split(';') if x.strip().startswith('UID=')), None)
        if not uid: raise RuntimeError('无法确认当前网盘账号身份。')
        self.account_key = hashlib.sha256(('115:' + uid).encode()).hexdigest()
        return token, cookie, uid

    def connect(self, *, batch=None):
        self.token, self.cookie, uid = (batch.read(('connection', id(self.t)), self._connection, self.cancelled)
                                        if batch is not None else self._connection())
        self.account_key = hashlib.sha256(('115:' + uid).encode()).hexdigest()
        root = self._raw_request(self.cookie, 'files/getid', {'path': self.remote}).get('id')
        if not root or str(root) == '0': raise RuntimeError('云端目录不存在，请先在网盘创建该目录。')
        self.root_id = str(root)
        identity = hashlib.sha256((uid + ':' + self.root_id).encode()).hexdigest()
        if self._required_identity is not None and identity != self._required_identity:
            raise RemoteChanged('按需读取期间网盘账号或同步根身份变化。')
        if identity != self.identity:
            self._directory_hints = {}
            self._bulk_previous = None
            self._bulk_retry_at = 0
            self._history_seeded = False
            self._history_pending = {}
            self._history_known = {}
        self.identity = identity

    def _remember_directories(self, rows):
        self._directory_hints = {'': self.root_id}
        self._directory_hints.update((name, row['id']) for name, row in rows.items() if row['kind'] == 'dir')

    def _bulk_snapshot(self):
        started = time.monotonic()
        reported = [0.0]
        def progress(entries, pages):
            now = time.monotonic()
            if self.progress and now - reported[0] >= 10:
                self.progress(0, 1, entries, round(now - started, 1))
                reported[0] = now
        def read():
            reader = RecursiveManifest(lambda endpoint, query: self._raw_request(self.cookie, endpoint, query),
                                       self.remote, self.root_id, cancelled=self.cancelled, progress=progress)
            return validate_manifest(reader.read())
        rows = read()
        if rows != self._bulk_previous:
            # A paginated result is not an atomic snapshot. Confirm first and
            # changed observations, including empty trees, before reconciliation.
            if read() != rows: raise ManifestUnavailable('云端批量清单仍在变化。')
        root = self._raw_request(self.cookie, 'files/getid', {'path': self.remote}).get('id')
        if str(root) != self.root_id: raise RemoteChanged('云端根目录在核验期间被替换。')
        self._bulk_previous = rows
        self._metric('bulk_snapshots')
        if self.progress:
            self.progress(1 + sum(row['kind'] == 'dir' for row in rows.values()), 0,
                          len(rows), round(time.monotonic() - started, 1))
        return rows

    @staticmethod
    def _visible_rows(rows, include_internal):
        if include_internal: return rows
        for name in rows:
            for part in name.split('/'):
                if INTERNAL.fullmatch(part):
                    if part.startswith(('_twodrive-stage-', '.twodrive-stage-')):
                        raise RemoteChanged('云端有未完成的提交；等待原任务恢复，暂不传播删除或覆盖。')
                    break
        return {name: row for name, row in rows.items() if not internal(name)}

    def snapshot(self, *, include_internal=False, transport=True, batch=None):
        with self._lock:
            self._metric('full_snapshots')
            self.connect(batch=batch)
            if not include_internal and not self.read_only: self.finish_aborts()
            if not transport and time.monotonic() >= self._bulk_retry_at:
                try:
                    rows = self._bulk_snapshot()
                except ManifestUnavailable:
                    # Never return the partial bulk result. Fall back to the
                    # existing complete authoritative traversal for this scan.
                    self._metric('bulk_fallbacks')
                    self._bulk_retry_at = time.monotonic() + 300
                    self._bulk_previous = None
                else:
                    self._remember_directories(rows)
                    if not include_internal and not self.read_only: self._recover_history(rows)
                    return self._visible_rows(rows, include_internal)
            rows, seen = {}, set()
            todo = [('', self.root_id)]
            started, reported = time.monotonic(), 0.0
            while todo:
                if self.cancelled is not None and self.cancelled.is_set(): raise InterruptedError('任务已暂停。')
                relative, cid = todo.pop()
                if cid in seen: raise ValueError('云端目录重复引用。')
                seen.add(cid)
                entries = self._directory_rows(relative, cid, transport=transport, batch=batch)
                for name, row in entries.items():
                    hidden = internal(name)
                    if hidden and not include_internal and name.rsplit('/',1)[-1].startswith(('_twodrive-stage-','.twodrive-stage-')):
                        raise RemoteChanged('云端有未完成的提交；等待原任务恢复，暂不传播删除或覆盖。')
                    rows[name] = row
                    if row['kind'] == 'dir': todo.append((name, row['id']))
                now = time.monotonic()
                if self.progress and (now-reported >= 10 or not todo):
                    self.progress(len(seen), len(todo), len(rows), round(now-started, 1))
                    reported = now
            validate_manifest(rows)
            self._remember_directories(rows)
            if not include_internal and not self.read_only: self._recover_history(rows)
            return self._visible_rows(rows, include_internal)

    def startup_snapshot(self):
        # Only identity and the root health marker are needed before connecting
        # CfAPI. The first scan still validates every directory before changes.
        with self._lock:
            self.connect()
            return self._directory_rows('', self.root_id)

    def scan_snapshot(self):
        return self.snapshot(transport=False)

    def batch_snapshot(self, batch):
        return self.snapshot(transport=False, batch=batch)

    def _scoped_rows(self, parent, *, transport=False):
        """Fresh ancestor/parent listings, never a cached tree for a mutation."""
        if not transport and self._transaction_parent is not None and parent in self._directory_hints:
            # IDs are only location hints. The fresh directory listing must
            # prove its full ancestry, including the root path and every ID.
            if not self._transaction_authenticated:
                token, cookie, uid = self._connection()
                identity = hashlib.sha256((uid + ':' + self.root_id).encode()).hexdigest()
                if identity != self.identity: raise RemoteChanged('事务期间网盘账号身份变化。')
                self.token, self.cookie = token, cookie
                self._transaction_authenticated = True
            try:
                rows = self._directory_rows(parent, self._directory_hints[parent], verify_path=True)
            except ManifestUnavailable:
                # Re-resolve all paths. The guarded caller compares identities
                # again, so stale hints cannot authorize a changed source.
                self._directory_hints = {}
            else:
                prefix, ancestors = '', {}
                for part in parent.split('/') if parent else []:
                    prefix = (prefix + '/' if prefix else '') + part
                    ancestors[prefix] = dict(kind='dir', id=self._directory_hints[prefix])
                ancestors.update(rows)
                self._metric('scoped_hint_reads')
                return ancestors
        previous_identity = self.identity
        self.connect()
        if self._transaction_parent is not None and previous_identity is not None and self.identity != previous_identity:
            raise RemoteChanged('事务期间云端根目录身份变化。')
        cid, prefix, ancestors = self.root_id, '', {}
        if parent:
            for part in relative_path(parent).split('/'):
                name = (prefix + '/' if prefix else '') + part
                row = self._directory_rows(prefix, cid, transport=transport).get(name)
                if not row or row['kind'] != 'dir': return {}
                ancestors[name] = row
                prefix, cid = name, row['id']
        ancestors.update(self._directory_rows(parent, cid, transport=transport))
        self._directory_hints.update((name, row['id']) for name, row in ancestors.items() if row['kind'] == 'dir')
        self._directory_hints[''] = self.root_id
        return ancestors

    def expect(self, relative, expected):
        with self._lock:
            self._metric('lookups')
            relative = relative_path(relative)
            actual = self._scoped_rows(relative.rpartition('/')[0]).get(relative)
            if version(actual) != version(expected): raise RemoteChanged('云端版本变化，已停止操作并保留本地内容。')
            return actual

    def remote_path(self, relative):
        return 'cloud115:' + self.remote + '/' + remote_relative(relative)

    def transfer(self, source, destination, cancelled, *, kind='upload'):
        if self.read_only and kind == 'upload': raise RuntimeError('只读会话禁止上传文件。')
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
        # Interactive hydration must not wait behind this task's long upload or
        # background download lock. The reader has no mutable transaction/path
        # context in common with its owner; only the safe HTTP pool is shared.
        identity = self.identity
        if identity is None: raise RemoteChanged('同步根尚未核验，不能按需读取。')
        reader = OpenListBackend(copy.deepcopy(self.task), self.t, read_only=True)
        reader._required_identity = identity
        reader.cancelled = cancelled
        reader.metrics, reader._metrics_lock = self.metrics, self._metrics_lock
        try:
            return reader._download(relative, expected, destination, cancelled, 'interactive')
        except BaseException:
            # Destinations are private hydration staging files. Never expose a
            # failed/cancelled read or a version whose postcheck did not pass.
            Path(destination).unlink(missing_ok=True)
            raise

    def _download(self, relative, expected, destination, cancelled, kind):
        with self._lock:
            # Refresh the path's ancestors and transport listing immediately
            # before a WebDAV read, instead of refreshing every unrelated
            # directory on every read-only discovery scan.
            self._expect_transfer(relative, expected)
            self.transfer(self.remote_path(relative), destination, cancelled, kind=kind)
            if Path(destination).stat().st_size != expected['size'] or sha1(destination) != expected['sha1']:
                raise RemoteChanged('下载校验失败。')
            self.expect(relative, expected)

    def upload(self, relative, source, expected, cancelled):
        if self.read_only: raise RuntimeError('只读会话禁止上传文件。')
        with self._lock, self.transaction_scope(relative):
            self.connect()
            try:
                result = self.transaction_upload(relative, Path(source), expected, cancelled)
                operation = self.last_operation_id
                tx = json.loads((self.journal_dir/(operation+'.json')).read_text(encoding='utf-8'))
                if tx.get('expected') and tx.get('committed') and tx.get('archived'):
                    self._history_known[operation] = tx
                    self._history_pending[operation] = tx['archive']
                return result
            except ConcurrentChange:
                self.abort_transaction(self.last_operation_id)
                raise

    @contextlib.contextmanager
    def transaction_scope(self, relative):
        previous = self._transaction_parent
        authenticated = self._transaction_authenticated
        self._transaction_parent = relative_path(relative).rpartition('/')[0]
        self._transaction_authenticated = False
        try: yield
        finally:
            self._transaction_parent = previous
            self._transaction_authenticated = authenticated

    def transaction_rows(self):
        if self._transaction_parent is not None: return self._scoped_rows(self._transaction_parent)
        return self.snapshot(include_internal=True, transport=False)

    def recovery_snapshot(self):
        return {p:r for p,r in self.transaction_rows().items() if not internal(p)}

    def recovery_entry(self, relative):
        with self._lock:
            return self._scoped_rows(relative_path(relative).rpartition('/')[0]).get(relative)

    def _expect_transfer(self, relative, expected):
        actual = self._scoped_rows(relative_path(relative).rpartition('/')[0], transport=True).get(relative)
        if version(actual) != version(expected): raise RemoteChanged('传输前云端路径或版本发生变化。')
        return actual

    def has_transaction(self, operation): return (self.journal_dir/(operation+'.json')).is_file()

    def upload_fresh(self, relative, source, cancelled):
        if self.read_only: raise RuntimeError('只读会话禁止上传文件。')
        self._expect_transfer(relative, None)
        self.transfer(source, self.remote_path(relative), cancelled)

    def post_identity(self, endpoint, fields):
        if self.read_only: raise RuntimeError('只读会话禁止修改云端文件。')
        # Same primary provider endpoints used by the bundled 115 driver.
        with METADATA.slot(self.cancelled, account=self.account_key):
            self._metric('metadata_requests')
            time.sleep(1)
            request = urllib.request.Request('https://webapi.115.com/'+endpoint,
                data=urllib.parse.urlencode(fields).encode('ascii'),
                headers={'Cookie':self.cookie, 'User-Agent':'Mozilla/5.0'})
            with self.t.HTTP.open(request, timeout=30) as response: result=json.load(response)
        if result.get('state') is not True: raise RemoteChanged('按文件身份提交未成功；保留原始内容和事务。')

    def rename_identity(self, old, new, expected):
        if self.read_only: raise RuntimeError('只读会话禁止修改云端文件。')
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
        result = self.transaction_rows()
        actual = result.get(new)
        # Raw identity mutations bypass OpenList's cache invalidation hooks.
        # Refresh both directories before WebDAV can serve the new name.
        for parent_path in {parent_old,parent_new}:
            path='/115/'+self.remote+('/'+remote_relative(parent_path) if parent_path else '')
            self._api('/api/fs/list',dict(path=path,password='',page=1,per_page=0,refresh=True))
        # 115 auto-numbers collisions instead of overwriting. Verify identity,
        # report the conflict, and retain every version including our upload.
        if not actual or actual['id'] != fid or content(actual) != content(expected):
            raise ConcurrentChange('另一客户端占用了目标名称；网盘保留了独立版本，请重新核对。')
        return result

    def mkdir(self, relative):
        if self.read_only: raise RuntimeError('只读会话禁止修改云端文件。')
        with self._lock:
            parent = relative_path(relative).rpartition('/')[0]
            self._scoped_rows(parent, transport=True)
            self._api('/api/fs/mkdir', {'path': '/115/' + self.remote + '/' + remote_relative(relative)})
            actual = self._scoped_rows(parent).get(relative)
            if not actual or actual['kind'] != 'dir': raise RemoteChanged('新建云端目录尚未确认。')
            return actual

    def delete(self, relative, expected):
        if self.read_only: raise RuntimeError('只读会话禁止修改云端文件。')
        with self._lock, self.transaction_scope(relative):
            if self.identity is None: self.connect()
            relative = relative_path(relative)
            if expected['kind'] == 'dir': self._prepare_empty_directory(relative, expected)
            operation = uuid.uuid4().hex
            parent = relative.rpartition('/')[0]
            target = (parent + '/' if parent else '') + '_twodrive-deleted-' + operation + (suffix(relative) if expected['kind'] == 'file' else '')
            # Record intent before submission, preserving the existing v1
            # recovery ABI. rename_identity performs the fresh source check
            # after the fault boundary and verifies target identity/content.
            tx = dict(version=1, kind='delete', path=relative, expected=expected,
                      archive=target, backend=self.identity)
            atomic_write_json(self.journal_dir/(operation + '.json'), tx, indent=2, durable=True)
            self.transaction_fault('before_remote_delete')
            if expected['kind'] == 'dir': self._assert_empty_directory(relative, expected)
            rows = self.rename_identity(relative, target, expected)
            self._history_known[operation] = tx
            self._history_pending[operation] = target
            result = self.migrate_delete_history([operation])[operation]
            if result['stage'] != 'moved':
                self._history_pending[operation] = target
                reason = '删除归档在移入历史区前变化，已保留内容和处理记录。'
                CloudHistory(self).conflict(operation, tx, reason)
                raise ConcurrentChange(reason)
            if rows.get(relative) is not None:
                raise RemoteChanged('删除期间出现新版本，已保留新版本。')

    def history_archive_info(self, operation_id):
        return CloudHistory(self).info(operation_id)

    def _history_journals(self):
        for path in self.journal_dir.glob('*.json'):
            tx = json.loads(path.read_text(encoding='utf-8'))
            if (tx.get('version') == 1 and tx.get('backend') == self.identity and tx.get('expected')
                    and (tx.get('kind') == 'delete' or (tx.get('committed') and tx.get('archived')))):
                yield path.stem, tx

    def _seed_history(self, rows):
        # Exactly one journal pass per connection identity. The authoritative
        # raw tree is an eligibility hint; every candidate is freshly checked.
        self._history_known = dict(self._history_journals())
        positions = {row['id']: path for path,row in rows.items()}
        history = CloudHistory(self)
        for operation, tx in self._history_known.items():
            receipt = history.info(operation)
            if receipt:
                if receipt['stage'] != 'moved':
                    self._history_pending[operation] = receipt['actual_source_archive']
                continue
            actual = positions.get(tx['expected']['id'])
            # Parent directories may have been renamed by old bottom-up
            # deletion. ID + full content + exact archive basename must agree.
            if actual and actual.rsplit('/',1)[-1] == tx['archive'].rsplit('/',1)[-1]:
                if version(rows.get(actual)) != version(tx['expected']):
                    # A crash may have happened after ID-rename but before the
                    # new deletion recorded a changed archive. Never silently
                    # discard this proven same-ID concurrent edit on restart.
                    history.conflict(operation, tx, '已归档文件在重启核验时发现并发修改，保留正文等待处理。',
                                     actual_archive=actual, observed=rows[actual])
                self._history_pending[operation] = actual
        self._history_seeded = True

    def _recover_history(self, rows):
        if not self._history_seeded: self._seed_history(rows)
        if not self._history_pending: return
        if time.monotonic() < self._history_retry_at:
            raise RemoteChanged('云端历史归档尚未处理完成，等待重试，暂不确认新的删除。')
        self._history_retry_at = time.monotonic() + 60
        # Child archives first, so verified empty parent archives can follow.
        operations = sorted(self._history_pending, key=lambda op:self._history_pending[op].count('/'), reverse=True)[:4]
        self.migrate_delete_history(operations)
        if self._history_pending:
            self._history_retry_at = 0
            raise RemoteChanged('云端历史正在分批归档，暂不确认新的删除。')

    def migrate_delete_history(self, operation_ids=None, limit=None):
        """Relocate verified v1 retained items; never infer success from a log.

        Explicit IDs avoid a tree scan (used for one-file repair). An omitted
        list selects existing archives from one complete unfiltered snapshot.
        Results map operation IDs to durable receipts or ``not_archived``.
        """
        with self._lock:
            if self.read_only: raise RuntimeError('只读会话禁止迁移同步历史。')
            if self.identity is None: self.connect()
            if operation_ids is None:
                if not self._history_seeded: self._seed_history(self.snapshot(include_internal=True, transport=False))
                operation_ids = sorted(self._history_pending, key=lambda op:self._history_pending[op].count('/'), reverse=True)
            if limit is not None: operation_ids = list(operation_ids)[:limit]
            results = {}
            for operation in operation_ids:
                history = CloudHistory(self)
                history.receipt_path(operation)  # Validate before opening a journal path.
                tx = json.loads((self.journal_dir/(operation+'.json')).read_text(encoding='utf-8'))
                if not (tx.get('kind') == 'delete' or (tx.get('committed') and tx.get('archived'))):
                    raise RemoteChanged('云端提交尚未完成，不能迁移历史版本。')
                actual = self._history_pending.get(operation, tx['archive'])
                with self.transaction_scope(actual):
                    result = history.archive(operation, tx, actual_archive=actual)
                results[operation] = result
                if result['stage'] in ('moved', 'not_archived'): self._history_pending.pop(operation, None)
            return results

    def _all_children(self, relative, expected):
        parent = relative.rpartition('/')[0]
        if version(self._scoped_rows(parent).get(relative)) != version(expected):
            raise ConcurrentChange('目录身份变化，不能删除。')
        hints = dict(self._directory_hints, **{relative:expected['id']})
        def request(cookie, endpoint, query):
            response = self._raw_request(cookie, endpoint, query)
            if not ancestry_matches(response.get('path'), self.remote, self.root_id, relative, hints):
                raise ConcurrentChange('目录路径变化，不能删除。')
            return response
        return self.t.native_115_list(self.cookie, expected['id'], request=request)

    def _assert_empty_directory(self, relative, expected):
        if self._all_children(relative, expected):
            raise ConcurrentChange('云端目录仍有文件或出现并发新增，已保留目录。')

    def _prepare_empty_directory(self, relative, expected):
        children = self._all_children(relative, expected)
        if children:
            if not self._history_known: self._history_known = dict(self._history_journals())
            by_id = {tx['expected']['id']:(op,tx) for op,tx in self._history_known.items()}
            operations = []
            for row in children:
                identity = str(row.get('fid') or row.get('cid'))
                found = by_id.get(identity)
                if not found or row['n'] != remote_relative(found[1]['archive']).rsplit('/',1)[-1]:
                    raise ConcurrentChange('云端目录仍有未归档文件，已保留目录。')
                op, tx = found
                self._history_pending[op] = relative+'/'+local_name(row['n'])
                operations.append(op)
            self.migrate_delete_history(operations)
        self._assert_empty_directory(relative, expected)

    def rename(self, old, new, expected):
        if self.read_only: raise RuntimeError('只读会话禁止修改云端文件。')
        with self._lock:
            self.expect(old, expected)
            self.expect(new, None)
            self.rename_identity(old, new, expected)
            rows = self.snapshot()
            if old in rows or content(rows.get(new)) != content(expected):
                raise RemoteChanged('云端改名尚未确认。')
