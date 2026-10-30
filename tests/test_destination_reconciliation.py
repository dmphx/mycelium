"""Fresh owned SQLite and read-only application observations, no live effects."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
import destination_reconciliation as journal
import request_execution_plans as plans
import request_plan_refresh as refresh
from test_destination_identity import identity_case,prepared,claimed,checked,no_effects,role_path
from test_destination_authority import case
from test_request_execution_plans import env,digest


@pytest.fixture()
def attempt(identity_case):
    c=identity_case;journal.init_schema(c['conn']);c['identity']=checked(c)
    with no_effects(c):c['attempt']=journal.enroll(c['conn'],c['checked_fence'],c['root_fd'],c['identity'])
    try:yield c
    finally:
        status=journal.stored_status(c['conn'],c['attempt']['domain_id'])
        assert not status['actual_forward_receipt_available'] and not status['qualified_for_effects'] and not status['qualified_for_rollback']
        c['case']['env'][5].update(reconciliation_state=status['state'],reconciliation_effects=0,
            reconciliation_event_sha256=status['event_sha256'],reconciliation_actual_forward_receipt=False,
            reconciliation_events=c['conn'].execute('SELECT COUNT(*) FROM destination_reconciliation_events').fetchone()[0])


def forward(c,h=None):
    with no_effects(c):return journal.stage_forward(c['conn'],c['root_fd'],c['attempt'] if h is None else h)


def inverse(c,h,**overrides):
    args={'forward_receipt_sha256':h['event_sha256'],'expected_forward_revision':1};args.update(overrides)
    with no_effects(c):return journal.stage_inverse(c['conn'],c['root_fd'],h,**args)


def validate(c,h):
    with no_effects(c):return journal.revalidate(c['conn'],c['root_fd'],h)


def test_staged_forward_inverse_preserve_original_files_and_hash_linked_generations(attempt):
    c=attempt;h=forward(c);assert validate(c,h)==h
    first=journal.stored_status(c['conn'],h['domain_id']);back=inverse(c,h)
    last=journal.stored_status(c['conn'],h['domain_id'])
    assert first['state']=='forward_staged' and first['detail']['from_revision']==0 and first['detail']['to_revision']==1
    assert last['state']=='inverse_staged' and last['detail']['from_revision']==1 and last['detail']['to_revision']==2
    assert last['detail']['forward_staged_receipt_sha256']==h['event_sha256']
    assert last['detail']['from_sha256']==first['detail']['to_sha256']
    assert last['detail']['to_sha256']==first['detail']['from_sha256']==first['detail']['rollback_sha256']
    assert last['previous_sha256']==h['event_sha256'] and back['revision']==2
    assert role_path(c,'destination').read_bytes()==role_path(c,'rollback').read_bytes()
    assert not last['current_authority_checked']
    with pytest.raises(plans.EffectsUnqualified):journal.require_effect_authority(c['conn'],back)
    with pytest.raises(plans.PlanConflict):inverse(c,back)
    c['case']['env'][5]['staged_inverse_generation_bound']=True


@pytest.mark.parametrize('phase',['prepared','forward_staged'])
@pytest.mark.parametrize('reason',['forward_outcome_unknown','rollback_outcome_unknown','preimage_unavailable'])
def test_uncertainty_is_retained_terminal_never_retried_or_inferred_from_matching_bytes(attempt,phase,reason):
    c=attempt;h=c['attempt'] if phase=='prepared' else forward(c)
    with no_effects(c):unknown=journal.report_unknown(c['conn'],h,reason=reason)
    # Destination and backup still match, but that is not a forward outcome receipt.
    for op in (lambda:forward(c,unknown),lambda:inverse(c,unknown),lambda:validate(c,unknown),
               lambda:journal.report_unknown(c['conn'],unknown,reason=reason)):
        with pytest.raises(plans.PlanConflict):op()
    assert journal.stored_status(c['conn'],h['domain_id'])['state']=='unknown'
    assert role_path(c,'destination').read_bytes()==role_path(c,'rollback').read_bytes()
    with pytest.raises(sqlite3.IntegrityError):journal.enroll(c['conn'],c['checked_fence'],c['root_fd'],c['identity'])


@pytest.mark.parametrize('phase',['prepared','forward_staged'])
@pytest.mark.parametrize('change',['disabled','dispatch_unknown','reader_generation','writer_generation','fence_generation'])
def test_observed_authority_loss_commits_invalidation_before_error_and_aba_restoration(attempt,phase,change):
    c=attempt;h=c['attempt'] if phase=='prepared' else forward(c);conn=c['conn'];db,_,uid,rid,_,_,_=c['case']['env']
    if change=='disabled':
        db.update_user(uid,enabled=0);restore=lambda:db.update_user(uid,enabled=1)
    elif change=='dispatch_unknown':
        old=dict(conn.execute('SELECT * FROM native_request_dispatch WHERE request_id=?',(rid,)).fetchone())
        conn.execute("UPDATE native_request_dispatch SET state='unknown',attempts=1 WHERE request_id=?",(rid,))
        restore=lambda:conn.execute('UPDATE native_request_dispatch SET state=?,attempts=? WHERE request_id=?',(old['state'],old['attempts'],rid))
    else:
        key={'reader_generation':'reader_generation','writer_generation':'writer_generation','fence_generation':'fence_revision'}[change]
        conn.execute('UPDATE destination_model_domains SET '+key+'='+key+'+1')
        restore=lambda:conn.execute('UPDATE destination_model_domains SET '+key+'='+key+'-1')
    with pytest.raises(plans.PlanConflict,match='durably invalidated'):validate(c,h)
    assert not conn.in_transaction
    status=journal.stored_status(conn,h['domain_id']);assert status['state']=='invalidated'
    restore() # Declared synthetic adversary restoration, not a permitted runtime repair.
    c['input_changes'].append('synthetic authority loss then ABA restoration:'+change)
    for handle in [h,{'domain_id':h['domain_id'],'revision':status['revision'],'event_sha256':status['event_sha256']}]:
        with pytest.raises(plans.PlanConflict):forward(c,handle)
        with pytest.raises(plans.PlanConflict):validate(c,handle)
    c['case']['env'][5]['durable_aba_invalidation']=True


@pytest.mark.parametrize('role',['source','destination','rollback'])
@pytest.mark.parametrize('change',['bytes','inode'])
def test_preimage_source_or_backup_uncertainty_never_creates_forward_or_inverse_receipt(attempt,role,change):
    c=attempt;h=forward(c);p=role_path(c,role);old=p.read_bytes()
    if change=='bytes':p.write_bytes(old+b' synthetic changed input')
    else:p.rename(p.with_name(p.name+'.prior'));p.write_bytes(old)
    c['input_changes'].append('declared changed '+role+':'+change)
    with pytest.raises(plans.PlanConflict,match='durably invalidated'):inverse(c,h)
    status=journal.stored_status(c['conn'],h['domain_id']);assert status['state']=='invalidated'
    assert c['conn'].execute("SELECT COUNT(*) FROM destination_reconciliation_events WHERE state='inverse_staged'").fetchone()[0]==0


def test_destination_matching_planned_postimage_is_not_proof_forward_happened(attempt):
    c=attempt;h=forward(c);role_path(c,'destination').write_bytes(role_path(c,'source').read_bytes())
    c['input_changes'].append('synthetic destination matches planned postimage without receipt')
    with pytest.raises(plans.PlanConflict,match='durably invalidated'):inverse(c,h)
    assert journal.stored_status(c['conn'],h['domain_id'])['state']=='invalidated'


def test_new_metadata_generation_invalidates_old_staged_inverse(attempt):
    c=attempt;h=forward(c);db,conn,uid,rid,f,_,_=c['case']['env'];md=copy.deepcopy(f['metadata']['selected'])
    md['episodes'][0]['name']='New synthetic metadata generation'
    refresh.refresh(conn,rid,uid,expected_generation=0,expected_plan_sha256=c['plan']['plan_sha256'],expected_revision=2,
                    policy_version=1,metadata=md,metadata_sha256=digest(md),current_preconditions=c['plan']['plan']['preconditions'])
    with pytest.raises(plans.PlanConflict,match='durably invalidated'):inverse(c,h)
    assert journal.stored_status(conn,h['domain_id'])['state']=='invalidated'


@pytest.mark.parametrize('overrides',[{'forward_receipt_sha256':'0'*64},{'expected_forward_revision':0},
    {'expected_forward_revision':2},{'expected_forward_revision':True}])
def test_inverse_requires_exact_staged_forward_receipt_and_strict_generation(attempt,overrides):
    c=attempt;h=forward(c)
    with pytest.raises(plans.PlanConflict):inverse(c,h,**overrides)
    assert journal.stored_status(c['conn'],h['domain_id'])['state']=='forward_staged'
    inverse(c,h)


@pytest.mark.parametrize('field,value',[('revision',True),('revision',-1),('revision',1),('event_sha256','0'*64),('domain_id','other')])
def test_stale_or_forged_handle_has_no_transition_or_quarantine(attempt,field,value):
    c=attempt
    with pytest.raises(plans.PlanConflict):forward(c,{**c['attempt'],field:value})
    assert journal.stored_status(c['conn'],c['attempt']['domain_id'])['state']=='prepared'
    forward(c)


def test_crash_reopen_keeps_unknown_and_original_receipt(attempt):
    c=attempt;h=forward(c);terminal=journal.report_unknown(c['conn'],h,reason='forward_outcome_unknown')
    conn=sqlite3.connect(c['case']['env'][0].DB_PATH,isolation_level=None);conn.row_factory=sqlite3.Row
    try:
        status=journal.stored_status(conn,h['domain_id']);assert status['event_sha256']==terminal['event_sha256']
        with pytest.raises(plans.PlanConflict):journal.stage_forward(conn,c['root_fd'],terminal)
    finally:conn.close()


def test_sql_cannot_erase_unknown_reclaim_revision_or_bypass_inverse_generation_chain(attempt):
    c=attempt;h=forward(c);journal.report_unknown(c['conn'],h,reason='rollback_outcome_unknown')
    for sql in ['DELETE FROM destination_reconciliation_events','DELETE FROM destination_reconciliation_bindings',
                "UPDATE destination_reconciliation_events SET state='prepared'", "UPDATE destination_reconciliation_bindings SET binding_json='{}'",
                "INSERT INTO destination_reconciliation_events SELECT domain_id,revision+1,'forward_staged',event_json,event_sha256,event_sha256 FROM destination_reconciliation_events WHERE state='unknown'"]:
        with pytest.raises(sqlite3.IntegrityError):c['conn'].execute(sql)
    assert journal.stored_status(c['conn'],h['domain_id'])['state']=='unknown'


def test_two_processes_stage_once_without_effect_and_loser_receipt_cannot_advance(attempt):
    c=attempt;env=c['case']['env'];go=env[6]/'reconciliation-go';children=[]
    script=r"""
import json,os,sqlite3,sys,time
from pathlib import Path
import destination_reconciliation as journal
import request_execution_plans as plans
path,root,handle,go=sys.argv[1:];conn=sqlite3.connect(path,isolation_level=None,timeout=5);conn.row_factory=sqlite3.Row
conn.execute('PRAGMA foreign_keys=ON');fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);deadline=time.monotonic()+10
while not Path(go).exists():
 if time.monotonic()>deadline:raise RuntimeError('bounded start wait')
 time.sleep(.01)
try:print(json.dumps({'outcome':'winner','receipt':journal.stage_forward(conn,fd,json.loads(handle)),'effects':0}))
except plans.PlanConflict:print(json.dumps({'outcome':'loser','effects':0}))
finally:os.close(fd);conn.close()
"""
    environment={k:v for k,v in os.environ.items() if k in ('PATH','HOME','TMPDIR')};environment['PYTHONDONTWRITEBYTECODE']='1'
    with no_effects(c):
        try:
            for _ in range(2):children.append(subprocess.Popen([sys.executable,'-c',script,env[0].DB_PATH,str(c['root']),json.dumps(c['attempt']),str(go)],
                cwd=Path(__file__).resolve().parents[1],env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
            go.write_text('start');outputs=[]
            for child in children:
                out,err=child.communicate(timeout=15);assert child.returncode==0,err[-1000:];outputs.append(json.loads(out))
        finally:
            for child in children:
                if child.poll() is None:child.terminate();child.wait(timeout=5)
    assert sorted(x['outcome'] for x in outputs)==['loser','winner']
    assert c['conn'].execute('SELECT COUNT(*) FROM destination_reconciliation_events').fetchone()[0]==2
    with pytest.raises(plans.PlanConflict):forward(c)
    winner=next(x['receipt'] for x in outputs if x['outcome']=='winner');inverse(c,winner)
    env[5].update(reconciliation_process_pids=[p.pid for p in children],reconciliation_process_results=outputs)
