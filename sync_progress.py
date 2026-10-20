"""Local, credential-free progress snapshots from real rclone JSON events."""
from __future__ import annotations
import datetime as dt
import json
import logging
import math
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import subprocess
import threading
import time


def now():
    return dt.datetime.now().astimezone().isoformat()


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)


class SyncPaused(Exception):
    """A requested pause stopped scanning or the owned transfer process."""


class SyncProgress:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'runtime/progress.json'
        activities = load(self.root / 'runtime/activity.json', [])
        self.activities = [x for x in activities if x.get('action') != 'backup'][-60:] if isinstance(activities,list) else []
        self.last_success = load(self.path, {}).get('last_success') or load(self.root/'runtime/status.json',{}).get('last_success')
        self.direction = 'checking'
        self.directions = {}
        self.pending = []
        self.raw_active = []
        self.backend_path = None
        self.backend_run = None
        self.backend_started = 0
        self.backend_aliases = {}
        self.completed_reports = set()
        self.data = {}
        self.last_save = 0
        self.cancelled = lambda: False
        self.begin()

    def begin(self):
        self.data = dict(phase='checking', bytes=0, total_bytes=0, speed=0,
            transfers=0, total_transfers=0, checks=0, total_checks=0,
            active=[], started_at=now(), last_success=self.last_success)
        self.publish(force=True)

    def publish(self, force=False):
        if not force and time.monotonic() - self.last_save < .35:
            return
        self.data['updated_at'] = now()
        write(self.path, self.data)
        self.last_save = time.monotonic()

    def activity(self, name, action):
        if not name or action == 'backup' or Path(name).name == '.twodrive-health':
            return
        item = dict(name=name, action=action, time=now())
        if self.activities and all(self.activities[-1].get(k) == item[k] for k in ('name', 'action')):
            if item['time'][:19] == self.activities[-1]['time'][:19]:
                return
        self.activities = (self.activities + [item])[-60:]
        write(self.root / 'runtime/activity.json', self.activities)

    def scan(self, path, directories, items, stage):
        if self.cancelled():
            raise SyncPaused()
        self.data.update(phase='checking',scan_stage=stage,current_path=path,
            scanned_dirs=directories,scanned_items=items)
        self.publish()

    def bind_transfer(self, args):
        """Bind only the current rclone command, never another/stale transfer."""
        self.backend_path = None
        self.backend_run = None
        self.completed_reports.clear()
        self.raw_active = []
        self.backend_aliases = {}
        try:
            ua = args[args.index('--user-agent')+1]
            config = Path(args[args.index('--config')+1])
            match = re.search(r'(?:^| )TwoDrive/([a-f0-9]{16})(?: |$)', ua)
            if match:
                self.backend_run = match.group(1)
                self.backend_path = config.parent/'transfer-resume'/('p-'+self.backend_run+'.json')
                self.backend_started = time.time()
                # copyto can rename the destination while rclone reports the
                # source basename. Bind that explicit pair, not arbitrary
                # same-sized files. Bisync keeps its normal path matching.
                if len(args)>3 and args[1]=='copyto' and args[3].startswith('cloud115:'):
                    source=args[2].replace('\\','/').rsplit('/',1)[-1]
                    target=args[3].replace('\\','/').rsplit('/',1)[-1]
                    self.backend_aliases[source]=target
        except (ValueError, IndexError):
            pass

    def backend_progress(self, include_error=False):
        try:
            if self.backend_path is None or self.backend_path.stat().st_size > 65536:
                return None
            row = load(self.backend_path, {})
            if not isinstance(row, dict):
                return None
            if (row.get('version') != 1 or row.get('run') != self.backend_run
                    or row.get('id') in self.completed_reports
                    or not re.fullmatch(r'[a-f0-9]{32}', row.get('id', ''))
                    or row.get('direction') not in ('upload', 'download')
                    or row.get('phase') not in (('verifying', 'resuming', 'transferring', 'complete', 'error')
                                               if include_error else ('verifying', 'resuming', 'transferring', 'complete'))
                    or not isinstance(row.get('name'), str) or not row['name']
                    or not self.backend_started-2 <= row['updated'] <= time.time()+5):
                return None
            for key in ('size', 'bytes', 'saved_bytes', 'speed'):
                if not isinstance(row.get(key), (int, float)) or not math.isfinite(row[key]) or row[key] < 0:
                    return None
            if row['size'] <= 0 or row['bytes'] > row['size'] or row['saved_bytes'] > row['size']:
                return None
            return row
        except (OSError, TypeError, ValueError, KeyError):
            return None

    def merge_backend(self, include_error=False):
        active = [dict(item) for item in self.raw_active]
        # Rclone's PUT bytes describe local delivery to OpenList, not cloud
        # completion. Until the backend responds, do not show that as 100%.
        if self.backend_run:
            for item in active:
                if ((item['direction']=='upload' and item['size']>10*1024**2)
                        or (item['direction']=='download' and item['size']>=16*1024**2)):
                    item.update(phase='verifying', bytes=0, percentage=None, speed=0,
                                progress_confirmed=False, saved_bytes=0,
                                progress_source='backend_wait', note='正在准备文件校验')
        row = self.backend_progress(include_error=include_error)
        if row:
            leaf = row['name'].replace('\\','/').rsplit('/',1)[-1]
            matches = [i for i,item in enumerate(active)
                       if self.backend_aliases.get(item['name'].replace('\\','/').rsplit('/',1)[-1],
                           item['name'].replace('\\','/').rsplit('/',1)[-1])==leaf
                       and item['size']==row['size']
                       and item['direction'] in (row['direction'],'checking')]
            # Current command + one matching active path, or the backend has
            # started before rclone's first stats event. Never match size alone.
            if len(matches)==1 or (not active and row['phase']!='complete'):
                current = active[matches[0]] if matches else dict(name=row['name'])
                confirmed = row.get('confirmed') is True
                count = row['bytes'] if confirmed else row['saved_bytes']
                current.update(direction=row['direction'], bytes=count, size=row['size'],
                    percentage=100*count/row['size'] if confirmed or count else None,
                    speed=row['speed'] if row['phase'] in ('resuming','transferring') else 0,
                    phase='verifying' if row['phase'] in ('complete','error') else row['phase'],
                    saved_bytes=row['saved_bytes'], progress_confirmed=confirmed,
                    resumed=row.get('resumed') is True, progress_source='backend',
                    report_id=row['id'], note=row.get('note',''))
                if matches:active[matches[0]]=current
                else:active.append(current)
                self.direction = row['direction']
                self.directions[current['name']] = row['direction']
        self.data['active'] = active
        if active:
            self.data.update(phase=active[0].get('phase','transferring'),
                             speed=sum(x['speed'] for x in active))

    def feed(self, row):
        message = row.get('msg', '')
        if 'Do queued copies to' in message:
            self.direction = 'download' if re.search(r'Path1\s*$', message) else 'upload'
        if 'Queue delete' in message:
            match = re.search(r'Queue delete\s+-\s+(.*)$', message)
            if match:
                self.pending.append(match.group(1).replace('\\', '/').rsplit('/', 1)[-1])
        obj = row.get('object', '')
        if obj and row.get('objectType', row.get('object_type', '')) != 'directory':
            if re.search(r'(?:^| )Copied \(', message) or 'Multi-thread Copied' in message:
                self.activity(obj, self.direction if self.direction in ('upload', 'download') else 'synced')
                for item in self.data.get('active',[]):
                    if item['name']==obj and item.get('report_id'):
                        self.completed_reports.add(item['report_id'])
                self.raw_active = [x for x in self.raw_active if x['name']!=obj]
            elif 'Moved into backup dir' in message:
                self.activity(obj, 'backup')
            elif message.startswith('Deleted'):
                self.activity(obj, 'deleted')
        stats = row.get('stats')
        if isinstance(stats, dict):
            for src, dst in [('bytes','bytes'), ('totalBytes','total_bytes'),
                ('speed','speed'), ('transfers','transfers'), ('totalTransfers','total_transfers'),
                ('checks','checks'), ('totalChecks','total_checks'),('listed','listed')]:
                self.data[dst] = max(0, stats.get(src) or 0)
            self.data['eta'] = stats.get('eta')
            active = []
            for item in stats.get('transferring') or []:
                name = item.get('name', '')
                if name.endswith('.twodrive-health'):
                    continue
                source, dest = str(item.get('srcFs', '')), str(item.get('dstFs', ''))
                if source.startswith('cloud115:'):
                    self.directions[name] = 'download'
                elif dest.startswith('cloud115:'):
                    self.directions[name] = 'upload'
                direction = self.directions.get(name, self.direction)
                if direction in ('upload','download'):
                    self.direction = direction
                count, size = max(0,item.get('bytes') or 0), max(0,item.get('size') or 0)
                active.append(dict(name=name, direction=direction, bytes=count, size=size,
                    percentage=min(100,100*count/size) if size else None,
                    speed=max(0,item.get('speed') or 0), eta=item.get('eta')))
            self.raw_active = active
            self.data['active'] = active
            self.data['phase'] = 'transferring' if active else 'checking'
            self.data['scan_stage'] = 'comparing'
            self.data['current_path'] = ''
            self.merge_backend()
            self.publish(force=True)
        elif any(text in message for text in ('Building Path1', 'checking for diffs', 'Checking access', 'Updating listings', 'Validating listings')):
            self.data['phase'] = 'checking'
            self.publish()
        elif obj and ('Copied' in message):
            self.merge_backend()
            self.publish(force=True)

    def finish(self, success):
        if success is None:
            self.merge_backend(include_error=True)
        self.data['phase'] = 'paused' if success is None else 'idle' if success else 'error'
        # Keep the last confirmed checkpoint visible while paused. The next
        # command still revalidates the source and server parts before reuse.
        if success is not None:
            self.data['active'] = []
        for item in self.data['active']:
            item['speed'] = 0
        self.raw_active = []
        self.data['speed'] = 0
        self.data['finished_at'] = now()
        if success:
            self.last_success = self.data['last_success'] = now()
        self.publish(force=True)


def execute(args, progress, log_path):
    """Drain stderr continuously; preserve bisync logs and process exit status."""
    if progress.cancelled():
        raise SyncPaused()
    values = [str(x) for x in args]
    cleaned = []
    index = 0
    replaced = {'--log-file', '--log-file-max-size', '--log-file-max-backups', '--stats'}
    while index < len(values):
        if values[index] in replaced:
            index += 2
        else:
            cleaned.append(values[index]); index += 1
    cleaned += ['--use-json-log', '--stats', '1s', '--stats-log-level', 'INFO']
    progress.bind_transfer(cleaned)
    path = Path(log_path); path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=10_000_000, backupCount=5, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'))
    tail = []
    done = threading.Event()
    paused = threading.Event()
    watcher = None
    try:
        process = subprocess.Popen(cleaned, cwd=progress.root,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            encoding='utf-8', errors='replace',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            callback = getattr(progress, 'process_started', None)
            if callback:
                callback(process)
        except Exception:
            # A transfer without a durable process record must not proceed.
            process.terminate()
            process.wait()
            process.stderr.close()
            raise
        def watch_pause():
            # stderr may be silent while hashing or waiting on an HTTP request.
            # Poll independently; terminate ONLY the Popen owned by this call.
            while not done.wait(.1):
                if process.poll() is not None:
                    return
                if progress.cancelled():
                    try:
                        process.terminate()
                    except OSError:
                        if process.poll() is None:
                            raise
                    paused.set()
                    return
        watcher = threading.Thread(target=watch_pause, name='transfer-pause', daemon=True)
        watcher.start()
        for line in process.stderr:
            handler.emit(logging.LogRecord('bisync', logging.INFO, '', 0, line.rstrip(), (), None))
            tail = (tail + [line])[-30:]
            try:
                row = json.loads(line)
            except ValueError:
                continue
            try:
                progress.feed(row)
            except OSError:
                # A UI snapshot must never interrupt or fail a file transfer.
                pass
        process.stderr.close()
        result = subprocess.CompletedProcess(cleaned, process.wait(), '', ''.join(tail))
        done.set()
        watcher.join()
        if paused.is_set():
            # Do not complete the transfer intent or report this as an error.
            # --recover will reconcile bisync listings on the next command.
            raise SyncPaused()
        return result
    finally:
        done.set()
        if watcher is not None:
            watcher.join()
        handler.close()
