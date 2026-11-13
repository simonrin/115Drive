import copy
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest

from local_change_index import LocalChangeIndex, route_records, CREATE, DELETE, OLD_NAME, NEW_NAME
from usn_journal import JournalState, JournalUnavailable, UsnRecord, UsnJournal, file_id, parse_records


def record(fid,parent,usn,name,reason=1,directory=False):
    return UsnRecord(file_id(fid),file_id(parent),usn,reason,0x10 if directory else 0,name)


class World:
    def __init__(self,root):
        self.root=Path(root);self.root_id=1;self.volume_id='volume';self.journal_id=7
        self.first=0;self.cursor=100;self.records=[];self.rows={};self.ids={};self.scan_count=0;self.refreshes=[];self.on_scan=None
        self.unavailable=False;self.gap_during_read=False
    def binding(self):return dict(root=str(self.root).casefold(),root_id=file_id(self.root_id),volume_id=self.volume_id,volume='fake-volume')
    def query(self):
        if self.unavailable:raise JournalUnavailable('No journal permissions')
        return JournalState(self.journal_id,self.first,self.cursor)
    def identity(self,path):
        name=Path(path).relative_to(self.root).as_posix()
        if name not in self.rows:raise JournalUnavailable('Item vanished')
        row=self.rows[name]
        return dict(file_id=file_id(self.ids[name]),volume_id=self.volume_id,usn=row['usn'],directory=row['kind']=='dir')
    def read(self,start,stop,journal_id):
        if self.gap_during_read:raise JournalUnavailable('Journal was truncated during replay')
        if journal_id!=self.journal_id or start<self.first:raise JournalUnavailable('Journal gap')
        return [r for r in self.records if start<=r.usn<stop]
    def close(self):pass
    def scan(self):
        self.scan_count+=1;result=copy.deepcopy(self.rows)
        if self.on_scan:callback=self.on_scan;self.on_scan=None;callback()
        return result
    def refresh(self,previous,paths):
        self.refreshes.append(set(paths));result=copy.deepcopy(previous)
        for path in paths:
            for name in list(result):
                if name==path or name.startswith(path+'/'):del result[name]
            for name,row in self.rows.items():
                if name==path or name.startswith(path+'/'):result[name]=copy.deepcopy(row)
        return result
    def add(self,name,fid,*,kind='file',reason=CREATE,emit=False):
        self.ids[name]=fid;self.rows[name]=dict(kind=kind,size=1,mtime=10,state=None,usn=self.cursor)
        if emit:
            parent=self.ids.get(name.rpartition('/')[0],self.root_id)
            self.records.append(record(fid,parent,self.cursor,name.rpartition('/')[2],reason,kind=='dir'));self.cursor+=1
    def modify(self,name):
        self.rows[name]['usn']=self.cursor;self.rows[name]['size']+=1
        self.records.append(record(self.ids[name],self.ids.get(name.rpartition('/')[0],self.root_id),self.cursor,name.rpartition('/')[2],directory=self.rows[name]['kind']=='dir'));self.cursor+=1


class LocalIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'root';self.world=World(self.root)
        self.world.add('dir',2,kind='dir');self.world.add('dir/a.txt',3)
        self.index=LocalChangeIndex(self.root,Path(self.tmp.name)/'cache',journal_factory=lambda root:self.world)
        self.addCleanup(self.index.close)
    def seed(self):
        result=self.index.recover(self.world.scan,self.world.refresh)
        self.assertEqual(result.mode,'full');self.assertEqual(result.snapshot,self.world.rows)
        return result
    def restart(self):
        self.index.close();self.index=LocalChangeIndex(self.root,Path(self.tmp.name)/'cache',journal_factory=lambda root:self.world)
        self.addCleanup(self.index.close)
    def recover(self):return self.index.recover(self.world.scan,self.world.refresh)

    def test_cached_restart_avoids_full_inventory_with_complete_journal(self):
        self.seed();self.restart();result=self.recover()
        self.assertEqual(result.mode,'cached');self.assertEqual(self.world.scan_count,1)
        self.assertEqual(result.snapshot,self.world.rows)

    def test_watcher_update_without_new_journal_record_is_not_replaced_by_cache(self):
        self.seed();self.world.rows['dir/a.txt']['size']=99
        self.assertTrue(self.index.observe(self.world.rows))
        result=self.recover()
        self.assertEqual(result.snapshot['dir/a.txt']['size'],99)
        self.assertEqual(self.world.scan_count,1)
        self.assertFalse(self.index.observe(result.snapshot))

    def test_audit_new_subtree_without_journal_delta_is_durable_before_restart(self):
        self.seed();self.world.add('new',20,kind='dir');self.world.add('new/file',21)
        self.assertTrue(self.index.observe(self.world.rows))
        self.restart();self.assertEqual(self.recover().snapshot,self.world.rows)
        self.assertEqual(self.world.scan_count,1)

    def test_observation_never_advances_unread_cursor(self):
        self.seed();old_cursor=self.index.memory['meta']['cursor']
        self.world.modify('dir/a.txt');self.index.observe(self.world.rows)
        self.assertEqual(self.index.memory['meta']['cursor'],old_cursor)
        self.restart();self.assertEqual(self.recover().snapshot,self.world.rows)

    def test_modify_create_delete_replay_refreshes_only_changed_paths(self):
        self.seed();self.world.modify('dir/a.txt');self.world.add('b.txt',4,emit=True)
        result=self.recover();self.assertEqual(result.mode,'journal')
        self.assertEqual(self.world.refreshes[-1],{'dir/a.txt','b.txt'})
        self.assertEqual(result.snapshot,self.world.rows)
        del self.world.rows['dir/a.txt'];del self.world.ids['dir/a.txt']
        self.world.records.append(record(3,2,self.world.cursor,'a.txt',DELETE));self.world.cursor+=1
        self.assertNotIn('dir/a.txt',self.recover().snapshot)
        self.assertEqual(self.world.scan_count,1)

    def test_scan_time_creation_and_modification_are_replayed(self):
        def during():self.world.modify('dir/a.txt');self.world.add('new.txt',4,emit=True)
        self.world.on_scan=during
        result=self.seed()
        self.assertEqual(set(result.dirty_paths),{'dir/a.txt','new.txt'})
        self.restart();self.assertEqual(self.recover().snapshot,self.world.rows)

    def test_directory_rename_refreshes_old_and_new_subtrees(self):
        self.seed();self.world.records.extend([record(2,1,100,'dir',OLD_NAME,True),record(2,1,101,'renamed',NEW_NAME,True)]);self.world.cursor=102
        self.world.rows={k.replace('dir','renamed',1):v for k,v in self.world.rows.items()}
        self.world.ids={k.replace('dir','renamed',1):v for k,v in self.world.ids.items()}
        result=self.recover()
        self.assertEqual(result.mode,'journal');self.assertEqual(set(result.dirty_paths),{'dir','renamed'})
        self.assertEqual(result.snapshot,self.world.rows)

    def test_populated_directory_move_in_and_known_directory_move_out(self):
        self.seed();self.world.add('incoming',10,kind='dir');self.world.add('incoming/nested',11,kind='dir');self.world.add('incoming/nested/file',12)
        self.world.records.extend([record(10,99,100,'incoming',OLD_NAME,True),record(10,1,101,'incoming',NEW_NAME,True),record(12,11,102,'file')]);self.world.cursor=103
        result=self.recover();self.assertEqual(result.mode,'journal');self.assertEqual(result.snapshot,self.world.rows)
        self.world.records.extend([record(2,1,103,'dir',OLD_NAME,True),record(2,99,104,'dir',NEW_NAME,True)]);self.world.cursor=105
        for key in ['dir','dir/a.txt']:del self.world.rows[key];del self.world.ids[key]
        result=self.recover();self.assertEqual(result.mode,'journal');self.assertEqual(result.snapshot,self.world.rows)

    def test_outside_root_changes_advance_memory_without_rewriting_cache(self):
        self.seed();before=self.index._db().total_changes
        self.world.records.append(record(99,98,100,'outside'));self.world.cursor=101
        result=self.recover();self.assertEqual(result.mode,'cached');self.assertEqual(before,self.index._db().total_changes)
        self.assertEqual(self.index.memory['meta']['cursor'],101)
        self.restart();self.assertEqual(self.recover().mode,'cached')

    def test_journal_gap_replaced_journal_and_root_change_force_full_scan(self):
        self.seed();self.world.first=101;self.world.cursor=102
        self.assertEqual(self.recover().mode,'full')
        self.world.journal_id+=1;self.assertEqual(self.recover().mode,'full')
        self.world.root_id=1000;self.assertEqual(self.recover().mode,'full')
        self.assertEqual(self.world.scan_count,4)

    def test_unavailable_journal_or_incomplete_replay_never_reuses_cache(self):
        self.seed();self.world.unavailable=True
        result=self.recover();self.assertEqual(result.mode,'full');self.assertIn('permissions',result.reason)
        self.assertFalse(self.index.available)
        self.world.unavailable=False;self.world.gap_during_read=True
        self.assertEqual(self.recover().mode,'full');self.assertEqual(self.world.scan_count,3)

    def test_crash_before_cursor_commit_leaves_old_checkpoint_intact(self):
        self.seed();self.world.modify('dir/a.txt')
        def fault(phase):
            if phase=='before_cursor_commit':raise RuntimeError('simulated crash')
        self.index.fault=fault
        with self.assertRaisesRegex(RuntimeError,'simulated'):self.recover()
        self.restart();result=self.recover()
        self.assertEqual(result.mode,'journal');self.assertEqual(result.snapshot,self.world.rows)

    def test_crash_after_dirty_commit_recovers_work_even_after_cursor_advanced(self):
        self.seed();self.world.modify('dir/a.txt')
        def fault(phase):
            if phase=='after_dirty_commit':raise RuntimeError('simulated crash')
        self.index.fault=fault
        with self.assertRaisesRegex(RuntimeError,'simulated'):self.recover()
        self.restart();self.assertEqual(self.recover().snapshot,self.world.rows)
        self.assertEqual(self.index._db().execute('SELECT count(*) FROM dirty').fetchone()[0],0)

    def test_crash_before_refreshed_snapshot_commit_retains_dirty_work(self):
        self.seed();self.world.modify('dir/a.txt')
        def fault(phase):
            if phase=='before_snapshot_commit':raise RuntimeError('simulated crash')
        self.index.fault=fault
        with self.assertRaisesRegex(RuntimeError,'simulated'):self.recover()
        self.restart();self.assertEqual(self.recover().snapshot,self.world.rows)

    def test_missing_directory_mapping_cannot_be_reused(self):
        self.seed();self.index._db().execute("DELETE FROM items WHERE path='dir'");self.index._db().commit();self.restart()
        result=self.recover();self.assertEqual(result.mode,'full');self.assertEqual(result.snapshot,self.world.rows)

    def test_abrupt_process_exit_preserves_uncommitted_or_pending_work(self):
        self.seed();self.world.modify('dir/a.txt');self.index.close()
        for phase in ('before_cursor_commit','after_dirty_commit'):
            script=f'''
import os
from pathlib import Path
from tests.test_local_change_index import World
from local_change_index import LocalChangeIndex
w=World(Path({str(self.root)!r}));w.add('dir',2,kind='dir');w.add('dir/a.txt',3);w.modify('dir/a.txt')
index=LocalChangeIndex(w.root,Path({str(self.index.path.parent)!r}),journal_factory=lambda root:w,
 fault=lambda current:os._exit(73) if current=={phase!r} else None)
index.recover(w.scan,w.refresh)
'''
            completed=subprocess.run([sys.executable,'-X','utf8','-c',script],capture_output=True,text=True,timeout=10)
            self.assertEqual(completed.returncode,73,completed.stderr)
        self.restart();self.assertEqual(self.recover().snapshot,self.world.rows)


class RecordParserTests(unittest.TestCase):
    def packed(self,major=2,name='文件.txt',usn=123):
        raw=name.encode('utf-16-le');base=60 if major==2 else 76;size=(base+len(raw)+7)//8*8
        out=bytearray(size);struct.pack_into('<IHH',out,0,size,major,0)
        width=8 if major==2 else 16;out[8:8+width]=(5).to_bytes(width,'little');out[8+width:8+2*width]=(2).to_bytes(width,'little')
        pos=8+2*width;struct.pack_into('<qQIIIIHH',out,pos,usn,0,1,0,0,0,len(raw),base);out[base:base+len(raw)]=raw
        return bytes(out)
    def test_complete_v2_and_v3_records(self):
        result=parse_records(self.packed()+self.packed(3,usn=124))
        self.assertEqual([r.usn for r in result],[123,124]);self.assertEqual(result[0].file_id,result[1].file_id)
        self.assertEqual(result[0].name,'文件.txt')
    def test_v3_preserves_high_bits_of_file_and_parent_identity(self):
        raw=bytearray(self.packed(3))
        fid=(1<<100)+5;parent=(1<<96)+2
        raw[8:24]=fid.to_bytes(16,'little');raw[24:40]=parent.to_bytes(16,'little')
        item=parse_records(raw)[0]
        self.assertEqual(item.file_id,file_id(fid));self.assertEqual(item.parent_id,file_id(parent))
        self.assertNotEqual(item.file_id,file_id(5));self.assertNotEqual(item.parent_id,file_id(2))
    def test_unknown_truncated_invalid_name_and_unaligned_records_fail_closed(self):
        malformed=[self.packed()[:-1],b'123',self.packed(name='../x')]
        unknown=bytearray(self.packed());struct.pack_into('<H',unknown,4,4);malformed.append(unknown)
        unaligned=bytearray(self.packed());struct.pack_into('<I',unaligned,0,len(unaligned)-1);malformed.append(unaligned)
        for raw in malformed:
            with self.assertRaises(JournalUnavailable):parse_records(raw)
    def test_ambiguous_topology_fails_closed(self):
        nodes={file_id(1):dict(path='',kind='dir',parent_id=None),file_id(2):dict(path='dir',kind='dir',parent_id=file_id(1))}
        for records in [[record(2,1,100,'dir',OLD_NAME|NEW_NAME,True)], [record(1,99,100,'root',NEW_NAME,True)], [record(2,99,100,'dir',1,True)]]:
            with self.assertRaises(JournalUnavailable):route_records(nodes,records,file_id(1))


class JournalProtocolTests(unittest.TestCase):
    def reader(self,pages,states=None):
        reader=object.__new__(UsnJournal);reader.handle=1;reader.read_code=UsnJournal.READ
        states=iter(states or [JournalState(7,0,102),JournalState(7,0,102)])
        reader.query=lambda:next(states)
        rows=iter(pages);reader._io=lambda *args,**kwargs:next(rows)
        return reader
    def test_bounded_complete_page_uses_target_cursor(self):
        packed=RecordParserTests().packed
        data=struct.pack('<q',103)+packed(usn=100)+packed(usn=101)+packed(usn=102)
        reader=self.reader([data])
        self.assertEqual([r.usn for r in reader.read(100,102,7,max_pages=1)],[100,101])
    def test_incomplete_nonprogress_and_out_of_order_pages_fail(self):
        packed=RecordParserTests().packed
        for data in [b'x',struct.pack('<q',100),struct.pack('<q',102)+packed(usn=101)+packed(usn=100)]:
            reader=self.reader([data])
            with self.assertRaises(JournalUnavailable):reader.read(100,102,7)
    def test_rotation_during_read_invalidates_results(self):
        data=struct.pack('<q',102)+RecordParserTests().packed(usn=100)
        for state in [JournalState(8,0,102),JournalState(7,101,102)]:
            reader=self.reader([data],[JournalState(7,0,102),state])
            with self.assertRaises(JournalUnavailable):reader.read(100,102,7)
    def test_cursor_must_be_within_retained_range(self):
        reader=self.reader([],[JournalState(7,101,102)])
        with self.assertRaises(JournalUnavailable):reader.read(100,102,7)


if __name__=='__main__':unittest.main()
