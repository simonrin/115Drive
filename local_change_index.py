"""Durable local inventory hints, backed by complete NTFS journal replay.

The cache never authorizes cloud/local mutations. Its dirty paths are committed
before its journal cursor, then refreshed through the caller's metadata scanner.
The authoritative sync baseline and existing operation journals are untouched.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
import json
from pathlib import Path
import sqlite3

from sync_tasks import relative_path
from usn_journal import UsnJournal, JournalUnavailable


CREATE, DELETE, OLD_NAME, NEW_NAME, HARDLINK = 0x100, 0x200, 0x1000, 0x2000, 0x10000


@dataclass
class Recovery:
    snapshot: dict
    mode: str
    reason: str = ''
    dirty_paths: tuple = ()


def under(path, parent):
    return path == parent or path.startswith(parent+'/')


def route_records(nodes, records, root_id):
    """Track membership through ordered journal events from a complete topology.

    All included directories must be mapped at the starting cursor. An entering
    populated subtree is refreshed recursively, so unknown descendants cannot
    be lost even if they have no individual create event. A known ID leaving the
    root invalidates its old subtree. Ambiguous topology always requires a scan.
    """
    current=copy.deepcopy(nodes);dirty=set();previous=-1
    if root_id not in current or current[root_id].get('path')!='':
        raise JournalUnavailable('Missing indexed root identity')
    for record in records:
        if record.usn<=previous:raise JournalUnavailable('Unordered replay records')
        previous=record.usn
        old=current.get(record.file_id)
        parent=current.get(record.parent_id)
        if record.file_id==root_id:
            if record.reason & (DELETE|OLD_NAME|NEW_NAME):raise JournalUnavailable('Sync root topology changed')
            continue
        if parent and parent['kind']!='dir':raise JournalUnavailable('Journal parent is not a directory')
        if old and (old['kind']=='dir')!=record.directory:
            raise JournalUnavailable('File identity kind changed')
        if not old and not parent:
            # Starting topology was complete and all membership events are
            # processed in order. New descendants of an entering subtree are
            # covered by that subtree's already-durable recursive refresh.
            continue
        if record.reason & HARDLINK:raise JournalUnavailable('Hardlink change requires complete inventory')
        if record.reason & OLD_NAME and record.reason & NEW_NAME:
            raise JournalUnavailable('Ambiguous combined rename record')
        new_path=relative_path((parent['path']+'/' if parent and parent['path'] else '')+record.name) if parent else None
        if old:dirty.add(old['path'])
        if new_path:dirty.add(new_path)
        if record.reason & OLD_NAME:
            # Retain the old ID until the matching new-name record, including
            # when the journal read ends between the two records.
            continue
        removing=bool(record.reason & DELETE) or (old is not None and parent is None and bool(record.reason & NEW_NAME))
        if removing:
            if old:
                for fid in [fid for fid,node in current.items() if under(node['path'],old['path'])]:current.pop(fid)
            continue
        if old and parent is None:
            raise JournalUnavailable('Known item has an unresolved parent')
        if not parent:continue
        if old and old['path']!=new_path:
            if not record.reason & NEW_NAME:raise JournalUnavailable('Path changed without rename proof')
            if old['kind']=='dir':
                old_path=old['path']
                for node in current.values():
                    if under(node['path'],old_path):node['path']=new_path+node['path'][len(old_path):]
        if not old and record.directory and not record.reason & (CREATE|NEW_NAME):
            raise JournalUnavailable('Directory membership lacks a creation or move event')
        current[record.file_id]=dict(path=new_path,kind='dir' if record.directory else 'file',parent_id=record.parent_id)
    # A parent refresh subsumes every child refresh.
    result=[]
    for path in sorted(dirty,key=lambda p:(p.count('/'),p)):
        if not path:raise JournalUnavailable('Root dirty state requires complete inventory')
        if not any(under(path,parent) for parent in result):result.append(path)
    return tuple(result)


class LocalChangeIndex:
    VERSION=1

    def __init__(self, root, directory, *, journal_factory=UsnJournal, fault=None):
        self.root=Path(root).absolute();self.path=Path(directory)/'local-index.sqlite3'
        self.factory=journal_factory;self.fault=fault or (lambda phase:None)
        self.connection=None;self.memory=None
        self.available=False
        self.disabled_reason='Not yet probed'

    def _db(self):
        if self.connection is None:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            self.connection=sqlite3.connect(self.path,timeout=5)
            self.connection.execute('PRAGMA journal_mode=WAL')
            self.connection.execute('PRAGMA synchronous=FULL')
            self.connection.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            self.connection.execute('CREATE TABLE IF NOT EXISTS items (path TEXT PRIMARY KEY,value TEXT NOT NULL,node TEXT NOT NULL)')
            self.connection.execute('CREATE TABLE IF NOT EXISTS dirty (path TEXT PRIMARY KEY)')
            self.connection.commit()
        return self.connection

    def _load(self):
        if self.memory is not None:return copy.deepcopy(self.memory)
        if not self.path.exists():return None
        db=self._db();raw=db.execute("SELECT value FROM meta WHERE key='checkpoint'").fetchone()
        if raw is None:return None
        meta=json.loads(raw[0])
        if not isinstance(meta,dict) or meta.get('version')!=self.VERSION or not isinstance(meta.get('binding'),dict):
            raise JournalUnavailable('Unsupported local index checkpoint')
        snapshot={};nodes={meta['binding']['root_id']:dict(path='',kind='dir',parent_id=None)}
        for path,value,node_text in db.execute('SELECT path,value,node FROM items'):
            relative_path(path);row=json.loads(value);node=json.loads(node_text)
            if not isinstance(row,dict) or not isinstance(node,dict):raise JournalUnavailable('Invalid local index row')
            fid=node.pop('file_id')
            if not isinstance(fid,str) or len(fid)!=32 or any(ch not in '0123456789abcdef' for ch in fid) or node.get('path')!=path or node.get('kind')!=row.get('kind') or fid in nodes:
                raise JournalUnavailable('Local index identity mapping is inconsistent')
            snapshot[path]=row;nodes[fid]=node
        self._validate_nodes(snapshot,nodes,meta['binding'])
        dirty=tuple(row[0] for row in db.execute('SELECT path FROM dirty'))
        for path in dirty:relative_path(path)
        return dict(meta=meta,snapshot=snapshot,nodes=nodes,dirty=dirty)

    @staticmethod
    def _validate_nodes(snapshot,nodes,binding):
        paths={node['path']:fid for fid,node in nodes.items()}
        if len(paths)!=len(nodes) or len(snapshot)+1!=len(nodes):
            raise JournalUnavailable('Incomplete local identity mapping')
        for fid,node in nodes.items():
            if fid==binding['root_id']:
                if node['path']!='' or node['kind']!='dir':raise JournalUnavailable('Invalid root mapping')
                continue
            path=node['path'];relative_path(path)
            parent=paths.get(path.rpartition('/')[0])
            if parent is None or nodes[parent]['kind']!='dir' or node['parent_id']!=parent:
                raise JournalUnavailable('Incomplete local directory topology')

    def _nodes(self,journal,snapshot,binding,*,previous=None,dirty=None,changed=None):
        nodes={binding['root_id']:dict(path='',kind='dir',parent_id=None)}
        path_ids={'':binding['root_id']}
        old={node['path']:(fid,node) for fid,node in (previous or {}).items()}
        for path,row in sorted(snapshot.items(),key=lambda item:(item[0].count('/'),item[0])):
            relative_path(path)
            if not isinstance(row,dict) or row.get('kind') not in ('file','dir'):
                raise JournalUnavailable('Invalid local snapshot row')
            if dirty is not None and path in old and not any(under(path,p) for p in dirty):
                fid,node=old[path];nodes[fid]=copy.deepcopy(node);path_ids[path]=fid;continue
            identity=journal.identity(self.root/path)
            if identity['volume_id']!=binding['volume_id'] or identity['directory']!=(row['kind']=='dir'):
                raise JournalUnavailable('Snapshot identity changed while indexing')
            if row.get('usn')!=identity['usn']:
                if changed is None:raise JournalUnavailable('Snapshot version changed while indexing')
                changed.add(path)
            fid=identity['file_id']
            if fid in nodes:raise JournalUnavailable('Hardlinks or duplicate file IDs require complete scan')
            parent_path=path.rpartition('/')[0]
            parent=path_ids.get(parent_path)
            if parent is None or nodes[parent]['kind']!='dir':raise JournalUnavailable('Snapshot parent is not indexed')
            nodes[fid]=dict(path=path,kind=row['kind'],parent_id=parent)
            path_ids[path]=fid
        self._validate_nodes(snapshot,nodes,binding)
        return nodes

    def _store(self,meta,snapshot,nodes,dirty):
        """One FULL-synchronous transaction owns snapshot, work and cursor."""
        db=self._db();by_path={node['path']:dict(node,file_id=fid) for fid,node in nodes.items() if node['path']}
        encode=lambda value:json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        try:
            db.execute('BEGIN IMMEDIATE')
            existing={path:(value,node) for path,value,node in db.execute('SELECT path,value,node FROM items')}
            for path in existing.keys()-snapshot.keys():db.execute('DELETE FROM items WHERE path=?',(path,))
            for path,row in snapshot.items():
                value,node=encode(row),encode(by_path[path])
                if existing.get(path)!=(value,node):
                    db.execute('INSERT OR REPLACE INTO items(path,value,node) VALUES(?,?,?)',(path,value,node))
            db.execute('DELETE FROM dirty')
            db.executemany('INSERT INTO dirty(path) VALUES(?)',((p,) for p in sorted(set(dirty))))
            self.fault('before_cursor_commit')
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('checkpoint',?)",(encode(meta),))
            db.commit()
        except BaseException:
            db.rollback();raise

    def _settle(self,journal,meta,snapshot,nodes,dirty):
        if dirty:
            # Make pending work durable before returning control to scanner.
            self._store(meta,snapshot,nodes,dirty)
            self.memory=None
            self.fault('after_dirty_commit')
        return dirty

    def _finish(self,journal,meta,snapshot,nodes,dirty,refresh,*,seed=False):
        if dirty:
            snapshot=refresh(snapshot,set(dirty))
            nodes=self._nodes(journal,snapshot,meta['binding'],previous=nodes,dirty=dirty)
            if journal.binding()!=meta['binding']:raise JournalUnavailable('Root identity changed during refresh')
            self.fault('before_snapshot_commit')
        if dirty or seed:self._store(meta,snapshot,nodes,())
        self.memory=dict(meta=meta,snapshot=copy.deepcopy(snapshot),nodes=nodes,dirty=())
        self.available=True;self.disabled_reason=''
        return snapshot

    def _full(self,scan,refresh,reason,journal=None):
        self.memory=None
        self.available=False;self.disabled_reason=reason
        try:
            binding=journal.binding();before=journal.query()
        except (OSError,AttributeError):
            return Recovery(scan(),'full',reason)
        snapshot=scan()
        try:
            changed=set()
            nodes=self._nodes(journal,snapshot,binding,changed=changed)
            after=journal.query()
            if after.journal_id!=before.journal_id or not after.covers(before.next_usn) or journal.binding()!=binding:
                raise JournalUnavailable('Journal or root changed during full inventory')
            records=journal.read(before.next_usn,after.next_usn,before.journal_id)
            dirty=tuple(sorted(changed|set(route_records(nodes,records,binding['root_id']))))
            meta=dict(version=self.VERSION,binding=binding,journal_id=after.journal_id,cursor=after.next_usn)
            # Seed is atomic too; a crash after this commit leaves enough work
            # to complete scan-time replay on the next startup.
            self._store(meta,snapshot,nodes,dirty)
            if dirty:self.fault('after_dirty_commit')
            snapshot=self._finish(journal,meta,snapshot,nodes,dirty,refresh)
            return Recovery(snapshot,'full',reason,dirty)
        except (OSError,sqlite3.DatabaseError,ValueError,KeyError,TypeError) as error:
            self.memory=None
            self.disabled_reason=reason+'; cache not established: '+str(error)
            return Recovery(snapshot,'full',reason+'; cache not established: '+str(error))

    def recover(self,full_scan,refresh):
        journal=None
        try:
            journal=self.factory(self.root)
            try:
                binding=journal.binding();state=journal.query();cached=self._load()
                if cached is None:return self._full(full_scan,refresh,'No reusable local index',journal)
                meta=cached['meta'];cursor=meta['cursor']
                if binding!=meta['binding']:raise JournalUnavailable('Sync root or volume identity changed')
                if state.journal_id!=meta['journal_id'] or not state.covers(cursor):
                    raise JournalUnavailable('Journal cursor expired or journal identity changed')
                if cached['dirty']:
                    # The durable cursor may describe changes whose directory
                    # map has not yet been refreshed after an interrupted run.
                    # Settle that work before routing newer directory events.
                    self._finish(journal,meta,cached['snapshot'],cached['nodes'],cached['dirty'],refresh)
                    cached=copy.deepcopy(self.memory)
                    state=journal.query()
                    if state.journal_id!=meta['journal_id'] or not state.covers(cursor):
                        raise JournalUnavailable('Journal changed while recovering pending work')
                records=journal.read(cursor,state.next_usn,state.journal_id)
                dirty=tuple(sorted(set(cached['dirty'])|set(route_records(cached['nodes'],records,binding['root_id']))))
                meta=dict(meta,cursor=state.next_usn)
                self._settle(journal,meta,cached['snapshot'],cached['nodes'],dirty)
                snapshot=self._finish(journal,meta,cached['snapshot'],cached['nodes'],dirty,refresh)
                return Recovery(snapshot,'journal' if dirty else 'cached','',dirty)
            except (OSError,sqlite3.DatabaseError,ValueError,KeyError,TypeError) as error:
                return self._full(full_scan,refresh,str(error),journal)
        except OSError as error:
            self.available=False;self.disabled_reason=str(error)
            return Recovery(full_scan(),'full',str(error))
        finally:
            if journal is not None:journal.close()

    def update(self,full_scan,refresh):
        return self.recover(full_scan,refresh)

    def observe(self,snapshot):
        """Retain fresher watcher/audit evidence before journal reconciliation.

        A still-open writer can produce directory notifications before another
        USN record is available. Never replace that observation with an older
        cached row. Persist its dirty paths while keeping the proven cursor and
        old snapshot together; recover/update will refresh them before reuse.
        """
        if not self.available:return False
        cached=self._load()
        if cached is None or not isinstance(snapshot,dict):
            raise JournalUnavailable('Observed snapshot cannot be indexed')
        changed={path for path in set(snapshot)|set(cached['snapshot'])
                 if snapshot.get(path)!=cached['snapshot'].get(path)}
        if not changed:return False
        for path in changed:relative_path(path)
        dirty=tuple(sorted(set(cached['dirty'])|changed))
        if set(dirty)==set(cached['dirty']):return False
        self._store(cached['meta'],cached['snapshot'],cached['nodes'],dirty)
        cached['dirty']=dirty;self.memory=cached
        self.fault('after_observed_dirty_commit')
        return True

    def close(self):
        if self.connection is not None:self.connection.close();self.connection=None
        self.memory=None
