"""One manager, independent task workers, shared account backend and native roots."""
from __future__ import annotations
import json
import base64
from pathlib import Path
import threading
import time
import os
import datetime as dt
from sync_tasks import TaskStore, file_lock, legacy_enabled
from task_backend import OpenListBackend
from task_engine import TaskEngine
from runtime_json import write_snapshot
from task_scheduling import IdleSchedule, BatchCoordinator

_MANAGER = None
_STARTED_AT = dt.datetime.now().astimezone().isoformat()


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
        self.batches = BatchCoordinator()
        self.check_requested = 0
        self.last_summary = None

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
        try:
            with file_lock(directory/'worker.lock'):
                with self.lock: task = self.workers[task_id]['task']
                if task['paused'] or self.store.removing(task_id): return
                engine = self.engine_factory(task, directory, self.backend_factory(task, self.host),
                    policy_writer=lambda path, policy: self.store.shell_override(task_id, path, policy))
                with self.lock: self.workers[task_id]['engine'] = engine
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
                        if requested != last_request or schedule.due(settings_changed=current != settings):
                            urgent = requested != last_request or not schedule.failures and (
                                settings is not None and current != settings or
                                schedule.local_deadline is not None and time.monotonic() >= schedule.local_deadline)
                            batch = self.batches.join(task['account'], task_id, stop, urgent=urgent)
                            if stop.is_set(): break
                            with self.lock:
                                task = self.workers[task_id]['task']
                                self.workers[task_id]['last_batch'] = batch.number
                            if task['paused']: break
                            engine.task = task
                            current = (task['policy'], task['cache_hours'], tuple(sorted(task['overrides'].items())))
                            changed = engine.check_batch(batch) if hasattr(engine, 'check_batch') else engine.cycle()
                            settings = current
                            last_request = requested
                            schedule.complete(True if changed is None else changed)
                    except InterruptedError: break
                    except Exception as error:
                        schedule.failed()
                        engine.status('error', str(error), retry_seconds=schedule.last_interval)
                    if stop.wait(1): break
        except Exception as error:
            write_snapshot(directory/'status.json', dict(state='error', message=str(error), updated_at=time.time()))
        finally:
            if engine: engine.close()
            if stop.is_set() or (engine and engine.task.get('paused')):
                write_snapshot(directory/'status.json', dict(state='paused', message='任务已暂停', updated_at=time.time()))

    def tick(self):
        tasks = {t['id']: t for t in self.store.read()['tasks']}
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
        states=[]
        for task in active:
            try: status=json.loads((self.store.directory(task['id'])/'status.json').read_text(encoding='utf-8'))
            except (OSError,ValueError): status={}
            if (task['id'] not in ready or task['id'] not in alive) and status.get('state') != 'error': status=dict(state='checking')
            states.append((task,status))
        errors=[task['name'] for task,status in states if status.get('state')=='error']
        checking=sum(status.get('state') not in ('idle','error','uploading','downloading') for _,status in states)
        transfers=sum(status.get('state') in ('uploading','downloading') for _,status in states)
        state=('stopping' if alive else 'paused') if not active else 'error' if errors else 'syncing' if transfers else 'checking' if checking else 'idle'
        message=(f'{len(tasks)} 个任务已暂停' if state=='paused' else '正在等待各任务停止' if state=='stopping' else
                 '需要处理：'+'、'.join(errors) if errors else f'{len(active)} 个任务已核对完成' if state=='idle' else
                 f'{len(active)} 个任务运行中 · {checking} 个检查 · {transfers} 个传输')
        checked=max((status.get('last_check_at',0) for _,status in states),default=0)
        value=dict(state=state,message=message,task_mode=True,task_count=len(tasks),active_tasks=len(active),paused_tasks=len(tasks)-len(active),
                   pid=os.getpid(),started_at=_STARTED_AT)
        if checked: value['last_check_at']=dt.datetime.fromtimestamp(checked, dt.timezone.utc).astimezone().isoformat()
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
