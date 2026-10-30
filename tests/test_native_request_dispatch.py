"""Native dispatch bookkeeping/CAS and real native routes with synthetic dispatch only."""
import ast
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest
import native_request_dispatch as dispatch


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    for name in ('db','config','settings'):
        sys.modules.pop(name, None)
    monkeypatch.setenv('DB_PATH', str(tmp_path/'fresh.sqlite'))
    import config
    config.DB_PATH = str(tmp_path/'fresh.sqlite')
    import db
    db.init()
    uid = db.create_user('synthetic-dispatch-owner','fixture$disabled')
    rid = db.create_user_request(uid,'tt2000101',101,'tv','Synthetic',status='approved',
              request_scope={'kind':'selected_seasons','seasons':[0,2]},raw_body_sha256='a'*64)
    return db, uid, rid


def state(db,rid):
    with db._connect() as c:
        return dict(c.execute('SELECT * FROM native_request_dispatch WHERE request_id=?',(rid,)).fetchone())


def test_auto_approval_atomically_records_ready_dispatch(fresh):
    db,uid,rid=fresh
    assert state(db,rid)['state']=='ready'
    assert state(db,rid)['attempts']==0
    assert db.get_user_requests(uid)[0]['request_dispatch']['state']=='ready'


def test_ready_commit_gap_has_explicit_zero_attempt_CAS_resume(fresh):
    db,uid,rid=fresh;calls=[]
    assert db.get_user_requests(uid)[0]['request_dispatch']['can_retry']
    result=dispatch.run(rid,lambda *a:calls.append(a),expected_attempt=0)
    assert result['state']=='accepted' and result['attempt']==1 and len(calls)==1
    with pytest.raises(dispatch.DispatchConflict):dispatch.run(rid,lambda *a:calls.append(a),expected_attempt=0)
    assert len(calls)==1


def test_pending_review_and_dispatch_enqueue_roll_back_together(fresh,monkeypatch):
    db,uid,_=fresh
    rid=db.create_user_request(uid,'tt2000102',102,'tv','Pending',request_scope={'kind':'all_seasons'},raw_body_sha256='b'*64)
    def fault(*a):raise RuntimeError('synthetic enqueue failure')
    monkeypatch.setattr(dispatch,'enqueue',fault)
    with pytest.raises(RuntimeError):db.review_native_request(rid,'approved',uid)
    assert db.get_user_request(rid)['status']=='pending'
    with db._connect() as c:assert c.execute('SELECT COUNT(*) FROM native_request_dispatch WHERE request_id=?',(rid,)).fetchone()[0]==0


def test_auto_approval_and_dispatch_enqueue_roll_back_together(fresh,monkeypatch):
    db,uid,_=fresh
    before=len(db.get_user_requests(uid))
    def fault(*a):raise RuntimeError('synthetic enqueue failure')
    monkeypatch.setattr(dispatch,'enqueue',fault)
    with pytest.raises(RuntimeError):db.create_user_request(uid,'tt2000102',102,'tv','Atomic',status='approved',request_scope={'kind':'all_seasons'},raw_body_sha256='b'*64)
    assert len(db.get_user_requests(uid))==before


def test_handoff_durable_unknown_before_dispatch_and_accepted_after_return(fresh):
    db,_,rid=fresh
    saved=state(db,rid);calls=[]
    def capture(*args):
        assert state(db,rid)['state']=='unknown'
        calls.append(args)
    result=dispatch.run(rid,capture)
    assert result=={'state':'accepted','attempt':1,'can_retry':False,'error_code':None}
    assert len(calls)==1 and calls[0][4:]==('selected',[0,2],None)
    assert state(db,rid)['binding_sha256']==saved['binding_sha256']


@pytest.mark.parametrize('marker',[False,True])
def test_dispatcher_exception_is_unknown_even_if_it_claims_no_effect(fresh,marker):
    db,_,rid=fresh;calls=[]
    def fault(*args):
        calls.append({'accepted_marker':marker})
        raise RuntimeError('synthetic failure')
    result=dispatch.run(rid,fault)
    assert result['state']=='unknown' and not result['can_retry']
    with pytest.raises(dispatch.DispatchConflict):dispatch.run(rid,fault,expected_attempt=1)
    assert len(calls)==1


def test_proven_pre_handoff_failure_allows_exact_explicit_CAS_retry(fresh,monkeypatch):
    db,_,rid=fresh;calls=[];original=dispatch._prepare
    def fault(*a):raise RuntimeError('synthetic pure preparation failure')
    monkeypatch.setattr(dispatch,'_prepare',fault)
    first=dispatch.run(rid,lambda *a:calls.append(a))
    assert first=={'state':'pre_effect_failed','attempt':1,'can_retry':True,'error_code':'preparation_failed'}
    assert state(db,rid)['pre_effect_proof']=='handoff_not_entered' and calls==[]
    monkeypatch.setattr(dispatch,'_prepare',original)
    for stale in (None,0,2,True,'1'):
        with pytest.raises(dispatch.DispatchConflict):dispatch.run(rid,lambda *a:calls.append(a),expected_attempt=stale)
    result=dispatch.run(rid,lambda *a:calls.append(a),expected_attempt=1)
    assert result['state']=='accepted' and result['attempt']==2 and len(calls)==1
    with pytest.raises(dispatch.DispatchConflict):dispatch.run(rid,lambda *a:calls.append(a),expected_attempt=1)


def test_retry_budget_and_no_automatic_retry(fresh,monkeypatch):
    db,_,rid=fresh
    def fault(*a):raise ValueError('synthetic pure preparation fault')
    monkeypatch.setattr(dispatch,'_prepare',fault)
    result=dispatch.run(rid,lambda *a:pytest.fail('must not enter dispatcher'))
    for expected in (1,2):result=dispatch.run(rid,lambda *a:pytest.fail('must not enter dispatcher'),expected_attempt=expected)
    assert result['attempt']==3 and not result['can_retry']
    with pytest.raises(dispatch.DispatchConflict):dispatch.run(rid,lambda:None,expected_attempt=3)


@pytest.mark.parametrize('field,value',[('user_id',999),('tmdb_id',102),('imdb_id','tt2000999'),('title','Changed'),('media_type','movie')])
def test_changed_accepted_identity_cannot_dispatch_or_retry(fresh,field,value):
    db,_,rid=fresh
    if field == 'user_id':
        value = db.create_user('synthetic-other-row-owner','fixture$disabled')
    with db._connect() as c:c.execute(f'UPDATE user_requests SET {field}=? WHERE id=?',(value,rid))
    with pytest.raises(dispatch.DispatchConflict):dispatch.run(rid,lambda *a:pytest.fail('must not dispatch'))
    assert state(db,rid)['attempts']==0


@pytest.mark.parametrize('change',['disabled','denied'])
def test_current_owner_and_review_status_gate_retry(fresh,monkeypatch,change):
    db,uid,rid=fresh
    def fault(*a):raise ValueError('synthetic pure preparation fault')
    monkeypatch.setattr(dispatch,'_prepare',fault);dispatch.run(rid,lambda:None)
    if change=='disabled':db.update_user(uid,enabled=0)
    else:db.update_user_request_status(rid,'denied')
    with pytest.raises(dispatch.DispatchConflict):dispatch.run(rid,lambda *a:pytest.fail('must not dispatch'),expected_attempt=1)
    assert state(db,rid)['attempts']==1


def test_preparing_and_unknown_after_interruption_are_never_reclaimed(fresh):
    db,_,rid=fresh
    _,_,attempt,digest=dispatch._claim(rid,None)
    for expected in (None,attempt):
        with pytest.raises(dispatch.DispatchConflict):dispatch.run(rid,lambda *a:pytest.fail('must not dispatch'),expected_attempt=expected)
    dispatch._before_handoff(rid,attempt,digest)
    for expected in (None,attempt):
        with pytest.raises(dispatch.DispatchConflict):dispatch.run(rid,lambda *a:pytest.fail('must not dispatch'),expected_attempt=expected)
    # A stale failure completion cannot downgrade unknown into a retryable state.
    assert not dispatch._finish(rid,attempt,'preparing','pre_effect_failed','synthetic','handoff_not_entered')
    assert state(db,rid)['state']=='unknown'


def test_concurrent_explicit_retry_has_one_CAS_winner(fresh,monkeypatch):
    db,_,rid=fresh;original=dispatch._prepare
    def fault(*a):raise ValueError('synthetic pure preparation fault')
    monkeypatch.setattr(dispatch,'_prepare',fault);dispatch.run(rid,lambda:None)
    monkeypatch.setattr(dispatch,'_prepare',original)
    barrier=threading.Barrier(2);outcomes=[];calls=[]
    def retry():
        barrier.wait(timeout=5)
        try:outcomes.append(dispatch.run(rid,lambda *a:calls.append(a),expected_attempt=1)['state'])
        except dispatch.DispatchConflict:outcomes.append('conflict')
    threads=[threading.Thread(target=retry) for _ in range(2)]
    for t in threads:t.start()
    for t in threads:t.join(timeout=10);assert not t.is_alive()
    assert sorted(outcomes)==['accepted','conflict'] and len(calls)==1
    assert state(db,rid)['attempts']==2


def test_identity_binding_immutable(fresh):
    db,_,rid=fresh
    with db._connect() as c:
        with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE native_request_dispatch SET binding_json='{}' WHERE request_id=?",(rid,))
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


@pytest.mark.parametrize('scenario',['approval_preparation','approval_raise','approval_unknown','approval_accepted','approval_ready_gap','auto_preparation','auto_raise'])
def test_actual_native_CSRF_review_history_retry_boundary(tmp_path,scenario):
    root=Path(__file__).resolve().parents[1]
    tree=ast.parse((root/'tests/test_native_request_scopes.py').read_text())
    bootstrap=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='SCRIPT' for t in n.targets)).split('uid=db.create_user')[0]
    script=bootstrap+ROUTE_SCRIPT
    evidence=Path(os.environ.get('NATIVE_DISPATCH_EVIDENCE',str(tmp_path)))/(scenario+'.json')
    env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','TMPDIR')}
    env.update(DB_PATH=str(tmp_path/'fresh.sqlite'),MEDIA_PATH=str(tmp_path/'media'),SPORE_MEDIA_PATH=str(tmp_path/'spore'),AUTH_ENABLED='true',AUTH_SESSION_SECRET='synthetic-native-dispatch-only-'*3,WEBHOOK_SECRET='synthetic-only',TORBOX_API_KEY='synthetic-only',INSECURE_ALLOW_ANON='false',LITE_MODE='true',CATCHUP_ENABLED='false',SPORE_READ_CACHE_ENABLED='false',PYTHONDONTWRITEBYTECODE='1',SCENARIO=scenario,RESULT=str(evidence))
    result=subprocess.run([sys.executable,'-c',script],cwd=root,env=env,capture_output=True,text=True,timeout=45)
    assert result.returncode==0 and 'NATIVE_DISPATCH_BOUNDARY_PASS' in result.stdout,result.stdout[-1000:]+result.stderr[-3000:]


ROUTE_SCRIPT=r'''
import os,hashlib,native_request_dispatch as dispatch
real.app.config['PROPAGATE_EXCEPTIONS']=False
assert Path(real.__file__).resolve().parent==Path.cwd()
uid=db.create_user('synthetic-dispatch-owner','fixture$disabled',role='user')
c=real.app.test_client()
with c.session_transaction() as s:s.update(user_id=uid,user='synthetic-dispatch-owner',role='user',auth_source='password')
class Meta(HTMLParser):
 token=None
 def handle_starttag(self,tag,attrs):
  a=dict(attrs)
  if tag=='meta' and a.get('name')=='csrf-token':self.token=a['content']
p=c.get('/app');assert p.status_code==200
parser=Meta();parser.feed(p.get_data(as_text=True));assert parser.token
headers={'X-CSRFToken':parser.token}
real.tmdb.tmdb_to_imdb=lambda *a,**k:'tt2000101'
calls=[];accepted=[];scenario=os.environ['SCENARIO'];original=dispatch._prepare
scopes.utc_today=lambda:'2026-10-21'
def preparation_fault(*a):raise RuntimeError('synthetic pure local preparation fault')
def injected(*args):
 calls.append(list(args))
 if scenario=='approval_unknown':accepted.append('synthetic handoff marker only, no executor')
 if scenario.endswith('raise') or scenario=='approval_unknown':raise RuntimeError('synthetic dispatch exception')
real._kick_off_processing=injected
if scenario.endswith('preparation'):dispatch._prepare=preparation_fault
body={'tmdb_id':101,'media_type':'tv','title':'Synthetic dispatch','monitor_mode':'selected','seasons':[0,2]}
if scenario=='auto_preparation':body.update(monitor_mode='future',seasons=[])
raw=json.dumps(body,sort_keys=True).encode();before_raw_sha=hashlib.sha256(raw).hexdigest()
if scenario.startswith('auto'):db.update_user(uid,auto_approve=1)
# Actual POST sends these exact captured bytes, rather than reconstructing them afterward.
post=c.post('/ui/api/discover/add',data=raw,content_type='application/json',headers=headers)
assert post.status_code==200 and post.json['request_id']
rid=post.json['request_id'];expected=post.json['request_scope']
with db._connect() as conn:
 before=dict(conn.execute('SELECT * FROM native_request_scopes WHERE request_id=?',(rid,)).fetchone())
 assert before['raw_body_sha256']==before_raw_sha
if not scenario.startswith('auto'):
 assert post.json['status']=='pending' and calls==[]
 if scenario=='approval_ready_gap':
  original_run=dispatch.run
  def crash_before_claim(*a,**k):raise RuntimeError('synthetic crash after review commit before claim')
  dispatch.run=crash_before_claim
 db.update_user(uid,role='admin')
 response=c.post(f'/ui/api/user-requests/{rid}/approve',json={},headers=headers)
else:
 response=post;db.update_user(uid,role='admin')
if scenario=='approval_ready_gap':
 assert response.status_code==500
 dispatch.run=original_run
 first=c.get('/ui/api/user-requests?mine=1').json['items'][0]['request_dispatch']
 assert first['state']=='ready' and first['attempt']==0 and first['can_retry'] and calls==[]
else:
 assert response.status_code==200 and response.json['status']=='approved'
 first=response.json['request_dispatch'];assert first['attempt']==1
if scenario=='approval_ready_gap':pass
elif scenario.endswith('preparation'):
 assert first['state']=='pre_effect_failed' and first['can_retry'] and calls==[]
else:
 assert first['state']==('accepted' if scenario.endswith('accepted') else 'unknown')
 assert not first['can_retry'] and len(calls)==1
repeat=c.post(f'/ui/api/user-requests/{rid}/approve',json={},headers=headers);assert repeat.status_code==409
assert c.post(f'/ui/api/user-requests/{rid}/retry-dispatch',json={'attempt':1}).status_code==400
for payload in ({},{'attempt':True},{'attempt':'1'},{'attempt':1,'extra':1}):
 assert c.post(f'/ui/api/user-requests/{rid}/retry-dispatch',json=payload,headers=headers).status_code==400
assert c.post(f'/ui/api/user-requests/{rid}/retry-dispatch',json={'attempt':2},headers=headers).status_code==409
# Existing synthetic owner becomes ordinary again; this is no real identity/admin activation.
db.update_user(uid,role='user')
assert c.post(f'/ui/api/user-requests/{rid}/retry-dispatch',json={'attempt':1},headers=headers).status_code==403
db.update_user(uid,role='admin');dispatch._prepare=original
scopes.utc_today=lambda:'2026-11-02'
expected_attempt=0 if scenario=='approval_ready_gap' else 1
retry=c.post(f'/ui/api/user-requests/{rid}/retry-dispatch',json={'attempt':expected_attempt},headers=headers)
if scenario=='approval_ready_gap':
 assert retry.status_code==200 and retry.json['request_dispatch']['state']=='accepted'
 assert retry.json['request_dispatch']['attempt']==1 and len(calls)==1
elif scenario.endswith('preparation'):
 assert retry.status_code==200 and retry.json['request_dispatch']['state']=='accepted'
 assert retry.json['request_dispatch']['attempt']==2 and len(calls)==1
else:assert retry.status_code==409 and len(calls)==1
assert c.post(f'/ui/api/user-requests/{rid}/retry-dispatch',json={'attempt':expected_attempt},headers=headers).status_code==409
history=next(r for r in c.get('/ui/api/user-requests?mine=1').json['items'] if r['id']==rid)
assert history['status']=='approved' and history['request_scope']==expected
assert history['request_dispatch']['state']==('accepted' if scenario.endswith('preparation') or scenario.endswith('accepted') or scenario=='approval_ready_gap' else 'unknown')
mode,seasons,cutoff=scopes.dispatch_args(expected);assert calls[0][4:]==[mode,seasons,cutoff]
with db._connect() as conn:
 after=dict(conn.execute('SELECT * FROM native_request_scopes WHERE request_id=?',(rid,)).fetchone())
 assert before==after and conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
 assert len(db.get_user_requests(uid))==1
Path(os.environ['RESULT']).write_text(json.dumps({'scenario':scenario,'origins':{'app':real.__file__,'db':db.__file__,'dispatch':dispatch.__file__},'HTTP':{'POST':post.status_code,'review_or_auto':response.status_code,'repeat_review':repeat.status_code,'retry':retry.status_code},'request_id':rid,'native_owner':uid,'review_status':history['status'],'first_dispatch':first,'final_dispatch':history['request_dispatch'],'scope_before':expected,'scope_after':history['request_scope'],'scope_sha256_before':before['scope_sha256'],'scope_sha256_after':after['scope_sha256'],'captured_before_POST_body_sha256':before_raw_sha,'stored_body_sha256_before':before['raw_body_sha256'],'stored_body_sha256_after':after['raw_body_sha256'],'dispatcher_calls':len(calls),'captured_dispatch':calls,'synthetic_handoff_markers':len(accepted),'real_executor_provider_monitor_executed':False},indent=2)+'\n')
print('NATIVE_DISPATCH_BOUNDARY_PASS')
'''
