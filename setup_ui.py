"""Loopback-only setup/status page. Never echoes or logs 115 tokens."""
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlsplit
import twodrive as t
from auth115 import QRController
from folder_change import FolderChanger
import desktop_settings

NONCE=secrets.token_urlsafe(32)
BUSY=threading.Lock()
FOLDERS=FolderChanger(t)
PAGE='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>115Drive 自动同步</title><style>
body{font:16px/1.7 system-ui;background:#f2f5fb;color:#162238;max-width:850px;margin:50px auto;padding:20px}
main{background:white;border-radius:20px;padding:32px;box-shadow:0 12px 45px #193d6610}h1{margin-top:0}label{display:block;margin-top:18px}
input{width:96%;padding:12px;border:1px solid #bbc5d2;border-radius:8px;font:inherit}button,a.btn{display:inline-block;background:#165dce;color:white;border:0;border-radius:8px;padding:12px 22px;margin:16px 8px 0 0;text-decoration:none;cursor:pointer;font:inherit}
.note{color:#53627a}#status{background:#edf5ff;padding:15px;border-radius:10px;white-space:pre-wrap}code{background:#eef1f6;padding:3px 7px;overflow-wrap:anywhere}
[hidden]{display:none!important}button:disabled{opacity:.5;cursor:default}.secondary{background:#e8eef8;color:#24416d}#folder-settings{border:1px solid #dce4f0;border-radius:14px;padding:22px;margin-top:24px}#folder-settings h2{margin:0}#folder-message{white-space:pre-wrap;overflow-wrap:anywhere}progress{width:100%;height:12px}#folder-path{box-sizing:border-box;width:100%}
</style><main><h1>115Drive 自动同步</h1><p><code id="current-folder">正在读取同步目录…</code> ↔ 115 网盘 <code>/115Drive</code></p>
<p id="status">正在读取状态…</p>
<section id="folder-settings"><h2>同步文件夹</h2><p class="note">更改本机保存位置，网盘目录仍为 /115Drive。文件会复制到新目录，校验通过后恢复同步，旧目录保留为副本。</p>
<label for="folder-path">新的本地文件夹</label><input id="folder-path" type="text" placeholder="例如 D:\\115Drive" autocomplete="off" spellcheck="false">
<button id="folder-browse" class="secondary" onclick="browseFolder()">选择文件夹…</button><button id="folder-preview" onclick="previewFolder()">检查目标</button>
<p id="folder-message" role="status" aria-live="polite">请选择空文件夹，或输入尚未创建的完整路径。</p><progress id="folder-progress" hidden></progress>
<button id="folder-apply" onclick="changeFolder()" hidden>复制并切换</button><button id="folder-cancel" class="secondary" onclick="cancelFolder()" hidden>取消更改</button>
<p class="note">复制和校验期间请关闭正在编辑的同步文件。切换会保留同步记录；如果原来已暂停，切换后仍保持暂停。</p></section>
<section id="setup"><h2>连接你的 115 网盘</h2>
<p>公共开放平台应用当前返回“应用不存在”（40140107）。可先使用下面的本机扫码入口连接普通网盘。</p>
<button onclick="control('retry')">重试已保存的连接</button><button id="qr-start" onclick="startQR()">本机扫码连接</button><button id="qr-cancel" onclick="control('qr/cancel')" hidden>取消扫码</button>
<div><img id="qr-image" hidden alt="115 登录二维码" style="width:240px;height:240px;image-rendering:pixelated"><p id="qr-message"></p></div>
<p class="note">用手机 115 App 扫描并确认。登录凭据由本机直接向 115 获取并保存，不需要复制令牌。这会使用微信小程序登录会话，可能替换你已有的同类登录。普通网盘接口的稳定性弱于开放平台；凭据失效时需要重新扫码。</p>
<details><summary>已有 115 开放平台令牌时使用</summary>
<p>若你已有有效的 Access Token 和 Refresh Token，可直接填入。当前 api.oplist.org 的公共应用 ID 无效，重复网页登录不能解决。</p>
<form id="form"><label>Access Token（访问令牌）<input name="access" type="password" autocomplete="off" required></label>
<label>Refresh Token（刷新令牌）<input name="refresh" type="password" autocomplete="off" required></label>
<button type="submit" id="submit">连接、测试并启用同步</button></form></details>
<p class="note">扫码确认后会在独立测试目录验证上传、下载和双向删除，测试通过才开始首次同步并注册登录自启动。配置仅存入这台电脑的受限目录。</p></section>
<h2>同步规则</h2><ul><li>本地每 2 秒检查变化，静置 3 秒后触发；云端每轮同步结束后 30 秒再次检查，传输和限流时间另计。</li>
<li>登录 Windows 后自动开始，并核对关机期间的云端变化。电脑关闭期间无法下载。</li>
<li>删除会传播。恢复副本默认关闭，普通覆盖和删除不再额外归档；已有恢复目录保留。</li>
<li>两边同时修改同一文件时保留本机版本，另一版本以带 conflict 标记的文件保留；一次删除比例超过 50% 会暂停，需检查日志后处理。</li>
<li>首次连接时合并两边内容；若同名不同内容，使用云端版本。更改本地目录不会重新执行首次合并。</li>
<li>这是完整本地副本，磁盘需要足够空间；不提供 OneDrive 的按需下载占位符与资源管理器状态图标。</li>
<li>请保留同步目录中的 <code>.twodrive-health</code> 健康标记。</li></ul>
<button onclick="control('pause')">暂停同步</button><button onclick="control('resume')">恢复同步</button>
<p class="note">配置及日志：APP_DIRECTORY。当前页面仅在本机 127.0.0.1 开放。</p></main>
<script>const nonce='NONCE';let folderPreview=null,folderOperation=false,folderRequest=false,lastFolderState='';
const el=id=>document.getElementById(id),size=n=>n>=1073741824?(n/1073741824).toFixed(2)+' GiB':(n/1048576).toFixed(1)+' MiB';
async function folderPost(action,data={}){let r=await fetch('/folder/'+action,{method:'POST',headers:{'X-TwoDrive-Nonce':nonce,'Content-Type':'application/json'},body:JSON.stringify(data)});let result=await r.json();if(!r.ok)throw Error(result.error||'操作失败');return result;}
function folderButtons(){for(let id of ['folder-path','folder-browse','folder-preview'])el(id).disabled=folderOperation||folderRequest;el('folder-apply').disabled=folderRequest;}
function clearPreview(){folderPreview=null;el('folder-apply').hidden=true;}
el('folder-path').oninput=()=>{clearPreview();el('folder-message').textContent='修改路径后，请重新检查目标。';};
async function browseFolder(){folderRequest=true;folderButtons();try{let r=await folderPost('choose');if(r.path){el('folder-path').value=r.path;clearPreview();el('folder-message').textContent='已选择文件夹，点击“检查目标”继续。';}}catch(e){el('folder-message').textContent=e.message;}finally{folderRequest=false;folderButtons();}}
async function previewFolder(){clearPreview();folderRequest=true;folderButtons();el('folder-message').textContent='正在检查文件夹和可用空间…';try{let p=await folderPost('preview',{path:el('folder-path').value});folderPreview=p;el('folder-message').textContent='当前：'+p.source+'\\n目标：'+p.target+'\\n复制 '+p.files+' 个文件，共 '+size(p.bytes)+'；目标可用 '+size(p.free_bytes)+'。\\n检查通过。点击“复制并切换”开始，旧目录将保留。';el('folder-apply').hidden=false;}catch(e){el('folder-message').textContent=e.message;}finally{folderRequest=false;folderButtons();}}
async function changeFolder(){if(!folderPreview)return;folderRequest=true;folderButtons();try{await folderPost('change',{path:folderPreview.target});clearPreview();lastFolderState='';await update();}catch(e){el('folder-message').textContent=e.message;}finally{folderRequest=false;folderButtons();}}
async function cancelFolder(){try{await folderPost('cancel');el('folder-message').textContent='正在安全取消…';}catch(e){el('folder-message').textContent=e.message;}}
async function update(){try{let s=await(await fetch('/status')).json();document.querySelector('#status').textContent=s.message+'\\n更新：'+s.updated_at;
el('current-folder').textContent=s.local_path||'尚未设置';el('folder-settings').hidden=!s.initialized;
let f=s.folder_change||{};folderOperation=!!s.folder_changing;folderButtons();el('folder-cancel').hidden=!folderOperation||!f.cancellable;
el('folder-progress').hidden=!folderOperation;let bar=el('folder-progress');if(f.total_bytes>0&&f.step==='copying'){bar.max=f.total_bytes;bar.value=f.bytes||0;}else bar.removeAttribute('value');
let fs=JSON.stringify(f);if(!folderPreview&&!folderRequest&&fs!==lastFolderState){if(folderOperation||f.state!=='idle')el('folder-message').textContent=(f.message||'')+(f.name?'\\n'+f.name:'')+(f.total_bytes?'\\n'+size(f.bytes||0)+' / '+size(f.total_bytes):'');lastFolderState=fs;}
document.querySelector('#setup').hidden=s.initialized&&!s.authorization_allowed;
if(!s.initialized||s.authorization_allowed){let q=await(await fetch('/qr/status')).json();document.querySelector('#qr-message').textContent=q.message;
let im=document.querySelector('#qr-image');im.hidden=!q.image;if(q.image&&im.getAttribute('src')!==q.image)im.src=q.image;
document.querySelector('#qr-start').disabled=['generating','waiting','scanned','connecting'].includes(q.state);
document.querySelector('#qr-cancel').hidden=!q.cancellable;}}catch(e){document.querySelector('#status').textContent='本机服务未响应，请重新运行打开控制台。';}}
async function startQR(){await control('qr/start');}
async function control(action){let r=await fetch('/'+action,{method:'POST',headers:{'X-TwoDrive-Nonce':nonce}});if(!r.ok)alert(await r.text());update();}
document.querySelector('#form').onsubmit=async e=>{e.preventDefault();let b=document.querySelector('#submit');b.disabled=true;try{let data=new URLSearchParams(new FormData(e.target));let r=await fetch('/configure',{method:'POST',headers:{'X-TwoDrive-Nonce':nonce},body:data});if(!r.ok)throw Error(await r.text());e.target.reset();update();}catch(e){alert(e.message)}finally{b.disabled=false}};
update();setInterval(update,3000);</script></html>'''

def authorization_allowed():
    if FOLDERS.guard.exists(): return False
    if not (t.PRIVATE/'initialized.json').exists():
        return True
    if not (t.ROOT/'runtime/stop').exists():
        return False
    try: return t.read_json(t.ROOT/'runtime/status.json').get('state') in ('paused','error')
    except FileNotFoundError: return False

def finish_activation():
    from tests.smoke import exercise
    t.status('testing','正在独立目录中测试真实上传、下载、修改、删除和冲突…')
    exercise(cloud=True)
    if (t.PRIVATE/'initialized.json').exists():
        cfg=t.read_json(t.CONFIG)
        marker=t.read_json(t.PRIVATE/'initialized.json')
        if (marker['local'],marker['remote'])!=(cfg['local_path'],cfg['remote_path']) or marker['filter_sha256']!=t.hashlib.sha256((t.ROOT/'filters.txt').read_bytes()).hexdigest():
            raise RuntimeError('同步路径或过滤规则已改变，请先检查现有同步历史')
        t.status('initializing','重新连接测试通过，保留已有同步历史并恢复运行…')
    else:
        t.status('initializing','测试通过，正在首次合并同步目录…')
        t.initialize_sync()
    t.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',t.ROOT/'install-startup.ps1'])
    t.status('ready','已启用双向同步和登录自启动。')

def activate(access,refresh):
    try:
        t.status('authorizing','正在验证 115 授权及目标文件夹…')
        t.configure(access,refresh)
        del access,refresh
        finish_activation()
    except Exception as error:
        t.status('error',(str(error) or type(error).__name__)+'；尚未启用新的同步任务。')
    finally: BUSY.release()

def activate_qr(cookie):
    try:
        t.status('authorizing','正在验证 115 扫码会话及目标文件夹…')
        t.configure_cookie(cookie)
        del cookie
        finish_activation()
        return True
    except Exception as error:
        t.status('error',(str(error) or type(error).__name__)+'；尚未启用新的同步任务。')
        return False

def retry_saved():
    try:
        t.status('authorizing','正在复用已保存的 115 会话并检查本机写入权限…')
        t.ensure_server()
        token=t.login()
        token=t.ensure_webdav_permissions(token)
        records=[record for record in t.storages(token) if record['mount_path']=='/115' and record.get('remark')=='Managed by TwoDrive']
        if len(records)!=1 or records[0]['driver'] not in ('115 Cloud','115 Open'):
            raise RuntimeError('没有可复用的 115 连接，请先扫码')
        t.cloud_folder(token,'/115','115Drive')
        t.cloud_recovery_folder(token)
        t.write_rclone_config()
        finish_activation()
    except Exception as error:
        t.status('error',(str(error) or type(error).__name__)+'；尚未启用新的同步任务。')
    finally: BUSY.release()

QR=QRController(BUSY,activate_qr)

def change_folder(path):
    try: FOLDERS.change(path)
    except Exception as error:
        FOLDERS.publish('error',str(error)+'；请重新打开控制台检查恢复状态。')
    finally: BUSY.release()

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send(self,code,body,content_type='text/plain; charset=utf-8'):
        data=body if isinstance(body,bytes) else body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer')
        self.end_headers();self.wfile.write(data)
    def valid_host(self):
        return self.headers.get('Host') in ('127.0.0.1:5245','localhost:5245')
    def do_GET(self):
        if not self.valid_host(): return self.send(403,'Invalid host')
        if self.path=='/': return self.send(200,PAGE.replace('NONCE',NONCE).replace('APP_DIRECTORY',html.escape(str(t.ROOT))),'text/html; charset=utf-8')
        if self.path=='/desktop':
            result=desktop_settings.view()
            result['nonce']=NONCE
            return self.send(200,json.dumps(result,ensure_ascii=False),'application/json; charset=utf-8')
        if self.path=='/qr/status':
            return self.send(200,json.dumps(QR.view(),ensure_ascii=False),'application/json; charset=utf-8')
        parsed=urlsplit(self.path)
        if parsed.path=='/qr/image':
            key=parse_qs(parsed.query).get('key',[''])[0]
            data=QR.get_image(key)
            return self.send(200,data,'image/png') if data else self.send(404,'Not found')
        if self.path=='/status':
            try: result=t.read_json(t.ROOT/'runtime/status.json')
            except FileNotFoundError: result=dict(message='等待 115 授权',updated_at='尚未连接')
            result['initialized']=(t.PRIVATE/'initialized.json').exists()
            result['authorization_allowed']=authorization_allowed()
            result['local_path']=t.read_json(t.CONFIG)['local_path']
            result['folder_change']=FOLDERS.view()
            result['folder_changing']=FOLDERS.guard.exists() or (BUSY.locked() and result['folder_change'].get('state')=='running')
            if result['folder_changing']:
                result['state']='relocating'
                result['message']=result['folder_change'].get('message','正在更改同步文件夹…')
            return self.send(200,json.dumps(result,ensure_ascii=False),'application/json; charset=utf-8')
        self.send(404,'Not found')
    def do_POST(self):
        if not self.valid_host() or self.headers.get('X-TwoDrive-Nonce')!=NONCE:
            return self.send(403,'Invalid request')
        if self.headers.get('Origin') not in ('http://127.0.0.1:5245','http://localhost:5245',None):
            return self.send(403,'Invalid origin')
        if self.path.startswith('/folder/'):
            return self.folder_request()
        if FOLDERS.guard.exists(): return self.send(409,'正在更改同步文件夹，请等待完成或取消。')
        if self.path in ('/desktop/preferences','/desktop/initial-folder'):
            if not BUSY.acquire(blocking=False): return self.send(409,'正在处理其他操作，请稍候。')
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=4096: raise ValueError('无效的请求大小。')
                body=json.loads(self.rfile.read(length).decode('utf-8'))
                if not isinstance(body,dict): raise ValueError('无效的请求。')
                result=desktop_settings.preferences(body) if self.path.endswith('preferences') else desktop_settings.initial_folder(body.get('path'))
                return self.send(200,json.dumps(result,ensure_ascii=False),'application/json; charset=utf-8')
            except Exception as error:
                return self.send(400,json.dumps(dict(error=str(error)),ensure_ascii=False),'application/json; charset=utf-8')
            finally: BUSY.release()
        if self.path=='/retry':
            if not authorization_allowed(): return self.send(409,'请先暂停同步，等待状态显示已暂停后重新连接。')
            if not BUSY.acquire(blocking=False): return self.send(409,'正在连接或测试，请稍候。')
            threading.Thread(target=retry_saved,daemon=True).start()
            return self.send(202,'正在复用已保存的连接')
        if self.path=='/qr/start':
            if not authorization_allowed(): return self.send(409,'请先暂停同步，等待状态显示已暂停后重新连接。')
            if not QR.start(): return self.send(409,'正在连接或测试，请稍候。')
            return self.send(202,'正在生成二维码')
        if self.path=='/qr/cancel':
            if not QR.cancel(): return self.send(409,'当前没有可取消的扫码；连接测试开始后请等待结果。')
            return self.send(200,'正在取消扫码')
        if self.path=='/configure':
            if not authorization_allowed(): return self.send(409,'请先暂停同步，等待状态显示已暂停后重新连接。')
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=20000: return self.send(400,'Invalid body size')
            fields=parse_qs(self.rfile.read(length).decode())
            access=fields.get('access',[''])[0].strip();refresh=fields.get('refresh',[''])[0].strip()
            if not access: return self.send(400,'请填写访问令牌。')
            if not refresh: return self.send(400,'此入口需要有效的两枚开放平台令牌；没有刷新令牌时请使用上方“本机扫码连接”。')
            if not BUSY.acquire(blocking=False): return self.send(409,'正在处理，请稍候。')
            threading.Thread(target=activate,args=(access,refresh),daemon=True).start()
            return self.send(202,'正在连接')
        if self.path=='/pause':
            if (t.ROOT/'runtime/stop').exists():
                return self.send(200,'已请求暂停')
            t.status('stopping','正在停止当前传输并保留续传分片。')
            (t.ROOT/'runtime/stop').touch()
            return self.send(200,'正在暂停')
        if self.path in ('/resume','/sync-now'):
            if (t.ROOT/'runtime/stop').exists() and t.read_json(t.ROOT/'runtime/status.json').get('state')=='stopping':
                return self.send(409,'正在停止当前传输，请在显示已暂停后恢复。')
            if BUSY.locked(): return self.send(409,'正在连接或测试，请等待完成后恢复同步。')
            if not (t.PRIVATE/'initialized.json').exists(): return self.send(409,'请先完成授权和测试。')
            # A scheduled task refuses a second concurrent instance.
            (t.ROOT/'runtime/stop').unlink(missing_ok=True)
            if self.path=='/sync-now': (t.ROOT/'runtime/sync-now').touch()
            result=t.run(['schtasks.exe','/Run','/TN','TwoDrive-115-Sync'],check=False)
            if result.returncode:
                t.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',t.ROOT/'install-startup.ps1'])
            return self.send(200,'正在恢复')
        self.send(404,'Not found')

    def folder_request(self):
        acquired=False
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=4096: raise ValueError('无效的请求大小。')
            body=json.loads(self.rfile.read(length).decode('utf-8'))
            if not isinstance(body,dict): raise ValueError('无效的请求。')
            if self.path=='/folder/cancel':
                if not FOLDERS.cancel(): raise ValueError('当前没有可取消的操作，或已进入最后切换阶段。')
                result=dict(ok=True)
            else:
                if FOLDERS.guard.exists(): raise ValueError('正在更改同步文件夹，请等待完成。')
                acquired=BUSY.acquire(blocking=False)
                if not acquired: raise ValueError('正在处理其他操作，请稍候。')
                path=body.get('path','')
                if not isinstance(path,str): raise ValueError('请输入文件夹路径。')
                if self.path=='/folder/preview': result=FOLDERS.preview(path)
                elif self.path=='/folder/choose':
                    picked=t.run(['powershell.exe','-NoProfile','-STA','-ExecutionPolicy','Bypass','-File',t.ROOT/'choose-folder.ps1'])
                    result=json.loads(picked.stdout.lstrip('\ufeff'))
                elif self.path=='/folder/change':
                    if not path.strip(): raise ValueError('请输入文件夹路径。')
                    threading.Thread(target=change_folder,args=(path,),daemon=True).start()
                    acquired=False  # The transaction now owns BUSY.
                    return self.send(202,json.dumps(dict(ok=True)),'application/json; charset=utf-8')
                else: raise ValueError('未知的操作。')
            return self.send(200,json.dumps(result,ensure_ascii=False),'application/json; charset=utf-8')
        except Exception as error:
            return self.send(400,json.dumps(dict(error=str(error)),ensure_ascii=False),'application/json; charset=utf-8')
        finally:
            if acquired: BUSY.release()

def serve(bootstrap=False):
    server=ThreadingHTTPServer(('127.0.0.1',5245),Handler)
    if bootstrap and not (t.PRIVATE/'admin.json').exists(): t.bootstrap()
    FOLDERS.recover()
    t.ensure_server()
    server.serve_forever()

if __name__=='__main__': serve()
