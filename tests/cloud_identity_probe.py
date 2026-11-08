"""Prove provider rename semantics using generated identities only."""
import json,sys,uuid,threading,urllib.request,urllib.parse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import twodrive as t
from task_backend import OpenListBackend
from runtime_json import atomic_write_json

if '--live-115' not in sys.argv:
    raise SystemExit('Pass --live-115 to use a new dedicated cloud test folder.')

run=t.ROOT/'tests/runs'/('identity-probe-'+uuid.uuid4().hex[:12]);run.mkdir(parents=True)
cloud='/.twodrive-probe-'+uuid.uuid4().hex
atomic_write_json(run/'setup.json',{'cloud':cloud});print(run,flush=True)
task=dict(id=uuid.uuid4().hex,cloud_path=cloud)
t.api('/api/fs/mkdir',dict(path='/115'+cloud),t.login())
b=OpenListBackend(task,t)
def post(path,fields):
    req=urllib.request.Request('https://webapi.115.com/'+path,data=urllib.parse.urlencode(fields).encode(),headers={'Cookie':b.cookie,'User-Agent':'Mozilla/5.0'})
    with t.HTTP.open(req,timeout=30) as response:return json.load(response)
for name in ('a.txt','b.txt'):
    p=run/name;p.write_text(name+uuid.uuid4().hex)
    b.upload(name,p,None,threading.Event())
rows=b.snapshot(); fid=rows['b.txt']['id']
response=post('files/batch_rename',{'fid':fid,'file_name':'a.txt','files_new_name['+fid+']':'a.txt'})
raw=t.native_115_list(b.cookie,b.root_id)
result={'rename_accepted':response.get('state'),'entries':[{k:v for k,v in row.items() if k in ('fid','n','sha','s')} for row in raw]}
assert {r['fid'] for r in raw}=={row['id'] for row in rows.values()},'Rename lost an identity'
assert len({r['sha'] for r in raw})==2,'Rename lost content'
atomic_write_json(run/'result.json',result,indent=2);print(json.dumps(result),flush=True)
post('files/batch_rename',{'fid':fid,'file_name':'b.txt','files_new_name['+fid+']':'b.txt'})
post('rb/delete',{'fid[0]':rows['a.txt']['id'],'fid[1]':fid})
assert not t.native_115_list(b.cookie,b.root_id)
t.api('/api/fs/list',dict(path='/115'+cloud,password='',page=1,per_page=0,refresh=True),b.token)
t.rc('rmdir','cloud115:'+cloud.lstrip('/'))
result['ok']=True;atomic_write_json(run/'result.json',result,indent=2);print(json.dumps(result))
