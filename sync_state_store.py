"""Durable per-item sync state with a guarded legacy JSON export.

SQLite is authoritative once state.sqlite3 exists. A legacy state.json may lag
while a cycle is running, but it must still match its last exported hash. An
unrelated JSON edit is never silently imported over acknowledged operations.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import sqlite3
import tempfile
import threading

from runtime_json import atomic_write_bytes
from sync_tasks import cloud_path, file_lock, relative_path, validate_id


DATABASE = 'state.sqlite3'
SCHEMA_VERSION = 2


class StateStoreError(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(payload):
    return hashlib.sha256(payload if isinstance(payload, bytes) else payload.encode('utf-8')).hexdigest()


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result: raise StateStoreError('同步状态包含重复字段，已保留原记录。')
        result[key] = value
    return result


def _decode(value):
    try:
        return json.loads(value, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite number')))
    except (ValueError, TypeError, UnicodeError) as error:
        raise StateStoreError('同步状态无法解析，不能重建同步基线。') from error


def _unpack_export(payload):
    state = _decode(payload)
    guarded = isinstance(state, dict) and type(state.get('version')) is int and state.get('version') == 2
    if guarded:
        if state.get('storage_format') != 'sqlite-v1':
            raise StateStoreError('同步历史导出格式不受支持，不能重建基线。')
        state = dict(state)
        state.pop('storage_format')
        state['version'] = 1
    elif isinstance(state, dict) and 'storage_format' in state:
        raise StateStoreError('同步历史导出标记与版本不一致。')
    return state, guarded


def _validate(state, previous=None):
    try:
        if not isinstance(state, dict) or type(state.get('version')) is not int or state['version'] != 1:
            raise ValueError('Invalid state version')
        validate_id(state.get('task'))
        if not isinstance(state.get('local'), str) or not PureWindowsPath(state['local']).is_absolute():
            raise ValueError('Invalid local root')
        if cloud_path(state.get('cloud')) != state['cloud']:
            raise ValueError('Invalid cloud root')
        if state.get('backend') is not None and not isinstance(state['backend'], str):
            raise ValueError('Invalid backend')
        if state.get('local_root_id') is not None and (type(state['local_root_id']) is not int or state['local_root_id'] < 0):
            raise ValueError('Invalid root identity')
        if 'health' in state and (not isinstance(state['health'], str) or not state['health']):
            raise ValueError('Invalid health identity')
        if state.get('pending') is not None and not isinstance(state['pending'], dict):
            raise ValueError('Invalid pending operation')
        if not isinstance(state.get('items'), dict): raise ValueError('Missing items')
        names = set()
        old_items = previous.get('items', {}) if previous is not None else {}
        for path, row in state['items'].items():
            if not isinstance(path, str) or path.casefold() in names:
                raise ValueError('Invalid or duplicate item path')
            names.add(path.casefold())
            if path in old_items and row == old_items[path]: continue
            if relative_path(path) != path: raise ValueError('Invalid item path')
            if not isinstance(row, dict) or not isinstance(row.get('remote'), dict):
                raise ValueError('Invalid item baseline')
            _json(row)
        _json({k:v for k,v in state.items() if k != 'items'})
    except (ValueError, TypeError, KeyError) as error:
        raise StateStoreError('同步状态结构或目录身份无效，不能重建同步基线。') from error
    return state


def _identity(previous, current):
    for key in ('version', 'task', 'local', 'cloud'):
        if previous.get(key) != current.get(key):
            raise StateStoreError('同步任务身份发生变化，已保留原基线。')
    for key in ('backend', 'local_root_id', 'health'):
        if previous.get(key) is not None and previous[key] != current.get(key):
            raise StateStoreError('同步目录身份发生变化，已保留原基线。')


def _read(connection):
    try:
        schema = connection.execute('PRAGMA user_version').fetchone()[0]
        if schema not in (1, SCHEMA_VERSION):
            raise StateStoreError('同步数据库版本不受支持，不能重建同步基线。')
        row = connection.execute('SELECT meta, digest, revision, export_hash, item_count FROM control WHERE id=1').fetchone()
        if row is None: raise StateStoreError('同步数据库缺少基线，已停止。')
        meta, digest, revision, exported, count = row
        if _digest(meta) != digest: raise StateStoreError('同步数据库元数据校验失败。')
        state = _decode(meta)
        if not isinstance(state, dict) or 'items' in state: raise StateStoreError('同步数据库结构损坏。')
        items = {}
        for path, payload, digest in connection.execute('SELECT path, payload, digest FROM items'):
            if _digest(payload) != digest: raise StateStoreError('同步数据库项目校验失败。')
            items[path] = _decode(payload)
        if len(items) != count or type(revision) is not int or revision < 0:
            raise StateStoreError('同步数据库项目计数或提交记录损坏。')
        state['items'] = items
        guard = False
        if schema >= 2:
            flags = connection.execute('SELECT guard_legacy, legacy_export FROM control WHERE id=1').fetchone()
            if flags is None or any(type(flag) is not int or flag not in (0, 1) for flag in flags):
                raise StateStoreError('同步数据库兼容性标记损坏。')
            if flags[1]:
                raise StateStoreError('同步状态已导出供旧版回退，请先完成停机回退或显式迁移，不能自动恢复数据库。')
            guard = bool(flags[0])
        return _validate(state), revision, exported, guard
    except sqlite3.DatabaseError as error:
        raise StateStoreError('同步数据库无法读取，保留原文件后检查恢复记录。') from error


def _check_export(path, state, exported):
    try: payload = path.read_bytes()
    except FileNotFoundError:
        if exported is None: return None, None
        raise StateStoreError('同步历史导出文件丢失，不能自动重建。')
    actual = _digest(payload)
    if actual == exported: return payload, actual
    if _unpack_export(payload)[0] == state:
        # The JSON rename can succeed just before a crash rolls back the small
        # export-hash transaction. Exact canonical content is safe to accept.
        return payload, actual
    raise StateStoreError('同步历史文件被外部修改，已停止以保留数据库中的已完成操作。')


def load_state(directory):
    """Read canonical state without migrating or writing; legacy behavior otherwise."""
    directory = Path(directory)
    database, exported = directory/DATABASE, directory/'state.json'
    if not database.exists():
        return json.loads(exported.read_text(encoding='utf-8'))
    try:
        connection = sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True, isolation_level=None)
        try:
            connection.execute('BEGIN')
            state, _, known, _ = _read(connection)
            _check_export(exported, state, known)
            connection.execute('ROLLBACK')
            return state
        finally: connection.close()
    except sqlite3.DatabaseError as error:
        raise StateStoreError('同步数据库无法读取，不能回退到过期的 JSON 基线。') from error


class StateStore:
    def __init__(self, directory, *, fault=None, guard_legacy=False):
        self.directory = Path(directory)
        self.path = self.directory/DATABASE
        self.json_path = self.directory/'state.json'
        self.fault = fault or (lambda _: None)
        self.connection = None
        self._state = None
        self._revision = None
        self._lock = threading.RLock()
        self._closed = False
        self.guard_legacy = bool(guard_legacy)
        self._guard_committed = False
        self.metrics = dict(commits=0, item_upserts=0, item_deletes=0, exports=0)

    def _initialize(self, supplied=None):
        self.directory.mkdir(parents=True, exist_ok=True)
        with file_lock(self.directory/'state-store.lock'):
            if self.path.exists(): return
            try:
                original = self.json_path.read_bytes()
                normalized, guarded = _unpack_export(original)
                # A guarded export is an accompaniment to a canonical DB, not
                # a backup from which a missing DB may be silently rebuilt.
                if guarded: raise StateStoreError('受保护的同步历史缺少权威数据库，已停止自动导入。')
                state = _validate(normalized)
                exported = _digest(original)
            except FileNotFoundError:
                if supplied is None: raise
                state, exported = _validate(copy.deepcopy(supplied)), None
            fd, temporary = tempfile.mkstemp(prefix='.state.sqlite3.', suffix='.tmp', dir=self.directory)
            os.close(fd)
            temporary = Path(temporary)
            connection = None
            try:
                connection = sqlite3.connect(temporary, isolation_level=None)
                connection.execute('PRAGMA synchronous=FULL')
                connection.execute('BEGIN IMMEDIATE')
                connection.execute('CREATE TABLE control(id INTEGER PRIMARY KEY CHECK(id=1), meta TEXT NOT NULL, digest TEXT NOT NULL, revision INTEGER NOT NULL, export_hash TEXT, item_count INTEGER NOT NULL, guard_legacy INTEGER NOT NULL DEFAULT 0, legacy_export INTEGER NOT NULL DEFAULT 0)')
                connection.execute('CREATE TABLE items(path TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL) WITHOUT ROWID')
                meta = _json({k:v for k,v in state.items() if k != 'items'})
                connection.execute('INSERT INTO control VALUES(1,?,?,?,?,?,0,0)', (meta, _digest(meta), 0, exported, len(state['items'])))
                for path, value in state['items'].items():
                    payload = _json(value)
                    connection.execute('INSERT INTO items VALUES(?,?,?)', (path, payload, _digest(payload)))
                connection.execute('PRAGMA user_version='+str(SCHEMA_VERSION))
                connection.execute('COMMIT')
                connection.close(); connection = None
                with temporary.open('r+b') as stream: os.fsync(stream.fileno())
                self.fault('before_import_replace')
                os.replace(temporary, self.path)
                self.fault('after_import_replace')
            finally:
                if connection is not None: connection.close()
                temporary.unlink(missing_ok=True)

    def _open(self, supplied=None):
        if self._closed: raise StateStoreError('同步状态存储已关闭。')
        if self.connection is not None: return
        if not self.path.exists(): self._initialize(supplied)
        try:
            connection = sqlite3.connect(self.path, isolation_level=None, timeout=5, check_same_thread=False)
            # Validate before changing any database setting or trusting JSON.
            connection.execute('BEGIN')
            state, revision, exported, guard = _read(connection)
            payload, _ = _check_export(self.json_path, state, exported)
            connection.execute('ROLLBACK')
            if connection.execute('PRAGMA user_version').fetchone()[0] == 1:
                connection.execute('BEGIN IMMEDIATE')
                connection.execute('ALTER TABLE control ADD COLUMN guard_legacy INTEGER NOT NULL DEFAULT 0')
                connection.execute('ALTER TABLE control ADD COLUMN legacy_export INTEGER NOT NULL DEFAULT 0')
                connection.execute('PRAGMA user_version='+str(SCHEMA_VERSION))
                connection.execute('COMMIT')
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA synchronous=FULL')
            self.guard_legacy = self.guard_legacy or guard or bool(payload and _unpack_export(payload)[1])
            self._guard_committed = guard
            self.connection, self._state, self._revision = connection, state, revision
        except Exception:
            if 'connection' in locals(): connection.close()
            raise

    def load(self):
        with self._lock:
            self._open()
            self.connection.execute('BEGIN')
            try:
                state, revision, exported, guard = _read(self.connection)
                _check_export(self.json_path, state, exported)
                self.guard_legacy = self.guard_legacy or guard
                self._guard_committed = guard
                self._state, self._revision = state, revision
                return copy.deepcopy(state)
            finally: self.connection.execute('ROLLBACK')

    def save(self, state, previous=None):
        """Commit only changed item rows; reject stale writers and identity changes."""
        with self._lock:
            _validate(state, self._state)
            self._open(state)
            # The old v1 reader must be excluded before any new canonical
            # mutation can make its JSON snapshot lag behind.
            if self.guard_legacy and not self._guard_committed: self.export()
            if previous is not None and previous != self._state:
                raise StateStoreError('同步基线已被其他写入改变，请重新载入后恢复。')
            _identity(self._state, state)
            connection = self.connection
            connection.execute('BEGIN IMMEDIATE')
            try:
                row = connection.execute('SELECT revision, export_hash FROM control WHERE id=1').fetchone()
                if row is None or row[0] != self._revision:
                    raise StateStoreError('同步数据库存在新的提交，拒绝覆盖旧基线。')
                _check_export(self.json_path, self._state, row[1])
                if self._state == state:
                    connection.execute('ROLLBACK')
                    return False
                old_items, new_items = self._state['items'], state['items']
                removed = old_items.keys()-new_items.keys()
                changed = [p for p in new_items if p not in old_items or old_items[p] != new_items[p]]
                for path in removed: connection.execute('DELETE FROM items WHERE path=?', (path,))
                for path in changed:
                    payload = _json(new_items[path])
                    connection.execute('INSERT INTO items VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET payload=excluded.payload,digest=excluded.digest', (path, payload, _digest(payload)))
                meta = _json({k:v for k,v in state.items() if k != 'items'})
                connection.execute('UPDATE control SET meta=?,digest=?,revision=revision+1,item_count=? WHERE id=1', (meta, _digest(meta), len(new_items)))
                self.fault('before_state_commit')
                connection.execute('COMMIT')
                self._state = copy.deepcopy(state)
                self._revision += 1
                self.metrics['commits'] += 1
                self.metrics['item_upserts'] += len(changed)
                self.metrics['item_deletes'] += len(removed)
                self.fault('after_state_commit')
                return True
            finally:
                if connection.in_transaction: connection.execute('ROLLBACK')

    def export(self):
        """Atomically export current canonical state without rewriting equal JSON."""
        with self._lock:
            self._open()
            connection = self.connection
            connection.execute('BEGIN IMMEDIATE')
            try:
                state, revision, known, guard = _read(connection)
                payload, actual = _check_export(self.json_path, state, known)
                self.guard_legacy = self.guard_legacy or guard
                normalized, wrapped = _unpack_export(payload) if payload is not None else (None, False)
                changed = normalized != state or wrapped != self.guard_legacy
                if changed:
                    value = dict(state, version=2, storage_format='sqlite-v1') if self.guard_legacy else state
                    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
                    self.fault('before_export_replace')
                    atomic_write_bytes(self.json_path, payload, durable=True)
                    actual = _digest(payload)
                    self.metrics['exports'] += 1
                    self.fault('after_export_replace')
                if actual != known or guard != self.guard_legacy:
                    connection.execute('UPDATE control SET export_hash=?,guard_legacy=? WHERE id=1', (actual, int(self.guard_legacy)))
                    self.fault('before_export_commit')
                    connection.execute('COMMIT')
                    self.fault('after_export_commit')
                else: connection.execute('ROLLBACK')
                self._state, self._revision = state, revision
                self._guard_committed = self.guard_legacy
                return changed
            finally:
                if connection.in_transaction: connection.execute('ROLLBACK')

    def export_legacy(self):
        """Explicit stopped-worker rollback handoff, never called during close.

        Require a current exported snapshot, then mark this DB deactivated so
        an old binary's subsequent JSON updates cannot later be ignored. The
        coordinator retains/archives the DB before launching the old version;
        re-enabling SQLite requires an explicit migration from that latest JSON.
        """
        with self._lock, file_lock(self.directory/'worker.lock'):
            self._open()
            connection = self.connection
            connection.execute('BEGIN IMMEDIATE')
            try:
                state, _, known, _ = _read(connection)
                payload, _ = _check_export(self.json_path, state, known)
                if payload is None or _unpack_export(payload)[0] != state:
                    raise StateStoreError('回退前必须先导出最新的已确认同步状态。')
                legacy = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
                atomic_write_bytes(self.json_path, legacy, durable=True)
                connection.execute('UPDATE control SET export_hash=?,legacy_export=1 WHERE id=1', (_digest(legacy),))
                connection.execute('COMMIT')
                return copy.deepcopy(state)
            finally:
                if connection.in_transaction: connection.execute('ROLLBACK')
                connection.close()
                self.connection = None
                self._closed = True

    def close(self, *, export=True):
        with self._lock:
            try:
                if export and self.connection is not None: self.export()
            finally:
                if self.connection is not None:
                    self.connection.close()
                    self.connection = None
                self._closed = True

