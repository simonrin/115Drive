"""Credential-free native-settings contract, shared with the loopback controller."""
from pathlib import Path
import uuid
import twodrive as t
from folder_change import ordinary_path


def view():
    cfg=t.read_json(t.CONFIG)
    return dict(root=str(t.ROOT),local_path=cfg['local_path'],cloud_path='/115Drive',
        start_with_windows=cfg.get('start_with_windows',True),
        initialized=(t.PRIVATE/'initialized.json').exists(),
        version='0.2.0',data_path=str(t.ROOT))


def initial_folder(value):
    if (t.PRIVATE/'initialized.json').exists():
        raise ValueError('已建立同步，请使用“复制并切换”。')
    if not isinstance(value,str): raise ValueError('请输入本地文件夹路径。')
    raw=value.strip()
    path=Path(raw)
    if not path.is_absolute() or not path.drive or path.drive.startswith('\\') or ':' in raw[2:] or any(ord(c)<32 or c in '<>"|?*' for c in raw):
        raise ValueError('请输入有效的本地完整路径。')
    ordinary_path(t,path)
    path=path.resolve()
    if path==Path(path.anchor) or path.is_reserved() or any(p.endswith((' ','.')) for p in path.parts[1:]):
        raise ValueError('请选择磁盘下的普通文件夹。')
    if path.is_relative_to(t.ROOT) or t.ROOT.is_relative_to(path):
        raise ValueError('同步目录不能与程序目录重叠。')
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError('首次设置请选择空文件夹或尚未创建的文件夹。')
    if not Path(path.anchor).is_dir(): raise ValueError('目标磁盘不可访问。')
    cfg=t.read_json(t.CONFIG)
    cfg.update(local_path=str(path),local_namespace='virtual_v1',local_cache_id=uuid.uuid4().hex)
    t.write_json(t.CONFIG,cfg)
    return dict(ok=True,local_path=str(path))


def preferences(body):
    enabled=body.get('start_with_windows')
    if not isinstance(enabled,bool): raise ValueError('无效的自启动设置。')
    if (t.PRIVATE/'initialized.json').exists():
        t.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',t.ROOT/'set-startup.ps1',
               '-Enabled', '1' if enabled else '0'])
    cfg=t.read_json(t.CONFIG)
    cfg['start_with_windows']=enabled
    t.write_json(t.CONFIG,cfg)
    return dict(ok=True,start_with_windows=enabled)
