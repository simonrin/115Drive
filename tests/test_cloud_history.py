import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

import twodrive
from cloud_history import CloudHistory, HISTORY_PREFIX
from runtime_json import atomic_write_json
from task_backend import OpenListBackend, RemoteChanged


class Cloud:
    """Identity-addressed provider model; raw listings include excluded names."""
    def __init__(self, root):
        self.PRIVATE = root/'private'
        self.uid = '123'
        self.dirs = {'10':('0','test'), '20':('10','branch')}
        self.files = {}
        self.calls = []
        self.posts = []
        self.counter = 1000
        self.native_115_list = twodrive.native_115_list
        self.after_post = None
        self.put('100','20','photo.png', b'original')

    def put(self, fid, parent, name, body):
        self.files[fid] = dict(fid=fid,cid=parent,n=name,s=len(body),sha=hashlib.sha1(body).hexdigest())

    def ancestry(self, cid):
        out = []
        while cid != '0':
            parent,name = self.dirs[cid]
            out.append(dict(cid=cid,name=name)); cid = parent
        return [dict(cid='0',name='root')] + list(reversed(out))

    def resolve(self, path):
        current = '0'
        for part in path.strip('/').split('/'):
            current = next((cid for cid,(parent,name) in self.dirs.items() if parent==current and name==part), None)
            if current is None: return None
        return current

    def login(self): return 'token'
    def storages(self, token):
        return [dict(mount_path='/115', driver='115 Cloud', remark='Managed by TwoDrive', addition=json.dumps(dict(cookie='UID='+self.uid+'_test;')))]

    def native_115_request(self, cookie, endpoint, query):
        self.calls.append((endpoint,copy.deepcopy(query)))
        if endpoint == 'files/getid': return dict(id=self.resolve(query['path']) or '0')
        if endpoint == 'files/file':
            cid = str(query.get('file_id',query.get('fid','10')))
            return dict(data=[dict(file_id=cid,file_name=self.dirs[cid][1],pick_code=cid)])
        if endpoint == 'files/downfolders':
            cid = query['pickcode']
            rows = [dict(fid=k,pid=v[0],fn=v[1]) for k,v in self.dirs.items() if k!=cid and any(p['cid']==cid for p in self.ancestry(k))]
            return dict(data=dict(root=dict(fid=cid,fn=self.dirs[cid][1]), count=len(rows),list=rows,has_next_page=False))
        if endpoint == 'files':
            cid = str(query['cid']); offset=query.get('offset',0)
            if query.get('cur') == 0:
                rows=[r for r in self.files.values() if any(p['cid']==cid for p in self.ancestry(r['cid']))]
            else:
                rows=[dict(cid=k,pid=p,n=n) for k,(p,n) in self.dirs.items() if p==cid]
                rows += [r for r in self.files.values() if r['cid']==cid]
            return dict(cid=cid,count=len(rows),offset=offset,path=self.ancestry(cid),data=copy.deepcopy(rows[offset:offset+1150]))
        raise AssertionError(endpoint)

    def api(self, endpoint, data, token):
        self.calls.append(('openlist:'+endpoint,copy.deepcopy(data)))
        path=data['path'].removeprefix('/115').strip('/')
        if endpoint == '/api/fs/mkdir':
            parent,_,name=path.rpartition('/')
            pid = self.resolve(parent) if parent else '0'
            assert pid is not None
            if self.resolve(path) is None:
                self.counter += 1; self.dirs[str(self.counter)] = (pid,name)
            return {}
        cid=self.resolve(path)
        rows=[] if cid is None else self.native_115_request('', 'files', dict(cid=cid))['data']
        return dict(content=[dict(name=r['n'],is_dir=not bool(r.get('fid')),size=r.get('s',0)) for r in rows])

    def post(self, endpoint, fields):
        self.posts.append((endpoint,dict(fields)))
        fid=str(fields.get('fid') or fields['fid[0]'])
        if endpoint == 'files/batch_rename':
            if fid in self.files: self.files[fid]['n']=fields['file_name']
            else: self.dirs[fid]=(self.dirs[fid][0],fields['file_name'])
        elif endpoint == 'files/move':
            if fid in self.files: self.files[fid]['cid']=fields['pid']
            else: self.dirs[fid]=(fields['pid'],self.dirs[fid][1])
        else: raise AssertionError(endpoint)
        if self.after_post: self.after_post(endpoint,fields)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.cloud=Cloud(Path(self.temp.name))
        self.backend=self.make_backend()
        self.expected=self.backend.scan_snapshot()['branch/photo.png']

    def make_backend(self, task_id='a'*32, **kwargs):
        backend=OpenListBackend(dict(id=task_id,cloud_path='/test'),self.cloud,**kwargs)
        backend.post_identity=self.cloud.post
        backend.connect()
        return backend

    def legacy(self, operation='b'*32, original='branch/photo.png', expected=None):
        expected=expected or self.expected
        archive=original.rpartition('/')[0]+'/_twodrive-deleted-'+operation+('.png' if expected['kind']=='file' else '')
        archive=archive.lstrip('/')
        with self.backend.transaction_scope(original): self.backend.rename_identity(original,archive,expected)
        tx=dict(version=1,kind='delete',path=original,expected=expected,archive=archive,backend=self.backend.identity)
        atomic_write_json(self.backend.journal_dir/(operation+'.json'),tx,durable=True)
        return operation,tx

    def test_new_delete_clears_raw_parent_and_retains_original_identity(self):
        self.backend.delete('branch/photo.png',self.expected)
        op=next(self.backend.journal_dir.glob('*.json')).stem
        receipt=self.backend.history_archive_info(op)
        self.assertEqual(receipt['stage'],'moved')
        self.assertEqual(self.cloud.files['100']['sha'],self.expected['sha1'])
        self.assertEqual(self.cloud.files['100']['cid'],receipt['destination_parent_id'])
        self.assertTrue(receipt['destination_path'].startswith('/'+HISTORY_PREFIX))
        self.assertFalse([r for r in self.cloud.files.values() if r['cid']=='20'])
        tx=json.loads((self.backend.journal_dir/(op+'.json')).read_bytes())
        self.assertEqual(set(tx),{'version','kind','path','expected','archive','backend'})

    def test_v1_migration_idempotent_no_repeat_network_or_state_write(self):
        op,tx=self.legacy()
        original=(self.backend.journal_dir/(op+'.json')).read_bytes()
        self.backend.migrate_delete_history([op])
        proof=CloudHistory(self.backend).receipt_path(op)
        stamp=proof.stat().st_mtime_ns
        self.cloud.calls.clear();self.cloud.posts.clear()
        self.backend.migrate_delete_history([op])
        self.assertEqual((self.cloud.calls,self.cloud.posts),([],[]))
        self.assertEqual(proof.stat().st_mtime_ns,stamp)
        self.assertEqual((self.backend.journal_dir/(op+'.json')).read_bytes(),original)

    def test_intent_only_or_changed_archive_never_moves(self):
        op,tx=self.legacy()
        self.cloud.files['100']['n']='photo.png'
        self.assertEqual(self.backend.migrate_delete_history([op])[op]['stage'],'not_archived')
        self.assertFalse(CloudHistory(self.backend).registry_path.exists())
        self.cloud.put('100','20',tx['archive'].split('/')[-1],b'new body')
        self.assertEqual(self.backend.migrate_delete_history([op])[op]['stage'],'not_archived')
        self.assertFalse(any(e=='files/move' for e,_ in self.cloud.posts))

    def test_lost_move_reply_recovers_by_identity_without_resubmission(self):
        op,tx=self.legacy()
        def lose(endpoint,fields):
            if endpoint=='files/move':
                self.cloud.after_post=None
                raise TimeoutError('lost response')
        self.cloud.after_post=lose
        with self.assertRaises(TimeoutError): self.backend.migrate_delete_history([op])
        restarted=self.make_backend()
        self.assertEqual(restarted.migrate_delete_history([op])[op]['stage'],'moved')
        restarted.migrate_delete_history([op])
        self.assertEqual(sum(e=='files/move' for e,_ in self.cloud.posts),1)

    def test_recreated_original_survives_while_old_archive_is_relocated(self):
        def recreate(endpoint,fields):
            if endpoint=='files/batch_rename': self.cloud.put('101','20','photo.png',b'recreated')
        self.cloud.after_post=recreate
        with self.assertRaises(RemoteChanged): self.backend.delete('branch/photo.png',self.expected)
        self.assertEqual(self.cloud.files['101']['cid'],'20')
        self.assertEqual(self.cloud.files['101']['n'],'photo.png')
        self.assertNotEqual(self.cloud.files['100']['cid'],'20')

    def test_changed_source_before_submission_never_renames(self):
        self.backend.fault=lambda step:self.cloud.put('100','20','photo.png',b'changed') if step=='before_remote_delete' else None
        with self.assertRaises(RemoteChanged): self.backend.delete('branch/photo.png',self.expected)
        self.assertEqual(self.cloud.posts,[])

    def test_changed_archive_before_move_is_preserved_in_place(self):
        op,tx=self.legacy()
        self.backend.fault=lambda step:self.cloud.put('100','20',tx['archive'].split('/')[-1],b'changed') if step=='before_history_move' else None
        with self.assertRaises(RemoteChanged): self.backend.migrate_delete_history([op])
        self.assertEqual(self.cloud.files['100']['cid'],'20')

    def test_restart_changed_same_id_archive_is_durable_conflict(self):
        op,tx=self.legacy()
        self.cloud.put('100','20',tx['archive'].split('/')[-1],b'changed after rename')
        restarted=self.make_backend()
        with self.assertRaises(RemoteChanged):restarted.scan_snapshot()
        receipt=restarted.history_archive_info(op)
        self.assertEqual(receipt['stage'],'conflict')
        self.assertEqual(receipt['observed']['sha1'],hashlib.sha1(b'changed after rename').hexdigest())
        self.assertIn(op,restarted._history_pending)
        self.assertEqual(self.cloud.files['100']['cid'],'20')
        self.assertFalse(any(e=='files/move' for e,_ in self.cloud.posts))
        self.cloud.calls.clear()
        with self.assertRaises(RemoteChanged):restarted.scan_snapshot()
        self.assertEqual(receipt,restarted.history_archive_info(op))

    def test_new_delete_archive_change_after_rename_cannot_report_success(self):
        original=self.backend.rename_identity
        def rename(*args):
            result=original(*args)
            row=self.cloud.files['100']
            self.cloud.put('100',row['cid'],row['n'],b'concurrent edit')
            return result
        self.backend.rename_identity=rename
        with self.assertRaises(RemoteChanged):self.backend.delete('branch/photo.png',self.expected)
        op=next(self.backend.journal_dir.glob('*.json')).stem
        self.assertEqual(self.backend.history_archive_info(op)['stage'],'conflict')
        self.assertIn(op,self.backend._history_pending)
        self.assertEqual(self.cloud.files['100']['cid'],'20')
        restarted=self.make_backend()
        with self.assertRaises(RemoteChanged):restarted.migrate_delete_history([op])

    def test_account_switch_before_move_is_rejected(self):
        op,tx=self.legacy()
        self.backend.fault=lambda step:setattr(self.cloud,'uid','999') if step=='before_history_move' else None
        with self.assertRaises(RemoteChanged): self.backend.migrate_delete_history([op])
        self.assertEqual(self.cloud.files['100']['cid'],'20')

    def test_registered_root_replacement_is_not_adopted(self):
        op,tx=self.legacy();self.backend.migrate_delete_history([op])
        registry=json.loads(CloudHistory(self.backend).registry_path.read_bytes())
        old=registry['root']['id']; parent,name=self.cloud.dirs[old]
        self.cloud.dirs[old]=(parent,name+'-moved');self.cloud.dirs['777']=(parent,name)
        self.cloud.put('102','20','second.png',b'second')
        second=dict(kind='file',id='102',size=6,sha1=hashlib.sha1(b'second').hexdigest())
        with self.assertRaises(RuntimeError): self.backend.delete('branch/second.png',second)
        self.assertEqual(self.cloud.files['102']['cid'],'20')

    def test_nonempty_directory_including_excluded_items_is_never_deleted(self):
        self.cloud.files.clear();self.cloud.put('100','20','desktop.ini',b'user body')
        with self.assertRaises(RemoteChanged):self.backend.delete('branch',dict(kind='dir',id='20'))
        self.assertEqual(self.cloud.posts,[])

    def test_directory_new_child_before_rename_is_preserved(self):
        self.cloud.files.clear()
        self.backend.fault=lambda step:self.cloud.put('101','20','new.png',b'new') if step=='before_remote_delete' else None
        with self.assertRaises(RemoteChanged):self.backend.delete('branch',dict(kind='dir',id='20'))
        self.assertEqual(self.cloud.dirs['20'],('10','branch'))

    def test_directory_new_child_after_move_is_conflict_not_success(self):
        self.cloud.files.clear()
        self.backend.fault=lambda step:self.cloud.put('101','20','new.png',b'new') if step=='after_history_move' else None
        with self.assertRaises(RemoteChanged):self.backend.delete('branch',dict(kind='dir',id='20'))
        op=next(self.backend.journal_dir.glob('*.json')).stem
        self.assertEqual(self.backend.history_archive_info(op)['stage'],'conflict')
        self.assertEqual(self.cloud.files['101']['cid'],'20')
        with self.assertRaises(RemoteChanged):self.backend.migrate_delete_history([op])

    def test_directory_new_child_before_move_restores_visible_name(self):
        self.cloud.files.clear()
        self.backend.fault=lambda step:self.cloud.put('101','20','new.png',b'new') if step=='before_history_move' else None
        with self.assertRaises(RemoteChanged):self.backend.delete('branch',dict(kind='dir',id='20'))
        self.assertEqual(self.cloud.dirs['20'],('10','branch'))
        op=next(self.backend.journal_dir.glob('*.json')).stem
        self.assertEqual(self.backend.history_archive_info(op)['restored_source_path'],'/test/branch')

    def test_directory_cleanup_moves_only_proven_child_archives(self):
        op,tx=self.legacy()
        self.backend.delete('branch',dict(kind='dir',id='20'))
        self.assertEqual(self.backend.history_archive_info(op)['stage'],'moved')
        self.assertNotEqual(self.cloud.dirs['20'][0],'10')
        self.assertNotEqual(self.cloud.files['100']['cid'],'20')

    def test_legacy_renamed_parent_locates_child_by_id_and_hash(self):
        child,tx=self.legacy()
        parent,ptx=self.legacy('c'*32,'branch',dict(kind='dir',id='20'))
        restarted=self.make_backend()
        result=restarted.migrate_delete_history()
        self.assertEqual(result[child]['stage'],'moved')
        self.assertEqual(result[parent]['stage'],'moved')
        self.assertEqual(result[child]['source_archive'],tx['archive'])
        self.assertTrue(result[child]['actual_source_archive'].startswith(ptx['archive']+'/'))

    def test_absent_legacy_archives_do_not_trigger_per_journal_cloud_reads(self):
        for i in range(100):
            op=f'{i:032x}'
            tx=dict(version=1,kind='delete',path='gone.png',expected=self.expected,archive='_twodrive-deleted-'+op+'.png',backend=self.backend.identity)
            atomic_write_json(self.backend.journal_dir/(op+'.json'),tx)
        self.backend._history_seeded=False
        self.cloud.calls.clear();self.backend.scan_snapshot()
        self.assertEqual(len(self.cloud.calls),5)
        self.cloud.calls.clear();self.backend.scan_snapshot()
        self.assertEqual(len(self.cloud.calls),5)

    def test_read_only_scan_never_recovers_old_archives(self):
        op,tx=self.legacy()
        reader=self.make_backend(read_only=True)
        self.cloud.calls.clear()
        reader.scan_snapshot()
        self.assertEqual(self.cloud.files['100']['cid'],'20')
        with self.assertRaises(RuntimeError):reader.migrate_delete_history([op])
        self.assertFalse(CloudHistory(reader).registry_path.exists())
        self.assertFalse(CloudHistory(reader).directory.exists())
        self.assertFalse(any(e=='openlist:/api/fs/mkdir' for e,q in self.cloud.calls))

    def test_new_upload_version_is_queued_after_first_scan_without_restarting(self):
        source=Path(self.temp.name)/'replacement.png';source.write_bytes(b'replacement')
        def upload_fresh(relative,local,cancelled):
            self.cloud.put('101','20',relative.split('/')[-1],local.read_bytes())
        self.backend.upload_fresh=upload_fresh
        result=self.backend.upload('branch/photo.png',source,self.expected,threading.Event())
        self.assertEqual(result['id'],'101')
        op=self.backend.last_operation_id
        self.assertIn(op,self.backend._history_pending)
        self.backend.scan_snapshot()
        self.assertEqual(self.backend.history_archive_info(op)['stage'],'moved')
        self.assertEqual(self.cloud.files['101']['n'],'photo.png')
        self.assertNotEqual(self.cloud.files['100']['cid'],'20')

    def test_destination_collision_preserves_both_files(self):
        op,tx=self.legacy()
        original=CloudHistory._destination_rows
        def collision(history,registry,relative,partition):
            self.cloud.put('999',partition['id'],tx['archive'].split('/')[-1],b'foreign')
            return original(history,registry,relative,partition)
        from unittest.mock import patch
        with patch.object(CloudHistory,'_destination_rows',collision):
            with self.assertRaises(RemoteChanged):self.backend.migrate_delete_history([op])
        self.assertEqual(self.cloud.files['100']['cid'],'20')
        self.assertEqual(self.cloud.files['999']['sha'],hashlib.sha1(b'foreign').hexdigest())

    def test_read_only_mutation_entries_reject_before_any_cloud_call(self):
        reader=self.make_backend(read_only=True)
        self.cloud.calls.clear();self.cloud.posts.clear()
        operations=[lambda:reader.mkdir('new'),lambda:reader.upload('new',Path('missing'),None,threading.Event()),
                    lambda:reader.upload_fresh('new',Path('missing'),threading.Event()),
                    lambda:reader.delete('branch/photo.png',self.expected),
                    lambda:reader.rename('branch/photo.png','branch/new.png',self.expected)]
        for operation in operations:
            with self.assertRaises(RuntimeError):operation()
        self.assertEqual((self.cloud.calls,self.cloud.posts),([],[]))

    def test_concurrent_tasks_share_one_durable_account_history_root(self):
        self.cloud.put('101','20','second.png',b'second')
        other=self.make_backend('d'*32)
        expected=dict(kind='file',id='101',size=6,sha1=hashlib.sha1(b'second').hexdigest())
        failures=[]
        def run(backend,path,value):
            try:backend.delete(path,value)
            except BaseException:
                import traceback
                failures.append(traceback.format_exc())
        threads=[threading.Thread(target=run,args=(self.backend,'branch/photo.png',self.expected)),
                 threading.Thread(target=run,args=(other,'branch/second.png',expected))]
        for thread in threads:thread.start()
        for thread in threads:thread.join(5)
        self.assertEqual(failures,[])
        self.assertEqual(sum(p=='0' and n.startswith(HISTORY_PREFIX) for p,n in self.cloud.dirs.values()),1)


if __name__=='__main__':unittest.main()
