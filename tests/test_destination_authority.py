"""Synthetic-only completeness, preimage and cross-process fence qualification."""
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
import destination_authority as gate
import request_execution_plans as plans
import request_plan_refresh as refresh
from test_request_execution_plans import env, digest

FIXTURE=Path(__file__).parent/'fixtures/destination-fence-model-v10-dash-maps.json'
FIXTURE_SHA='b989a6502f04717a301612370e87c9727ea338101d4e87cd103df084062729b4'
INVENTORY=FIXTURE.with_name('destination-path-inventory-v10-dash-maps.json')
INVENTORY_SHA='c38d4532c427976b3ad8767457a6cab8f7b9791c32bb6433ede3d26f1b54ccc0'
MODEL=json.loads(FIXTURE.read_text())
MEMBERS=[(kind,member) for kind in ('writer','reader') for member in MODEL[kind+'s']]


@pytest.fixture()
def case(env,monkeypatch):
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest()==FIXTURE_SHA
    assert hashlib.sha256(INVENTORY.read_bytes()).hexdigest()==INVENTORY_SHA
    assert MODEL['source_inventory_sha256']==INVENTORY_SHA
    assert hashlib.sha256(Path(gate.__file__).read_bytes()).hexdigest()==MODEL['gateway_sha256']
    c=env[1];refresh.init_schema(c);gate.init_schema(c)
    assert c.execute('SELECT COUNT(*) FROM destination_model_domains').fetchone()[0]==0
    state={'env':env,'manifest':copy.deepcopy(MODEL),'effects':0,'provider':0,'input_tamper':False}
    def no_network(*args,**kwargs):
        state['provider']+=1
        raise AssertionError('Provider/network access forbidden in offline fence test')
    monkeypatch.setattr(socket.socket,'connect',no_network);monkeypatch.setattr(socket,'getaddrinfo',no_network)
    yield state
    assert state['effects']==state['provider']==0
    env[5].update(destination_authority_fixture_sha256=FIXTURE_SHA,source_inventory_sha256=INVENTORY_SHA,
                  destination_effects=state['effects'],destination_provider_calls=state['provider'],
                  all_writer_fences=c.execute('SELECT COUNT(*) FROM destination_model_fences').fetchone()[0],
                  modeled_writer_count=len(MODEL['writers']),modeled_reader_count=len(MODEL['readers']),
                  declared_synthetic_input_tamper=state['input_tamper'],live_coverage_qualified=False)


def enroll(case,missing=None,manifest=None):
    m=case['manifest'] if manifest is None else manifest;c=case['env'][1]
    gate.enroll(c,m,digest(m))
    for kind,member in MEMBERS:
        if (kind,member)!=missing:gate.register(c,m['domain_id'],kind,member,m['gateway_sha256'])
    return m['domain_id']


def closed(case):
    domain=enroll(case);gate.seal(case['env'][1],domain);return domain


def plan(case,snapshot=None):
    env=case['env'];_,c,uid,rid,fixture,_,_=env
    if snapshot is None:snapshot=gate.observe(c,case['manifest']['domain_id'])
    pins={key:snapshot[key] for key in plans._PRECONDITIONS};md=fixture['metadata']['selected']
    p=plans.prepare(c,rid,uid,expected_dispatch_attempt=0,metadata=md,metadata_sha256=digest(md),preconditions=pins)
    h=plans.claim(c,rid,uid,expected_revision=0,plan_sha256=p['plan_sha256'],worker_id='synthetic_plan_worker')
    p=plans.validate(c,h,metadata=md,metadata_sha256=digest(md),current_preconditions=pins)
    return {**p,'generation':0}


def reserve(case,p,expected=None,observed_preimage=None,**override):
    domain=case['manifest']['domain_id'];c=case['env'][1]
    args={'plan_generation':p['generation'],'plan_sha256':p['plan_sha256'],
          'expected':gate.observe(c,domain) if expected is None else expected,
          'observed_preimage':case['manifest']['preimage'] if observed_preimage is None else observed_preimage,
          'worker_id':'synthetic_fence_worker'}
    args.update(override)
    return gate.reserve(c,domain,p['request_id'],p['owner_id'],**args)


def test_source_inventory_has_actual_unfenced_writers_and_reader_unknowns(case):
    inventory=json.loads(INVENTORY.read_text());source=Path(__file__).resolve().parents[1]
    assert inventory['runtime_completeness']=='UNPROVED'
    assert not inventory['source_search_is_completeness_proof']
    assert len(inventory['writer_groups'])==14 and len(inventory['reader_domains'])==6
    assert {x['id'] for x in inventory['writer_groups']}==set(MODEL['writers'])
    assert {x['id'] for x in inventory['reader_domains']}==set(MODEL['readers'])
    assert all(not x['runtime_fenced'] for x in inventory['writer_groups'])
    assert all(not x['runtime_authority_complete'] for x in inventory['reader_domains'])
    for item in inventory['audited_python_files']+[pin for x in inventory['writer_groups']+inventory['reader_domains'] for pin in x['source_files']]:
        assert hashlib.sha256((source/item['path']).read_bytes()).hexdigest()==item['sha256']
    assert len(inventory['audited_python_files'])==110 and len(inventory['static_sink_calls'])==179
    from tools.source_inventory import sink_calls
    assert inventory['static_sink_calls']==sink_calls(source, [x['path'] for x in inventory['audited_python_files']])
    assert inventory['omitted_runtime_domains']
    for runtime in ['app.py','db.py','processor.py','monitor.py','retry_queue.py','arr_import.py','cleanup.py','identity_repair.py']:
        assert 'import destination_authority' not in (source/runtime).read_text()
    case['env'][5].update(source_writer_groups=14,source_reader_domains=6,audited_python_files=110,static_sink_calls=174)


def test_complete_closed_model_can_CAS_check_but_never_call_effect_adapter(case):
    domain=closed(case);c=case['env'][1];before=gate.observe(c,domain);p=plan(case);h=reserve(case,p,expected=before)
    for kind,member in MEMBERS:
        with pytest.raises(plans.PlanConflict):gate.enter(c,domain,kind,member,expected_generation=before[kind+'_generation'])
    checked=gate.check(c,h,observed_preimage=case['manifest']['preimage'])
    assert checked['model_coverage_checked'] and not checked['qualified_for_effects']
    with pytest.raises(plans.EffectsUnqualified):
        gate.require_effect_authority(c,checked)
        case['effects']+=1
    assert gate.observe(c,domain)=={**before,'fence_revision':1}
    assert c.execute('SELECT preimage FROM destination_model_domains').fetchone()[0]==case['manifest']['preimage']
    with pytest.raises(plans.PlanConflict):gate.check(c,h,observed_preimage=case['manifest']['preimage'])
    with pytest.raises(plans.PlanConflict):reserve(case,p,expected=before)
    case['env'][5].update(model_checked=True,all20_entrypoints_blocked=True,preimage_before_after_sha256=before['destination_sha256'],qualified_for_effects=False)


@pytest.mark.parametrize('kind,member',MEMBERS)
def test_every_omitted_writer_or_reader_gateway_prevents_completeness(case,kind,member):
    domain=enroll(case,missing=(kind,member));c=case['env'][1]
    assert c.execute('SELECT COUNT(*) FROM destination_model_events').fetchone()[0]==0
    with pytest.raises(plans.PlanConflict):gate.seal(c,domain)
    # Zero observed readers does not permit a fence even if supplied hashes agree.
    snapshot=gate._snapshot(gate._domain(c,domain)[0]);p=plan(case,snapshot)
    with pytest.raises(plans.PlanConflict):reserve(case,p,expected=snapshot)
    assert c.execute('SELECT COUNT(*) FROM destination_model_fences').fetchone()[0]==0


@pytest.mark.parametrize('domain',['cdn_bearer','external_nfs_handles','jellyfin_indexer','unlisted_admin_import','excluded_runtime_process'])
def test_unknown_reader_or_writer_blocks_even_empty_observations(case,domain):
    m=case['manifest'];m['unknown_domains']=[domain];enroll(case)
    assert case['env'][1].execute('SELECT COUNT(*) FROM destination_model_events').fetchone()[0]==0
    with pytest.raises(plans.PlanConflict):gate.seal(case['env'][1],m['domain_id'])


@pytest.mark.parametrize('field,value',[('mode','production'),('version',True),('readers',[]),('writers',[]),('domain_id','/media/library'),('gateway_sha256','bad'),('reader_generation',True),('writer_generation',-1),('readers',['x','x'])])
def test_fabricated_completeness_or_malformed_manifest_never_enrolls(case,field,value):
    m={**case['manifest'],field:value}
    with pytest.raises(plans.PlanConflict):gate.enroll(case['env'][1],m,digest(m))
    assert case['env'][1].execute('SELECT COUNT(*) FROM destination_model_domains').fetchone()[0]==0


def test_complete_boolean_gateway_mismatch_and_new_member_cannot_supply_authority(case):
    c=case['env'][1];m=case['manifest']
    with pytest.raises(plans.PlanConflict):gate.enroll(c,{**m,'complete':True},digest({**m,'complete':True}))
    gate.enroll(c,m,digest(m))
    with pytest.raises(plans.PlanConflict):gate.register(c,m['domain_id'],'writer',m['writers'][0],'0'*64)
    with pytest.raises(plans.PlanConflict):gate.register(c,m['domain_id'],'reader','unlisted_reader',m['gateway_sha256'])
    for kind,member in MEMBERS:gate.register(c,m['domain_id'],kind,member,m['gateway_sha256'])
    gate.seal(c,m['domain_id'])
    with pytest.raises(plans.PlanConflict):gate.register(c,m['domain_id'],'writer',m['writers'][0],m['gateway_sha256'])
    with pytest.raises(sqlite3.IntegrityError):c.execute('DELETE FROM destination_model_members')


@pytest.mark.parametrize('kind,member',MEMBERS)
def test_each_active_or_unknown_reader_writer_refuses_fence_without_expiry(case,kind,member):
    domain=closed(case);c=case['env'][1];before=gate.observe(c,domain)
    h=gate.enter(c,domain,kind,member,expected_generation=before[kind+'_generation']);p=plan(case)
    for unknown in [False,True]:
        if unknown:gate.finish(c,h,unknown=True)
        with pytest.raises(plans.PlanConflict):reserve(case,p)
        assert c.execute('SELECT COUNT(*) FROM destination_model_fences').fetchone()[0]==0
    with pytest.raises(plans.PlanConflict):gate.finish(c,h)
    with pytest.raises(plans.PlanConflict):gate.enter(c,domain,kind,member,expected_generation=gate.observe(c,domain)[kind+'_generation'])
    assert c.execute("SELECT COUNT(*) FROM destination_model_events WHERE state='unknown'").fetchone()[0]==1


@pytest.mark.parametrize('kind',['reader','writer'])
def test_explicit_completion_advances_generation_and_stale_plan_remains_refused(case,kind):
    domain=closed(case);c=case['env'][1];snapshot=gate.observe(c,domain);p=plan(case)
    h=gate.enter(c,domain,kind,case['manifest'][kind+'s'][0],expected_generation=snapshot[kind+'_generation'])
    gate.finish(c,h)
    assert gate.observe(c,domain)[kind+'_generation']==snapshot[kind+'_generation']+2
    with pytest.raises(plans.PlanConflict):gate.finish(c,h)
    with pytest.raises(plans.PlanConflict):reserve(case,p,expected=snapshot)
    with pytest.raises(plans.PlanConflict):reserve(case,p)


@pytest.mark.parametrize('field,value',[('destination_sha256','0'*64),('destination_revision',1),('reader_generation',8),('writer_generation',5),('fence_revision',1),('reader_generation',True)])
def test_exact_expected_preimage_revision_and_generation_mismatch_refused(case,field,value):
    domain=closed(case);c=case['env'][1];p=plan(case);before=gate.observe(c,domain)
    with pytest.raises(plans.PlanConflict):reserve(case,p,expected={**before,field:value})
    assert gate.observe(c,domain)==before
    assert c.execute('SELECT COUNT(*) FROM destination_model_fences').fetchone()[0]==0


def test_observed_preimage_bytes_are_required_not_just_caller_digest(case):
    domain=closed(case);c=case['env'][1];p=plan(case);before=gate.observe(c,domain)
    with pytest.raises(plans.PlanConflict):reserve(case,p,expected=before,observed_preimage='synthetic different bytes')
    assert gate.observe(c,domain)==before
    assert c.execute('SELECT COUNT(*) FROM destination_model_fences').fetchone()[0]==0


@pytest.mark.parametrize('field,value',[('preimage','synthetic foreign replacement'),('destination_revision',1),('reader_generation',8),('writer_generation',5)])
def test_changed_preimage_or_generations_after_reservation_refuse_check(case,field,value):
    domain=closed(case);c=case['env'][1];p=plan(case);h=reserve(case,p)
    # Explicit synthetic adversarial preimage, not a product effect or adapter.
    c.execute('UPDATE destination_model_domains SET '+field+'=? WHERE domain_id=?',(value,domain));case['input_tamper']=True
    before=tuple(c.execute('SELECT * FROM destination_model_domains').fetchone())
    with pytest.raises(plans.PlanConflict):gate.check(c,h,observed_preimage=case['manifest']['preimage'])
    assert tuple(c.execute('SELECT * FROM destination_model_domains').fetchone())==before
    assert c.execute('SELECT revision FROM destination_model_fences').fetchone()[0]==0


@pytest.mark.parametrize('field,value',[('worker_id','stale_worker'),('owner_id',999),('owner_id',True),('plan_generation',True),('plan_generation',1),('plan_sha256','0'*64),('fence_revision',0),('fence_revision',True),('revision',True),('binding_sha256','0'*64)])
def test_substituted_or_stale_fence_handle_cannot_check(case,field,value):
    closed(case);c=case['env'][1];p=plan(case);h=reserve(case,p)
    with pytest.raises(plans.PlanConflict):gate.check(c,{**h,field:value},observed_preimage=case['manifest']['preimage'])
    assert c.execute('SELECT revision FROM destination_model_fences').fetchone()[0]==0


@pytest.mark.parametrize('change',['disabled','denied','unknown_dispatch','dispatch_attempt','new_metadata_head'])
def test_live_owned_plan_source_and_metadata_generation_rechecked(case,change):
    closed(case);env=case['env'];db,c,uid,rid,fixture,_,_=env;p=plan(case);h=reserve(case,p)
    if change=='disabled':db.update_user(uid,enabled=0)
    elif change=='denied':db.update_user_request_status(rid,'denied')
    elif change=='unknown_dispatch':c.execute("UPDATE native_request_dispatch SET state='unknown',attempts=1 WHERE request_id=?",(rid,))
    elif change=='dispatch_attempt':c.execute('UPDATE native_request_dispatch SET attempts=1 WHERE request_id=?',(rid,))
    else:
        md=copy.deepcopy(fixture['metadata']['selected']);md['episodes'][0]['name']='Synthetic later metadata description'
        refresh.refresh(c,rid,uid,expected_generation=0,expected_plan_sha256=p['plan_sha256'],expected_revision=2,policy_version=1,metadata=md,metadata_sha256=digest(md),current_preconditions=p['plan']['preconditions'])
    with pytest.raises(plans.PlanConflict):gate.check(c,h,observed_preimage=case['manifest']['preimage'])
    assert c.execute('SELECT revision FROM destination_model_fences').fetchone()[0]==0


def test_interrupted_fence_has_no_reclaim_even_with_no_observed_readers(case):
    domain=closed(case);c=case['env'][1];p=plan(case);h=reserve(case,p)
    with pytest.raises(plans.PlanConflict):reserve(case,p,worker_id='replacement_worker')
    assert c.execute('SELECT COUNT(*) FROM destination_model_events').fetchone()[0]==0
    assert gate.observe(c,domain)['fence_revision']==1
    with pytest.raises(plans.EffectsUnqualified):gate.require_effect_authority(c,h)
    with pytest.raises(sqlite3.IntegrityError):c.execute('DELETE FROM destination_model_fences')
    with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE destination_model_fences SET worker_id='replacement'")


def test_nested_transaction_refused_without_rolling_back_caller(case):
    closed(case);c=case['env'][1];p=plan(case);c.execute('BEGIN IMMEDIATE')
    with pytest.raises(plans.PlanConflict):reserve(case,p)
    assert c.in_transaction;c.execute('ROLLBACK')


def test_two_processes_have_one_all_writer_fence_winner_zero_effects(case):
    domain=closed(case);env=case['env'];c=env[1];p=plan(case);tmp=env[6];go=tmp/'fence-go';children=[]
    script=r'''
import json,sqlite3,sys,time
from pathlib import Path
import destination_authority as gate
import request_execution_plans as plans
path,go,domain,rid,uid,digest,expected,preimage,worker=sys.argv[1:]
c=sqlite3.connect(path,isolation_level=None,timeout=5);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA busy_timeout=5000')
end=time.monotonic()+10
while not Path(go).exists():
 if time.monotonic()>end:raise RuntimeError('bounded start deadline')
 time.sleep(.01)
try:
 h=gate.reserve(c,domain,int(rid),int(uid),plan_generation=0,plan_sha256=digest,expected=json.loads(expected),observed_preimage=preimage,worker_id=worker)
 result=gate.check(c,h,observed_preimage=preimage)
 print(json.dumps({'outcome':'winner','result':result,'effects':0}))
except plans.PlanConflict:print(json.dumps({'outcome':'loser','effects':0}))
finally:c.close()
'''
    environment={k:v for k,v in os.environ.items() if k in ('PATH','HOME','TMPDIR')};environment['PYTHONDONTWRITEBYTECODE']='1'
    expected=gate.observe(c,domain)
    try:
        for worker in ('synthetic_fence_one','synthetic_fence_two'):
            children.append(subprocess.Popen([sys.executable,'-c',script,env[0].DB_PATH,str(go),domain,str(p['request_id']),str(p['owner_id']),p['plan_sha256'],json.dumps(expected),case['manifest']['preimage'],worker],cwd=Path(__file__).resolve().parents[1],env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
        go.write_text('start');outputs=[]
        for child in children:
            out,err=child.communicate(timeout=15);assert child.returncode==0,err[-1000:];outputs.append(json.loads(out))
    finally:
        for child in children:
            if child.poll() is None:child.terminate();child.wait(timeout=5)
    assert sorted(x['outcome'] for x in outputs)==['loser','winner']
    assert c.execute('SELECT COUNT(*) FROM destination_model_fences').fetchone()[0]==1
    assert c.execute('SELECT revision FROM destination_model_fences').fetchone()[0]==1
    assert c.execute('SELECT preimage FROM destination_model_domains').fetchone()[0]==case['manifest']['preimage']
    case['env'][5].update(fence_process_pids=[p.pid for p in children],fence_process_results=outputs,fence_winners=1,fence_losers=1,preimage_unchanged=True)


@pytest.mark.parametrize('kind',['reader','writer'])
def test_old_completion_cannot_erase_new_reader_or_writer_generation(case,kind):
    domain=closed(case);c=case['env'][1];member=case['manifest'][kind+'s'][0]
    first=gate.enter(c,domain,kind,member,expected_generation=gate.observe(c,domain)[kind+'_generation']);gate.finish(c,first)
    second=gate.enter(c,domain,kind,member,expected_generation=gate.observe(c,domain)[kind+'_generation'])
    p=plan(case)
    with pytest.raises(plans.PlanConflict):gate.finish(c,first)
    with pytest.raises(plans.PlanConflict):reserve(case,p)
    assert c.execute("SELECT COUNT(*) FROM destination_model_events WHERE state='active'").fetchone()[0]==1
    assert second['generation']>first['generation']


def test_cross_owner_and_other_destination_handles_are_not_interchangeable(case):
    domain=closed(case);env=case['env'];db,c=env[:2];p=plan(case);first=reserve(case,p)
    other=db.create_user('synthetic-fence-other','fixture$disabled')
    with pytest.raises(plans.PlanConflict):gate.reserve(c,domain,p['request_id'],other,plan_generation=0,plan_sha256=p['plan_sha256'],expected=gate.observe(c,domain),observed_preimage=case['manifest']['preimage'],worker_id='synthetic_fence_worker')
    m=copy.deepcopy(case['manifest']);m['domain_id']='synthetic_other_destination'
    gate.enroll(c,m,digest(m))
    for kind,member in MEMBERS:gate.register(c,m['domain_id'],kind,member,m['gateway_sha256'])
    gate.seal(c,m['domain_id'])
    second=gate.reserve(c,m['domain_id'],p['request_id'],p['owner_id'],plan_generation=0,plan_sha256=p['plan_sha256'],expected=gate.observe(c,m['domain_id']),observed_preimage=m['preimage'],worker_id='synthetic_fence_worker')
    assert first['binding_sha256']!=second['binding_sha256']
    with pytest.raises(plans.PlanConflict):gate.check(c,{**first,'domain_id':m['domain_id']},observed_preimage=m['preimage'])
    assert c.execute('SELECT SUM(revision) FROM destination_model_fences').fetchone()[0]==0


@pytest.mark.parametrize('kind',['reader','writer'])
def test_concurrent_entry_and_fence_cannot_both_win(case,kind):
    domain=closed(case);env=case['env'];c=env[1];p=plan(case);tmp=env[6];go=tmp/'entry-fence-go';children=[]
    script=r'''
import json,sqlite3,sys,time
from pathlib import Path
import destination_authority as gate
import request_execution_plans as plans
path,go,domain,rid,uid,digest,expected,preimage,kind,member,action=sys.argv[1:]
c=sqlite3.connect(path,isolation_level=None,timeout=5);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA busy_timeout=5000')
end=time.monotonic()+10
while not Path(go).exists():
 if time.monotonic()>end:raise RuntimeError('bounded start deadline')
 time.sleep(.01)
try:
 if action=='entry':gate.enter(c,domain,kind,member,expected_generation=json.loads(expected)[kind+'_generation'])
 else:gate.reserve(c,domain,int(rid),int(uid),plan_generation=0,plan_sha256=digest,expected=json.loads(expected),observed_preimage=preimage,worker_id='synthetic_race_fence')
 print(json.dumps({'action':action,'outcome':'winner','effects':0}))
except plans.PlanConflict:print(json.dumps({'action':action,'outcome':'loser','effects':0}))
finally:c.close()
'''
    environment={k:v for k,v in os.environ.items() if k in ('PATH','HOME','TMPDIR')};environment['PYTHONDONTWRITEBYTECODE']='1'
    expected=gate.observe(c,domain)
    try:
        for action in ('entry','fence'):
            children.append(subprocess.Popen([sys.executable,'-c',script,env[0].DB_PATH,str(go),domain,str(p['request_id']),str(p['owner_id']),p['plan_sha256'],json.dumps(expected),case['manifest']['preimage'],kind,case['manifest'][kind+'s'][0],action],cwd=Path(__file__).resolve().parents[1],env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
        go.write_text('start');outputs=[]
        for child in children:
            out,err=child.communicate(timeout=15);assert child.returncode==0,err[-1000:];outputs.append(json.loads(out))
    finally:
        for child in children:
            if child.poll() is None:child.terminate();child.wait(timeout=5)
    assert sorted(x['outcome'] for x in outputs)==['loser','winner']
    events=c.execute("SELECT COUNT(*) FROM destination_model_events WHERE state='active'").fetchone()[0]
    fences=c.execute('SELECT COUNT(*) FROM destination_model_fences').fetchone()[0]
    assert (events,fences) in [(1,0),(0,1)]
    env[5].update(admission_race_kind=kind,admission_race_process_pids=[p.pid for p in children],admission_race_results=outputs,admission_race_active_events=events,admission_race_fences=fences)


@pytest.mark.parametrize('operation',['delete','clear_unknown','rewrite_generation'])
def test_unknown_reader_history_requires_retained_completion_evidence(case,operation):
    domain=closed(case);c=case['env'][1]
    event=gate.enter(c,domain,'reader',case['manifest']['readers'][0],expected_generation=7)
    gate.finish(c,event,unknown=True);p=plan(case)
    sql={'delete':'DELETE FROM destination_model_events',
         'clear_unknown':"UPDATE destination_model_events SET state='closed'",
         'rewrite_generation':'UPDATE destination_model_events SET generation=generation+1'}[operation]
    before=[tuple(row) for row in c.execute('SELECT * FROM destination_model_events')]
    with pytest.raises(sqlite3.IntegrityError):c.execute(sql)
    assert [tuple(row) for row in c.execute('SELECT * FROM destination_model_events')]==before
    with pytest.raises(plans.PlanConflict):reserve(case,p)
    assert c.execute('SELECT COUNT(*) FROM destination_model_fences').fetchone()[0]==0
    case['env'][5].update(journal_guard_operation=operation,unknown_reader_retained=True,model_fence_refused=True)


def test_prior_inventory_versions_are_preserved_with_explicit_source_delta():
    from collections import Counter
    old_path=INVENTORY.with_name('destination-path-inventory-v1.json')
    old_model=FIXTURE.with_name('destination-fence-model-v1.json')
    assert hashlib.sha256(old_path.read_bytes()).hexdigest()=='a5b82372b0fbd603d17e04e9198651b33b281c7e2b1347bbf862de42141da87a'
    assert hashlib.sha256(old_model.read_bytes()).hexdigest()=='cb8912326af25eae735c12cfcf257cf91b1d4532364e405ddc70d30756ca228f'
    prior_path=INVENTORY.with_name('destination-path-inventory-v2.json')
    prior_model=FIXTURE.with_name('destination-fence-model-v2.json')
    assert hashlib.sha256(prior_path.read_bytes()).hexdigest()=='6fdcbc7e7573a7dacc4fd519e71f6b0448ed60df835157bf9d8539fd68c3f465'
    assert hashlib.sha256(prior_model.read_bytes()).hexdigest()=='057fef162e62e7332f145281a048da11c145474984dfe516210723f059c7d9c5'
    old=json.loads(old_path.read_text());prior=json.loads(prior_path.read_text())
    subtitle_path=INVENTORY.with_name('destination-path-inventory-v9-subtitle-completion.json')
    assert hashlib.sha256(subtitle_path.read_bytes()).hexdigest()=='762ba3b049f877d08263c95b637981e508a9ea6ee82b178dce03c97430b76c0e'
    current=json.loads(subtitle_path.read_text());source=Path(__file__).resolve().parents[1]
    assert {x['path'] for x in prior['reviewed_source_changes']}=={'app.py','mdblist.py'}
    assert prior['parent_inventory_sha256']==hashlib.sha256(old_path.read_bytes()).hexdigest()
    def calls(items):return Counter((x['path'],x['function'],x['call']) for x in items)
    assert calls(prior['static_sink_calls'])==calls(old['static_sink_calls'])
    integrated_path=INVENTORY.with_name('destination-path-inventory-v3.json')
    integrated_model=FIXTURE.with_name('destination-fence-model-v3.json')
    assert hashlib.sha256(integrated_path.read_bytes()).hexdigest()=='d4eedc6a1f47b94fda34b5bd223a3fe8947f09bc2c9977790fa8db475478cbe8'
    assert hashlib.sha256(integrated_model.read_bytes()).hexdigest()=='efdb993d9b8ca44a1b7cf6c8c1760456366281cc539a5f84a7072bd720310dbb'
    integrated=json.loads(integrated_path.read_text())
    prior_pins={x['path']:x['sha256'] for x in prior['audited_python_files']}
    integrated_pins={x['path']:x['sha256'] for x in integrated['audited_python_files']}
    assert integrated['parent_inventory_sha256']==hashlib.sha256(prior_path.read_bytes()).hexdigest()
    assert {x['path'] for x in integrated['reviewed_source_changes']}=={p for p in prior_pins if prior_pins[p]!=integrated_pins[p]}
    assert integrated['added_audited_paths']==['episode_scheme.py']
    assert not any(x['path']=='episode_scheme.py' for x in prior['audited_python_files'])
    assert any(x['path']=='episode_scheme.py' for g in current['writer_groups'] for x in g['source_files'])
    fixed_path=INVENTORY.with_name('destination-path-inventory-v4.json')
    fixed_model=FIXTURE.with_name('destination-fence-model-v4.json')
    assert hashlib.sha256(fixed_path.read_bytes()).hexdigest()=='edec12a611d7cea66b01550a05093047e4f2f764aee3d547f68ceac57318fb3d'
    assert hashlib.sha256(fixed_model.read_bytes()).hexdigest()=='2b8e5b1e0a15017f15043e19311e8de9d7900ac95a65514f2b4889e48a920c81'
    fixed=json.loads(fixed_path.read_text())
    fixed_pins={x['path']:x['sha256'] for x in fixed['audited_python_files']}
    assert {x['path'] for x in fixed['reviewed_source_changes']}=={'plugins/webplayer/routes.py','plugins/webplayer/web_player.py'}
    assert fixed['parent_inventory_sha256']==hashlib.sha256(integrated_path.read_bytes()).hexdigest()
    for item in fixed['reviewed_source_changes']:
        assert item['old_sha256']==integrated_pins[item['path']]
        assert item['new_sha256']==fixed_pins[item['path']]
    startup_path=INVENTORY.with_name('destination-path-inventory-v5.json')
    startup_model=FIXTURE.with_name('destination-fence-model-v5.json')
    assert hashlib.sha256(startup_path.read_bytes()).hexdigest()=='013de3c56d9f2f77ab67c375f3365e2f6b55b758809adf052ab27b5c4d5c3437'
    assert hashlib.sha256(startup_model.read_bytes()).hexdigest()=='dfbe2f56e8d2b60946b1699363cddfdb8cb5ebc0e9bcb05ff0296d828a921ae9'
    startup=json.loads(startup_path.read_text())
    startup_pins={x['path']:x['sha256'] for x in startup['audited_python_files']}
    assert {p for p in fixed_pins if fixed_pins[p]!=startup_pins[p]}=={'app.py'}
    assert startup['parent_inventory_sha256']==hashlib.sha256(fixed_path.read_bytes()).hexdigest()
    for item in startup['reviewed_source_changes']:
        assert item['old_sha256']==fixed_pins[item['path']]
        assert item['new_sha256']==startup_pins[item['path']]
    nfo_path=INVENTORY.with_name('destination-path-inventory-v6-nfo.json')
    nfo_model=FIXTURE.with_name('destination-fence-model-v6-nfo.json')
    assert hashlib.sha256(nfo_path.read_bytes()).hexdigest()=='4a153eb9d4cd6cd52c69c53ed4330827d9982fe711a41546bdfc7299bdebdda0'
    assert hashlib.sha256(nfo_model.read_bytes()).hexdigest()=='8824555d6d432e731a2725805258d03594635bd625eebdf8944c892e845ae807'
    nfo=json.loads(nfo_path.read_text())
    nfo_pins={x['path']:x['sha256'] for x in nfo['audited_python_files']}
    assert {p for p in startup_pins if startup_pins[p]!=nfo_pins[p]}=={'nfo_generator.py'}
    assert nfo['parent_inventory_sha256']==hashlib.sha256(startup_path.read_bytes()).hexdigest()
    for item in nfo['reviewed_source_changes']:
        assert item['old_sha256']==startup_pins[item['path']]
        assert item['new_sha256']==nfo_pins[item['path']]
    independent_size_path=INVENTORY.with_name('destination-path-inventory-v6-size.json')
    independent_size_model=FIXTURE.with_name('destination-fence-model-v6-size.json')
    assert hashlib.sha256(independent_size_path.read_bytes()).hexdigest()=='524adc6b27863e9633d4d75d593648bbc65b73e94d4f73769c592ff616b8de26'
    assert hashlib.sha256(independent_size_model.read_bytes()).hexdigest()=='4c89b55e291b9da3871a6060618e37dc0ab01d40891c5ba25635ff5267bb3a84'
    independent_size=json.loads(independent_size_path.read_text())
    independent_size_pins={x['path']:x['sha256'] for x in independent_size['audited_python_files']}
    assert independent_size['parent_inventory_sha256']==hashlib.sha256(startup_path.read_bytes()).hexdigest()
    assert {p for p in startup_pins if startup_pins[p]!=independent_size_pins[p]}=={'torrentio.py'}
    for item in independent_size['reviewed_source_changes']:
        assert item['old_sha256']==startup_pins[item['path']]
        assert item['new_sha256']==independent_size_pins[item['path']]
    integrated_nfo_size_path=INVENTORY.with_name('destination-path-inventory-v7-nfo-size.json')
    integrated_nfo_size_model=FIXTURE.with_name('destination-fence-model-v7-nfo-size.json')
    assert hashlib.sha256(integrated_nfo_size_path.read_bytes()).hexdigest()=='9f6edc0c075a6ff646d66ea198f08e8965113841de4bbeededb289e28474fc54'
    assert hashlib.sha256(integrated_nfo_size_model.read_bytes()).hexdigest()=='419d70a0a330c9f54bf71ac8e7631ee29260de0f46158be41261cd26d3cd51a7'
    integrated_nfo_size=json.loads(integrated_nfo_size_path.read_bytes())
    integrated_nfo_size_pins={x['path']:x['sha256'] for x in integrated_nfo_size['audited_python_files']}
    drift={p for p in nfo_pins if integrated_nfo_size_pins[p]!=nfo_pins[p]}
    assert drift=={'torrentio.py'}
    assert {x['path'] for x in integrated_nfo_size['reviewed_source_changes']}==drift
    assert integrated_nfo_size['parent_inventory_sha256']==hashlib.sha256(nfo_path.read_bytes()).hexdigest()
    for item in integrated_nfo_size['reviewed_source_changes']:
        assert item['old_sha256']==nfo_pins[item['path']]
        assert item['new_sha256']==integrated_nfo_size_pins[item['path']]
    player_path=INVENTORY.with_name('destination-path-inventory-v8-player-account.json')
    player_model=FIXTURE.with_name('destination-fence-model-v8-player-account.json')
    assert hashlib.sha256(player_path.read_bytes()).hexdigest()=='526c9577b544bb33c93a517d651775dd8367bab9842f05ef633b21b70591cd67'
    assert hashlib.sha256(player_model.read_bytes()).hexdigest()=='f33fc01a512ee5dd4a60e5c767ca1e05d9af9b000bf2f6082ac0547ab0cfb16e'
    player=json.loads(player_path.read_bytes())
    player_pins={x['path']:x['sha256'] for x in player['audited_python_files']}
    drift={p for p in integrated_nfo_size_pins if player_pins[p]!=integrated_nfo_size_pins[p]}
    assert drift=={'native_request_scopes.py','native_request_dispatch.py','plugins/webplayer/web_player.py'}
    assert player['parent_inventory_sha256']==hashlib.sha256(integrated_nfo_size_path.read_bytes()).hexdigest()
    assert {x['path'] for x in player['reviewed_source_changes']}==drift
    for item in player['reviewed_source_changes']:
        assert item['old_sha256']==integrated_nfo_size_pins[item['path']]
        assert item['new_sha256']==player_pins[item['path']]
    current_pins={x['path']:x['sha256'] for x in current['audited_python_files']}
    drift={p for p in player_pins if current_pins[p]!=player_pins[p]}
    assert drift=={'plugins/webplayer/web_player.py'}
    assert current['parent_inventory_sha256']==hashlib.sha256(player_path.read_bytes()).hexdigest()
    assert {x['path'] for x in current['reviewed_source_changes']}==drift
    for item in current['reviewed_source_changes']:
        assert item['old_sha256']==player_pins[item['path']]
        assert item['new_sha256']==hashlib.sha256((source/item['path']).read_bytes()).hexdigest()
    assert current['added_audited_paths']==[]
    assert current['runtime_completeness']=='UNPROVED' and not current['source_search_is_completeness_proof']


def test_dash_inventory_only_updates_wrapper_and_preserves_history():
    old_path=INVENTORY.with_name('destination-path-inventory-v9-subtitle-completion.json')
    old=json.loads(old_path.read_text());current=json.loads(INVENTORY.read_text())
    def pins(inventory):
        return {item['path']:item['sha256'] for group in inventory['writer_groups']+inventory['reader_domains'] for item in group['source_files']}
    before,after=pins(old),pins(current)
    assert before.keys()==after.keys()
    assert {p for p in before if before[p]!=after[p]}=={'spore/plex_transcoder_wrapper.sh'}
    assert current['parent_inventory_sha256']==hashlib.sha256(old_path.read_bytes()).hexdigest()
    assert current['reviewed_source_changes']==[{'path':'spore/plex_transcoder_wrapper.sh','old_sha256':before['spore/plex_transcoder_wrapper.sh'],'new_sha256':after['spore/plex_transcoder_wrapper.sh']}]
    for key in ['audited_python_files','static_sink_calls','omitted_runtime_domains','required_before_live_authority','runtime_completeness','source_search_is_completeness_proof']:
        assert current[key]==old[key]
