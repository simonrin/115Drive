"""One manager, independent task workers, shared account backend and native roots."""
from __future__ import annotations
import json
import base64
from pathlib import Path
import threading
import time
import os
import datetime as dt
import math
from sync_tasks import TaskStore, file_lock, legacy_enabled, relative_path
from task_backend import OpenListBackend
from task_engine import TaskEngine
from runtime_json import write_snapshot
from task_scheduling import IdleSchedule, AccountChecks, METADATA

_MANAGER = None
_STARTED_AT = dt.datetime.now().astimezone().isoformat()


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else 0
    except (TypeError, ValueError, OverflowError): return 0


def _timestamp(value):
    if isinstance(value, str):
        try: return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except (ValueError, OverflowError, OSError): return 0
    return _number(value)


def _relative(value):
    try: return relative_path(value) if isinstance(value, str) and value else ''
    except (ValueError, TypeError): return ''


def task_progress(task, status):
    """Only publish bounded display fields; neither snapshots nor UI authorize work."""
    phase = {'uploading':'uploading', 'downloading':'downloading', 'checking':'scanning'}.get(status.get('state'), status.get('state', 'queued'))
    raw = status.get('progress')
    raw = raw if isinstance(raw, dict) else {}
    phases = {'scanning', 'queued', 'waiting_network', 'waiting_rate', 'deleting',
              'uploading', 'downloading', 'verifying', 'resuming', 'renaming', 'preparing', 'idle', 'paused', 'error'}
    if raw.get('phase') in phases: phase = raw['phase']
    if phase not in phases: phase = 'preparing'
    row = dict(task_id=task['id'], task_name=task['name'], local_root=task['local_path'],
               state=status.get('state', 'checking'), phase=phase,
               name=_relative(raw.get('name', status.get('file', ''))),
               message=str(status.get('message', ''))[:1000])
    for key in ('completed', 'total', 'bytes', 'size', 'speed', 'saved_bytes'):
        if key in raw: row[key] = _number(raw[key])
    if 'total' in row and 'completed' in row: row['completed'] = min(row['completed'], row['total'])
    for key in ('wait_reason', 'note'):
        if isinstance(raw.get(key), str): row[key] = raw[key][:500]
    for key in ('progress_source', 'direction'):
        if isinstance(raw.get(key), str): row[key] = raw[key][:32]
    if 'direction' not in row and phase in ('uploading', 'downloading'): row['direction'] = phase[:-3]
    if raw.get('progress_confirmed') is True: row['progress_confirmed'] = True
    scan = status.get('scan')
    if isinstance(scan, dict):
        row['scan'] = {key:_number(scan[key]) for key in ('directories', 'pending', 'entries') if key in scan}
    return row


def task_activity(task, rows):
    """Bind history to the configured task, never to roots supplied by an event."""
    if not isinstance(rows, list): return []
    result = []
    for event in rows[-64:]:
        if not isinstance(event, dict) or event.get('action') not in ('upload', 'download', 'deleted', 'rename'): continue
        name, stamp = _relative(event.get('name')), _timestamp(event.get('time'))
        identity = event.get('id')
        if not name or not stamp or not isinstance(identity, str) or not identity or len(identity)>128: continue
        try: iso = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError): continue
        result.append(dict(id=identity, task_id=task['id'], task_name=task['name'], local_root=task['local_path'],
                           name=name, action=event['action'], time=iso, size=_number(event.get('size'))))
    return result


def authorization_allowed(root):
    try:
        if any(not t['paused'] for t in TaskStore(root).read()['tasks']): return False
        return _MANAGER is None or not _MANAGER.active()
    except (ValueError, OSError): return False


class TaskManager:
    def __init__(self, host, *, backend_factory=OpenListBackend, engine_factory=TaskEngine, shell=True):
        self.host, self.store = host, TaskStore(host.ROOT)
        self.backend_factory, self.engine_factory = backend_factory, engine_factory
        self.shell = shell
        self.stop = threading.Event()
        self.workers = {}
        self.lock = threading.Lock()
        self.batches = AccountChecks()
        self.check_requested = 0
        self.last_summary = None
        self.last_progress = None
        self.last_activity = None
        self.completed_checks = {}
        self.last_diagnostic_key = None
        self.diagnostics = {}

    def request_check(self):
        with self.lock: self.check_requested += 1

    def active(self):
        with self.lock: return any(row['thread'].is_alive() for row in self.workers.values())

    def worker_stopped(self, task_id):
        with self.lock:
            row = self.workers.get(task_id)
            return row is None or not row['thread'].is_alive()

    def shell_register(self, task, engine):
        if not self.shell: return
        icon = self.host.ROOT/'bin/TwoDriveTray/TwoDriveTray.exe'
        if not icon.exists(): icon = self.host.ROOT/'115Drive.exe'
        self.host.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            self.host.ROOT/'register-sync-root.ps1', '-Action', 'Register', '-Root', task['local_path'],
            '-TaskId', task['id'], '-DisplayName', '115Drive · ' + task['name'],
            '-IconPath', icon,
            '-ContextBase64', base64.b64encode(engine.native.owner).decode('ascii')], timeout=45)

    def worker(self, task_id, stop):
        directory = self.store.directory(task_id)
        engine = None
        with self.lock: task = self.workers[task_id]['task']
        crashed = False
        try:
            with file_lock(directory/'worker.lock'):
                with self.lock: task = self.workers[task_id]['task']
                if task['paused'] or self.store.removing(task_id): return
                engine = self.engine_factory(task, directory, self.backend_factory(task, self.host),
                    policy_writer=lambda path, policy: self.store.shell_override(task_id, path, policy))
                with self.lock: self.workers[task_id]['engine'] = engine
                if hasattr(engine, 'enable_state_store'): engine.enable_state_store()
                schedule = IdleSchedule(task_id)
                engine.status('checking', '正在核对同步目录并连接文件随选')
                engine.start(registrar=lambda: self.shell_register(task, engine))
                with self.lock: self.workers[task_id]['ready'] = True
                if hasattr(engine, 'enable_watch'): engine.enable_watch()
                settings = None
                last_request = 0
                while not stop.is_set():
                    with self.lock:
                        task = self.workers[task_id]['task']
                        requested = self.check_requested
                    if task['paused']: break
                    engine.task = task
                    try:
                        current = (task['policy'], task['cache_hours'], tuple(sorted(task['overrides'].items())))
                        if not schedule.failures and getattr(engine, 'local_changed', lambda:False)():
                            schedule.local_change()
                        if not schedule.failures or schedule.due():
                            urgent = settings is None or requested != last_request or not schedule.failures and (
                                settings is not None and current != settings or
                                schedule.local_deadline is not None and time.monotonic() >= schedule.local_deadline)
                            batch = self.batches.take(task['account'], task_id, stop, urgent=urgent)
                            if batch is None:
                                stop.wait(.5)
                                continue
                            if stop.is_set(): break
                            with self.lock:
                                task = self.workers[task_id]['task']
                                self.workers[task_id]['last_batch'] = batch.number
                            if task['paused']: break
                            engine.task = task
                            current = (task['policy'], task['cache_hours'], tuple(sorted(task['overrides'].items())))
                            engine.scan_complete = lambda changed: self.batches.scanned(task['account'], task_id, batch, changed)
                            try:
                                changed = engine.check_batch(batch) if hasattr(engine, 'check_batch') else engine.cycle()
                            except BaseException:
                                self.batches.finished(task['account'], task_id, batch, retry_seconds=min(300, 15 * 2**min(schedule.failures, 5)))
                                raise
                            else:
                                self.batches.finished(task['account'], task_id, batch, changed=True if changed is None else changed)
                            finally: engine.scan_complete = None
                            settings = current
                            last_request = requested
                            schedule.complete(True if changed is None else changed)
                            if getattr(engine, 'more_work', False): schedule.local_change()
                    except InterruptedError: break
                    except Exception as error:
                        schedule.failed()
                        engine.status('error', str(error), retry_seconds=schedule.last_interval)
                    if stop.wait(1): break
        except Exception as error:
            crashed = True
            write_snapshot(directory/'status.json', dict(state='error', message=str(error), updated_at=time.time()))
        finally:
            self.batches.unavailable(task['account'], task_id, retry_seconds=30 if crashed else 0)
            if engine:
                try: engine.close()
                except Exception as error:
                    write_snapshot(directory/'status.json', dict(state='error', message=str(error), updated_at=time.time()))
            if stop.is_set() or (engine and engine.task.get('paused')):
                write_snapshot(directory/'status.json', dict(state='paused', message='任务已暂停', updated_at=time.time()))

    def tick(self):
        tasks = {t['id']: t for t in self.store.read()['tasks']}
        self.batches.configure([dict(task, paused=task['paused'] or self.stop.is_set()) for task in tasks.values()])
        with self.lock:
            for task_id, row in list(self.workers.items()):
                if task_id in tasks: row['task'] = tasks[task_id]
                if task_id not in tasks or tasks[task_id]['paused'] or self.stop.is_set():
                    row['stop'].set()
                    if row.get('engine'): row['engine'].cancel.set()
                if not row['thread'].is_alive():
                    # Startup failures are retried with a cooldown, not a tight loop.
                    if time.monotonic() - row['started'] >= 30 or tasks.get(task_id, {}).get('paused', True):
                        del self.workers[task_id]
            for task_id, task in tasks.items():
                if self.stop.is_set() or task['paused'] or task_id in self.workers: continue
                stopped = threading.Event()
                thread = threading.Thread(target=self.worker, args=(task_id, stopped), daemon=True, name='sync-' + task_id[:8])
                self.workers[task_id] = dict(thread=thread, stop=stopped, task=task, started=time.monotonic())
                thread.start()
        if not legacy_enabled(self.host.ROOT): self.publish_summary(tasks)

    def publish_summary(self, tasks):
        active = [task for task in tasks.values() if not task['paused']]
        with self.lock:
            alive = {key for key,row in self.workers.items() if row['thread'].is_alive()}
            ready = {key for key,row in self.workers.items() if row.get('ready')}
        states, displays, activity = [], [], []
        for task in tasks.values():
            try: status=json.loads((self.store.directory(task['id'])/'status.json').read_text(encoding='utf-8'))
            except (OSError,ValueError): status={}
            if not isinstance(status, dict): status={}
            if task['paused']: status=dict(state='paused', message='任务已暂停')
            elif (task['id'] not in ready or task['id'] not in alive) and status.get('state') != 'error':
                status=dict(state='checking', message='正在连接同步任务', progress=dict(phase='preparing'))
            if not task['paused']: states.append((task,status))
            displays.append(task_progress(task,status))
            try: events=json.loads((self.store.directory(task['id'])/'activity.json').read_text(encoding='utf-8'))
            except (OSError,ValueError): events=[]
            activity.extend(task_activity(task, events))
        active_displays = [row for row in displays if row['state'] not in ('idle','paused','error')]
        priority = {'uploading':0, 'downloading':0, 'deleting':1, 'renaming':1, 'verifying':1,
                    'waiting_network':2, 'waiting_rate':2, 'queued':3, 'scanning':4, 'preparing':5}
        active_displays.sort(key=lambda row:(priority.get(row['phase'],3), row['task_name'], row['task_id']))
        progress=dict(task_mode=True, active=active_displays, tasks=displays)
        unique = {(row['task_id'],row['id']):row for row in activity}
        activity = sorted(unique.values(), key=lambda row:(row['time'],row['task_id'],row['id']))[-64:]
        if progress != self.last_progress and write_snapshot(self.host.ROOT/'runtime/tasks-progress.json', progress):
            self.last_progress = progress
        if activity != self.last_activity and write_snapshot(self.host.ROOT/'runtime/tasks-activity.json', activity):
            self.last_activity = activity
        errors=[task['name'] for task,status in states if status.get('state')=='error']
        transfers=sum(row['state'] in ('uploading','downloading') for row in active_displays)
        operations=sum(row['phase'] in ('deleting','renaming','verifying') and row['state'] not in ('uploading','downloading') for row in active_displays)
        checking=len(active_displays)-transfers-operations
        state=('stopping' if alive else 'paused') if not active else 'error' if errors else 'syncing' if transfers or operations else 'checking' if checking else 'idle'
        message=(f'{len(tasks)} 个任务已暂停' if state=='paused' else '正在等待各任务停止' if state=='stopping' else
                 '需要处理：'+'、'.join(errors) if errors else f'{len(active)} 个任务已核对完成' if state=='idle' else
                 f'{len(active)} 个任务运行中 · {checking} 个检查 · {transfers} 个传输'+(f' · {operations} 个变更处理' if operations else ''))
        checked=max((_timestamp(status.get('last_check_at',0)) for _,status in states),default=0)
        for task,status in states:
            metrics=status.get('metrics')
            stamp=_timestamp(status.get('last_check_at'))
            if isinstance(metrics,dict) and 'cycles' in metrics and stamp:
                self.completed_checks[task['id']]=(_number(metrics['cycles']),stamp)
        self.completed_checks={key:value for key,value in self.completed_checks.items() if key in tasks}
        diagnostic_key=tuple(sorted(self.completed_checks.items()))
        if diagnostic_key and diagnostic_key!=self.last_diagnostic_key:
            # Snapshot readers do not generate network calls or update counters.
            # Publish only at a completed check, not on UI refresh/elapsed time.
            sources=(('http',getattr(self.host,'HTTP',None),('requests','responses','failures','http_errors','redirects',
                'connections_opened','connections_reused','connections_closed','bytes_received','pool_wait_seconds',
                'connection_seconds','header_seconds','body_seconds','request_seconds')),
                ('metadata',METADATA,('acquired','cancelled','timed_out','wait_seconds','max_wait_seconds','slot_seconds','active','queued','peak_active')))
            captured={}
            for name,source,keys in sources:
                try: raw=source.snapshot()
                except Exception: continue  # Optional diagnostics must never stop synchronization.
                if isinstance(raw,dict):captured[name]={key:round(_number(raw[key]),6) for key in keys if key in raw}
            self.diagnostics=dict(at_completed_check=max(value[1] for value in self.completed_checks.values()),**captured)
            self.last_diagnostic_key=diagnostic_key
        value=dict(state=state,message=message,task_mode=True,task_count=len(tasks),active_tasks=len(active),paused_tasks=len(tasks)-len(active),
                   pid=os.getpid(),started_at=_STARTED_AT)
        if self.diagnostics:value['diagnostics']=self.diagnostics
        if checked: value['last_check_at']=dt.datetime.fromtimestamp(checked, dt.timezone.utc).astimezone().isoformat()
        if activity: value['last_sync_at']=activity[-1]['time']
        transferred = [row['time'] for row in activity if row['action'] in ('upload','download')]
        if transferred: value['last_transfer_at']=transferred[-1]
        if value!=self.last_summary and write_snapshot(self.host.ROOT/'runtime/tasks-status.json',dict(value,updated_at=dt.datetime.now().astimezone().isoformat())):
            self.last_summary=value

    def run(self):
        try:
            with file_lock(self.store.private/'task-manager.lock'):
                while not self.stop.is_set():
                    try: self.tick()
                    except Exception as error:
                        # Invalid task configuration cannot leave existing workers
                        # running with stale path/ownership assumptions.
                        with self.lock:
                            for row in self.workers.values():
                                row['stop'].set()
                                if row.get('engine'): row['engine'].cancel.set()
                        write_snapshot(self.host.ROOT/'runtime/tasks-status.json', dict(state='error', message=str(error)))
                    self.stop.wait(1)
        except OSError:
            write_snapshot(self.host.ROOT/'runtime/tasks-status.json', dict(state='error', message='另一任务管理器正在运行。'))


def start(host):
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = TaskManager(host)
        threading.Thread(target=_MANAGER.run, daemon=True, name='sync-task-manager').start()
    return _MANAGER
