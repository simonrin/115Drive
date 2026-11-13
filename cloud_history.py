"""Verified relocation of retained cloud versions outside every sync root.

Existing v1 remote transaction journals remain immutable. A separate account
registry and per-operation receipt make an uncertain move recoverable by ID.
"""
from __future__ import annotations
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import threading
import uuid

from runtime_json import atomic_write_json
from sync_tasks import file_lock, relative_path
from task_names import remote_relative
from remote_manifest import ancestry_matches

HISTORY_PREFIX = '115Drive 同步历史-'
HISTORY_NAME = re.compile(r'^115Drive 同步历史-[0-9a-f]{32}$')
_LOCKS = {}
_LOCKS_LOCK = threading.Lock()


def _version(item):
    if item is None: return None
    return item['kind'], item['id'], item.get('size', 0), item.get('sha1', '')


class CloudHistory:
    def __init__(self, backend):
        if not backend.account_key or not backend.identity: raise RuntimeError('历史区需要已核验的网盘账号。')
        self.backend = backend
        self.account = backend.account_key
        self.source_identity = backend.identity
        self.directory = Path(backend.t.PRIVATE/'cloud-history')/self.account
        self.registry_path = self.directory/'root.json'
        self.operation_dir = self.directory/'operations'
        # Windows resolve() can temporarily return a \\?\ spelling while the
        # directory is being created; normalize it before choosing the lock.
        key = os.path.normcase(str(self.directory.absolute()).removeprefix('\\\\?\\'))
        with _LOCKS_LOCK: self.lock = _LOCKS.setdefault(key, threading.RLock())

    @contextmanager
    def locked(self):
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            with file_lock(self.directory/'history.lock'):
                if self.backend.account_key != self.account: raise RuntimeError('历史区账号身份变化。')
                yield

    def receipt_path(self, operation):
        if not re.fullmatch('[a-f0-9]{32}', operation): raise ValueError('Invalid history operation')
        return self.operation_dir/(self.backend.task['id']+'-'+operation+'.json')

    def info(self, operation):
        try: value = json.loads(self.receipt_path(operation).read_text(encoding='utf-8'))
        except FileNotFoundError: return None
        if (not isinstance(value, dict) or value.get('version') != 1 or value.get('account') != self.account
                or value.get('task_id') != self.backend.task['id'] or value.get('operation') != operation):
            raise RuntimeError('云端历史映射损坏，已保留原文件。')
        return value

    def _save(self, path, value):
        # Compare before replacing: completed/unchanged history is never a
        # heartbeat write, and intent must be durable before a remote mutation.
        try: old = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError: old = None
        if old != value: atomic_write_json(path, value, indent=2, durable=True)

    def conflict(self, operation, tx, reason, *, actual_archive=None, observed=None):
        """Persist a failed new deletion even before a destination is allocated."""
        with self.locked():
            receipt = self.info(operation)
            if receipt is None:
                receipt = dict(version=1, account=self.account, task_id=self.backend.task['id'], operation=operation,
                    backend=tx['backend'], source_root_id=self.backend.root_id, expected=tx['expected'],
                    source_cloud_path='/'+self.backend.remote+'/'+remote_relative(tx['path']),
                    source_archive=tx['archive'], actual_source_archive=actual_archive or tx['archive'])
            if observed is not None: receipt['observed'] = observed
            receipt.update(stage='conflict', reason=reason)
            self._save(self.receipt_path(operation), receipt)
            return receipt

    def _lookup(self, path):
        result = self.backend._raw_request(self.backend.cookie, 'files/getid', {'path':path.lstrip('/')})
        identity = result.get('id')
        return str(identity) if identity and str(identity) != '0' else None

    def _verify_account(self):
        from task_backend import RemoteChanged
        self.backend.connect()
        if self.backend.account_key != self.account or self.backend.identity != self.source_identity:
            raise RemoteChanged('归档期间网盘账号或同步根变化。')

    def _check_outside_tasks(self, path):
        roots = ['/' + self.backend.remote]
        source = self.backend.t.PRIVATE/'sync-tasks.json'
        if Path(source).exists():
            configured = json.loads(Path(source).read_text(encoding='utf-8'))
            roots.extend(task['cloud_path'] for task in configured['tasks'])
        for root in roots:
            root = root.rstrip('/').casefold()
            if not root or path.casefold() == root or path.casefold().startswith(root+'/'):
                raise RuntimeError('同步历史区不能位于同步目录中。')

    def _root(self):
        try: registry = json.loads(self.registry_path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            name = HISTORY_PREFIX + uuid.uuid4().hex
            if self._lookup('/'+name) is not None:
                raise RuntimeError('随机历史区名称已存在，拒绝接管未知目录。')
            registry = dict(version=1, account=self.account,
                            root=dict(name=name, path='/'+name, id=None), directories={})
            self._check_outside_tasks('/'+name)
            self._save(self.registry_path, registry)
        root = registry.get('root', {})
        if (registry.get('version') != 1 or registry.get('account') != self.account
                or not HISTORY_NAME.fullmatch(root.get('name', '')) or root.get('path') != '/'+root['name']
                or not isinstance(registry.get('directories'), dict)):
            raise RuntimeError('同步历史区登记损坏，不能自动重建。')
        self._check_outside_tasks(root['path'])
        actual = self._lookup(root['path'])
        if root['id'] is not None:
            if actual != root['id']: raise RuntimeError('同步历史区已移动或被替换，保留原归档等待处理。')
        else:
            # The unpredictable name was persisted before creation. A lost
            # mkdir reply can adopt that exact pending path on the next retry.
            if actual is None:
                self.backend._api('/api/fs/mkdir', dict(path='/115'+root['path']))
                actual = self._lookup(root['path'])
            if actual is None: raise RuntimeError('同步历史区创建尚未确认。')
            root['id'] = actual
            self._save(self.registry_path, registry)
        return registry

    def _partition(self, registry, operation):
        root = registry['root']
        relative = '任务-'+self.backend.task['id']+'/'+operation[:2]
        prefix = ''
        for name in relative.split('/'):
            prefix = (prefix+'/' if prefix else '')+name
            path = root['path']+'/'+prefix
            directory = registry['directories'].get(prefix)
            if directory is None:
                directory = dict(path=path, id=None)
                registry['directories'][prefix] = directory
                self._save(self.registry_path, registry)
            if directory.get('path') != path: raise RuntimeError('历史分片路径登记不一致。')
            actual = self._lookup(path)
            if directory['id'] is not None:
                if actual != directory['id']: raise RuntimeError('历史分片目录已被替换。')
            else:
                if actual is None:
                    self.backend._api('/api/fs/mkdir', dict(path='/115'+path))
                    actual = self._lookup(path)
                if actual is None: raise RuntimeError('历史分片目录创建尚未确认。')
                directory['id'] = actual
                self._save(self.registry_path, registry)
        return relative, registry['directories'][relative]

    def _destination_rows(self, registry, relative, partition):
        root = registry['root']
        hints = {key: value['id'] for key, value in registry['directories'].items()}
        def request(cookie, endpoint, query):
            response = self.backend._raw_request(cookie, endpoint, query)
            if not ancestry_matches(response.get('path'), root['name'], root['id'], relative, hints):
                raise RuntimeError('历史分片目录路径或身份变化，停止移动。')
            return response
        from cloud_scan import file_key, valid_names
        rows = self.backend.t.native_115_list(self.backend.cookie, partition['id'], request=request)
        valid_names(rows)
        result = {}
        for row in rows:
            if row.get('fid'):
                file_key(row)
                if str(row['cid']) != partition['id']: raise RuntimeError('历史文件父目录不一致。')
                item = dict(kind='file',id=str(row['fid']),size=int(row['s']),sha1=row['sha'].lower())
            else:
                if str(row.get('pid', partition['id'])) != partition['id']: raise RuntimeError('历史目录归属不一致。')
                item = dict(kind='dir',id=str(row['cid']))
            result[row['n']] = item
        return result

    def _children(self, identity):
        # Emptiness means every provider entry, including excluded filenames.
        return self.backend.t.native_115_list(self.backend.cookie, identity, request=self.backend._raw_request)

    def _finish(self, operation, receipt, expected):
        from task_backend import ConcurrentChange
        if expected['kind'] == 'dir' and self._children(expected['id']):
            receipt['stage'] = 'conflict'
            receipt['reason'] = '归档目录出现并发新增文件；正文保留，等待恢复处理。'
            self._save(self.receipt_path(operation), receipt)
            raise ConcurrentChange(receipt['reason'])
        parent = receipt['actual_source_archive'].rpartition('/')[0]
        self.backend._api('/api/fs/list', dict(path='/115/'+self.backend.remote+('/'+remote_relative(parent) if parent else ''),
                                             password='', page=1, per_page=0, refresh=True))
        receipt['stage'] = 'moved'
        self._save(self.receipt_path(operation), receipt)
        return receipt

    def archive(self, operation, tx, *, actual_archive=None):
        """Move only a freshly proven retained item; recover uncertain moves."""
        from task_backend import RemoteChanged, ConcurrentChange
        if self.backend.read_only: raise RuntimeError('只读会话禁止迁移同步历史。')
        expected = tx.get('expected')
        if (tx.get('version') != 1 or tx.get('backend') != self.backend.identity
                or not isinstance(expected, dict) or expected.get('kind') not in ('file','dir')):
            raise RemoteChanged('删除历史的账号或文件身份不匹配。')
        original = relative_path(tx['path']); archive = relative_path(actual_archive or tx['archive'])
        parent = archive.rpartition('/')[0]
        with self.locked():
            receipt = self.info(operation)
            wanted = dict(account=self.account, task_id=self.backend.task['id'], operation=operation,
                          backend=tx['backend'], source_root_id=self.backend.root_id,
                          source_cloud_path='/'+self.backend.remote+'/'+remote_relative(original),
                          source_archive=tx['archive'], expected=expected)
            if receipt:
                if any(receipt.get(key) != value for key,value in wanted.items()):
                    raise RemoteChanged('已保存历史映射与原事务不一致。')
                if receipt.get('stage') == 'moved': return receipt
                if receipt.get('stage') == 'conflict': raise ConcurrentChange(receipt.get('reason', '历史归档需要处理。'))
                archive = relative_path(receipt['actual_source_archive'])
                parent = archive.rpartition('/')[0]
            self._verify_account()
            source = self.backend._scoped_rows(parent)
            if self.backend.account_key != self.account or self.backend.identity != tx['backend']:
                raise RemoteChanged('归档期间网盘账号或同步根变化。')
            archived = source.get(archive)
            if receipt is None and _version(archived) != _version(expected):
                # An old log only records intent. Missing archives/originals
                # never justify moving some similarly named current item.
                return dict(stage='not_archived', operation=operation)
            registry = self._root()
            relative, partition = self._partition(registry, operation)
            target_name = remote_relative(archive).rsplit('/',1)[-1]
            destination = partition['path']+'/'+target_name
            if receipt is None:
                receipt = dict(version=1, **wanted, source_archive_cloud_path='/'+self.backend.remote+'/'+remote_relative(archive),
                               actual_source_archive=archive,
                               history_root_id=registry['root']['id'], destination_parent_id=partition['id'],
                               destination_path=destination, stage='prepared')
                self._save(self.receipt_path(operation), receipt)
            elif (receipt.get('history_root_id') != registry['root']['id']
                    or receipt.get('destination_parent_id') != partition['id'] or receipt.get('destination_path') != destination):
                raise RemoteChanged('历史目标身份发生变化。')
            destination_rows = self._destination_rows(registry, relative, partition)
            target = destination_rows.get(target_name)
            if target is not None:
                if _version(target) != _version(expected) or archived is not None:
                    raise ConcurrentChange('历史目标被占用或来源仍存在，保留双方内容。')
                return self._finish(operation, receipt, expected)
            # Destination probing may take time; source version is re-read
            # immediately before submission. A different original-name file
            # is deliberately left untouched, including concurrent recreation.
            self.backend.transaction_fault('before_history_move')
            self._verify_account()
            source = self.backend._scoped_rows(parent)
            if self.backend.account_key != self.account or self.backend.identity != tx['backend']:
                raise RemoteChanged('归档期间网盘账号或同步根变化。')
            if _version(source.get(archive)) != _version(expected):
                raise ConcurrentChange('移入历史区前归档版本变化，保留文件。')
            if source.get(original, {}).get('id') == expected['id']:
                raise ConcurrentChange('原文件尚未完成归档，不能移入历史区。')
            if expected['kind'] == 'dir':
                children = self.backend._all_children(archive, expected)
                if children:
                    receipt['stage'] = 'conflict'
                    receipt['reason'] = '归档目录出现新文件，停止移入历史区。'
                    self._save(self.receipt_path(operation), receipt)
                    # Best-effort visible restoration only in the same parent,
                    # by the proven ID and only while original name is free.
                    if original.rpartition('/')[0] == parent and source.get(original) is None:
                        try:
                            with self.backend.transaction_scope(archive):
                                fresh = self.backend.transaction_rows()
                                if fresh.get(original) is None and _version(fresh.get(archive)) == _version(expected):
                                    self.backend.rename_identity(archive, original, expected)
                                    receipt['restored_source_path'] = receipt['source_cloud_path']
                                    self._save(self.receipt_path(operation), receipt)
                        except (RuntimeError, OSError):
                            pass  # Both the content and recovery mapping remain.
                    raise ConcurrentChange(receipt['reason'])
            receipt['stage'] = 'moving'
            self._save(self.receipt_path(operation), receipt)
            self.backend.post_identity('files/move', {'pid':partition['id'], 'fid[0]':expected['id']})
            self.backend.transaction_fault('after_history_move')
            self._verify_account()
            destination_rows = self._destination_rows(registry, relative, partition)
            source = self.backend._scoped_rows(parent)
            target = destination_rows.get(target_name)
            if _version(target) != _version(expected) or source.get(archive) is not None:
                raise RemoteChanged('历史移动尚未核验，保留事务以便恢复。')
            return self._finish(operation, receipt, expected)
