"""Stop only this installation, after its synchronization work has drained."""
from __future__ import annotations
import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

from installer_upgrade import ordinary
from runtime_json import atomic_write_bytes


class WindowsController:
    def __init__(self, root, *, api='http://127.0.0.1:5245', timeout=120):
        self.root = ordinary(root)
        self.api, self.timeout = api, timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.prior = {}

    def request(self, path, body=None, nonce=None):
        headers = {'X-TwoDrive-Nonce':nonce} if nonce else {}
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.api+path, data=data, headers=headers)
        with self.opener.open(request, timeout=5) as response:
            raw = response.read().decode('utf-8')
            try: return json.loads(raw)
            except ValueError: return raw

    def native(self, action, data=None):
        # The script is part of the new, verified payload, never from the old installation.
        import sys
        script_root = Path(sys.executable).parent.parent if getattr(sys, 'frozen', False) else Path(__file__).parent
        script = script_root/'installer-process.ps1'
        payload = base64.b64encode(json.dumps(data or {}).encode()).decode('ascii')
        result = subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),
            '-Root',str(self.root),'-Action',action,'-Data',payload], capture_output=True, timeout=35,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode: raise RuntimeError('无法安全停止或恢复此安装的后台进程和自启动；未强制中断传输。')
        return json.loads(result.stdout.decode('utf-8-sig') or '{}')

    def quiet(self, tx, recovering=False):
        control = tx.data['control']
        snapshot = self.native('Inspect')
        if any(p['role']=='foreign' for p in snapshot['processes']):
            raise RuntimeError('此程序文件正被另一个数据目录使用，未停止其他实例。')
        if not control:
            task_path = ordinary(self.root/'runtime/private/sync-tasks.json')
            stop_path = ordinary(self.root/'runtime/stop')
            task_bytes = task_path.read_bytes() if task_path.exists() else None
            task_data = json.loads(task_bytes.decode('utf-8-sig')) if task_bytes else None
            for row in snapshot['startup']:
                if any(old['name']==row['name'] and old.get('running') for old in self.prior.get('tasks_startup',[])):
                    row['running']=True
            control.update(tasks_before=base64.b64encode(task_bytes).decode() if task_bytes is not None else None,
                           task_versions=[task_data] if task_data else [],
                           stop_before=base64.b64encode(stop_path.read_bytes()).decode() if stop_path.exists() else None,
                           tasks_startup=snapshot['startup'], was_running=bool(snapshot['processes']) or self.prior.get('was_running',False),
                           worker_running=any(p.get('worker') for p in snapshot['processes']) or self.prior.get('worker_running',False))
            tx.save()
        self.native('Suspend', control)
        if not snapshot['processes']:
            control['stopped'] = True; tx.save(); return
        if recovering and control.get('drained'):
            prior = {(p['pid'],p['started'],p['path']) for p in control['stop_candidates']['processes']}
            current = {(p['pid'],p['started'],p['path']) for p in snapshot['processes']}
            if current <= prior:
                self.native('Stop',snapshot)
                if self.native('Inspect')['processes']: raise RuntimeError('上次升级的旧进程尚未退出。')
                control['stopped']=True;tx.save();return
        desktop = self.request('/desktop')
        if Path(desktop['root']) != self.root:
            raise RuntimeError('本机控制服务属于另一个安装目录；未停止其他程序。')
        nonce = desktop['nonce']
        status = self.request('/status')
        qr = self.request('/qr/status')
        if status.get('maintenance_busy') or status.get('folder_changing') or status.get('state') in ('authorizing','testing','initializing','relocating') or qr.get('state') in ('generating','waiting','scanned','connecting'):
            raise RuntimeError('正在连接账号或更改同步位置，请等待完成后升级。')
        trays = [p for p in snapshot['processes'] if p['role']=='tray']
        if trays:
            control['ui_closed']=True;tx.save()
            self.native('Stop',dict(processes=trays))
        task_path = ordinary(self.root/'runtime/private/sync-tasks.json')
        if task_path.exists():
            current = json.loads(task_path.read_text(encoding='utf-8-sig'))
            if current not in control['task_versions']:
                raise RuntimeError('同步任务设置已变化，请关闭安装器后重新升级。')
            for task in current['tasks']:
                if task['paused']: continue
                revision = current['revision']
                task['paused'] = True; current['revision'] += 1
                control['task_versions'].append(copy.deepcopy(current)); tx.save()
                self.request('/tasks/pause', dict(id=task['id'],revision=revision), nonce)
        control['legacy_pause_requested']=desktop.get('legacy_enabled',True);tx.save()
        self.request('/pause', {}, nonce)
        deadline = time.monotonic()+self.timeout
        while True:
            status = self.request('/status')
            if status.get('authorization_allowed') and not status.get('folder_changing') and not status.get('maintenance_busy'): break
            if time.monotonic() >= deadline: raise TimeoutError('同步仍在停止，升级尚未替换程序，请稍后重试。')
            time.sleep(.25)
        # No transfers may survive a drained controller. Never kill a transfer to unlock an EXE.
        snapshot = self.native('Inspect')
        if any(p['role']=='transfer' for p in snapshot['processes']):
            raise RuntimeError('仍有传输进程，未替换程序。')
        control.update(drained=True,stop_candidates=snapshot);tx.save()
        self.native('Stop', snapshot)
        if self.native('Inspect')['processes']: raise RuntimeError('旧程序尚未完全退出。')
        control['stopped'] = True; tx.save()

    def restore(self, tx):
        control = tx.data['control']
        if not control: return
        task_path = ordinary(self.root/'runtime/private/sync-tasks.json')
        if control.get('tasks_before') is not None:
            old = base64.b64decode(control['tasks_before'])
            current = task_path.read_bytes()
            if current != old:
                if json.loads(current.decode('utf-8-sig')) not in control['task_versions']:
                    raise RuntimeError('任务配置被同时修改，未覆盖；保留升级恢复记录。')
                atomic_write_bytes(task_path, old, durable=True)
        stop_path = ordinary(self.root/'runtime/stop')
        if control.get('legacy_pause_requested') and control.get('stop_before') is None and stop_path.exists():
            if stop_path.read_bytes()!=b'':raise RuntimeError('暂停标记被同时修改，已保留。')
            stop_path.unlink()
        self.native('Restore', control)

    def resume(self, tx):
        control = tx.data['control']
        if (control.get('stopped') or control.get('ui_closed')) and control.get('was_running'):
            self.native('Resume', control)

    def remember(self, tx):
        self.prior = tx.data['control']
