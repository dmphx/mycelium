"""Offline versioned metadata policy, immutable native requests and zero effects."""
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys

import pytest
import request_execution_plans as plans
import request_plan_refresh as refresh
from test_request_execution_plans import env, prepare, claim, validate, digest

FIXTURE=Path(__file__).parent/'fixtures/execution-plan-refresh-v1.json'
FIXTURE_SHA='ca5502f0609f99ddfa10c4dbdddb9f48efd289c04b648b7640b008d493588faa'


@pytest.fixture()
def case(env,monkeypatch):
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest()==FIXTURE_SHA
    fixture=json.loads(FIXTURE.read_text());c=env[1]
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='request_plan_metadata_versions'").fetchone()[0]==0
    refresh.init_schema(c)
    effects={'provider':0,'adapter':0}
    def denied(*args,**kwargs):
        effects['provider']+=1
        raise AssertionError('No outbound access in offline refresh')
    monkeypatch.setattr(socket.socket,'connect',denied)
    monkeypatch.setattr(socket,'getaddrinfo',denied)
    yield env,fixture,effects
    assert effects=={'provider':0,'adapter':0}
    env[5].update(refresh_policy_version=1,refresh_fixture_sha256=FIXTURE_SHA,
                  metadata_versions=c.execute('SELECT COUNT(*) FROM request_plan_metadata_versions').fetchone()[0],
                  refresh_effect_counts=effects,original_plan_retained=True)


def root(case,name='selected'):
    env,_,_=case
    if name=='selected':p=prepare(env)
    else:
        db,_,uid,_,fixture,_,_=env;scope=copy.deepcopy(fixture['metadata'][name]['scope'])
        if name=='future':scope['starts_on']='2026-10-21'
        rid=db.create_user_request(uid,'tt2000101',101,fixture['metadata'][name]['media']['type'],'Synthetic Show',status='approved',request_scope=scope,raw_body_sha256='b'*64)
        p=prepare(env,name,rid=rid)
    handle=claim(env,p);validated=validate(env,handle,name)
    return {**validated,'generation':0}


_UNSET=object()

def append(case,current,name='selected',metadata=_UNSET,**overrides):
    md=copy.deepcopy(case[1]['metadata'][name] if metadata is _UNSET else metadata)
    args=dict(expected_generation=current['generation'],expected_plan_sha256=current['plan_sha256'],
              expected_revision=2,policy_version=1,metadata=md,metadata_sha256=digest(md),
              current_preconditions=case[0][4]['preconditions'])
    args.update(overrides)
    return refresh.refresh(case[0][1],current['request_id'],current['owner_id'],**args)


def finish(case,current,name='selected',metadata=None,worker='synthetic_refresh_worker'):
    c=case[0][1];md=copy.deepcopy(case[1]['metadata'][name] if metadata is None else metadata)
    handle=refresh.claim(c,current['request_id'],current['owner_id'],expected_generation=current['generation'],expected_revision=0,plan_sha256=current['plan_sha256'],worker_id=worker)
    result=refresh.validate(c,handle,metadata=md,metadata_sha256=digest(md),current_preconditions=case[0][4]['preconditions'])
    return handle,result


@pytest.mark.parametrize('name',['selected','all','future','movie'])
def test_versioned_refresh_preserves_original_predicate_scope_body_and_owner(case,monkeypatch,name):
    env,_,effects=case;c=env[1];old=root(case,name)
    original=c.execute('SELECT plan_json FROM request_execution_plans WHERE request_id=?',(old['request_id'],)).fetchone()[0]
    # Retain the actual v1 limitation rather than changing its immutable semantics.
    md=case[1]['metadata'][name]
    with pytest.raises(plans.PlanConflict):plans.prepare(c,old['request_id'],old['owner_id'],expected_dispatch_attempt=0,metadata=md,metadata_sha256=digest(md),preconditions=env[4]['preconditions'])
    monkeypatch.setattr(plans.scopes,'utc_today',lambda:pytest.fail('refresh must not read the clock'))
    new=append(case,old,name);assert new['generation']==1 and new['revision']==0 and new['state']=='planned'
    assert new['plan']['source']==old['plan']['source']
    assert new['plan']['parent_plan_sha256']==old['plan_sha256']
    assert new['plan']['predicate']==old['plan']['predicate']
    assert new['plan']['snapshot']['cutoff']==old['plan']['snapshot']['cutoff']
    assert c.execute('SELECT plan_json FROM request_execution_plans WHERE request_id=?',(old['request_id'],)).fetchone()[0]==original
    handle,qualified=finish(case,new,name)
    assert qualified['state']=='validated' and qualified['revision']==2 and not qualified['qualified_for_effects']
    assert refresh.get(c,old['request_id'],old['owner_id'])==qualified
    with pytest.raises(plans.EffectsUnqualified):
        refresh.require_effect_authority(c,handle)
        effects['adapter']+=1
    if name=='future':assert new['plan']['predicate']['starts_on']=='2026-10-21'
    if name=='selected':assert {e['season'] for e in new['plan']['snapshot']['episodes']}=={0,2}
    if name in ('all','future'):assert all(e['season']>0 for e in new['plan']['snapshot']['episodes'])
    if name!='movie':assert len(new['plan']['snapshot']['episodes'])==len(old['plan']['snapshot']['episodes'])+1
    env[5].update(old_plan_sha256=old['plan_sha256'],new_plan_sha256=new['plan_sha256'],refresh_generation=1,source_binding_unchanged=True)


@pytest.mark.parametrize('state',['planned','claimed'])
def test_unfinished_generation_never_reclaimed_or_refreshed(case,state):
    env=case[0];p=prepare(env)
    if state=='claimed':claim(env,p)
    with pytest.raises(plans.PlanConflict):append(case,{**p,'generation':0})
    assert env[1].execute('SELECT COUNT(*) FROM request_plan_metadata_versions').fetchone()[0]==0


def test_successors_require_validation_and_strict_parent_CAS(case):
    old=root(case);new=append(case,old)
    for current in [old,new]:
        with pytest.raises(plans.PlanConflict):append(case,current)
    first_handle,current=finish(case,new)
    md=copy.deepcopy(case[1]['metadata']['selected']);md['episodes'][0]['name']='Synthetic revised description'
    second=append(case,current,metadata=md)
    assert second['generation']==2 and second['plan']['parent_plan_sha256']==current['plan_sha256']
    with pytest.raises(plans.PlanConflict):refresh.validate(case[0][1],first_handle,metadata=case[1]['metadata']['selected'],metadata_sha256=digest(case[1]['metadata']['selected']),current_preconditions=case[0][4]['preconditions'])
    with pytest.raises(plans.PlanConflict):refresh.claim(case[0][1],new['request_id'],new['owner_id'],expected_generation=1,expected_revision=0,plan_sha256=new['plan_sha256'],worker_id='stale')
    _,final=finish(case,second,metadata=md);assert final['generation']==2 and final['revision']==2
    assert case[0][1].execute('SELECT COUNT(*) FROM request_plan_metadata_versions').fetchone()[0]==2


@pytest.mark.parametrize('field,value',[
 ('expected_generation',True),('expected_generation',-1),('expected_generation',1),('expected_generation',64),('expected_generation',65),
 ('expected_revision',True),('expected_revision',1),('policy_version',True),('policy_version',2),('expected_plan_sha256','0'*64),('metadata_sha256','0'*64)])
def test_strict_policy_generation_revision_and_hash_inputs(case,field,value):
    old=root(case)
    with pytest.raises(plans.PlanConflict):append(case,old,**{field:value})
    assert case[0][1].execute('SELECT COUNT(*) FROM request_plan_metadata_versions').fetchone()[0]==0


@pytest.mark.parametrize('change',['remove','remap','known_date','out_of_scope','media','title','missing','cutoff','empty'])
def test_ambiguous_removed_or_broadened_metadata_refused_atomically(case,change):
    old=root(case);md=copy.deepcopy(case[1]['metadata']['selected'])
    if change=='remove':md['episodes'].pop(1)
    elif change=='remap':md['episodes'][0]['tmdb_episode_id']=909
    elif change=='known_date':md['episodes'][0]['air_date']='2020-01-02'
    elif change=='out_of_scope':md['episodes'][-1]['season']=1
    elif change=='media':md['media']['tmdb_id']=102
    elif change=='title':md['title']='Synthetic other title'
    elif change=='missing':md=None
    elif change=='cutoff':md['cutoff']='2026-10-22'
    elif change=='empty':md['episodes']=[]
    with pytest.raises(plans.PlanConflict):append(case,old,metadata=md,metadata_sha256=digest(md))
    assert refresh.get(case[0][1],old['request_id'],old['owner_id'])==old
    assert case[0][1].execute('SELECT COUNT(*) FROM request_plan_metadata_versions').fetchone()[0]==0


@pytest.mark.parametrize('change',['back_catalog','specials','rebase','unknown_removed','unknown_to_backcatalog'])
def test_future_refresh_preserves_receiver_cutoff_without_guessing_or_backfill(case,change):
    old=root(case,'future');md=copy.deepcopy(case[1]['metadata']['future'])
    if change=='back_catalog':md['episodes'][-1].update(air_date='2020-01-01',air_state='aired')
    elif change=='specials':md['episodes'][-1]['season']=0
    elif change=='rebase':md['cutoff']='2026-11-02'
    elif change=='unknown_removed':md['episodes']=[e for e in md['episodes'] if e['tmdb_episode_id']!=21]
    elif change=='unknown_to_backcatalog':next(e for e in md['episodes'] if e['tmdb_episode_id']==21).update(air_date='2020-01-01',air_state='aired')
    with pytest.raises(plans.PlanConflict):append(case,old,'future',metadata=md)
    assert refresh.get(case[0][1],old['request_id'],old['owner_id'])==old


@pytest.mark.parametrize('field,value',[('destination_sha256','e'*64),('reader_generation',8),('writer_generation',5)])
def test_changed_preconditions_cannot_be_authorized_by_metadata_refresh(case,field,value):
    old=root(case);pins={**case[0][4]['preconditions'],field:value}
    with pytest.raises(plans.PlanConflict):append(case,old,current_preconditions=pins)
    new=append(case,old);c=case[0][1]
    handle=refresh.claim(c,new['request_id'],new['owner_id'],expected_generation=1,expected_revision=0,plan_sha256=new['plan_sha256'],worker_id='synthetic')
    md=case[1]['metadata']['selected']
    with pytest.raises(plans.PlanConflict):refresh.validate(c,handle,metadata=md,metadata_sha256=digest(md),current_preconditions=pins)
    assert refresh.get(c,new['request_id'],new['owner_id'])['revision']==1


@pytest.mark.parametrize('change',['disabled','denied','owner','identity','accepted','unknown','preparing','attempt'])
def test_current_native_source_fences_every_refresh_generation(case,change):
    env=case[0];db,c,uid,rid,_,_,_=env;old=root(case)
    if change=='disabled':db.update_user(uid,enabled=0)
    elif change=='denied':db.update_user_request_status(rid,'denied')
    elif change=='owner':
        other=db.create_user('synthetic-refresh-other','fixture$disabled');c.execute('UPDATE user_requests SET user_id=? WHERE id=?',(other,rid))
    elif change=='identity':c.execute('UPDATE user_requests SET tmdb_id=102 WHERE id=?',(rid,))
    elif change=='attempt':c.execute('UPDATE native_request_dispatch SET attempts=1 WHERE request_id=?',(rid,))
    else:c.execute('UPDATE native_request_dispatch SET state=?,attempts=1 WHERE request_id=?',(change,rid))
    with pytest.raises(plans.PlanConflict):append(case,old)
    assert c.execute('SELECT COUNT(*) FROM request_plan_metadata_versions').fetchone()[0]==0


def test_cross_owner_reads_and_refresh_never_share_same_title_plan(case):
    old=root(case);db,c=case[0][:2];other=db.create_user('synthetic-refresh-stranger','fixture$disabled')
    with pytest.raises(plans.PlanConflict):refresh.get(c,old['request_id'],other)
    with pytest.raises(plans.PlanConflict):append(case,{**old,'owner_id':other})
    assert refresh.get(c,old['request_id'],old['owner_id'])==old


def test_unchanged_metadata_does_not_allocate_new_generation(case):
    old=root(case);md=case[0][4]['metadata']['selected']
    with pytest.raises(plans.PlanConflict):append(case,old,metadata=md)
    assert case[0][1].execute('SELECT COUNT(*) FROM request_plan_metadata_versions').fetchone()[0]==0


def test_unknown_air_dates_remain_unknown_and_new_generation_is_immutable(case):
    old=root(case);new=append(case,old);c=case[0][1]
    assert new['plan']['snapshot']['episodes'][-1]['air_date'] is None
    for sql in ["UPDATE request_plan_metadata_versions SET plan_json='{}'",'DELETE FROM request_plan_metadata_versions',"UPDATE request_plan_metadata_heads SET owner_id=999",'DELETE FROM request_plan_metadata_heads']:
        with pytest.raises(sqlite3.IntegrityError):c.execute(sql)
    assert refresh.get(c,new['request_id'],new['owner_id'])==new


@pytest.mark.parametrize('field,value',[('generation',True),('generation',0),('revision',True),('owner_id',True),('worker_id','other'),('plan_sha256','0'*64)])
def test_stale_or_substituted_refresh_handle_cannot_validate(case,field,value):
    old=root(case);new=append(case,old);c=case[0][1];md=case[1]['metadata']['selected']
    handle=refresh.claim(c,new['request_id'],new['owner_id'],expected_generation=1,expected_revision=0,plan_sha256=new['plan_sha256'],worker_id='synthetic')
    with pytest.raises(plans.PlanConflict):refresh.validate(c,{**handle,field:value},metadata=md,metadata_sha256=digest(md),current_preconditions=case[0][4]['preconditions'])
    assert refresh.get(c,new['request_id'],new['owner_id'])['revision']==1


def test_two_process_refresh_CAS_has_one_successor_one_claimant_zero_effects(case):
    old=root(case);env=case[0];tmp=env[6];go=tmp/'refresh-go';md=tmp/'pinned-refresh.json';md.write_text(json.dumps(case[1]['metadata']['selected']));children=[]
    script=r'''
import json,sqlite3,sys,time
from pathlib import Path
import request_execution_plans as plans
import request_plan_refresh as refresh
path,go,md,rid,uid,parent,worker,pins=sys.argv[1:]
c=sqlite3.connect(path,isolation_level=None,timeout=5);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA busy_timeout=5000')
end=time.monotonic()+10
while not Path(go).exists():
 if time.monotonic()>end:raise RuntimeError('bounded start deadline')
 time.sleep(.01)
metadata=json.loads(Path(md).read_text());digest=plans._sha(plans.scopes.canonical(metadata));pre=json.loads(pins)
try:
 p=refresh.refresh(c,int(rid),int(uid),expected_generation=0,expected_plan_sha256=parent,expected_revision=2,policy_version=1,metadata=metadata,metadata_sha256=digest,current_preconditions=pre)
 h=refresh.claim(c,int(rid),int(uid),expected_generation=1,expected_revision=0,plan_sha256=p['plan_sha256'],worker_id=worker)
 final=refresh.validate(c,h,metadata=metadata,metadata_sha256=digest,current_preconditions=pre)
 print(json.dumps({'outcome':'winner','generation':final['generation'],'state':final['state'],'qualified_for_effects':final['qualified_for_effects'],'effects':0}))
except plans.PlanConflict:print(json.dumps({'outcome':'loser','effects':0}))
finally:c.close()
'''
    environment={k:v for k,v in os.environ.items() if k in ('PATH','HOME','TMPDIR')};environment['PYTHONDONTWRITEBYTECODE']='1'
    try:
        for worker in ('refresh_process_one','refresh_process_two'):
            children.append(subprocess.Popen([sys.executable,'-c',script,env[0].DB_PATH,str(go),str(md),str(old['request_id']),str(old['owner_id']),old['plan_sha256'],worker,json.dumps(env[4]['preconditions'])],cwd=Path(__file__).resolve().parents[1],env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
        go.write_text('start');outputs=[]
        for child in children:
            out,err=child.communicate(timeout=15);assert child.returncode==0,err[-1000:];outputs.append(json.loads(out))
    finally:
        for child in children:
            if child.poll() is None:child.terminate();child.wait(timeout=5)
    assert sorted(x['outcome'] for x in outputs)==['loser','winner']
    assert env[1].execute('SELECT COUNT(*) FROM request_plan_metadata_versions').fetchone()[0]==1
    assert refresh.get(env[1],old['request_id'],old['owner_id'])['state']=='validated'
    env[5].update(refresh_process_pids=[p.pid for p in children],refresh_process_results=outputs,refresh_winners=1,refresh_losers=1)


def test_database_generation_and_claim_revision_cannot_rewind_or_skip(case):
    old=root(case);new=append(case,old);c=case[0][1]
    for sql in ["UPDATE request_plan_metadata_heads SET generation=0", "UPDATE request_plan_metadata_heads SET generation=2", "UPDATE request_plan_metadata_versions SET state='validated',revision=2"]:
        with pytest.raises(sqlite3.IntegrityError):c.execute(sql)
    handle,current=finish(case,new)
    for sql in ["UPDATE request_plan_metadata_versions SET state='planned',revision=0,worker_id=NULL", "UPDATE request_plan_metadata_versions SET worker_id='replacement'"]:
        with pytest.raises(sqlite3.IntegrityError):c.execute(sql)
    assert refresh.get(c,new['request_id'],new['owner_id'])==current


def test_validation_rechecks_metadata_and_owner_after_claim(case):
    old=root(case);new=append(case,old);c=case[0][1];md=copy.deepcopy(case[1]['metadata']['selected'])
    h=refresh.claim(c,new['request_id'],new['owner_id'],expected_generation=1,expected_revision=0,plan_sha256=new['plan_sha256'],worker_id='synthetic')
    md['episodes'][0]['name']='Synthetic changed after claim'
    with pytest.raises(plans.PlanConflict):refresh.validate(c,h,metadata=md,metadata_sha256=digest(md),current_preconditions=case[0][4]['preconditions'])
    assert refresh.get(c,new['request_id'],new['owner_id'])['revision']==1
    case[0][0].update_user(new['owner_id'],enabled=0)
    md=case[1]['metadata']['selected']
    with pytest.raises(plans.PlanConflict):refresh.validate(c,h,metadata=md,metadata_sha256=digest(md),current_preconditions=case[0][4]['preconditions'])
    assert c.execute('SELECT revision FROM request_plan_metadata_versions').fetchone()[0]==1


def test_refresh_preserves_callers_transaction_and_old_base_handles_stay_spent(case):
    old=root(case);c=case[0][1];new=append(case,old)
    with pytest.raises(plans.PlanConflict):claim(case[0],old)
    c.execute('BEGIN IMMEDIATE')
    with pytest.raises(plans.PlanConflict):append(case,new)
    assert c.in_transaction;c.execute('ROLLBACK')
