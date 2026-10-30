"""Incremental 115 metadata checks. File deletions require fresh complete lists.

The persisted index is a performance hint, never a replacement for a live file
manifest. Unknown/malformed responses fail before bisync can mutate either side.
The complete recursive directory topology is checked on each pass as well.
"""
from __future__ import annotations
import copy
import hashlib
import json
import time
from pathlib import PurePosixPath
from sync_errors import ActionRequiredError, IndexInvalidError, ScanChangedError, recovery_policy


def valid_names(rows):
    names = set()
    for row in rows:
        name = row['n']
        if not name or name in ('.', '..') or '/' in name or '\\' in name:
            raise ActionRequiredError('云端文件名无法映射到 Windows')
        key = name.casefold()
        if key in names:
            raise ActionRequiredError('云端存在重复名称（含仅大小写不同），已停止同步：' + name)
        names.add(key)


def file_key(row):
    sha = str(row.get('sha', '')).lower()
    if len(sha) != 40 or any(c not in '0123456789abcdef' for c in sha):
        raise IndexInvalidError('115 文件校验信息不完整，已停止同步')
    size = int(row['s'])
    if size < 0:
        raise IndexInvalidError('115 文件大小无效')
    return (str(row['cid']), row['n'], size, sha)


def signature(rows):
    return sorted((('f', str(r['fid']), *file_key(r)) if r.get('fid') else
                   ('d', str(r['cid']), str(r.get('pid', '')), r['n'])) for r in rows)


def files_in(dirs):
    result = {}
    for directory in dirs.values():
        for row in directory['rows']:
            if row.get('fid'):
                fid = str(row['fid'])
                if fid in result:
                    raise IndexInvalidError('115 文件被重复引用，已停止同步')
                result[fid] = file_key(row)
    return result


class CloudScan:
    VERSION = 3

    def __init__(self, owner, remote):
        if remote != 'cloud115:115Drive':
            raise ValueError('Incremental scanning is restricted to the configured sync root')
        self.t = owner
        self.remote = remote
        self.path = owner.PRIVATE / 'cloud-index.json'
        self.receipt_path = owner.PRIVATE / 'cloud-index-verified.json'
        self._content = None
        self._digest = None
        self.index = None
        self.pending = set()
        self.audit_cursor = 0
        self.token = None
        self.cookie = None
        self.requests = 0
        self.last_audit = 0
        self.force_full = False
        try:
            saved = owner.read_json(self.path)
            if saved.get('version') in (1, 2, self.VERSION) and saved.get('remote') == remote:
                files_in(saved['dirs'])  # Reject malformed persisted hints.
                if saved['version'] == self.VERSION:
                    self._content = copy.deepcopy(saved)
                    self._digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
                    saved = dict(saved, checked_at=0, needs_sync=True)
                    try:
                        receipt = owner.read_json(self.receipt_path)
                        if (receipt.get('version') == 1 and receipt.get('sha256') == self._digest
                                and isinstance(receipt.get('needs_sync'), bool)):
                            saved.update(checked_at=float(receipt['checked_at']), needs_sync=receipt['needs_sync'])
                    except (OSError, ValueError, KeyError, TypeError):
                        pass  # An uncommitted index remains pending across restart.
                self.index = saved
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            pass

    def persist(self, index):
        # Commit immutable content first, then a small SHA-256-bound receipt.
        # A crash between the two leaves a mismatch, never a fresh stale index.
        content = {k: v for k, v in index.items() if k not in ('checked_at', 'needs_sync')}
        written = self._content != content
        if written:
            self.t.write_json(self.path, content)
            self._digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
            self._content = copy.deepcopy(content)
        self.t.write_json(self.receipt_path, dict(version=1, sha256=self._digest,
            checked_at=index['checked_at'], needs_sync=index['needs_sync']))
        self.index = index
        return written

    def connect(self):
        self.token = self.t.login()
        record = next((r for r in self.t.storages(self.token) if r['mount_path'] == '/115'), None)
        if not record or record['driver'] != '115 Cloud' or record.get('remark') != 'Managed by TwoDrive':
            raise ActionRequiredError('增量检查仅适用于 TwoDrive 管理的 115 Cloud 挂载')
        if record.get('cache_expiration', 0) < 60:
            # Scope checked above; retain every other storage setting.
            record['cache_expiration'] = 60
            self.t.save_storage(self.token, record)
        self.cookie = json.loads(record['addition'])['cookie']
        identity = hashlib.sha256((str(record['id']) + self.cookie).encode()).hexdigest()
        if self.index and self.index.get('identity') != identity:
            self.index = None
        self.identity = identity

    def request(self, path, query, progress=None):
        if progress and progress.cancelled():
            raise self.t.SyncPaused()
        self.requests += 1
        return self.t.native_115_request(self.cookie, path, query)

    def listing(self, cid, *, recursive=False, progress=None):
        rows = []
        expected_count = None
        seen = set()
        ancestry = None
        while True:
            response = self.request('files', dict(aid=1, cid=cid, o='file_name', asc=1,
                offset=len(rows), show_dir=0 if recursive else 1, cur=0 if recursive else 1,
                limit=1150, format='json', snap=0, natsort=0, record_open_time=0, fc_mix=0), progress)
            if str(response.get('cid')) != str(cid):
                raise IndexInvalidError('115 返回了不同目录，已停止同步')
            count = int(response['count'])
            if count < 0 or (expected_count is not None and count != expected_count):
                raise ScanChangedError('115 目录在分页时发生变化，稍后重新检查')
            expected_count = count
            ancestry = response.get('path')
            page = response.get('data')
            if not isinstance(page, list) or (not page and len(rows) < count):
                raise IndexInvalidError('115 目录分页不完整，已停止同步')
            if int(response.get('offset', len(rows))) != len(rows):
                raise IndexInvalidError('115 返回了重复分页，已停止同步')
            for row in page:
                key = ('f', str(row['fid'])) if row.get('fid') else ('d', str(row['cid']))
                if key in seen or (recursive and key[0] != 'f'):
                    raise IndexInvalidError('115 批量清单不完整，已停止同步')
                seen.add(key)
                if row.get('fid'):
                    file_key(row)
                rows.append(row)
            if len(rows) > count:
                raise IndexInvalidError('115 目录计数不一致，已停止同步')
            if progress and recursive:
                progress.scan('115Drive', 0, len(rows), 'manifest' if recursive else 'incremental')
            if len(rows) == count:
                break
        if not isinstance(ancestry, list):
            raise IndexInvalidError('115 目录路径缺失，已停止同步')
        return rows, ancestry

    @staticmethod
    def relative_path(ancestry, root_id):
        ids = [str(item.get('cid')) for item in ancestry]
        if str(root_id) not in ids:
            return None
        start = ids.index(str(root_id))
        parts = [item['name'] for item in ancestry[start:]]
        if not parts or parts[0] != '115Drive' or any('/' in p or '\\' in p or p in ('', '.', '..') for p in parts):
            raise IndexInvalidError('115 目录路径无法安全映射')
        return '/'.join(parts)

    def manifest(self, root_id, progress):
        rows, ancestry = self.listing(root_id, recursive=True, progress=progress)
        if self.relative_path(ancestry, root_id) != '115Drive':
            raise IndexInvalidError('115 同步根目录路径发生变化')
        by_parent = {}
        result = {}
        for row in rows:
            result[str(row['fid'])] = file_key(row)
            by_parent.setdefault(str(row['cid']), []).append(row)
        for values in by_parent.values():
            valid_names(values)
        return result

    def warm(self, path, rows):
        response = self.t.api('/api/fs/list', dict(path='/115/' + path, password='',
            page=1, per_page=0, refresh=True), self.token)
        # Do not allow a merged duplicate, stale page, or a mid-scan mutation to
        # masquerade as the raw directory we just validated.
        expected = sorted((r['n'], not bool(r.get('fid')), int(r.get('s', 0)) if r.get('fid') else 0) for r in rows)
        actual = sorted((r['name'], r['is_dir'], int(r['size']) if not r['is_dir'] else 0)
                        for r in response['content'] or [])
        if expected != actual:
            raise ScanChangedError('网盘目录在检查时发生变化，已停止本轮并等待重试')

    def refresh(self, dirs, cid, path, root_id, progress, *, full=False):
        old = dirs.get(cid)
        rows, ancestry = self.listing(cid, progress=progress)
        if self.relative_path(ancestry, root_id) != path:
            raise IndexInvalidError('网盘目录位置已改变，下一轮将完整核对')
        valid_names(rows)
        changed = old is None or old['path'] != path or signature(old['rows']) != signature(rows)
        if full or changed:
            self.warm(path, rows)
        # Remove obsolete branches. Recreated directories have different IDs.
        children = {str(r['cid']): r['n'] for r in rows if not r.get('fid')}
        for row in old['rows'] if old else []:
            if row.get('fid'):
                continue
            child = str(row['cid'])
            if child not in children or children[child] != row['n']:
                prefix = path + '/' + row['n']
                for key in list(dirs):
                    if dirs[key]['path'] == prefix or dirs[key]['path'].startswith(prefix + '/'):
                        del dirs[key]
        dirs[cid] = dict(path=path, rows=rows)
        if progress:
            progress.scan(path, len(dirs), sum(len(d['rows']) for d in dirs.values()),
                          'cloud_check' if full else 'incremental')
        for child, name in children.items():
            child_path = path + '/' + name
            if full or child not in dirs or dirs[child]['path'] != child_path:
                self.refresh(dirs, child, child_path, root_id, progress, full=full)
        return changed

    @staticmethod
    def tree_in(dirs):
        return {str(r['cid']): (str(r['pid']), r['n'])
                for d in dirs.values() for r in d['rows'] if not r.get('fid')}

    def folder_tree(self, root_id, progress):
        """Fresh recursive directory topology, including empty folders and moves.

        Protocol reference: p115client.client.download_folders (files/downfolders).
        This endpoint only lists metadata; it does not start a download task.
        """
        pickcode = self.index.get('root_pickcode') if self.index else None
        if not pickcode:
            rows = self.request('files/file', {'file_id': root_id}, progress).get('data')
            if not isinstance(rows, list) or len(rows) != 1 or str(rows[0]['file_id']) != root_id or rows[0]['file_name'] != '115Drive':
                raise IndexInvalidError('115 同步根目录信息不匹配')
            pickcode = rows[0]['pick_code']
        if not pickcode:
            raise IndexInvalidError('115 同步根目录提取码缺失')
        self.root_pickcode = pickcode
        tree = {}
        expected = None
        page = 1
        while True:
            data = self.request('files/downfolders', dict(pickcode=pickcode, page=page, per_page=5000), progress)['data']
            if str(data['root']['fid']) != root_id or data['root']['fn'] != '115Drive':
                raise IndexInvalidError('115 批量目录根节点不匹配')
            count = int(data['count'])
            if count < 0 or (expected is not None and count != expected):
                raise ScanChangedError('115 批量目录计数发生变化')
            expected = count
            rows = data['list']
            if not isinstance(rows, list) or not isinstance(data['has_next_page'], bool):
                raise IndexInvalidError('115 批量目录分页无效')
            for r in rows:
                cid, parent = str(r['fid']), str(r['pid'])
                if cid in tree or cid == root_id or cid == parent:
                    raise IndexInvalidError('115 批量目录重复或存在循环')
                valid_names([dict(n=r['fn'])])
                tree[cid] = (parent, r['fn'])
            if len(tree) > count or (data['has_next_page'] and (not rows or len(tree) >= count)):
                raise IndexInvalidError('115 批量目录分页不完整')
            if not data['has_next_page']:
                if len(tree) != count:
                    raise IndexInvalidError('115 批量目录计数不一致')
                break
            page += 1
        # Reject disconnected/cyclic data before using it to infer deletions.
        connected = {root_id}
        for cid in tree:
            chain = set()
            while cid not in connected:
                if cid in chain or cid not in tree:
                    raise IndexInvalidError('115 批量目录路径不完整')
                chain.add(cid)
                cid = tree[cid][0]
            connected.update(chain)
        return tree

    def folder_changes(self, dirs, tree):
        before = self.tree_in(dirs)
        dirty = set()
        for cid in before.keys() | tree.keys():
            if before.get(cid) == tree.get(cid):
                continue
            for value, topology in ((before.get(cid), before), (tree.get(cid), tree)):
                if value:
                    parent = value[0]
                    while parent not in dirs:
                        parent = topology[parent][0]
                    dirty.add(parent)
        return dirty

    def prepare(self, changed_paths, progress):
        started = time.monotonic()
        self.requests = 0
        self.connect()
        root_id = str(self.request('files/getid', {'path': '115Drive'}, progress).get('id', ''))
        if not root_id or root_id == '0':
            raise ActionRequiredError('115 同步根目录不可访问，已停止同步')
        if self.index and self.index['root_id'] != root_id:
            raise ActionRequiredError('115 同步根目录已被替换，需检查原同步历史')
        mode = 'incremental'
        try:
            tree = self.folder_tree(root_id, progress)
            if self.index is None or self.force_full:
                mode = 'full'
                dirs = {}
                self.refresh(dirs, root_id, '115Drive', root_id, progress, full=True)
                live = self.manifest(root_id, progress)
                changed = True
            else:
                dirs = copy.deepcopy(self.index['dirs'])
                before = files_in(dirs)
                live = self.manifest(root_id, progress)
                changed = self.index.get('needs_sync', True) or live != before or bool(changed_paths) or bool(self.pending)
                # Confirm a changed paginated manifest before propagating deletes.
                topology_changed = tree != self.tree_in(dirs)
                if (live != before or topology_changed) and (self.manifest(root_id, progress) != live or self.folder_tree(root_id, progress) != tree):
                    raise ScanChangedError('网盘文件仍在变化，稍后重新检查')
                dirty = self.folder_changes(dirs, tree) | self.pending | {root_id}
                changed = changed or bool(dirty - {root_id})
                resolved_parents = {}
                for fid in before.keys() | live.keys():
                    if before.get(fid) != live.get(fid):
                        for value in (before.get(fid), live.get(fid)):
                            if value:
                                parent = value[0]
                                if parent in dirs:
                                    dirty.add(parent)
                                else:
                                    # Resolve a newly populated folder to the closest
                                    # already known ancestor; scan its new branches.
                                    if parent not in resolved_parents:
                                        _, ancestry = self.listing(parent, progress=progress)
                                        if self.relative_path(ancestry, root_id) is None:
                                            raise ScanChangedError('网盘文件位置在检查期间变化')
                                        known = [str(a['cid']) for a in ancestry if str(a['cid']) in dirs]
                                        resolved_parents[parent] = known[-1]
                                    dirty.add(resolved_parents[parent])
                paths = {d['path']: cid for cid, d in dirs.items()}
                for name in changed_paths or []:
                    parent = PurePosixPath('115Drive') / PurePosixPath(name.replace('\\', '/')).parent
                    while str(parent) != '.':
                        if str(parent) in paths:
                            dirty.add(paths[str(parent)])
                        parent = parent.parent
                # Ancestors first, so deletions and renames invalidate old branches.
                ancestors = set()
                for cid in dirty:
                    if cid in dirs:
                        parent = PurePosixPath(dirs[cid]['path']).parent
                        while str(parent) in paths:
                            ancestors.add(paths[str(parent)])
                            parent = parent.parent
                dirty |= ancestors
                order = sorted((cid for cid in dirty if cid in dirs), key=lambda cid: dirs[cid]['path'].count('/'))
                for cid in order:
                    if cid in dirs:
                        changed = self.refresh(dirs, cid, dirs[cid]['path'], root_id, progress) or changed
            if files_in(dirs) != live:
                raise IndexInvalidError('增量目录与完整文件清单不一致，下一轮将完整核对')
            if self.tree_in(dirs) != tree:
                raise IndexInvalidError('增量目录与完整目录树不一致，下一轮将完整核对')
            if not any(r.get('fid') and r['n'] == self.t.SENTINEL for r in dirs[root_id]['rows']):
                raise ActionRequiredError('网盘健康标记缺失，已停止同步')
            index = dict(version=self.VERSION, remote=self.remote, root_id=root_id,
                              identity=self.identity, dirs=dirs, checked_at=time.time(),
                              root_pickcode=self.root_pickcode, tree_verified=True)
            # A freshly observed cloud change must remain pending until rclone
            # succeeds. A failed transfer/restart must never acknowledge it.
            index['needs_sync'] = changed
            index_written = self.persist(index)
            self.pending.clear()
            self.force_full = False
            report = dict(mode=mode, seconds=round(time.monotonic()-started, 3),
                          native_requests=self.requests, files=len(live), directories=len(dirs),
                          changed=changed, checked_at=self.index['checked_at'],
                          index_written=index_written, index_bytes=self.path.stat().st_size,
                          receipt_bytes=self.receipt_path.stat().st_size)
            self.t.write_json(self.t.ROOT/'runtime/scan-metrics.json', report)
            return changed
        except self.t.SyncPaused:
            raise
        except Exception as error:
            if recovery_policy(error).rebuild_index:
                self.force_full = True
            raise

    def synchronized(self):
        if self.index:
            self.persist(dict(self.index, needs_sync=False))

    def audit_step(self):
        """One read-only directory per idle worker turn; never block on a full walk."""
        if not self.index or not self.cookie or self.pending:
            return bool(self.pending)
        ids = sorted(self.index['dirs'])
        cid = ids[self.audit_cursor % len(ids)]
        directory = self.index['dirs'][cid]
        rows, ancestry = self.listing(cid)
        actual_path = self.relative_path(ancestry, self.index['root_id'])
        if actual_path != directory['path']:
            # The next bulk topology check resolves moves without a full walk.
            self.pending.add(cid)
        elif signature(rows) != signature(directory['rows']):
            self.pending.add(cid)
        self.audit_cursor += 1
        self.last_audit = time.time()
        self.t.write_json(self.t.ROOT/'runtime/scan-audit.json',dict(
            checked_at=self.last_audit, directories=len(ids),
            cursor=self.audit_cursor, pending_change=bool(self.pending)))
        return bool(self.pending)
