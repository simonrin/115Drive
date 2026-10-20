import copy
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock
from cloud_scan import CloudScan, files_in
from sync_progress import SyncPaused


def file(fid, parent, name, data=b'content'):
    return dict(fid=fid,cid=parent,n=name,s=len(data),sha=hashlib.sha1(data).hexdigest())


class Fake115:
    SENTINEL = '.twodrive-health'
    SyncPaused = SyncPaused

    def __init__(self, root):
        self.ROOT = Path(root)
        self.PRIVATE = self.ROOT/'private'
        self.root_id = '1'
        self.dirs = {'1':dict(name='115Drive',parent='0',rows=[file('10','1',self.SENTINEL)]),
                     '2':dict(name='nested',parent='1',rows=[file('11','2','test.txt')])}
        self.dirs['1']['rows'].append(dict(cid='2',pid='1',n='nested'))
        self.warmed = []
        self.calls = []
        self.bad_page = False

    def read_json(self, path):
        return json.loads(Path(path).read_text())

    def write_json(self, path, data):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(data))

    def login(self): return 'test'
    def storages(self, _):
        return [dict(mount_path='/115',driver='115 Cloud',remark='Managed by TwoDrive',
                     cache_expiration=60,id=1,addition=json.dumps({'cookie':'test'}))]

    def ancestry(self, cid):
        result=[]
        while cid != '0':
            d=self.dirs[cid]
            result.insert(0,dict(cid=cid,name=d['name'],pid=d['parent']))
            cid=d['parent']
        return [dict(cid='0',name='root',pid='0')]+result

    def native_115_request(self, cookie, path, query):
        self.calls.append((path,copy.deepcopy(query)))
        if path == 'files/getid': return {'id':self.root_id}
        if path == 'files/file':
            return {'data':[dict(file_id=k,file_name=self.dirs[k]['name'],pick_code='root-pick')
                            for k in query['file_id'].split(',') if k in self.dirs]}
        if path == 'files/downfolders':
            rows=[dict(fid=cid,pid=d['parent'],fn=d['name']) for cid,d in self.dirs.items() if cid != self.root_id]
            return {'data':dict(root=dict(fid=self.root_id,fn='115Drive'),list=rows,count=len(rows),has_next_page=False)}
        cid=str(query['cid'])
        if query['show_dir'] == 0:
            rows=[r for d in self.dirs.values() for r in d['rows'] if r.get('fid')]
        else: rows=self.dirs[cid]['rows']
        return dict(cid=cid,count=len(rows)+(1 if self.bad_page else 0),
                    offset=query['offset'],data=copy.deepcopy(rows[query['offset']:]),path=self.ancestry(cid))

    def api(self, route, body, token):
        path=body['path'].removeprefix('/115/')
        self.warmed.append(path)
        rows=next(d['rows'] for cid,d in self.dirs.items()
                  if '/'.join(p['name'] for p in self.ancestry(cid)[1:]) == path)
        return {'content':[dict(name=r['n'],is_dir=not bool(r.get('fid')),size=r.get('s',0)) for r in rows]}


class CloudScanTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.t=Fake115(self.temp.name)
        self.scan=CloudScan(self.t,'cloud115:115Drive')
        self.p=types.SimpleNamespace(cancelled=lambda:False,scan=lambda *a:None)
        self.scan.prepare(None,self.p)
        self.scan.synchronized()
        self.t.calls.clear();self.t.warmed.clear()

    def test_unchanged_manifest_skips_tree_refresh(self):
        self.assertFalse(self.scan.prepare(set(),self.p))
        listed=[q['cid'] for p,q in self.t.calls if p=='files' and q['show_dir']==1]
        self.assertEqual(listed,['1'])
        self.assertEqual(self.t.warmed,[])

    def test_same_size_cloud_edit_refreshes_parent(self):
        self.t.dirs['2']['rows'][0]=file('11','2','test.txt',b'changed')
        self.assertTrue(self.scan.prepare(set(),self.p))
        self.assertEqual(self.t.warmed,['115Drive/nested'])
        self.assertEqual(files_in(self.scan.index['dirs'])['11'][3],hashlib.sha1(b'changed').hexdigest())

    def test_cloud_file_delete_requires_fresh_parent(self):
        self.t.dirs['2']['rows']=[]
        self.assertTrue(self.scan.prepare(set(),self.p))
        self.assertNotIn('11',files_in(self.scan.index['dirs']))
        self.assertIn('115Drive/nested',self.t.warmed)

    def test_local_edit_checks_existing_ancestors(self):
        self.assertTrue(self.scan.prepare({'nested/test.txt'},self.p))
        listed=[q['cid'] for p,q in self.t.calls if p=='files' and q['show_dir']==1]
        self.assertEqual(listed,['1','2'])

    def test_new_nested_cloud_parent_is_discovered(self):
        self.t.dirs['3']=dict(name='new',parent='2',rows=[file('12','3','new.txt')])
        self.t.dirs['2']['rows'].append(dict(cid='3',pid='2',n='new'))
        self.assertTrue(self.scan.prepare(set(),self.p))
        self.assertEqual(self.scan.index['dirs']['3']['path'],'115Drive/nested/new')

    def test_renamed_folder_refreshed_as_branch(self):
        self.t.dirs['2']['name']='renamed'
        self.t.dirs['1']['rows'][-1]['n']='renamed'
        self.assertTrue(self.scan.prepare(set(),self.p))
        self.assertEqual(self.scan.index['dirs']['2']['path'],'115Drive/renamed')

    def test_bulk_new_folder_resolves_parent_once(self):
        self.t.dirs['3']=dict(name='bulk',parent='2',rows=[
            file(str(100+i),'3',str(i)+'.txt') for i in range(500)])
        self.t.dirs['2']['rows'].append(dict(cid='3',pid='2',n='bulk'))
        self.assertTrue(self.scan.prepare(set(),self.p))
        listed=[q['cid'] for p,q in self.t.calls if p=='files' and q['show_dir']==1]
        self.assertEqual(listed.count('3'),2)  # ancestry lookup + branch validation
        self.assertEqual(len(files_in(self.scan.index['dirs'])),502)

    def test_empty_folder_addition_found_in_idle_audit(self):
        self.t.dirs['3']=dict(name='empty',parent='2',rows=[])
        self.t.dirs['2']['rows'].append(dict(cid='3',pid='2',n='empty'))
        self.assertFalse(self.scan.audit_step())
        self.assertTrue(self.scan.audit_step())
        self.assertTrue(self.scan.prepare(set(),self.p))
        self.assertIn('3',self.scan.index['dirs'])

    def test_duplicate_names_abort_without_committing_index(self):
        self.t.dirs['2']['rows'].append(file('12','2','TEST.txt'))
        with self.assertRaisesRegex(RuntimeError,'重复名称'):
            self.scan.prepare(set(),self.p)
        self.assertNotIn('12',files_in(self.scan.index['dirs']))

    def test_incomplete_pagination_aborts(self):
        self.t.bad_page=True
        with self.assertRaisesRegex(RuntimeError,'分页不完整'):
            self.scan.prepare(set(),self.p)
        self.assertTrue(self.scan.force_full)

    def test_root_replacement_does_not_reuse_history(self):
        self.t.root_id='999'
        with self.assertRaisesRegex(RuntimeError,'替换'):
            self.scan.prepare(set(),self.p)

    def test_pause_does_not_invalidate_good_index(self):
        self.p.cancelled=lambda:True
        with self.assertRaises(SyncPaused): self.scan.prepare(set(),self.p)
        self.assertIsNotNone(self.scan.index)

    def test_missing_health_marker_aborts(self):
        self.t.dirs['1']['rows']=[r for r in self.t.dirs['1']['rows'] if r['n']!=self.t.SENTINEL]
        with self.assertRaisesRegex(RuntimeError,'健康标记'):
            self.scan.prepare(set(),self.p)

    def test_reloaded_index_still_reads_live_manifest(self):
        scan=CloudScan(self.t,'cloud115:115Drive')
        self.assertFalse(scan.prepare(set(),self.p))
        self.assertTrue(any(p=='files' and q['show_dir']==0 for p,q in self.t.calls))

    def test_failed_transfer_remains_pending_across_restart(self):
        self.t.dirs['2']['rows'].append(file('12','2','new.txt'))
        self.assertTrue(self.scan.prepare(set(),self.p))
        # No synchronized() call: the transfer failed or the process exited.
        restarted=CloudScan(self.t,'cloud115:115Drive')
        self.assertTrue(restarted.prepare(set(),self.p))
        restarted.synchronized()
        self.assertFalse(restarted.prepare(set(),self.p))

    def test_restart_detects_move_with_unchanged_folder_name(self):
        self.t.dirs['3']=dict(name='destination',parent='1',rows=[])
        self.t.dirs['1']['rows'].append(dict(cid='3',pid='1',n='destination'))
        self.scan.prepare(set(),self.p);self.scan.synchronized()
        self.t.dirs['1']['rows']=[r for r in self.t.dirs['1']['rows'] if r.get('cid')!='2']
        self.t.dirs['3']['rows'].append(dict(cid='2',pid='3',n='nested'))
        self.t.dirs['2']['parent']='3'
        restarted=CloudScan(self.t,'cloud115:115Drive')
        self.assertTrue(restarted.prepare(set(),self.p))
        self.assertEqual(restarted.index['dirs']['2']['path'],'115Drive/destination/nested')

    def test_restart_detects_deep_empty_folder_without_audit(self):
        self.t.dirs['3']=dict(name='empty',parent='2',rows=[])
        self.t.dirs['2']['rows'].append(dict(cid='3',pid='2',n='empty'))
        restarted=CloudScan(self.t,'cloud115:115Drive')
        self.assertTrue(restarted.prepare(set(),self.p))
        self.assertIn('3',restarted.index['dirs'])

    def test_incomplete_folder_tree_cannot_publish_verified_index(self):
        original=self.t.native_115_request
        def truncated(cookie,path,query):
            response=original(cookie,path,query)
            if path=='files/downfolders': response['data']['count']+=1
            return response
        self.t.native_115_request=truncated
        checked=self.scan.index['checked_at']
        with self.assertRaisesRegex(RuntimeError,'计数不一致'):
            self.scan.prepare(set(),self.p)
        self.assertEqual(checked,self.t.read_json(self.scan.path)['checked_at'])

    def test_folder_cycle_rejected(self):
        self.t.dirs['2']['parent']='2'
        with self.assertRaisesRegex(RuntimeError,'循环'):
            self.scan.prepare(set(),self.p)
