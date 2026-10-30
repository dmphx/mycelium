"""Native scope snapshots and real Flask decision/dispatch boundary, synthetic only."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
import native_request_scopes as scopes


def payload(**extra):
    return {'tmdb_id':101,'media_type':'tv','title':'Synthetic scope',**extra}


@pytest.mark.parametrize('extra',[
 {'tmdb_id':True},{'tmdb_id':0},{'media_type':'person'},{'title':''},
 {'monitor_mode':'unknown'},{'monitor_mode':'selected','seasons':[]},
 {'monitor_mode':'selected','seasons':[True]},{'seasons':[-1]},
 {'seasons':[1000]},{'seasons':[2,2]},{'seasons':'2'},
 {'starts_on':'2020-01-01'},{'future_starts_on':'2020-01-01'},
 {'monitor_mode':None},{'seasons':[1]*101},
])
def test_malformed_native_scope_rejected(extra):
    with pytest.raises(scopes.ScopeError):scopes.prepare(payload(**extra))


@pytest.mark.parametrize('extra,expected',[
 ({'monitor_mode':'selected','seasons':[2,0]}, {'kind':'selected_seasons','seasons':[0,2]}),
 ({'monitor_mode':'all','seasons':[]}, {'kind':'all_seasons'}),
 ({'media_type':'movie'}, {'kind':'movie'}),
 ({'monitor_mode':'future'}, {'kind':'future','starts_on':'2026-10-21'}),
])
def test_receiver_scope_and_UTC_anchor(monkeypatch,extra,expected):
    monkeypatch.setattr(scopes,'utc_today',lambda:'2026-10-21')
    assert scopes.prepare(payload(**extra))==expected


@pytest.mark.parametrize('change', ['user_id','imdb_id','tmdb_id','media_type'])
def test_snapshot_cannot_move_to_another_identity(change):
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;scopes.init_schema(c)
    row={'id':1,'user_id':1,'imdb_id':'tt2000101','tmdb_id':101,'media_type':'tv'}
    scopes.store(c,1,1,'tt2000101',101,'tv',{'kind':'selected_seasons','seasons':[2]},'a'*64)
    row[change]=2 if change in ('user_id','tmdb_id') else 'different'
    with pytest.raises(scopes.ScopeError):scopes.read(c,row)


def test_stored_scope_immutable_and_corrupt_hash_rejected():
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;scopes.init_schema(c)
    row={'id':1,'user_id':1,'imdb_id':'tt2000101','tmdb_id':101,'media_type':'tv'}
    scopes.store(c,1,1,'tt2000101',101,'tv',{'kind':'future','starts_on':'2026-10-21'},'a'*64)
    with pytest.raises(sqlite3.IntegrityError):c.execute('UPDATE native_request_scopes SET scope_json=?',('{}',))
    with pytest.raises(sqlite3.IntegrityError):c.execute('DELETE FROM native_request_scopes')
    c.execute('DROP TRIGGER native_request_scopes_immutable_update')
    c.execute("UPDATE native_request_scopes SET scope_sha256=?",('0'*64,))
    with pytest.raises(scopes.ScopeError):scopes.read(c,row)


SCRIPT=r'''
import json, socket, threading, sqlite3
from pathlib import Path
from html.parser import HTMLParser
from apscheduler.schedulers.background import BackgroundScheduler
from limits.storage.memory import MemoryStorage
def deny(*a,**k):raise AssertionError('Unexpected provider/network/worker')
socket.socket.connect=deny;socket.create_connection=deny;socket.getaddrinfo=deny
BackgroundScheduler.start=lambda *a,**k:None
original_start=threading.Thread.start
known={'movie-sync-init','series-sync-init','strm-init','resolve-unknowns-init','series-monitored-init','nfo-init','nfo-repair','tmdb-id-backfill'}
def guarded(self):
 if self.name in known:return
 if isinstance(self,threading.Timer) and getattr(getattr(self,'function',None),'__func__',None) is MemoryStorage._MemoryStorage__expire_events:return
 return deny()
threading.Thread.start=guarded
import app as real, db, native_request_scopes as scopes
from datetime import date
real.app.config['TESTING']=True
uid=db.create_user('synthetic-scope-owner','fixture$disabled',role='user')
c=real.app.test_client()
with c.session_transaction() as s:s.update(user_id=uid,user='synthetic-scope-owner',role='user',auth_source='password')
class Meta(HTMLParser):
 token=None
 def handle_starttag(self,tag,attrs):
  a=dict(attrs)
  if tag=='meta' and a.get('name')=='csrf-token':self.token=a['content']
p=c.get('/app');assert p.status_code==200
parser=Meta();parser.feed(p.get_data(as_text=True));assert parser.token
headers={'X-CSRFToken':parser.token}
real.tmdb.tmdb_to_imdb=lambda tid,media_type='movie':'tt2000101'
calls=[];real._kick_off_processing=lambda *a,**k:calls.append(a)
observations=[]
cases=[({'monitor_mode':'selected','seasons':[2]}, {'kind':'selected_seasons','seasons':[2]}),({'monitor_mode':'selected','seasons':[0,2]}, {'kind':'selected_seasons','seasons':[0,2]}),({'monitor_mode':'future','seasons':[]},{'kind':'future','starts_on':'2026-10-21'}),({'monitor_mode':'all','seasons':[]},{'kind':'all_seasons'})]
for index,(extra,expected) in enumerate(cases):
 db.update_user(uid,role='user');scopes.utc_today=lambda:'2026-10-21'
 body={'tmdb_id':101+index,'media_type':'tv','title':'Synthetic scope',**extra}
 before=len(calls);post=c.post('/ui/api/discover/add',json=body,headers=headers);assert post.status_code==200,post.json
 rid=post.json['request_id'];assert post.json['request_scope']==expected and post.json['status']=='pending' and len(calls)==before
 row=next(r for r in c.get('/ui/api/user-requests?mine=1').json['items'] if r['id']==rid)
 assert row['request_scope']==expected and row['request_scope_state']=='recorded'
 assert row['seasons']==(','.join(str(s) for s in expected['seasons']) if 'seasons' in expected else None)
 assert c.post(f'/ui/api/user-requests/{rid}/approve',headers=headers).status_code==403
 db.update_user(uid,role='admin');scopes.utc_today=lambda:'2026-11-02'
 reviewed=c.post(f'/ui/api/user-requests/{rid}/approve',json={},headers=headers);assert reviewed.status_code==200,reviewed.json
 assert reviewed.json['request_scope']==expected and len(calls)==before+1
 mode,seasons,cutoff=scopes.dispatch_args(expected);assert calls[-1][4:]==(mode,seasons,cutoff)
 again=c.post(f'/ui/api/user-requests/{rid}/approve',json={},headers=headers);assert again.status_code==409 and len(calls)==before+1
 assert c.post(f'/ui/api/user-requests/{rid}/deny',json={},headers=headers).status_code==409
 after=next(r for r in c.get('/ui/api/user-requests?mine=1').json['items'] if r['id']==rid)
 assert after['request_scope']==expected and after['status']=='approved'
 observations.append({'submitted':body,'native_receipt':post.json,'history_before':row,'native_approval':reviewed.json,'history_after':after,'captured_dispatch':list(calls[-1]),'processor_executed':False})
legacy=db.create_user_request(uid,'tt2000101',900,'tv','Unknown old scope')
assert c.post(f'/ui/api/user-requests/{legacy}/approve',headers=headers).status_code==409
assert db.get_user_request(legacy)['status']=='pending'
# Snapshot and request creation roll back together, including a failing hash input.
before=len(db.get_user_requests(uid))
try:db.create_user_request(uid,'tt2000101',901,'tv','Atomic',request_scope={'kind':'all_seasons'},raw_body_sha256='bad')
except scopes.ScopeError:pass
else:raise AssertionError('Invalid hash accepted')
assert len(db.get_user_requests(uid))==before
# Identity changed after acceptance cannot dispatch even if the title still matches.
db.update_user(uid,role='user');body={'tmdb_id':999,'media_type':'tv','title':'Tamper','monitor_mode':'selected','seasons':[2]}
rid=c.post('/ui/api/discover/add',json=body,headers=headers).json['request_id']
with db._connect() as conn:conn.execute('UPDATE user_requests SET tmdb_id=998 WHERE id=?',(rid,))
db.update_user(uid,role='admin');before=len(calls)
assert c.post(f'/ui/api/user-requests/{rid}/approve',headers=headers).status_code==409
assert len(calls)==before and db.get_user_request(rid)['status']=='pending'
# Existing clock is not used when the original receiver cutoff is supplied.
db.upsert_monitored_series('tt8888888',888,'Future',[1],monitor_mode='all')
db.upsert_monitored_series('tt8888888',888,'Future',[1],monitor_mode='future',added_at_date='2026-10-21')
assert next(r for r in db.get_all_monitored_series() if r['imdb_id']=='tt8888888')['added_at_date']=='2026-10-21'
assert db.integrity_check() is not False
Path(__import__('os').environ['NATIVE_SCOPE_EVIDENCE']).write_text(json.dumps({'source_bound_native_observations':observations,'legacy_TV_approval':'409 original scope unavailable, no reconstruction','atomic_rollback':True,'identity_tamper_approval':'409, no dispatch','duplicate_review':'409, no second dispatch','dispatch_capture_only':True},indent=2)+'\n')
print('NATIVE_SCOPE_BOUNDARY_PASS')
'''


def test_real_native_scope_history_and_approval_dispatch(tmp_path):
    root=Path(__file__).resolve().parents[1]
    env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','TMPDIR')}
    evidence=Path(os.environ.get('NATIVE_SCOPE_EVIDENCE',str(tmp_path/'native-scope-observations.json')))
    env.update(DB_PATH=str(tmp_path/'scope.sqlite'),MEDIA_PATH=str(tmp_path/'media'),SPORE_MEDIA_PATH=str(tmp_path/'spore'),
        AUTH_ENABLED='true',AUTH_SESSION_SECRET='synthetic-native-scope-only-'*3,WEBHOOK_SECRET='synthetic-native-scope-only',
        TORBOX_API_KEY='synthetic-only',INSECURE_ALLOW_ANON='false',LITE_MODE='true',CATCHUP_ENABLED='false',
        SPORE_READ_CACHE_ENABLED='false',PYTHONDONTWRITEBYTECODE='1',NATIVE_SCOPE_EVIDENCE=str(evidence))
    result=subprocess.run([sys.executable,'-c',SCRIPT],cwd=root,env=env,capture_output=True,text=True,timeout=45)
    assert result.returncode==0 and 'NATIVE_SCOPE_BOUNDARY_PASS' in result.stdout,result.stdout[-1000:]+result.stderr[-3000:]
