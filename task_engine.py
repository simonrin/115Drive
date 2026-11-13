"""Placeholder-aware reconciliation. One owner per root; cloud cache is never truth."""
from __future__ import annotations
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
import uuid
from cloud_files import CloudFiles, LocalContentChanged
from runtime_json import atomic_write_json, write_snapshot
from sync_tasks import effective_policy, relative_path, placeholder_stat
from task_backend import sha1, content, version, RemoteChanged, ConcurrentChange
from cloud_transactions import internal
from local_watch import LocalChangeMonitor
from task_names import excluded
from task_activity import TaskActivity
from task_scheduling import work_priority
from sync_state_store import StateStore, load_state
from local_change_index import LocalChangeIndex


class LinkedCancellation:
    """Either application pause or native read cancellation stops the transfer."""
    def __init__(self, *events): self.events = events
    def is_set(self): return any(event.is_set() for event in self.events)
    def wait(self, timeout):
        end = time.monotonic() + timeout
        while not self.is_set():
            remaining = end - time.monotonic()
            if remaining <= 0: return False
            self.events[0].wait(min(.05, remaining))
        return True


class TaskEngine:
    def __init__(self, task, directory, backend, *, native_factory=CloudFiles, policy_writer=None, fault=None):
        self.task = copy.deepcopy(task)
        self.policy_writer = policy_writer
        self.fault = fault or (lambda step: None)
        self.directory, self.backend = Path(directory), backend
        self.directory.mkdir(parents=True, exist_ok=True)
        self.root = Path(task['local_path'])
        self.cancel = threading.Event()
        self.scan_complete = None
        if backend is not None:
            backend.cancelled = self.cancel
            backend.progress = self.scan_progress
        self.access_lock = threading.RLock()
        self.access, self.open_counts = {}, {}
        self.prefetched = {}
        self.background_hydrations = set()
        self.monitor = None
        self.local_index = None
        self._index_attempted = False
        self._journal_checked_at = 0
        self._last_local = None
        self._last_remote = None
        self.metrics = dict(state_writes=0, inventory_scans=0, remote_scans=0, cycles=0)
        if hasattr(self.backend, 'bind'): self.backend.bind(self.directory)
        self.native = native_factory(self.root, task['id'], self.fetch, event=self.event)
        self.state_path = self.directory/'state.json'
        existed = self.state_path.exists()
        try:
            self.state = load_state(self.directory)
        except FileNotFoundError:
            self.state = dict(version=1, task=task['id'], local=str(self.root), cloud=task['cloud_path'],
                              backend=None, items={}, pending=None)
        if not isinstance(self.state, dict) or self.state.get('version') != 1 or self.state.get('task') != task['id'] or self.state.get('local') != str(self.root) or self.state.get('cloud') != task['cloud_path'] or not isinstance(self.state.get('items'), dict):
            raise RuntimeError('任务状态损坏或目录身份变化，不能重建同步基线。')
        for path, row in self.state['items'].items():
            relative_path(path)
            if not isinstance(row, dict) or not isinstance(row.get('remote'), dict):
                raise RuntimeError('文件同步基线损坏。')
        self.started = False
        self._saved_state = copy.deepcopy(self.state) if existed or (self.directory/'state.sqlite3').exists() else None
        self.state_store = None
        self._last_status = None
        self.activity = TaskActivity(self.directory)
        self._delete_done = 0
        self._delete_total = 0
        self._policy_key = None
        self.more_work = False
        if (self.directory/'state.sqlite3').exists(): self.enable_state_store()

    def enable_state_store(self):
        if self.state_store is None:
            store = StateStore(self.directory, guard_legacy=True)
            try: store.save(self.state, self._saved_state)
            except BaseException:
                store.close(export=False)
                raise
            self.state_store = store
            self._saved_state = copy.deepcopy(self.state)

    def save(self):
        if self.state == self._saved_state: return False
        if self.state_store is None:
            atomic_write_json(self.state_path, self.state, indent=2, durable=True)
        else:
            self.state_store.save(self.state, self._saved_state)
        self._saved_state = copy.deepcopy(self.state)
        self.metrics['state_writes'] += 1
        return True

    def status(self, state, message, **extra):
        if 'progress' not in extra and state in ('uploading', 'downloading'):
            extra['progress'] = dict(phase=state, name=extra.get('file', ''), completed=0, total=1)
        value = dict(state=state, message=message, task_id=self.task['id'], **extra)
        if value == self._last_status: return
        if write_snapshot(self.directory/'status.json', dict(value, updated_at=time.time())):
            self._last_status = value

    def enable_watch(self):
        self.local_index = LocalChangeIndex(self.root, self.directory)
        self.monitor = LocalChangeMonitor(self.root, lambda _: self.local_snapshot(),
            full_scan_seconds=1800, poll_seconds=2, incremental_scan=self.refresh_inventory)

    def local_snapshot(self):
        if self.local_index is not None and not self._index_attempted:
            self._index_attempted = True
            result = self.local_index.recover(self.inventory, self.refresh_inventory)
            self.metrics['local_recovery'] = dict(mode=result.mode, journal_available=self.local_index.available)
            return result.snapshot
        return self.inventory()

    def local_changed(self):
        if self.monitor is None: return False
        current = self.monitor.poll()
        if self.local_index is not None and self.local_index.available:
            self.local_index.observe(current)
        if self.local_index is not None and self.local_index.available and time.monotonic()-self._journal_checked_at >= 1:
            result = self.local_index.update(self.inventory, self.refresh_inventory)
            current = self.monitor.current = result.snapshot
            self.metrics['local_recovery'] = dict(mode=result.mode, journal_available=self.local_index.available)
            self._journal_checked_at = time.monotonic()
        return current != self._last_local

    def start(self, registrar=None):
        # Validate the authoritative backend before creating/registering a root.
        started = time.monotonic()
        remote = getattr(self.backend, 'startup_snapshot',
                         getattr(self.backend, 'recovery_snapshot', self.backend.snapshot))()
        self._check_backend()
        root_id = self.state.get('local_root_id')
        if root_id is not None and (not self.root.exists() or self.root.stat().st_ino != root_id):
            raise RuntimeError('本地同步根已丢失或被替换，已停止任务。')
        self._prepare_health(remote)
        if registrar:
            self.root.mkdir(parents=True, exist_ok=True)
            registrar()
        self.native.connect()
        self.started = True
        self.state['backend'] = self.backend.identity
        self.state['local_root_id'] = self.root.stat().st_ino
        self.save()
        self.metrics['startup_seconds'] = round(time.monotonic()-started, 3)
        return remote

    def scan_progress(self, checked, pending, entries, elapsed):
        self.status('checking', f'正在核对云端目录 · 已检查 {checked} 个目录，{entries} 项',
                    scan=dict(directories=checked, pending=pending, entries=entries, seconds=elapsed),
                    progress=dict(phase='scanning', completed=checked, total=checked+pending, entries=entries))

    @property
    def health_name(self):
        return '.twodrive-health-' + self.task['id']

    def _prepare_health(self, remote):
        if 'health' not in self.state:
            if self.state['backend'] is not None: raise RuntimeError('任务健康记录缺失，不能重置。')
            self.state['health'] = uuid.uuid4().hex
            self.save()
        payload = self.state['health'].encode('ascii')
        expected = ('file', len(payload), hashlib.sha1(payload).hexdigest())
        if self.health_name not in remote:
            if self.state['backend'] is not None: raise RuntimeError('云端健康标记丢失，停止删除与覆盖。')
            source = self.directory/'health-upload'
            source.write_bytes(payload)
            self.backend.operation_id = uuid.uuid5(uuid.NAMESPACE_OID,self.task['id']+self.state['health']).hex
            try: self.backend.upload(self.health_name, source, None, self.cancel)
            finally:
                self.backend.operation_id = None
                source.unlink(missing_ok=True)
        elif content(remote[self.health_name]) != expected:
            raise RuntimeError('云端健康标记不匹配。')

    def remote_snapshot(self, *, batch=None):
        self.metrics['remote_scans'] += 1
        with work_priority('background'):
            rows = (self.backend.batch_snapshot(batch) if batch is not None and hasattr(self.backend, 'batch_snapshot')
                    else getattr(self.backend, 'scan_snapshot', self.backend.snapshot)())
        self._check_backend()
        self._prepare_health(rows)
        return {p: row for p, row in rows.items() if not p.startswith('.twodrive-health-') and not excluded(p)}

    def _check_backend(self):
        if self.state['backend'] is not None and self.state['backend'] != self.backend.identity:
            raise RuntimeError('网盘账号或云端根目录身份变化，已停止任务。')

    def close(self):
        self.cancel.set()
        errors = []
        try:
            if self.monitor:
                try: self.monitor.close()
                except BaseException as error: errors.append(error)
                finally: self.monitor = None
            if self.local_index is not None:
                try: self.local_index.close()
                except BaseException as error: errors.append(error)
                finally: self.local_index = None
            if self.started:
                try: self.native.close()
                except BaseException as error: errors.append(error)
                else: self.started = False
        finally:
            if self.state_store is not None:
                try: self.state_store.close()
                except BaseException as error: errors.append(error)
                finally: self.state_store = None
        if errors: raise errors[0]

    def event(self, kind, identity):
        if kind=='fetch_error':
            self.status('error','按需下载失败：'+identity.get('detail',identity.get('error','未知错误')),
                        file=identity.get('identity',{}).get('path',''))
            return
        path = identity.get('path')
        if not path: return
        with self.access_lock:
            if kind in ('open', 'fetch', 'hydrated', 'close'):
                self.access[path] = time.time()
            if kind == 'open': self.open_counts[path] = self.open_counts.get(path, 0) + 1
            elif kind == 'close': self.open_counts[path] = max(0, self.open_counts.get(path, 0) - 1)

    @contextlib.contextmanager
    def fetch(self, identity, cancelled):
        if self.cancel.is_set() or self.task.get('paused'):
            raise InterruptedError('同步任务已暂停。')
        path = relative_path(identity['path'])
        expected = {k: identity[k] for k in ('kind', 'id', 'size', 'sha1')}
        if identity['task'] != self.task['id']: raise ValueError('文件不属于当前任务。')
        with self.access_lock:
            prepared = self.prefetched.get((path, version(expected)))
        if prepared:
            yield prepared
            return
        self.status('downloading', '正在按需下载', file=path)
        stage = self.directory/'downloads'/uuid.uuid4().hex
        stage.parent.mkdir(exist_ok=True)
        try:
            with self.access_lock: background = path in self.background_hydrations
            download = self.backend.download if background else getattr(self.backend, 'download_for_open', self.backend.download)
            with work_priority('normal' if background else 'interactive'):
                download(path, expected, stage, LinkedCancellation(cancelled, self.cancel))
            self._check_backend()
            if sha1(stage) != expected['sha1']: raise RemoteChanged('文件下载校验失败。')
            yield stage
            self.activity.completed('download', path, expected, operation=stage.name)
        finally:
            stage.unlink(missing_ok=True)

    def identity(self, path, remote):
        return dict(task=self.task['id'], path=path, **remote)

    def inventory(self):
        self.metrics['inventory_scans'] += 1
        if not self.root.is_dir() or self.root.stat().st_ino != self.state.get('local_root_id'):
            raise RuntimeError('本地同步根丢失或身份变化，停止同步。')
        result = {}
        def failure(error): raise error
        for parent, dirs, files in os.walk(self.root, followlinks=False, onerror=failure):
            dirs[:] = [name for name in dirs if not excluded(name)]
            for name in dirs + files:
                if excluded(name): continue
                path = Path(parent)/name
                rel = relative_path(path.relative_to(self.root).as_posix())
                if internal(rel): raise RuntimeError('本地文件使用了云端事务保留名称，请先改名。')
                if rel == self.health_name: raise RuntimeError('本地文件占用了任务健康标记名称。')
                entry = self.inventory_entry(rel)
                if entry is not None: result[rel] = entry
        return result

    def inventory_entry(self, relative):
        """Fresh metadata only; never hydrate a placeholder to discover changes."""
        if not self.root.is_dir() or self.root.stat().st_ino != self.state.get('local_root_id'):
            raise RuntimeError('本地同步根丢失或身份变化，停止同步。')
        rel = relative_path(relative)
        if internal(rel) or rel == self.health_name:
            raise RuntimeError('本地文件使用了同步保留名称，请先改名。')
        path = self.native.path(rel)
        try: st = placeholder_stat(path)
        except FileNotFoundError: return None
        state = self.native.state(path)
        if state and state['identity'].get('task') != self.task['id']:
            raise RuntimeError('文件属于其他同步任务。')
        if not state and getattr(st, 'st_file_attributes', 0) & 0x400:
            raise RuntimeError('同步目录含非云文件重解析点。')
        import stat
        return dict(kind='dir' if stat.S_ISDIR(st.st_mode) else 'file', size=st.st_size,
                    mtime=st.st_mtime_ns, state=state, usn=self.native.usn(rel))

    def refresh_inventory(self, previous, paths):
        result = dict(previous)
        roots = []
        for raw in sorted(paths, key=lambda p: (p.count('/'), p.casefold())):
            path = relative_path(raw)
            if any(excluded(part) for part in path.split('/')): continue
            if any(path == parent or path.startswith(parent+'/') for parent in roots): continue
            roots.append(path)
            for old in list(result):
                if old.casefold() == path.casefold() or old.casefold().startswith(path.casefold()+'/'):
                    result.pop(old)
            value = self.inventory_entry(path)
            if value is None: continue
            result[path] = value
            if value['kind'] == 'dir':
                def failure(error): raise error
                for parent, dirs, files in os.walk(self.native.path(path), followlinks=False, onerror=failure):
                    dirs[:] = [x for x in dirs if not excluded(x)]
                    for name in dirs+files:
                        if excluded(name): continue
                        rel = (Path(parent)/name).relative_to(self.root).as_posix()
                        entry = self.inventory_entry(rel)
                        if entry is not None: result[rel] = entry
            ancestor = path.rpartition('/')[0]
            while ancestor:
                value = self.inventory_entry(ancestor)
                if value is not None: result[ancestor] = value
                ancestor = ancestor.rpartition('/')[0]
        self.metrics['incremental_inventory_scans'] = self.metrics.get('incremental_inventory_scans', 0)+1
        return result

    def local_content(self, path, item):
        if item is None: return None
        if item['kind'] == 'dir': return ('dir', 0, '')
        state = item['state']
        if state and state['insync'] and not state['modified']:
            identity = state['identity']
            if 'sha1' in identity and identity.get('size') == item['size']:
                return content(identity)
        if state and state['partial']:
            raise RuntimeError('文件含未同步的部分内容，暂不执行覆盖或释放。')
        digest = sha1(self.native.path(path))
        if self.native.usn(path) != item['usn']:
            raise RuntimeError('文件正在修改，稍后重试。')
        return ('file', item['size'], digest)

    def checkpoint(self, path, remote, *, applied_pin=None):
        old = self.state['items'].get(path, {})
        with self.access_lock:
            accessed = self.access.get(path, old.get('last_access', time.time()))
        value = dict(old, remote=remote, last_access=accessed,
            applied_pin=old.get('applied_pin') if applied_pin is None else applied_pin)
        if old != value:
            self.state['items'][path] = value
            self.save()

    def upload(self, path, expected):
        source = self.native.path(path)
        # Convert without claiming cloud synchronization; this permits USN-guarded
        # completion even if the user edits the file during a slow upload.
        if self.native.state(source) is None:
            self.native.adopt(path, dict(task=self.task['id'], path=path, kind='file', id='pending', size=source.stat().st_size), synced=False)
        usn = self.native.usn(path)
        stage = self.directory/'uploads'/uuid.uuid4().hex
        stage.parent.mkdir(exist_ok=True)
        shutil.copyfile(source, stage)
        with stage.open('rb+') as durable: os.fsync(durable.fileno())
        if self.native.usn(path) != usn:
            stage.unlink(); raise RuntimeError('文件在上传准备时变化，保留修改后重试。')
        self.state['pending'] = dict(kind='upload', path=path, expected=expected, usn=usn,
                                    stage=stage.name, size=stage.stat().st_size, sha1=sha1(stage))
        self.save()
        self.fault('upload_intent')
        self.recover_pending()

    def recover_pending(self):
        pending = self.state.get('pending')
        if not pending: return
        path = relative_path(pending['path'])
        if pending.get('kind') == 'rename':
            return self.recover_rename(pending)
        if pending.get('kind') != 'upload': raise RuntimeError('未知的恢复事务。')
        stage_name = pending.get('stage', '')
        if len(stage_name) != 32 or any(c not in '0123456789abcdef' for c in stage_name):
            raise RuntimeError('上传恢复记录损坏。')
        stage = self.directory/'uploads'/stage_name
        if not stage.is_file() or stage.stat().st_size != pending['size'] or sha1(stage) != pending['sha1']:
            raise RuntimeError('上传恢复内容丢失或校验失败。')
        remote = (self.backend.recovery_entry(path) if hasattr(self.backend, 'recovery_entry') else
                  getattr(self.backend, 'recovery_snapshot', self.backend.snapshot)().get(path))
        self._check_backend()
        wanted = ('file', pending['size'], pending['sha1'])
        transaction = getattr(self.backend, 'has_transaction', lambda _:False)(pending['stage'])
        if content(remote) != wanted or transaction:
            if not transaction and version(remote) != version(pending['expected']):
                # Retain the immutable upload input and surface the conflict.
                archived = self.directory / ('conflict-upload-' + stage_name + '.json')
                atomic_write_json(archived, pending, indent=2)
                self.state['pending'] = None
                self.save()
                raise RemoteChanged('上传期间云端版本变化；原上传内容已保存在任务恢复目录。')
            self.status('uploading', '正在上传并核验', file=path)
            self.backend.operation_id = pending['stage']
            try: remote = self.backend.upload(path, stage, pending['expected'], self.cancel)
            except ConcurrentChange:
                atomic_write_json(self.directory/('conflict-upload-'+stage_name+'.json'),pending,indent=2,durable=True)
                self.state['pending'] = None
                self.save()
                raise
            finally: self.backend.operation_id = None
            self.fault('remote_uploaded')
        if self.native.path(path).exists():
            try:
                self.native.adopt(path, self.identity(path, remote), expected_usn=pending['usn'])
            except LocalContentChanged as error:
                # A new local edit must stay dirty; the uploaded version is still
                # the new remote baseline, so the next cycle uploads that edit.
                self.state['last_local_change'] = dict(path=path, expected_usn=pending['usn'], actual_usn=self.native.usn(path), error=str(error))
        self.fault('local_adopted')
        self.checkpoint(path, remote)
        self.fault('upload_checkpoint')
        self.state['pending'] = None
        self.save()
        self.activity.completed('upload', path, remote, operation=stage_name)
        self.fault('upload_complete')
        stage.unlink(missing_ok=True)

    def conflict(self, path):
        source = self.native.path(path)
        name = source.stem + '.conflict-local-' + uuid.uuid4().hex[:8] + source.suffix
        destination = source.with_name(name)
        relative = destination.relative_to(self.root).as_posix()
        self.state.setdefault('conflicts', {})[relative] = dict(source=path, created_at=time.time())
        self.save()
        # Windows rename fails if the destination already exists.
        source.rename(destination)
        self.fault('conflict_renamed')
        return relative

    def refresh_remote(self, path, remote, local):
        keep = effective_policy(self.task, path) == 'always_keep'
        stage = self.directory/'downloads'/uuid.uuid4().hex
        key = (path, version(remote))
        try:
            if keep:
                stage.parent.mkdir(exist_ok=True)
                # Keep the old offline copy until the complete replacement is
                # available and verified. No network wait under an NTFS lock.
                self.backend.download(path, remote, stage, self.cancel)
                self._check_backend()
                with self.access_lock: self.prefetched[key] = stage
            else:
                self.backend.expect(path, remote)
                self._check_backend()
            self.native.update_remote(path, self.identity(path, remote), local['usn'])
            self.fault('remote_refreshed')
            self.checkpoint(path, remote)
            if keep:
                self.native.hydrate(path)
                self.activity.completed('download', path, remote, operation=stage.name)
        finally:
            with self.access_lock: self.prefetched.pop(key, None)
            stage.unlink(missing_ok=True)

    def local_rename(self, local, remote):
        for path in sorted(local, key=lambda p: p.count('/')):
            if path in self.state.get('conflicts', {}): continue
            hint = local[path].get('state')
            if not hint or hint.get('identity', {}).get('path') == path: continue
            fresh = self.inventory_entry(path)
            if fresh is None:
                local.pop(path, None)
                continue
            local[path] = fresh
            state = fresh['state']
            if not state: continue
            old = state['identity'].get('path')
            if old == path or old not in self.state['items'] or old in local: continue
            if self.inventory_entry(old) is not None: continue
            if path in remote: raise RemoteChanged('本地改名目标与云端文件冲突，保留双方。')
            expected = self.state['items'][old]['remote']
            if version(remote.get(old)) != version(expected):
                raise RemoteChanged('本地改名期间云端源文件变化，保留双方。')
            self.state['pending'] = dict(kind='rename', path=old, target=path, expected=expected)
            self.save()
            self.recover_pending()
            return True
        return False

    def recover_rename(self, pending):
        old, new = relative_path(pending['path']), relative_path(pending['target'])
        if self.native.path(old).exists() or not self.native.path(new).exists():
            raise RuntimeError('待恢复改名的本地路径再次变化，请先处理。')
        rows = self.remote_snapshot()
        if old in rows:
            if new in rows or version(rows[old]) != version(pending['expected']):
                raise RemoteChanged('改名恢复检测到云端冲突，已保留两端。')
            self.backend.rename(old, new, pending['expected'])
            self.fault('remote_renamed')
            rows = self.remote_snapshot()
        if old in rows or content(rows.get(new)) != content(pending['expected']):
            raise RemoteChanged('无法核验云端改名结果。')
        changes = {}
        for previous, row in list(self.state['items'].items()):
            if previous != old and not previous.startswith(old + '/'): continue
            target = new + previous[len(old):]
            remote = rows.get(target)
            if content(remote) != content(row['remote']):
                raise RemoteChanged('改名目录中的内容发生变化，保留恢复记录。')
            if self.native.path(target).exists():
                self.native.reidentify(target, self.identity(target, remote))
            changes[target] = dict(row, remote=remote)
        for previous in list(self.state['items']):
            if previous == old or previous.startswith(old + '/'): del self.state['items'][previous]
        self.state['items'].update(changes)
        self.state['pending'] = None
        self.save()
        self.activity.completed('rename', new, rows.get(new))

    def check_batch(self, batch):
        self.metrics['last_batch'] = batch.number
        return self.cycle(batch=batch)

    def cycle(self, *, batch=None):
        if self.cancel.is_set(): raise InterruptedError()
        started = time.monotonic()
        self.more_work = False
        if not self.started: self.start()
        self.status('checking', '正在核对本地与云端文件')
        if self.state.get('pending') and self.scan_complete:
            self.scan_complete(True)  # Recovery may transfer a large staged file.
        self.recover_pending()
        remote = self.remote_snapshot(batch=batch)
        # Event snapshots limit discovery work; each known path is revalidated
        # below before any mutation. Overflow/restart still performs a full scan.
        local = self.monitor.poll() if self.monitor else self.inventory()
        if self.scan_complete: self.scan_complete(remote != self._last_remote or local != self._last_local)
        while self.local_rename(local, remote):
            remote, local = self.remote_snapshot(), self.inventory()
        changed = remote != self._last_remote or local != self._last_local
        previous_remote, previous_local = self._last_remote, self._last_local
        base = copy.deepcopy(self.state['items'])
        deferred_dirs = []
        self._delete_done = 0
        self._delete_total = sum(1 for path, row in base.items() if row['remote']['kind']=='file'
            and ((path not in local and version(remote.get(path)) == version(row['remote']))
                 or (path not in remote and path in local)))
        all_names = set(remote) | set(local) | set(base)
        if previous_remote is None or previous_local is None:
            dirty = all_names
        else:
            dirty = {p for p in all_names if remote.get(p) != previous_remote.get(p) or local.get(p) != previous_local.get(p)}
            # The last observed snapshots precede our own creates/uploads. An
            # immediate user deletion may therefore equal that old absence.
            # Existing baselines missing on either side always need resolution.
            dirty.update(p for p in base if p not in remote or p not in local)
        # Newly created/edited local files precede a bulk deletion backlog.
        names = sorted(dirty, key=lambda p: (p.count('/'), 0 if p in local and (previous_local is None or local.get(p) != previous_local.get(p)) else 1, p.casefold()))
        processed = set()
        polled_at = time.monotonic()
        for number, path in enumerate(names):
            if number and self.monitor and (number % 8 == 0 or time.monotonic()-polled_at >= .25):
                latest = self.monitor.poll()
                polled_at = time.monotonic()
                incoming = {p for p in set(latest) | set(local) if latest.get(p) != local.get(p) and
                    (p not in processed or latest.get(p) and (not latest[p].get('state') or latest[p]['state'].get('modified')))}
                if incoming:
                    # All completed operations already have durable checkpoints.
                    # Keep the old comparison snapshots so unfinished work is retried.
                    self.more_work = True
                    self.status('checking', '检测到新文件，正在优先安排',
                                progress=dict(phase='queued', wait_reason='new_local_files'))
                    return True
            processed.add(path)
            if self.cancel.is_set(): raise InterruptedError()
            r, l = remote.get(path), self.inventory_entry(path)
            if l is None: local.pop(path, None)
            else: local[path] = l
            b = base.get(path, {}).get('remote')
            kinds = {x['kind'] for x in (r, l, b) if x}
            if len(kinds) > 1: raise RuntimeError('同一路径的文件与文件夹发生冲突，请先处理。')
            if kinds == {'dir'}:
                if r and l:
                    if l['state'] is None: self.native.adopt(path, self.identity(path, r))
                    self.checkpoint(path, r)
                elif r and not b:
                    self.native.create(path, self.identity(path, r), directory=True)
                    self.checkpoint(path, r)
                elif l and not b:
                    created = self.backend.mkdir(path)
                    self.native.adopt(path, self.identity(path, created))
                    self.checkpoint(path, created)
                else: deferred_dirs.append((path, r, l, b))
                continue
            lc, rc, bc = self.local_content(path, l), content(r), content(b)
            if l and r:
                if lc == rc:
                    if l['state'] is None:
                        l['usn'] = self.native.adopt(path, self.identity(path, r), synced=False, expected_usn=l['usn'])
                    if not l['state'] or not l['state']['insync'] or l['state']['identity'] != self.identity(path, r):
                        if l['state'] and l['state']['partial'] and l['state']['insync']:
                            self.native.reidentify(path, self.identity(path, r))
                        else:
                            self.native.adopt(path, self.identity(path, r), expected_usn=l['usn'])
                    self.checkpoint(path, r)
                elif b and lc == bc:
                    # An already synchronized local file can be invalidated only
                    # under an exclusive oplock and exact NTFS version check.
                    self.refresh_remote(path, r, l)
                elif b and rc == bc:
                    self.upload(path, r)
                else:
                    self.conflict(path)
                    self.native.create(path, self.identity(path, r))
                    self.fault('placeholder_created')
                    self.checkpoint(path, r)
            elif r:
                if b and version(r) == version(b):
                    self.deleting(path)
                    self.backend.delete(path, r)
                    self.state['items'].pop(path, None); self.save()
                    self.deleted(path, r)
                else:
                    self.native.create(path, self.identity(path, r))
                    self.fault('placeholder_created')
                    self.checkpoint(path, r)
            elif l:
                if b and lc == bc:
                    self.deleting(path)
                    self.backend.expect(path, None)
                    self._check_backend()
                    self.native.remove(path, l['usn'])
                    self.fault('local_deleted')
                    self.state['items'].pop(path, None); self.save()
                    self.deleted(path, b)
                else: self.upload(path, None)
            else:
                self.state['items'].pop(path, None); self.save()
        for path, r, l, b in reversed(deferred_dirs):
            if r and not l:
                self.backend.delete(path, r)  # Refuses nonempty directories.
            elif l and not r:
                self.backend.expect(path, None)
                self._check_backend()
                self.native.remove(path, self.native.usn(path))  # Also refuses nonempty directories.
            self.state['items'].pop(path, None); self.save()
        self.apply_policies(paths=dirty)
        self.metrics['reconciled_items'] = self.metrics.get('reconciled_items', 0)+len(names)
        self._last_remote, self._last_local = copy.deepcopy(remote), copy.deepcopy(local)
        self.metrics['cycles'] += 1
        self.metrics['cycle_seconds'] = round(time.monotonic()-started, 3)
        if hasattr(self.backend, 'metrics'): self.metrics['backend'] = dict(self.backend.metrics)
        if self.state_store is not None:
            self.state_store.export()
            self.metrics['state_store'] = dict(self.state_store.metrics)
        self.status('idle', '本轮文件核对完成', files=len(self.state['items']), last_check_at=time.time(), metrics=dict(self.metrics))
        return changed

    def deleting(self, path):
        self.status('syncing', '正在同步删除', file=path,
                    progress=dict(phase='deleting', name=path, completed=self._delete_done,
                                  total=max(self._delete_total, self._delete_done+1)))

    def deleted(self, path, remote):
        self._delete_done += 1
        self.activity.completed('deleted', path, remote)
        self.status('syncing', '已同步删除', file=path,
                    progress=dict(phase='deleting', name=path, completed=self._delete_done,
                                  total=max(self._delete_total, self._delete_done)))

    def apply_policies(self, paths=None):
        now = time.time()
        key = (self.task.get('policy'), self.task.get('cache_hours'), tuple(sorted(self.task.get('overrides', {}).items())))
        policy_changed = key != self._policy_key
        eligible = None if paths is None or policy_changed else set(paths)
        if eligible is not None:
            with self.access_lock: eligible.update(self.access)
            for path, row in self.state['items'].items():
                if row.get('applied_pin') != 1 and now-row.get('last_access', now) >= self.task['cache_hours']*3600:
                    eligible.add(path)
        for path, row in sorted(self.state['items'].items(), key=lambda pair: pair[0].count('/')):
            if eligible is not None and path not in eligible: continue
            if not self.native.path(path).exists(): continue
            state = self.native.state(self.native.path(path))
            if not state: continue
            # Native shell pin changes become persistent per-file exceptions.
            applied = row.get('applied_pin')
            if applied is not None and state['pin'] != applied and state['pin'] in (1, 2):
                policy = 'always_keep' if state['pin'] == 1 else 'online_only'
                if self.policy_writer:
                    self.task = self.policy_writer(path, policy)
                else:
                    self.task['overrides'][path] = policy
            desired = effective_policy(self.task, path)
            if path in self.state.get('conflicts', {}):
                # Conflict copies stay offline until the user explicitly asks
                # to release this specific copy through the shell or settings.
                if self.task.get('overrides', {}).get(path) == 'online_only':
                    del self.state['conflicts'][path]
                else: desired = 'always_keep'
            pin = 1 if desired == 'always_keep' else 0
            # Persist intent before native metadata changes, so a restart cannot
            # mistake an internally applied pin for a manual shell override.
            if row.get('applied_pin') != pin:
                row['applied_pin'] = pin
                self.save()
            if state['pin'] != pin: self.native.pin(path, pin)
            if row['remote']['kind'] != 'file': continue
            if pin == 1:
                if state['partial']:
                    with self.access_lock: self.background_hydrations.add(path)
                    try: self.native.hydrate(path)
                    finally:
                        with self.access_lock: self.background_hydrations.discard(path)
                continue
            with self.access_lock:
                row['last_access'] = max(row.get('last_access', now), self.access.get(path, 0))
                opened = self.open_counts.get(path, 0)
            manual_release = state['pin'] == 2
            expired = now - row['last_access'] >= self.task['cache_hours'] * 3600
            if not opened and not state['partial'] and state['insync'] and not state['modified'] and (manual_release or expired):
                self.backend.expect(path, row['remote'])
                self._check_backend()
                self.native.dehydrate(path)
                self.fault('dehydrated')
        self.save()
        self._policy_key = key
