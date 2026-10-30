"""Isolated request-plan ownership/CAS. No real effect or shared writer is enabled."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
import request_execution_plans as plans

FIXTURE=Path(__file__).parent/'fixtures/execution-plan-v1.json'
FIXTURE_SHA='42d10a1ab2afcbf42e2f7a33b1ab8a5c9b12d53883948bb9d84d2221ab9d8705'


def digest(value):return hashlib.sha256(plans.scopes.canonical(value).encode()).hexdigest()


@pytest.fixture()
def env(monkeypatch,tmp_path):
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest()==FIXTURE_SHA
    fixture=json.loads(FIXTURE.read_text())
    for name in ('db','config','settings'):
        sys.modules.pop(name,None)
    monkeypatch.setenv('DB_PATH',str(tmp_path/'isolated.sqlite'))
    import config
    config.DB_PATH=str(tmp_path/'isolated.sqlite')
    import db
    db.init()
    with db._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='request_execution_plans'").fetchone()[0]==0
        plans.init_schema(c)
    uid=db.create_user('synthetic-plan-owner','fixture$disabled')
    scope={'kind':'selected_seasons','seasons':[0,2]}
    rid=db.create_user_request(uid,'tt2000101',101,'tv','Synthetic Show',status='approved',request_scope=scope,raw_body_sha256='a'*64)
    # Seed a disposable legacy-shaped sentinel, then prohibit any writer call.
    db.upsert_monitored_series('tt2000999',999,'Synthetic sentinel',[8],monitor_mode='selected')
    c=db._raw_connect()
    original_scope=dict(c.execute('SELECT * FROM native_request_scopes WHERE request_id=?',(rid,)).fetchone())
    monitor_before=[dict(r) for r in c.execute('SELECT * FROM monitored_series')]
    audit={'native_request_id':rid,'native_owner_id':uid,'fixture_sha256':FIXTURE_SHA,
           'winner_count':0,'loser_count':0,'fake_effect_calls':0,'shared_writer_calls':0,'provider_calls':0}
    def no_shared(*a,**k):
        audit['shared_writer_calls']+=1
        raise AssertionError('Unexpected shared writer during offline planning')
    monkeypatch.setattr(db,'upsert_monitored_series',no_shared)
    monkeypatch.setattr(db,'update_monitored_series',no_shared)
    yield db,c,uid,rid,fixture,audit,tmp_path
    assert dict(c.execute('SELECT * FROM native_request_scopes WHERE request_id=?',(rid,)).fetchone())==original_scope
    assert [dict(r) for r in c.execute('SELECT * FROM monitored_series')]==monitor_before
    assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert audit['fake_effect_calls']==audit['shared_writer_calls']==audit['provider_calls']==0
    audit.update(native_scope_sha256_before=digest(original_scope),native_scope_sha256_after=digest(dict(c.execute('SELECT * FROM native_request_scopes WHERE request_id=?',(rid,)).fetchone())),legacy_sentinel_unchanged=True)
    output=Path(os.environ.get('EXECUTOR_CAS_EVIDENCE',str(tmp_path)))
    output.mkdir(parents=True,exist_ok=True)
    (output/(tmp_path.name+'.json')).write_text(json.dumps(audit,indent=2)+'\n')
    c.close()


def prepare(env,name='selected',rid=None,uid=None):
    db,c,owner,request_id,fixture,_,_=env
    md=copy.deepcopy(fixture['metadata'][name])
    return plans.prepare(c,rid or request_id,uid or owner,expected_dispatch_attempt=0,
        metadata=md,metadata_sha256=digest(md),preconditions=fixture['preconditions'])


def claim(env,plan,worker='synthetic_worker'):
    return plans.claim(env[1],plan['request_id'],plan['owner_id'],expected_revision=0,
                       plan_sha256=plan['plan_sha256'],worker_id=worker)


def validate(env,handle,name='selected',preconditions=None,metadata=None):
    fixture=env[4];md=copy.deepcopy(metadata if metadata is not None else fixture['metadata'][name])
    return plans.validate(env[1],handle,metadata=md,metadata_sha256=digest(md),
                          current_preconditions=preconditions or fixture['preconditions'])


def test_same_title_different_owned_scopes_keep_independent_immutable_plans(env):
    db,c,uid,rid,fixture,audit,_=env
    other=db.create_user('synthetic-other-plan-owner','fixture$disabled')
    second=db.create_user_request(other,'tt2000101',101,'tv','Synthetic Show',status='approved',request_scope={'kind':'selected_seasons','seasons':[3]},raw_body_sha256='b'*64)
    first=prepare(env);second_plan=prepare(env,'other',rid=second,uid=other)
    assert first['plan_sha256']!=second_plan['plan_sha256']
    assert first['plan']['source']['scope']=={'kind':'selected_seasons','seasons':[0,2]}
    assert second_plan['plan']['source']['scope']=={'kind':'selected_seasons','seasons':[3]}
    assert plans.get(c,rid,uid)['plan_sha256']==first['plan_sha256']
    with pytest.raises(plans.PlanConflict):plans.get(c,rid,other)
    assert c.execute('SELECT COUNT(*) FROM request_execution_plans').fetchone()[0]==2
    audit.update(independent_plan_count=2,plan_hashes=[first['plan_sha256'],second_plan['plan_sha256']])


@pytest.mark.parametrize('name',['selected','all','future','movie'])
def test_exact_pinned_episode_pairs_and_fixed_receiver_cutoff(env,monkeypatch,name):
    db,c,uid,_,fixture,audit,_=env
    metadata=fixture['metadata'][name];scope=copy.deepcopy(metadata['scope'])
    if name=='future':scope['starts_on']='2026-10-21'
    rid=db.create_user_request(uid,'tt2000101',101,metadata['media']['type'],'Synthetic Show',status='approved',request_scope=scope,raw_body_sha256='b'*64)
    monkeypatch.setattr(plans.scopes,'utc_today',lambda:pytest.fail('must not recompute receiver cutoff'))
    plan=prepare(env,name,rid=rid)
    assert plan['plan']['source']['scope']==scope
    assert plan['plan']['metadata_sha256']==digest(metadata)
    assert plan['plan']['snapshot']['episodes']==[{k:e[k] for k in ('season','episode','tmdb_episode_id','name','runtime_min','air_date')} for e in metadata['episodes']]
    if name in ('all','future'):assert all(e['season']>0 for e in plan['plan']['snapshot']['episodes'])
    if name=='future':assert plan['plan']['predicate']['starts_on']=='2026-10-21'
    if name=='selected':assert {e['season'] for e in plan['plan']['snapshot']['episodes']}=={0,2}
    assert not plan['qualified_for_effects']
    audit.update(scope=scope,metadata_sha256=digest(metadata),exact_episode_count=len(metadata['episodes']))


@pytest.mark.parametrize('mutation',['hash','title','media','missing','empty','outside_scope','missing_selected_season','extra'])
def test_missing_changed_or_mismatched_metadata_cannot_create_plan(env,mutation):
    _,c,uid,rid,fixture,_,_=env;md=copy.deepcopy(fixture['metadata']['selected']);expected=digest(md)
    if mutation=='hash':md['episodes'][0]['name']='Changed'
    if mutation=='title':md['title']='Another title'
    if mutation=='media':md['media']['tmdb_id']=102
    if mutation=='missing':md=None
    if mutation=='empty':md['episodes']=[]
    if mutation=='outside_scope':md['episodes'][0]['season']=1
    if mutation=='missing_selected_season':md['episodes']=[e for e in md['episodes'] if e['season']==0]
    if mutation=='extra':md['assumed_complete']=True
    with pytest.raises(plans.PlanConflict):plans.prepare(c,rid,uid,expected_dispatch_attempt=0,metadata=md,
        metadata_sha256=expected if mutation=='hash' else digest(md),preconditions=fixture['preconditions'])
    assert c.execute('SELECT COUNT(*) FROM request_execution_plans').fetchone()[0]==0


def test_future_cutoff_cannot_be_rebased_by_later_metadata(env):
    db,c,uid,_,fixture,_,_=env
    rid=db.create_user_request(uid,'tt2000101',101,'tv','Synthetic Show',status='approved',request_scope={'kind':'future','starts_on':'2026-10-21'},raw_body_sha256='b'*64)
    md=copy.deepcopy(fixture['metadata']['future']);md['cutoff']='2026-11-02'
    for e in md['episodes']:e['air_state']='unknown' if e['air_date'] is None else ('aired' if e['air_date']<=md['cutoff'] else 'unaired')
    with pytest.raises(plans.PlanConflict):plans.prepare(c,rid,uid,expected_dispatch_attempt=0,metadata=md,metadata_sha256=digest(md),preconditions=fixture['preconditions'])


def test_identical_prepare_replays_but_new_metadata_cannot_replace_immutable_plan(env):
    _,c,uid,rid,fixture,audit,_=env;first=prepare(env);assert prepare(env)==first
    md=copy.deepcopy(fixture['metadata']['selected']);md['episodes'][0]['name']='Later name'
    with pytest.raises(plans.PlanConflict):plans.prepare(c,rid,uid,expected_dispatch_attempt=0,metadata=md,metadata_sha256=digest(md),preconditions=fixture['preconditions'])
    assert plans.get(c,rid,uid)==first
    with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE request_execution_plans SET plan_json='{}' WHERE request_id=?",(rid,))
    with pytest.raises(sqlite3.IntegrityError):c.execute('DELETE FROM request_execution_plans WHERE request_id=?',(rid,))
    audit.update(immutable_plan_sha256_before=first['plan_sha256'],immutable_plan_sha256_after=plans.get(c,rid,uid)['plan_sha256'])


@pytest.mark.parametrize('field,value',[('destination_sha256','e'*64),('reader_generation',8),('writer_generation',5)])
def test_changed_destination_or_reader_writer_generation_refuses_validation(env,field,value):
    plan=prepare(env);handle=claim(env,plan);pins=copy.deepcopy(env[4]['preconditions']);pins[field]=value
    with pytest.raises(plans.PlanConflict):validate(env,handle,preconditions=pins)
    row=plans.get(env[1],plan['request_id'],plan['owner_id']);assert row['state']=='claimed' and row['revision']==1


@pytest.mark.parametrize('field,value',[('revision',True),('reader_generation',True),('writer_generation',-1),('destination_sha256','bad'),('worker_id','unsafe identity')])
def test_strict_revision_generation_and_worker_inputs(env,field,value):
    plan=prepare(env);_,c,uid,rid,fixture,_,_=env
    if field in ('reader_generation','writer_generation','destination_sha256'):
        pins=copy.deepcopy(fixture['preconditions']);pins[field]=value
        with pytest.raises(plans.PlanConflict):plans.prepare(c,rid,uid,expected_dispatch_attempt=0,metadata=fixture['metadata']['selected'],metadata_sha256=digest(fixture['metadata']['selected']),preconditions=pins)
    else:
        with pytest.raises(plans.PlanConflict):plans.claim(c,rid,uid,expected_revision=value if field=='revision' else 0,plan_sha256=plan['plan_sha256'],worker_id=value if field=='worker_id' else 'synthetic')


@pytest.mark.parametrize('field,value',[('request_id',True),('owner_id',999),('revision',0),('worker_id','stale_worker'),('plan_sha256','0'*64)])
def test_stale_or_substituted_claim_cannot_validate(env,field,value):
    plan=prepare(env);handle=claim(env,plan);bad={**handle,field:value}
    with pytest.raises(plans.PlanConflict):validate(env,bad)
    assert plans.get(env[1],plan['request_id'],plan['owner_id'])['revision']==1
    good=validate(env,handle);assert good['state']=='validated' and good['revision']==2 and not good['qualified_for_effects']


@pytest.mark.parametrize('change',['disabled','denied','owner','identity','accepted','unknown','preparing','dispatch_attempt'])
def test_current_policy_identity_and_handoff_generation_fence_plan_claim(env,change):
    db,c,uid,rid,_,_,_=env;plan=prepare(env)
    if change=='disabled':db.update_user(uid,enabled=0)
    elif change=='denied':db.update_user_request_status(rid,'denied')
    elif change=='owner':
        other=db.create_user('synthetic-substituted-owner','fixture$disabled')
        c.execute('UPDATE user_requests SET user_id=? WHERE id=?',(other,rid))
    elif change=='identity':c.execute('UPDATE user_requests SET tmdb_id=102 WHERE id=?',(rid,))
    elif change=='dispatch_attempt':c.execute('UPDATE native_request_dispatch SET attempts=1 WHERE request_id=?',(rid,))
    else:c.execute('UPDATE native_request_dispatch SET state=?,attempts=1 WHERE request_id=?',(change,rid))
    with pytest.raises(plans.PlanConflict):claim(env,plan)
    row=c.execute('SELECT state,revision FROM request_execution_plans WHERE request_id=?',(rid,)).fetchone();assert tuple(row)==('planned',0)


@pytest.mark.parametrize('media_type',['tv','movie'])
def test_missing_original_scope_never_guessed_for_execution_plan(env,media_type):
    db,c,uid,_,fixture,_,_=env
    rid=db.create_user_request(uid,'tt2000101',101,media_type,'Synthetic Show',status='approved')
    md=fixture['metadata']['movie' if media_type=='movie' else 'selected']
    with pytest.raises(plans.PlanConflict):plans.prepare(c,rid,uid,expected_dispatch_attempt=0,metadata=md,metadata_sha256=digest(md),preconditions=fixture['preconditions'])


def test_interrupted_claim_has_no_expiry_reclamation_or_unknown_retry(env):
    plan=prepare(env);handle=claim(env,plan)
    with pytest.raises(plans.PlanConflict):claim(env,plan,'replacement_worker')
    env[1].execute("UPDATE native_request_dispatch SET state='unknown',attempts=1 WHERE request_id=?",(plan['request_id'],))
    with pytest.raises(plans.PlanConflict):validate(env,handle)
    assert env[1].execute('SELECT revision FROM request_execution_plans').fetchone()[0]==1


def test_even_validated_winner_cannot_invoke_effect_adapter(env):
    plan=prepare(env);handle=claim(env,plan);result=validate(env,handle)
    assert result['state']=='validated' and not result['qualified_for_effects']
    with pytest.raises(plans.EffectsUnqualified):
        plans.require_effect_authority(env[1],handle)
        env[5]['fake_effect_calls']+=1
    assert env[5]['fake_effect_calls']==0


def test_revalidation_rechecks_current_pinned_metadata(env):
    plan=prepare(env);handle=claim(env,plan);md=copy.deepcopy(env[4]['metadata']['selected']);md['episodes'][0]['name']='Changed after claim'
    with pytest.raises(plans.PlanConflict):validate(env,handle,metadata=md)
    assert env[1].execute('SELECT revision FROM request_execution_plans').fetchone()[0]==1


def test_nested_transaction_refused_without_rolling_back_callers_transaction(env):
    c=env[1];c.execute('BEGIN IMMEDIATE')
    with pytest.raises(plans.PlanConflict):prepare(env)
    assert c.in_transaction;c.execute('ROLLBACK')


def test_two_independent_processes_have_one_CAS_winner_zero_effects(env):
    db,c,uid,rid,_,audit,tmp=env;plan=prepare(env);go=tmp/'go';children=[]
    script=r'''
import json,sqlite3,sys,time
from pathlib import Path
import request_execution_plans as plans
path,go,rid,uid,digest,worker=sys.argv[1:]
c=sqlite3.connect(path,isolation_level=None,timeout=5);c.row_factory=sqlite3.Row
c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA busy_timeout=5000')
end=time.monotonic()+10
while not Path(go).exists():
 if time.monotonic()>end:raise RuntimeError('bounded start wait expired')
 time.sleep(.01)
try:
 handle=plans.claim(c,int(rid),int(uid),expected_revision=0,plan_sha256=digest,worker_id=worker)
 print(json.dumps({'outcome':'winner','handle':handle,'effect_calls':0}))
except plans.PlanConflict:
 print(json.dumps({'outcome':'loser','effect_calls':0}))
finally:c.close()
'''
    environment={k:v for k,v in os.environ.items() if k in ('PATH','HOME','TMPDIR')};environment['PYTHONDONTWRITEBYTECODE']='1'
    try:
        for worker in ('synthetic_process_one','synthetic_process_two'):
            children.append(subprocess.Popen([sys.executable,'-c',script,db.DB_PATH,str(go),str(rid),str(uid),plan['plan_sha256'],worker],cwd=Path(__file__).resolve().parents[1],env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
        go.write_text('start')
        outputs=[]
        for child in children:
            stdout,stderr=child.communicate(timeout=15);assert child.returncode==0,stderr[-1000:];outputs.append(json.loads(stdout))
    finally:
        for child in children:
            if child.poll() is None:child.terminate();child.wait(timeout=5)
    assert sorted(x['outcome'] for x in outputs)==['loser','winner']
    assert sum(x['effect_calls'] for x in outputs)==0
    winner=next(x['handle'] for x in outputs if x['outcome']=='winner')
    final=validate(env,winner);assert final['state']=='validated' and final['revision']==2
    with pytest.raises(plans.EffectsUnqualified):plans.require_effect_authority(c,winner)
    audit.update(winner_count=1,loser_count=1,process_pids=[p.pid for p in children],process_results=outputs,final_plan_sha256=final['plan_sha256'],final_revision=2)
