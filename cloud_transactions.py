"""Identity-based cloud commits. No existing remote content is overwritten/deleted."""
import json
from pathlib import PurePosixPath
import re
import uuid
from runtime_json import atomic_write_json

INTERNAL = re.compile(r'^[._]twodrive-(stage|version|deleted)-[a-f0-9]{32}(?:\.[^/]+)?$')

def suffix(path):
    name=PurePosixPath(path).name
    return '.'+name.rpartition('.')[2] if '.' in name else ''


def internal(path):
    return any(INTERNAL.fullmatch(part) for part in path.split('/'))


class IdentityTransactions:
    def bind(self, directory):
        self.journal_dir = directory/'remote-transactions'
        self.journal_dir.mkdir(parents=True, exist_ok=True)

    def transaction_fault(self, step):
        hook = getattr(self, 'fault', None)
        if hook: hook(step)

    def transaction_upload(self, relative, source, expected, cancelled):
        from task_backend import sha1, content, version, RemoteChanged, ConcurrentChange
        operation = getattr(self, 'operation_id', None) or uuid.uuid4().hex
        self.last_operation_id = operation
        if not re.fullmatch('[a-f0-9]{32}', operation): raise ValueError('Invalid transaction ID')
        journal = self.journal_dir/(operation+'.json')
        wanted = ('file', source.stat().st_size, sha1(source))
        parent = PurePosixPath(relative).parent
        # 115 preserves the original extension during rename, even for dotfiles.
        # Staging and archived versions must use the final path's same suffix.
        stage = (parent/('_twodrive-stage-'+operation+suffix(relative))).as_posix()
        archive = (parent/('_twodrive-version-'+operation+suffix(relative))).as_posix()
        try: tx = json.loads(journal.read_text(encoding='utf-8'))
        except FileNotFoundError:
            tx = dict(version=1, path=relative, expected=expected, stage=stage, archive=archive,
                      wanted=list(wanted), backend=self.identity, uploaded=None, archived=False, committed=False)
            atomic_write_json(journal, tx, durable=True)
        if (tx.get('path'), tx.get('wanted'), tx.get('backend')) != (relative, list(wanted), self.identity):
            raise RemoteChanged('云端事务身份或内容不匹配。')
        if tx.get('aborted'): raise ConcurrentChange('此前提交已因并发冲突保留为独立版本。')
        def save(): atomic_write_json(journal, tx, indent=2, durable=True)
        def current(): return self.transaction_rows()
        rows = current()
        own = tx.get('uploaded')
        at_target = rows.get(relative)
        if own and at_target and at_target['id'] == own['id'] and content(at_target) == wanted:
            tx['committed'] = True; save(); return at_target
        staged = rows.get(stage)
        if not own:
            if staged and content(staged) != wanted:
                raise RemoteChanged('事务暂存文件校验不匹配，保留内容等待处理。')
            if not staged:
                if version(at_target) != version(expected): raise ConcurrentChange('提交前云端版本已改变。')
                self.upload_fresh(stage, source, cancelled)
                staged = current().get(stage)
            if content(staged) != wanted: raise RemoteChanged('事务上传尚未通过内容核验。')
            own = tx['uploaded'] = staged; save()
        elif not staged or staged['id'] != own['id']:
            raise RemoteChanged('云端事务暂存文件丢失或被替换。')
        self.transaction_fault('remote_staged')
        if cancelled.is_set(): raise InterruptedError('任务已暂停。')
        rows = current()
        at_target = rows.get(relative)
        if expected:
            archived = rows.get(archive)
            if archived and archived['id'] == expected['id']:
                tx['archived'] = True
            elif version(at_target) == version(expected):
                self.rename_identity(relative, archive, expected)
                tx['archived'] = True
            else:
                raise ConcurrentChange('提交期间另一客户端修改了目标；本地及云端暂存版本已保留。')
            save()
            self.transaction_fault('remote_archived')
        if current().get(relative) is not None:
            raise ConcurrentChange('目标已由另一客户端写入，保留双方版本。')
        self.transaction_fault('before_remote_promote')
        # Rename by immutable ID. 115 auto-numbers duplicate names; verify the
        # final identity and never perform WebDAV PUT/replace on the final path.
        self.rename_identity(stage, relative, own)
        self.transaction_fault('remote_promoted')
        actual = current().get(relative)
        if not actual or actual['id'] != own['id'] or content(actual) != wanted:
            raise RemoteChanged('云端提交结果变化，保留事务以便恢复。')
        tx['committed'] = True; save()
        return actual

    def transaction_delete(self, relative, expected):
        from task_backend import version, RemoteChanged
        rows = self.transaction_rows()
        if version(rows.get(relative)) != version(expected): raise RemoteChanged('删除前云端版本变化。')
        operation = uuid.uuid4().hex
        target = (PurePosixPath(relative).parent/('_twodrive-deleted-'+operation+(suffix(relative) if expected['kind']=='file' else ''))).as_posix()
        tx = dict(version=1, kind='delete', path=relative, expected=expected, archive=target, backend=self.identity)
        atomic_write_json(self.journal_dir/(operation+'.json'), tx, durable=True)
        self.transaction_fault('before_remote_delete')
        self.rename_identity(relative, target, expected)
        if self.transaction_rows().get(relative) is not None:
            raise RemoteChanged('删除期间出现新版本，已保留新版本。')

    def abort_transaction(self, operation):
        journal=self.journal_dir/(operation+'.json')
        tx=json.loads(journal.read_text(encoding='utf-8'))
        tx['aborted']=True
        atomic_write_json(journal,tx,durable=True)
        rows=self.transaction_rows();staged=rows.get(tx['stage'])
        if staged:
            if not tx.get('uploaded') or staged['id']!=tx['uploaded']['id']:
                raise RuntimeError('冲突暂存身份不匹配，拒绝改名。')
            retained=(PurePosixPath(tx['stage']).parent/('_twodrive-version-'+uuid.uuid5(uuid.NAMESPACE_OID,operation+'conflict').hex+suffix(tx['path']))).as_posix()
            self.rename_identity(tx['stage'],retained,staged)
            tx['retained']=retained
        tx['abort_complete']=True
        atomic_write_json(journal,tx,durable=True)

    def finish_aborts(self):
        for journal in self.journal_dir.glob('*.json'):
            tx=json.loads(journal.read_text(encoding='utf-8'))
            if tx.get('aborted') and not tx.get('abort_complete'):
                self.abort_transaction(journal.stem)
