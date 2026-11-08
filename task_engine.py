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
        self.access_lock = threading.RLock()
        self.access, self.open_counts = {}, {}
        self.prefetched = {}
        self.background_hydrations = set()
        self.monitor = None
        self._last_local = None
        self._last_remote = None
        self.metrics = dict(state_writes=0, inventory_scans=0, remote_scans=0, cycles=0)
        if hasattr(self.backend, 'bind'): self.backend.bind(self.directory)
        self.native = native_factory(self.root, task['id'], self.fetch, event=self.event)
        self.state_path = self.directory/'state.json'
        existed = self.state_path.exists()
        try:
            self.state = json.loads(self.state_path.read_text(encoding='utf-8'))
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
        self._saved_state = copy.deepcopy(self.state) if existed else None
        self._last_status = None

    def save(self):
        if self.state == self._saved_state: return False
        atomic_write_json(self.state_path, self.state, indent=2, durable=True)
        self._saved_state = copy.deepcopy(self.state)
        self.metrics['state_writes'] += 1
        return True

    def status(self, state, message, **extra):
        value = dict(state=state, message=message, task_id=self.task['id'], **extra)
        if value == self._last_status: return
        if write_snapshot(self.directory/'status.json', dict(value, updated_at=time.time())):
            self._last_status = value

    def enable_watch(self):
        self.monitor = LocalChangeMonitor(self.root, lambda _: self.inventory(),
            full_scan_seconds=60, poll_seconds=2)

    def local_changed(self):
        return self.monitor is not None and self.monitor.poll() != self._last_local

    def start(self, registrar=None):
        # Validate the authoritative backend before creating/registering a root.
        remote = getattr(self.backend, 'recovery_snapshot', self.backend.snapshot)()
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
        return remote

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
        rows = (self.backend.batch_snapshot(batch) if batch is not None and hasattr(self.backend, 'batch_snapshot')
                else self.backend.snapshot())
        self._check_backend()
        self._prepare_health(rows)
        return {p: row for p, row in rows.items() if not p.startswith('.twodrive-health-') and not excluded(p)}

    def _check_backend(self):
        if self.state['backend'] is not None and self.state['backend'] != self.backend.identity:
            raise RuntimeError('网盘账号或云端根目录身份变化，已停止任务。')

    def close(self):
        self.cancel.set()
        if self.monitor:
            self.monitor.close()
            self.monitor = None
        if self.started:
            self.native.close()
            self.started = False

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
            download(path, expected, stage, LinkedCancellation(cancelled, self.cancel))
            self._check_backend()
            if sha1(stage) != expected['sha1']: raise RemoteChanged('文件下载校验失败。')
            yield stage
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
                st = placeholder_stat(path)
                state = self.native.state(path)
                if state and state['identity'].get('task') != self.task['id']:
                    raise RuntimeError('文件属于其他同步任务。')
                if not state and getattr(st, 'st_file_attributes', 0) & 0x400:
                    raise RuntimeError('同步目录含非云文件重解析点。')
                result[rel] = dict(kind='dir' if path.is_dir() else 'file', size=st.st_size,
                                   mtime=st.st_mtime_ns, state=state, usn=self.native.usn(rel))
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
        remote = getattr(self.backend, 'recovery_snapshot', self.backend.snapshot)().get(path)
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
            if keep: self.native.hydrate(path)
        finally:
            with self.access_lock: self.prefetched.pop(key, None)
            stage.unlink(missing_ok=True)

    def local_rename(self, local, remote):
        for path in sorted(local, key=lambda p: p.count('/')):
            if path in self.state.get('conflicts', {}): continue
            state = local[path]['state']
            if not state: continue
            old = state['identity'].get('path')
            if old == path or old not in self.state['items'] or old in local: continue
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

    def check_batch(self, batch):
        self.metrics['last_batch'] = batch.number
        return self.cycle(batch=batch)

    def cycle(self, *, batch=None):
        if self.cancel.is_set(): raise InterruptedError()
        if not self.started: self.start()
        self.status('checking', '正在核对本地与云端文件')
        self.recover_pending()
        remote = self.remote_snapshot(batch=batch)
        # A watcher is only a scheduling hint. Reconcile against a fresh local
        # inventory after the authoritative remote scan, never a cached deletion plan.
        local = self.monitor.poll(force=True) if self.monitor else self.inventory()
        while self.local_rename(local, remote):
            remote, local = self.remote_snapshot(), self.inventory()
        changed = remote != self._last_remote or local != self._last_local
        self._last_remote, self._last_local = copy.deepcopy(remote), copy.deepcopy(local)
        base = copy.deepcopy(self.state['items'])
        removals = [p for p, row in base.items() if p not in local or p not in remote]
        if len(removals) > 50:
            raise RuntimeError('本轮删除超过 50 项，已停止任务并保留现有内容。')
        deferred_dirs = []
        names = sorted(set(remote) | set(local) | set(base), key=lambda p: (p.count('/'), p.casefold()))
        for path in names:
            if self.cancel.is_set(): raise InterruptedError()
            r, l = remote.get(path), local.get(path)
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
                    self.backend.delete(path, r)
                    self.state['items'].pop(path, None); self.save()
                else:
                    self.native.create(path, self.identity(path, r))
                    self.fault('placeholder_created')
                    self.checkpoint(path, r)
            elif l:
                if b and lc == bc:
                    self.backend.expect(path, None)
                    self._check_backend()
                    self.native.remove(path, l['usn'])
                    self.fault('local_deleted')
                    self.state['items'].pop(path, None); self.save()
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
        self.apply_policies()
        self.metrics['cycles'] += 1
        self.status('idle', '本轮文件核对完成', files=len(self.state['items']), last_check_at=time.time(), metrics=dict(self.metrics))
        return changed

    def apply_policies(self):
        now = time.time()
        for path, row in sorted(self.state['items'].items(), key=lambda pair: pair[0].count('/')):
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
