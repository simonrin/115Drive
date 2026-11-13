"""Fresh recursive 115 metadata, without refreshing every directory separately.

This is an observation, never permission to overwrite/delete a cached version.
All pages, identities and topology must validate before a result is returned.
"""
from __future__ import annotations
from collections import defaultdict
import urllib.error
from cloud_scan import file_key, valid_names
from task_names import local_name, excluded
from sync_errors import IndexInvalidError, TransientSyncError


class ManifestUnavailable(ValueError):
    """Unsupported or incomplete bulk metadata; use a fresh directory traversal."""


def _identity(value):
    if isinstance(value, bool) or value is None or str(value) in ('', '0'):
        raise ManifestUnavailable('云端批量清单身份缺失。')
    return str(value)


def ancestry_matches(path, remote, root_id, relative='', directory_ids=None):
    """Validate a live listing's full path, including IDs of cached ancestors."""
    from task_names import remote_relative
    if not isinstance(path, list) or not path: return False
    try:
        parts = [(str(item['cid']), item['name']) for item in path]
        # 115 includes the account root, whose localized display name varies.
        if parts and parts[0][0] == '0': parts = parts[1:]
        wanted = remote.split('/') + (remote_relative(relative).split('/') if relative else [])
        if [name for _, name in parts] != wanted: return False
        offset = len(remote.split('/')) - 1
        if parts[offset][0] != str(root_id): return False
        if directory_ids:
            prefix = ''
            for identity, name in parts[offset + 1:]:
                prefix = (prefix + '/' if prefix else '') + local_name(name)
                if directory_ids.get(prefix) != identity: return False
        return True
    except (KeyError, TypeError, ValueError, IndexError):
        return False


class RecursiveManifest:
    def __init__(self, request, remote, root_id, *, cancelled=None, progress=None):
        self.request, self.remote, self.root_id = request, remote, str(root_id)
        self.cancelled, self.progress = cancelled, progress
        self.pages = 0

    def call(self, endpoint, query):
        if self.cancelled is not None and self.cancelled.is_set():
            raise InterruptedError('任务已暂停。')
        try:
            result = self.request(endpoint, query)
        except urllib.error.HTTPError as error:
            if error.code not in (400, 404, 405, 501): raise
            raise ManifestUnavailable('云端批量接口不可用。') from error
        except TransientSyncError as error:
            # The legacy adapter represents unsuccessful provider responses
            # this way. A fresh traversal either succeeds or raises its error;
            # no partial result is reused. Login failures are a separate class.
            raise ManifestUnavailable('云端批量接口未返回成功结果。') from error
        self.pages += 1
        if not isinstance(result, dict): raise ManifestUnavailable('云端批量清单响应无效。')
        return result

    def files(self):
        rows, seen, expected = [], set(), None
        while True:
            result = self.call('files', dict(aid=1, cid=self.root_id, o='file_name', asc=1,
                offset=len(rows), show_dir=0, cur=0, limit=1150, format='json', snap=0,
                natsort=0, record_open_time=0, fc_mix=0))
            if str(result.get('cid')) != self.root_id:
                raise ManifestUnavailable('云端批量文件根身份不一致。')
            if not ancestry_matches(result.get('path'), self.remote, self.root_id):
                raise ManifestUnavailable('云端批量文件根路径不一致。')
            count, offset = int(result['count']), int(result.get('offset', -1))
            if count < 0 or offset != len(rows) or (expected is not None and count != expected):
                raise ManifestUnavailable('云端批量文件分页期间发生变化。')
            expected, page = count, result.get('data')
            if not isinstance(page, list) or (not page and len(rows) < count):
                raise ManifestUnavailable('云端批量文件分页不完整。')
            for row in page:
                if not isinstance(row, dict) or not row.get('fid') or not row.get('cid'):
                    raise ManifestUnavailable('云端批量文件身份缺失。')
                identity = _identity(row['fid'])
                if identity in seen: raise ManifestUnavailable('云端批量文件重复。')
                file_key(row)
                seen.add(identity)
                rows.append(row)
            if len(rows) > count: raise ManifestUnavailable('云端批量文件计数无效。')
            if self.progress: self.progress(len(rows), self.pages)
            if len(rows) == count: return rows

    def directories(self, pickcode):
        rows, expected, page = {}, None, 1
        while True:
            data = self.call('files/downfolders', dict(pickcode=pickcode, page=page, per_page=5000))['data']
            root = data['root']
            if str(root.get('fid')) != self.root_id or root.get('fn') != self.remote.rsplit('/', 1)[-1]:
                raise ManifestUnavailable('云端批量目录根身份不一致。')
            count, values, more = int(data['count']), data['list'], data['has_next_page']
            if count < 0 or (expected is not None and expected != count):
                raise ManifestUnavailable('云端批量目录计数发生变化。')
            if not isinstance(values, list) or type(more) is not bool:
                raise ManifestUnavailable('云端批量目录分页无效。')
            expected = count
            for row in values:
                cid, parent, name = _identity(row['fid']), _identity(row['pid']), row['fn']
                if not cid or cid in rows or cid == self.root_id or cid == parent:
                    raise ManifestUnavailable('云端批量目录身份重复或循环。')
                local_name(name)
                rows[cid] = (parent, name)
            if len(rows) > count or (more and (not values or len(rows) >= count)):
                raise ManifestUnavailable('云端批量目录分页不完整。')
            if not more:
                if len(rows) != count: raise ManifestUnavailable('云端批量目录计数不一致。')
                return rows
            page += 1

    def read(self):
        try:
            info = self.call('files/file', {'file_id': self.root_id})['data']
            if (not isinstance(info, list) or len(info) != 1
                    or str(info[0].get('file_id')) != self.root_id
                    or info[0].get('file_name') != self.remote.rsplit('/', 1)[-1]
                    or not info[0].get('pick_code')):
                raise ManifestUnavailable('云端批量目录信息不可用。')
            tree = self.directories(info[0]['pick_code'])
            files = self.files()
            paths = {self.root_id: ''}

            def resolve(cid):
                # Iterative resolution also handles very deep valid trees.
                pending, visiting = [], set()
                while cid not in paths:
                    if cid in visiting or cid not in tree:
                        raise ManifestUnavailable('云端批量目录断开或循环。')
                    visiting.add(cid); pending.append(cid); cid = tree[cid][0]
                for identity in reversed(pending):
                    parent, name = tree[identity]
                    paths[identity] = (paths[parent] + '/' if paths[parent] else '') + local_name(name)

            groups = defaultdict(list)
            for cid, (parent, name) in tree.items():
                resolve(cid)
                groups[parent].append(dict(n=name))
            for row in files:
                if str(row['cid']) not in paths: raise ManifestUnavailable('云端文件父目录缺失。')
                if str(row['fid']) in paths: raise ManifestUnavailable('云端文件与目录身份冲突。')
                groups[str(row['cid'])].append(row)
            for group in groups.values(): valid_names(group)
            result, folded = {}, set()

            def add(path, item):
                if path.casefold() in folded: raise ManifestUnavailable('云端名称映射后重复。')
                folded.add(path.casefold())
                if not excluded(path): result[path] = item

            for cid in tree:
                add(paths[cid], dict(kind='dir', id=cid))
            for row in files:
                parent = paths[str(row['cid'])]
                path = (parent + '/' if parent else '') + local_name(row['n'])
                add(path, dict(kind='file', id=str(row['fid']), size=int(row['s']), sha1=row['sha'].lower()))
            return result
        except ManifestUnavailable:
            raise
        except (KeyError, TypeError, ValueError, OverflowError, IndexInvalidError) as error:
            raise ManifestUnavailable('云端批量清单不完整，改用逐目录核验。') from error
