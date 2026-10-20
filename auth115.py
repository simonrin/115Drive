"""115 QR login through 115 endpoints; session and cookies never leave this process."""
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request

TOKEN_URL='https://qrcodeapi.115.com/api/1.0/web/1.0/token/'
STATUS_URL='https://qrcodeapi.115.com/get/status/'
IMAGE_URL='https://qrcodeapi.115.com/api/1.0/web/1.0/qrcode'
LOGIN_URL='https://passportapi.115.com/app/1.0/wechatmini/1.0/login/qrcode/'
PNG_HEADER=b'\x89PNG\r\n\x1a\n'

class AuthError(RuntimeError):
    pass

def fetch_json(url, fields=None, *, post=False):
    body=urllib.parse.urlencode(fields or {}).encode() if post else None
    if fields and not post:
        url += '?' + urllib.parse.urlencode(fields)
    req=urllib.request.Request(url,data=body,headers={'User-Agent':'Mozilla/5.0','Content-Type':'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(req,timeout=15) as response:
            result=json.load(response)
    except Exception:
        raise AuthError('115 接口暂时无法连接，请稍后重新扫码。') from None
    if not result.get('state'):
        code=result.get('code',result.get('errno','unknown'))
        raise AuthError('115 登录接口返回错误，代码 '+str(code))
    return result.get('data') or {}

def fetch_image(uid):
    url=IMAGE_URL+'?'+urllib.parse.urlencode({'uid':uid})
    try:
        with urllib.request.urlopen(url,timeout=15) as response:
            body=response.read(1024*1024+1)
    except Exception:
        raise AuthError('115 二维码图片暂时无法加载，请重新生成。') from None
    if not body.startswith(PNG_HEADER) or len(body)>1024*1024:
        raise AuthError('115 二维码图片格式错误，请重新生成。')
    return body

def cookie_from_result(data):
    values=data.get('cookie') or {}
    if not all(isinstance(values.get(key),str) and values[key] for key in ('UID','CID','SEID')):
        raise AuthError('115 未返回可用登录会话，请重新扫码。')
    parts=[]
    for key in ('UID','CID','SEID','KID'):
        value=values.get(key)
        if not value:
            continue
        if any(c in value for c in ';\r\n'):
            raise AuthError('115 登录会话格式错误。')
        parts.append(key+'='+value)
    return ';'.join(parts)

class QRController:
    def __init__(self,busy,activate):
        self.busy=busy
        self.activate=activate
        self.guard=threading.Lock()
        self.stop=threading.Event()
        self.state='idle'
        self.message='点击“本机扫码连接”，用手机 115 App 扫描二维码。'
        self.image=None
        self.image_key=''
        self.thread=None
    def set_state(self,state,message):
        with self.guard:
            self.state=state
            self.message=message
            if state in ('done','error','expired','cancelled'):
                self.image=None
    def view(self):
        with self.guard:
            return dict(state=self.state,message=self.message,
                image='/qr/image?key='+self.image_key if self.image else None,
                cancellable=self.state in ('generating','waiting','scanned'))
    def get_image(self,key):
        with self.guard:
            if self.image and secrets.compare_digest(key,self.image_key):
                return self.image
            return None
    def start(self):
        if not self.busy.acquire(blocking=False):
            return False
        self.stop=threading.Event()
        with self.guard:
            self.state='generating'
            self.message='正在向 115 获取二维码…'
            self.image=None
            self.image_key=secrets.token_urlsafe(24)
        self.thread=threading.Thread(target=self.run,daemon=True)
        try:
            self.thread.start()
        except Exception:
            self.busy.release()
            raise
        return True
    def cancel(self):
        with self.guard:
            if self.state not in ('generating','waiting','scanned'):
                return False
            self.stop.set()
            return True
    def run(self):
        try:
            session=fetch_json(TOKEN_URL)
            if not all(key in session for key in ('uid','time','sign')):
                raise AuthError('115 未返回完整二维码会话。')
            image=fetch_image(session['uid'])
            if self.stop.is_set():
                self.set_state('cancelled','已取消扫码。')
                return
            with self.guard:
                self.image=image
                self.state='waiting'
                self.message='用手机 115 App 扫描上方二维码，并在手机上确认登录。'
            deadline=time.monotonic()+300
            while time.monotonic()<deadline:
                if self.stop.is_set():
                    self.set_state('cancelled','已取消扫码。')
                    return
                fields={key:session[key] for key in ('uid','time','sign')}
                fields['_']=str(int(time.time()))
                result=fetch_json(STATUS_URL,fields)
                status=int(result.get('status',-1))
                if status==2:
                    with self.guard:
                        if self.stop.is_set():
                            self.state='cancelled';self.message='已取消扫码。';self.image=None
                            return
                        self.state='connecting';self.message='扫码已确认，正在连接并测试同步…';self.image=None
                    data=fetch_json(LOGIN_URL,{'account':session['uid'],'app':'wechatmini'},post=True)
                    cookie=cookie_from_result(data)
                    success=self.activate(cookie)
                    del cookie,data
                    self.set_state('done' if success else 'error',
                        '连接测试完成。' if success else '连接或同步测试未通过，请查看上方状态。')
                    return
                if status==-1:
                    self.set_state('expired','二维码已过期，请重新生成。')
                    return
                if status==-2:
                    self.set_state('cancelled','已在手机上取消登录。')
                    return
                if status==1:
                    self.set_state('scanned','已扫描，请在手机上确认登录。')
                elif status!=0:
                    raise AuthError('115 返回未知扫码状态，请重新生成二维码。')
                if self.stop.wait(2):
                    self.set_state('cancelled','已取消扫码。')
                    return
            self.set_state('expired','二维码已过期，请重新生成。')
        except AuthError as error:
            self.set_state('error',str(error))
        except Exception:
            self.set_state('error','连接未完成，请查看上方状态或重新扫码。')
        finally:
            self.busy.release()