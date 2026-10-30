"""115Drive supervisor. Python 3.11+, standard library only; credentials stay local."""
from __future__ import annotations
import argparse
import contextlib
import ctypes
import datetime as dt
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
from local_watch import LocalChangeMonitor
from sync_progress import SyncProgress, SyncPaused, execute as execute_sync
from sync_errors import ActionRequiredError, TransientSyncError, recovery_policy

ROOT = Path(os.environ.get('TWODRIVE_HOME', Path(__file__).resolve().parent)).resolve()
PRIVATE = ROOT / 'runtime' / 'private'
DATA = PRIVATE / 'openlist'
RCLONE = ROOT / 'bin/rclone-v1.75.1/rclone-v1.75.1-windows-amd64/rclone.exe'
OPENLIST = ROOT / 'bin/openlist-v4.2.6/openlist.exe'
CONFIG = ROOT / 'config.json'
SENTINEL = '.twodrive-health'
API = 'http://127.0.0.1:5244'
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
log = logging.getLogger('twodrive')
_native_last_request = 0.0
_incremental_scanners = {}

def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + secrets.token_hex(4) + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)

def status(state, message, **extra):
    try:
        previous=read_json(ROOT/'runtime/status.json')
        # A completed file may precede a later failure in the same bisync run.
        # Preserve that observed transfer without claiming the run succeeded.
        try:
            observed=read_json(ROOT/'runtime/progress.json').get('last_transfer_at')
            if observed and observed > previous.get('last_transfer_at',''):
                previous['last_transfer_at']=observed
        except (OSError,ValueError): pass
        for field in ('last_success','last_check_at','last_sync_at','last_transfer_at'):
            if previous.get(field) and field not in extra:
                extra[field]=previous[field]
    except (OSError,ValueError): pass
    write_json(ROOT / 'runtime/status.json', dict(state=state, message=message,
        updated_at=dt.datetime.now().astimezone().isoformat(), **extra))
    log.info('%s: %s', state, message)

def run(args, *, timeout=180, data=None, check=True):
    p = subprocess.run([str(x) for x in args], input=data, capture_output=True,
        encoding='utf-8', errors='replace', timeout=timeout, creationflags=NO_WINDOW,
        cwd=ROOT)
    if check and p.returncode:
        # Never put captured authorization responses in exceptions or logs.
        detail=''
        if Path(str(args[0])).name.lower()=='rclone.exe':
            if '403 Forbidden' in p.stderr: detail='；WebDAV 写入被拒绝（403），请检查本机用户权限'
            elif '401 Unauthorized' in p.stderr: detail='；WebDAV 登录被拒绝（401），请检查本机授权'
            elif 'directory not found' in p.stderr: detail='；远端目录不存在或暂时不可访问'
        raise RuntimeError(f'{Path(str(args[0])).name} failed, exit={p.returncode}'+detail)
    return p

def api(path, body=None, token=None):
    headers = {'Content-Type':'application/json'}
    if token: headers['Authorization'] = token
    req = urllib.request.Request(API + path, headers=headers,
        data=json.dumps(body).encode() if body is not None else None)
    with HTTP.open(req, timeout=120) as response:
        result = json.load(response)
    if result.get('code') != 200:
        code=result.get('code')
        error=ActionRequiredError if code in (401,403) else TransientSyncError
        raise error(f'OpenList {path}: code {code}')
    return result.get('data')

def login():
    creds = read_json(PRIVATE / 'admin.json')
    return api('/api/auth/login', creds)['token']

def bootstrap():
    PRIVATE.mkdir(parents=True, exist_ok=True)
    if not (PRIVATE / 'admin.json').exists():
        sid = run(['powershell.exe','-NoProfile','-Command',
            '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value']).stdout.strip()
        run(['icacls.exe', PRIVATE, '/inheritance:r', '/grant:r',
             '*' + sid + ':(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'])
        password = secrets.token_urlsafe(32)
        run([OPENLIST, 'admin', 'set', password, '--data', DATA])
        write_json(PRIVATE / 'admin.json', {'username':'admin','password':password})
    cfg = read_json(DATA / 'config.json')
    cfg['scheme']['address'] = '127.0.0.1'
    cfg['scheme']['http_port'] = 5244
    cfg['scheme']['https_port'] = -1
    cfg['scheme']['enable_h2c'] = False
    for name in ('ftp','sftp','s3'):
        if name in cfg: cfg[name]['enable'] = False
    cfg['log']['name'] = str(ROOT / 'logs/openlist.log')
    cfg['log']['max_size'] = 10
    cfg['log']['max_backups'] = 3
    write_json(DATA / 'config.json', cfg)
    (ROOT / 'logs').mkdir(exist_ok=True)

def ensure_server():
    # Serialize the controller and worker so one cannot clean scratch while
    # the other is starting an upload server.
    with worker_lock(PRIVATE/'server-start.lock'):
        _ensure_server()


def _ensure_server():
    try:
        login()  # Check identity, not merely that the TCP port is listening.
        return
    except Exception:
        with contextlib.closing(socket.socket()) as sock:
            if sock.connect_ex(('127.0.0.1',5244)) == 0:
                raise RuntimeError('Port 5244 is occupied by another service; not changing it')
    from transfer_recovery import prepare_openlist_scratch, server_process_running
    if server_process_running(OPENLIST):
        raise RuntimeError('上次的上传服务进程尚未退出，等待结束后恢复。')
    scratch, cleaned = prepare_openlist_scratch(PRIVATE)
    server_config = read_json(DATA/'config.json')
    server_config['temp_dir'] = str(scratch)
    write_json(DATA/'config.json', server_config)
    write_json(ROOT/'runtime/temp-cleanup.json', dict(**cleaned, at=time.time()))
    out = open(ROOT / 'logs/openlist-process.log','ab')
    subprocess.Popen([str(OPENLIST),'server','--data',str(DATA)], cwd=ROOT,
        stdout=out, stderr=out, creationflags=NO_WINDOW,
        env={**os.environ, 'TWODRIVE_VERIFIED_INDEX': str(PRIVATE/'cloud-index.json'),
             'TWODRIVE_TRANSACTIONS': str(PRIVATE/'upload-transactions'),
             'TWODRIVE_RESUME': str(PRIVATE/'transfer-resume'),
             'OPENLIST_TEMP_DIR': str(scratch)})
    out.close()
    for _ in range(40):
        try:
            login()
            return
        except Exception: time.sleep(.5)
    raise RuntimeError('OpenList failed to start; see logs/openlist-process.log')

def ensure_webdav_permissions(token):
    """Apply the user-approved single permission bit on this private instance."""
    instance=read_json(DATA/'config.json')
    if instance['scheme']['address']!='127.0.0.1' or instance['scheme']['http_port']!=5244:
        raise RuntimeError('Private OpenList binding differs from the approved scope')
    users=api('/api/admin/user/list?page=1&per_page=100',token=token)['content'] or []
    matches=[user for user in users if user.get('id')==1 and user.get('username')=='admin' and user.get('role')==2]
    if len(matches)!=1:
        raise RuntimeError('Private OpenList administrator differs from the approved scope')
    user=matches[0]
    before=int(user['permission'])
    if before==29695:
        return token
    if before!=29183:
        raise RuntimeError('Permission mask differs from the approved scope; leaving it unchanged')
    after=before|(1<<9)
    backup=PRIVATE/'webdav-permissions-backup.json'
    if not backup.exists():
        write_json(backup,dict(user_id=user['id'],permission_before=before,permission_after=after))
    updated=dict(user,permission=after,password='')
    api('/api/admin/user/update',updated,token)
    token=login()  # Updating a user invalidates its previous API session.
    checked=api('/api/admin/user/list?page=1&per_page=100',token=token)['content'] or []
    current=next((value for value in checked if value['id']==user['id']),None)
    if current is None or int(current['permission'])!=after:
        raise RuntimeError('WebDAV permissions update was not persisted')
    return token

def storage(mount, addition, *, driver='115 Open'):
    return dict(mount_path=mount, driver=driver, cache_expiration=60,
        addition=json.dumps(addition), disabled=False, disable_index=True,
        web_proxy=True, webdav_policy='native_proxy', order=0, remark='Managed by TwoDrive')

def storages(token):
    return api('/api/admin/storage/list?page=1&per_page=100',token=token)['content'] or []

def save_storage(token, record):
    matches = [x for x in storages(token) if x['mount_path']==record['mount_path']]
    if matches:
        old = matches[0]
        if old.get('remark') != 'Managed by TwoDrive':
            raise RuntimeError('Existing mount is not managed by TwoDrive')
        record['id'] = old['id']
        api('/api/admin/storage/update',record,token)
    else:
        api('/api/admin/storage/create',record,token)

def cloud_folder(token, parent, name):
    contents = api('/api/fs/list',dict(path=parent,password='',page=1,per_page=0,refresh=True),token)
    matches = [x for x in contents['content'] or [] if x['name'].casefold()==name.casefold()]
    if len(matches)>1 or (matches and (not matches[0]['is_dir'] or matches[0]['name']!=name)):
        raise RuntimeError('Ambiguous cloud folder name: ' + name)
    if not matches:
        api('/api/fs/mkdir',dict(path=parent + '/' + name),token)
    return matches[0] if matches else {'name':name,'is_dir':True}

def cloud_recovery_folder(token, cfg=None):
    cfg = cfg or read_json(CONFIG)
    if not cfg.get('recovery_enabled', False):
        return None
    remote = cfg['remote_backup']
    if not remote.startswith('cloud115:115Drive_Recovery'):
        raise RuntimeError('Unexpected recovery root; refusing to create it')
    name = remote.split(':',1)[1]
    if any(c in name for c in '/\\:'):
        raise RuntimeError('Recovery folder must be a single cloud root folder')
    cloud_folder(token,'/115',name)
    return '/115/'+name

def configure(access, refresh):
    """One driver owns token refresh; the sync paths are fixed in config.json."""
    if not access or not refresh or len(access)>8192 or len(refresh)>8192:
        raise ValueError('Both authorization tokens are required')
    ensure_server()
    token = login()
    token=ensure_webdav_permissions(token)
    addition = dict(root_folder_id='0',access_token=access,refresh_token=refresh,
                    limit_rate=1,page_size=200,order_by='file_name',order_direction='asc')
    save_storage(token,storage('/115',addition))
    cloud_folder(token,'/115','115Drive')
    cloud_recovery_folder(token)
    write_rclone_config()
    status('configured','115 授权已保存，等待实际同步测试。')

def configure_cookie(cookie):
    """Fallback using 115 Cloud without a public OAuth application."""
    if not isinstance(cookie,str) or not 0<len(cookie)<=16384 or any(c in cookie for c in '\r\n'):
        raise ValueError('Invalid 115 login session')
    keys={item.split('=',1)[0] for item in cookie.split(';') if '=' in item}
    if not {'UID','CID','SEID'}<=keys:
        raise ValueError('Incomplete 115 login session')
    ensure_server()
    token=login()
    token=ensure_webdav_permissions(token)
    addition=dict(root_folder_id='0',cookie=cookie,qrcode_token='',
                  qrcode_source='wechatmini',limit_rate=1,page_size=200)
    save_storage(token,storage('/115',addition,driver='115 Cloud'))
    cloud_folder(token,'/115','115Drive')
    cloud_recovery_folder(token)
    write_rclone_config()
    record=next(x for x in storages(token) if x['mount_path']=='/115')
    saved=json.loads(record['addition'])
    if record['driver']!='115 Cloud' or not saved.get('cookie'):
        raise RuntimeError('115 login session was not persisted')
    status('configured','115 扫码会话已保存，等待实际同步测试。')

def write_rclone_config():
    from local_paths import configure_mapping
    password=read_json(PRIVATE/'admin.json')['password']
    obscure=run([RCLONE,'obscure','-'],data=password).stdout.strip()
    text='[cloud115]\ntype = webdav\n'
    text+=('url = http://127.0.0.1:5244/dav/115/\nvendor = owncloud\nuser = admin\n'
        'pass = '+obscure+'\n')
    (PRIVATE/'rclone.conf').write_text(configure_mapping(text,read_json(CONFIG),ROOT),encoding='utf-8')

def transfer_flags(cfg=None):
    # A new UA per command prevents stale 115 download-link cache entries.
    # Two 64 MiB ranges passed real-cloud integrity/speed checks; four were rejected.
    cfg=read_json(CONFIG) if cfg is None else cfg
    cache=PRIVATE/'rclone-cache'
    cache_id=cfg.get('local_cache_id')
    if cache_id:
        if len(cache_id)!=32 or any(c not in '0123456789abcdef' for c in cache_id):
            raise RuntimeError('Invalid local cache identity')
        cache=cache/cache_id
    return ['--cache-dir',str(cache),
        '--user-agent','Mozilla/5.0 (Windows NT 10.0; Win64; x64) TwoDrive/'+secrets.token_hex(8),
        '--multi-thread-streams','2','--multi-thread-cutoff','32Mi',
        '--multi-thread-chunk-size','64Mi']

def rc(*args, check=True, timeout=180):
    return run([RCLONE,*args,'--config',PRIVATE/'rclone.conf',*transfer_flags(),
        '--local-hashes','SHA1','--checkers','1','--transfers','1','--retries','2','--low-level-retries','2',
        '--contimeout','15s','--timeout','2m'],check=check,timeout=timeout)

def native_115_request(cookie,path,query):
    global _native_last_request
    # Limit request starts to one per second, without adding a second of sleep
    # on top of network latency for every directory.
    time.sleep(max(0, 1 - (time.monotonic() - _native_last_request)))
    _native_last_request = time.monotonic()
    url='https://webapi.115.com/'+path+'?'+urllib.parse.urlencode(query)
    req=urllib.request.Request(url,headers={'Cookie':cookie,'User-Agent':'Mozilla/5.0'})
    with HTTP.open(req,timeout=30) as response: result=json.load(response)
    if result.get('state') is not True:
        code=str(result.get('errno',result.get('errNo',result.get('code','unknown'))))
        if code in ('99','990001') or result.get('is_login') is False:
            raise ActionRequiredError('115 登录已失效，请暂停同步后重新扫码连接。')
        raise TransientSyncError('115 原始目录读取失败，等待重试（代码 '+code+'）')
    return result

def native_115_list(cookie,cid):
    rows=[];offset=0
    while True:
        response=native_115_request(cookie,'files',dict(aid=1,cid=str(cid),o='file_name',asc=1,
            offset=offset,show_dir=1,limit=1150,format='json',snap=0,natsort=0,record_open_time=0,fc_mix=0))
        if str(response.get('cid'))!=str(cid):
            raise RuntimeError('115 返回了不同目录，已停止同步')
        page=response.get('data') or []
        rows.extend(page)
        offset+=len(page)
        if offset>=int(response['count']): return rows
        if not page: raise RuntimeError('115 目录分页不完整，已停止同步')

def native_115_check_names(cookie,suffix,progress=None):
    lookup=native_115_request(cookie,'files/getid',dict(path=suffix))
    cid=str(lookup.get('id') or '')
    if not cid or cid=='0': raise RuntimeError('115 同步目录不存在，已停止同步')
    pending=[(suffix,cid)];visited=set();item_count=0
    while pending:
        parent,cid=pending.pop()
        if cid in visited: raise RuntimeError('115 目录重复引用，已停止同步')
        visited.add(cid);names=set()
        if progress: progress.scan(parent,len(visited)-1,item_count,'cloud_check')
        for item in native_115_list(cookie,cid):
            item_count+=1
            name=item['n'];key=name.casefold()
            if key in names:
                raise RuntimeError('云端存在重复名称（含仅大小写不同），已暂停：/'+parent+'/'+name)
            names.add(key)
            if not item.get('fid'): pending.append((parent+'/'+name,str(item['cid'])))
        if progress: progress.scan(parent,len(visited),item_count,'cloud_check')

def refresh_cloud_tree(remote,progress=None):
    """Refresh real cloud objects and reject names Windows cannot distinguish."""
    if not remote.startswith('cloud115:'): return
    suffix=remote.split(':',1)[1].strip('/')
    if suffix!='115Drive' and not suffix.startswith('115Drive/'):
        raise RuntimeError('Refusing to refresh outside the sync scope')
    token=login()
    record=next((x for x in storages(token) if x['mount_path']=='/115'),None)
    if record and record['driver']=='115 Cloud':
        native_115_check_names(json.loads(record['addition'])['cookie'],suffix,progress)
    pending=['/115/'+suffix];checked=0;items=0
    while pending:
        parent=pending.pop()
        if progress: progress.scan(parent,checked,items,'preparing')
        response=api('/api/fs/list',dict(path=parent,password='',page=1,per_page=0,refresh=True),token)
        names=set()
        for item in response['content'] or []:
            items+=1
            name=item['name']
            if not name or name in ('.','..') or '/' in name or '\\' in name:
                raise RuntimeError('云端文件名无法映射到 Windows：'+parent+'/'+name)
            key=name.casefold()
            if key in names:
                raise RuntimeError('云端存在重复名称（含仅大小写不同），已暂停：'+parent+'/'+name)
            names.add(key)
            if item['is_dir']: pending.append(parent+'/'+name)
        checked+=1
        if progress: progress.scan(parent,checked,items,'preparing')

def bisync_args(local, remote, workdir, backup_local, backup_remote, *, initial=False, cached_local=False, cfg=None):
    cfg=read_json(CONFIG) if cfg is None else cfg
    if cached_local:
        from local_paths import cached_paths
        local,backup_local=cached_paths(local,backup_local,cfg,ROOT)
    args = [RCLONE,'bisync',str(local),remote,'--config',str(PRIVATE/'rclone.conf'),
        '--workdir',str(workdir),*transfer_flags(cfg),'--local-hashes','SHA1','--compare','size,checksum','--create-empty-src-dirs',
        '--check-access','--check-filename',SENTINEL,'--resilient','--recover',
        '--max-lock','10m','--max-delete','50','--conflict-resolve','path1',
        '--conflict-loser','delete' if cfg.get('recovery_enabled',False) else 'num','--suffix-keep-extension',
        '--filters-file',str(ROOT/'filters.txt'),'--checkers','1','--transfers','1',
        '--retries','1','--low-level-retries','2','--contimeout','15s','--timeout','2m',
        '--log-level','INFO','--stats','0','--log-file',str(ROOT/'logs/bisync.log'),
        '--log-file-max-size','10M','--log-file-max-backups','5']
    if cfg.get('recovery_enabled',False):
        args += ['--backup-dir1',str(backup_local),'--backup-dir2',backup_remote]
    if initial: args += ['--resync','--resync-mode','path2']
    return args

def incremental_scanner(cfg):
    if not cfg.get('incremental_scan') or cfg['remote_path'] != 'cloud115:115Drive':
        return None
    from cloud_scan import CloudScan
    if cfg['remote_path'] not in _incremental_scanners:
        _incremental_scanners[cfg['remote_path']] = CloudScan(sys.modules[__name__],cfg['remote_path'])
    return _incremental_scanners[cfg['remote_path']]


def sync_once(cfg, initial=False, *, interruptible=False, changed_paths=None):
    progress=SyncProgress(ROOT)
    if interruptible: progress.cancelled=lambda:(ROOT/'runtime/stop').exists() or folder_guard().exists()
    try:
        synchronized=_sync_once(cfg,initial,progress,changed_paths=changed_paths)
    except SyncPaused:
        progress.finish(None)
        raise
    except Exception:
        progress.finish(False)
        raise
    progress.finish(True, synchronized=synchronized is not False)

def _sync_once(cfg, initial, progress, *, changed_paths=None):
    local = Path(cfg['local_path'])
    if not local.is_dir() or is_reparse(local):
        raise RuntimeError('Local sync root missing or redirected; sync stopped')
    stamp=dt.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3)
    from transfer_recovery import TransferRecovery
    cached=bool(cfg.get('local_hash_cache')) and (not initial or cfg.get('local_namespace')=='virtual_v1')
    workdir=PRIVATE/('bisync-cached' if cached else 'bisync')
    recovery_run=TransferRecovery(ROOT,cfg,workdir)
    interrupted=recovery_run.prepare()
    pending_cloud=list((PRIVATE/'upload-transactions').glob('*.json'))
    if pending_cloud:
        status('checking','正在恢复上次中断的上传。')
        # OpenList reconciles only journaled objects before returning a listing.
        api('/api/fs/list',dict(path='/115/115Drive',password='',page=1,per_page=1,refresh=True),login())
        if list((PRIVATE/'upload-transactions').glob('*.json')):
            raise RuntimeError('云端中断事务尚未恢复，已暂停同步以保留文件。')
        interrupted=True
    status('checking','正在检查本地和网盘文件变化。')
    progress.scan(cfg['remote_path'],0,0,'cloud_check')
    scanner = incremental_scanner(cfg) if not initial else None
    changed = scanner.prepare(changed_paths,progress) if scanner else True
    if not scanner:
        refresh_cloud_tree(cfg['remote_path'],progress)
    try:
        last_run=float(read_json(PRIVATE/'last-sync-run.json')['at'])
    except (OSError,ValueError,KeyError,TypeError):
        last_run=0
    # Even a file changed by a tool that preserves size AND mtime must
    # eventually be rehashed. Hasher entries expire after 24 hours.
    hash_audit_due=time.time()-last_run>=86400
    if scanner and not changed and changed_paths is not None and not hash_audit_due and not interrupted:
        status('idle','文件清单核对完成，等待变化。',last_check_at=dt.datetime.now().astimezone().isoformat())
        return False
    # A disabled optional archive must not add requests or block normal sync.
    if cfg.get('recovery_enabled',False):
        token=login()
        recovery=cloud_recovery_folder(token,cfg)
        api('/api/fs/list',dict(path=recovery,password='',page=1,per_page=0,refresh=True),token)
    args=bisync_args(local,cfg['remote_path'],workdir,
        ROOT/'recovery'/stamp,cfg['remote_backup']+'/'+stamp,initial=initial,cached_local=cached,cfg=cfg)
    status('syncing','正在核对并同步文件。')
    # No overall timeout: large legitimate transfers may take hours. Rclone's
    # per-I/O timeout still handles stalled connections.
    args=recovery_run.begin(args)
    progress.process_started=recovery_run.started
    result=execute_sync(args,progress,ROOT/'logs/bisync.log')
    if result.returncode:
        error=TransientSyncError if result.returncode in (1,5,6,10) else ActionRequiredError
        raise error(f'同步暂停，rclone 退出码 {result.returncode}；请查看 logs/bisync.log')
    if scanner:
        scanner.synchronized()
    recovery_run.complete()
    write_json(PRIVATE/'last-sync-run.json',dict(at=time.time()))
    finished=dt.datetime.now().astimezone().isoformat()
    times=dict(last_success=finished,last_check_at=finished,last_sync_at=finished)
    transferred=progress.data.get('last_transfer_at')
    if isinstance(transferred,str) and transferred: times['last_transfer_at']=transferred
    status('idle','同步完成，等待文件变化。',**times)
    return True

def initialize_sync():
    cfg=read_json(CONFIG)
    ensure_server()
    local=Path(cfg['local_path'])
    if (PRIVATE/'initialized.json').exists():
        raise RuntimeError('Already initialized; refusing to reset synchronization history')
    local.mkdir(parents=True,exist_ok=True)
    if is_reparse(local):
        raise RuntimeError('Sync root is a link or junction; refusing initialization')
    sentinel=local/SENTINEL
    sentinel.write_text('TwoDrive access marker; do not delete.\n',encoding='utf-8')
    rc('copyto',str(sentinel),cfg['remote_path']+'/'+SENTINEL)
    # Explicit preflight before a one-time union of both sides.
    refresh_cloud_tree(cfg['remote_path'])
    cached=bool(cfg.get('local_hash_cache')) and cfg.get('local_namespace')=='virtual_v1'
    args=bisync_args(local,cfg['remote_path'],PRIVATE/('bisync-cached' if cached else 'bisync'),ROOT/'recovery'/'initial',
        cfg['remote_backup']+'/initial',initial=True,cached_local=cached,cfg=cfg)+['--dry-run']
    run(args,timeout=None)
    sync_once(cfg,initial=True)
    write_json(PRIVATE/'initialized.json',dict(local=cfg['local_path'],remote=cfg['remote_path'],
        filter_sha256=hashlib.sha256((ROOT/'filters.txt').read_bytes()).hexdigest()))

def snapshot(root):
    result={}
    def fail(error):
        if not isinstance(error,FileNotFoundError): raise error
    def ordinary(path):
        try: return not is_reparse(path)
        except FileNotFoundError: return False
    for parent, dirs, files in os.walk(root,onerror=fail,followlinks=False):
        dirs[:] = [d for d in dirs if ordinary(Path(parent)/d)]
        for name in dirs+files:
            path=Path(parent)/name
            try:
                if not ordinary(path): continue
                st=path.stat()
            except FileNotFoundError:
                continue  # A normal deletion during scanning is picked up next pass.
            result[str(path.relative_to(root))]=(st.st_size,st.st_mtime_ns)
    return result

def is_reparse(path):
    return bool(getattr(Path(path).lstat(),'st_file_attributes',0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)

@contextlib.contextmanager
def worker_lock(lock_path=None):
    import msvcrt
    f=open(lock_path or PRIVATE/'worker.lock','a+b')
    # Reading byte zero itself fails on Windows while another worker locks it.
    if os.fstat(f.fileno()).st_size==0: f.write(b'0'); f.flush()
    f.seek(0)
    deadline=time.monotonic()+25 if lock_path else 0
    while True:
        try:
            msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            break
        except OSError:
            if lock_path and time.monotonic()<deadline:
                time.sleep(.1)
                continue
            f.close()
            raise RuntimeError('TwoDrive server is starting' if lock_path else 'TwoDrive worker already running')
    try: yield
    finally:
        f.seek(0); msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1); f.close()

def folder_guard():
    return PRIVATE/'folder-change-active.json'


def wait_for_folder_change():
    while folder_guard().exists():
        try: guard=read_json(folder_guard())
        except FileNotFoundError: continue
        except PermissionError:
            time.sleep(.05)  # A just-removed guard can briefly be delete-pending.
            continue
        write_json(PRIVATE/'folder-change-ack.json',dict(id=guard['id'],pid=os.getpid()))
        time.sleep(.5)


def wait_for_retry(error, failures):
    """Back off without confusing a recoverable failure with a user pause."""
    delay=min(300,30*2**min(max(failures-1,0),4))
    deadline=time.monotonic()+delay
    retry_at=dt.datetime.now().astimezone()+dt.timedelta(seconds=delay)
    status('retrying',str(error),error_category=recovery_policy(error).category,retry_seconds=delay,
           retry_at=retry_at.isoformat(),retry_attempt=failures)
    while True:
        if ((ROOT/'runtime/stop').exists() or (ROOT/'runtime/sync-now').exists()
                or folder_guard().exists()):
            return
        remaining=deadline-time.monotonic()
        if remaining<=0:
            return
        time.sleep(min(1,remaining))


def wait_for_action(error):
    status('error',str(error),error_category=recovery_policy(error).category,requires_action=True)
    while not ((ROOT/'runtime/stop').exists() or (ROOT/'runtime/sync-now').exists()
               or folder_guard().exists()):
        time.sleep(1)


def worker_loop():
    cfg=read_json(CONFIG)
    marker=read_json(PRIVATE/'initialized.json')
    if (marker['local'],marker['remote']) != (cfg['local_path'],cfg['remote_path']):
        raise RuntimeError('Sync path changed; refusing to reuse old state')
    if marker['filter_sha256'] != hashlib.sha256((ROOT/'filters.txt').read_bytes()).hexdigest():
        raise RuntimeError('Filters changed; manual review needed')
    previous=None
    synced_snapshot=None
    try:
        saved=read_json(PRIVATE/'local-snapshot.json')
        if saved['local']==cfg['local_path'] and saved['remote']==cfg['remote_path']:
            synced_snapshot={k:tuple(v) for k,v in saved['entries'].items()}
    except (OSError,ValueError,KeyError,TypeError): pass
    next_sync=0
    changed_at=0
    failures=0
    pause_reported=False
    force_scan=True
    def watch_metrics(data):
        write_json(ROOT/'runtime/local-watch.json',dict(data,pid=os.getpid(),
            updated_at=dt.datetime.now().astimezone().isoformat()))
    monitor=LocalChangeMonitor(Path(cfg['local_path']),snapshot,
        full_scan_seconds=cfg.get('local_full_scan_seconds',60),
        poll_seconds=cfg['local_scan_seconds'],enabled=cfg.get('local_watch_enabled',True),
        notify=watch_metrics)
    with contextlib.closing(monitor):
        while True:
            if folder_guard().exists():
                return  # Re-read config and snapshots after a completed/rolled-back move.
            if (ROOT/'runtime/stop').exists():
                if not pause_reported:
                    status('paused','同步已暂停，可以安全检查文件。')
                    pause_reported=True
                time.sleep(1)
                continue
            if pause_reported:
                next_sync=0
                failures=0
                pause_reported=False
                force_scan=True
            requested=ROOT/'runtime/sync-now'
            if requested.exists():
                requested.unlink(missing_ok=True)
                force_scan=True
                next_sync=0
                failures=0
            try:
                local=Path(cfg['local_path'])
                if not local.is_dir() or is_reparse(local) or not (local/SENTINEL).is_file():
                    raise ActionRequiredError('本地目录或健康标记缺失，已停止同步以保护云端文件。')
                current=monitor.poll(force=force_scan)
                force_scan=False
                now=time.monotonic()
                if current!=previous:
                    changed_at=now
                    previous=current
                due=now>=next_sync or (changed_at and now-changed_at>=cfg['debounce_seconds'])
                if due:
                    ensure_server()
                    changes=set(current) if synced_snapshot is None else {
                        key for key in current.keys() | synced_snapshot.keys()
                        if current.get(key) != synced_snapshot.get(key)}
                    sync_once(cfg,interruptible=True,changed_paths=changes)
                    if current != synced_snapshot:
                        write_json(PRIVATE/'local-snapshot.json',dict(local=cfg['local_path'],remote=cfg['remote_path'],entries=current))
                    synced_snapshot=current
                    failures=0
                    changed_at=0
                    next_sync=time.monotonic()+cfg['cloud_poll_seconds']
                    # Keep pre-sync snapshot so concurrent changes get another pass.
                elif not changed_at:
                    scanner=incremental_scanner(cfg)
                    if scanner and scanner.audit_step():
                        next_sync=0
                time.sleep(cfg['local_scan_seconds'])
            except SyncPaused:
                force_scan=True
                status('paused','同步已暂停，可以安全检查文件。')
                failures=0
            except Exception as error:
                force_scan=True
                failures+=1
                if recovery_policy(error).retryable:
                    wait_for_retry(error,failures)
                else:
                    wait_for_action(error)
                next_sync=0


def worker():
    with worker_lock():
        write_json(ROOT/'runtime/worker.json',dict(pid=os.getpid(),started_at=dt.datetime.now().astimezone().isoformat()))
        while True:
            wait_for_folder_change()
            worker_loop()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['bootstrap','server','initialize','once','worker','status'])
    args=parser.parse_args()
    (ROOT/'logs').mkdir(exist_ok=True)
    handler=RotatingFileHandler(ROOT/'logs/supervisor.log',maxBytes=2_000_000,backupCount=3,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    log.addHandler(handler); log.setLevel(logging.INFO)
    if args.action=='bootstrap': bootstrap()
    elif args.action=='server': ensure_server()
    elif args.action=='initialize': initialize_sync()
    elif args.action=='once':
        with worker_lock():
            if folder_guard().exists(): raise RuntimeError('Folder change is in progress')
            ensure_server(); sync_once(read_json(CONFIG))
    elif args.action=='worker': worker()
    elif args.action=='status': print(json.dumps(read_json(ROOT/'runtime/status.json'),ensure_ascii=False,indent=2))

if __name__=='__main__':
    try: main()
    except Exception as exc:
        status('error',str(exc))
        print(str(exc),file=sys.stderr)
        sys.exit(1)
