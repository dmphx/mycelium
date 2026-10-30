"""Actual owned fixture-file observations, no application filesystem effects."""
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
import destination_identity as identity
import destination_authority as gate
import request_execution_plans as plans
import request_plan_refresh as refresh
from test_request_execution_plans import env,digest
from test_destination_authority import case,closed,plan,reserve

FIXTURE=Path(__file__).parent/'fixtures/destination-identity-v1.json'
FIXTURE_SHA='adea6f6b4b6ca8eccb2fc1c012e91ca3ac082b4be314676d3171786d25e77433'
ROLES=('source','destination','rollback')


def tree_snapshot(root):
    result={}
    for p in sorted(root.rglob('*')):
        s=p.lstat();row={'device':s.st_dev,'inode':s.st_ino,'mode':s.st_mode,'links':s.st_nlink,'size':s.st_size,'mtime_ns':s.st_mtime_ns,'ctime_ns':s.st_ctime_ns}
        if p.is_symlink():row['link']=os.readlink(p)
        elif p.is_file():row['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
        result[str(p.relative_to(root))]=row
    return result


@contextmanager
def no_effects(c):
    before=tree_snapshot(c['root'])
    try:yield
    finally:
        assert tree_snapshot(c['root'])==before,'Application operation changed owned fixture filesystem'
        c['readonly_operations']+=1


@pytest.fixture()
def identity_case(case):
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest()==FIXTURE_SHA
    fixture=json.loads(FIXTURE.read_text());c=case['env'][1];identity.init_schema(c)
    closed(case);p=plan(case);fence=reserve(case,p);checked=gate.check(c,fence,observed_preimage=case['manifest']['preimage'])
    root=case['env'][6]/'owned_identity_tree';root.mkdir()
    for name,content in fixture['files'].items():
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content.encode())
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    state={'case':case,'conn':c,'root':root,'root_fd':fd,'manifest':copy.deepcopy(fixture['manifest']),
           'checked_fence':checked,'plan':p,'readonly_operations':0,'effects':0,'input_changes':[]}
    try:yield state
    finally:
        os.close(fd);assert state['effects']==0
        case['env'][5].update(identity_fixture_sha256=FIXTURE_SHA,identity_effects=0,
            identity_readonly_operations=state['readonly_operations'],identity_input_changes=state['input_changes'],
            identity_bindings=c.execute('SELECT COUNT(*) FROM destination_identity_bindings').fetchone()[0],
            real_namespace_authority=False,real_rollback_authority=False)


def role_path(c,role):
    m=c['manifest'];return c['root']/(m['source_candidates'][0]['path'] if role=='source' else m[role+'_path'])


def prepared(c,manifest=None):
    m=c['manifest'] if manifest is None else manifest
    with no_effects(c):return identity.prepare(c['conn'],c['checked_fence'],c['root_fd'],m,digest(m))


def claimed(c,p=None,**overrides):
    p=prepared(c) if p is None else p
    args=dict(binding_sha256=p['binding_sha256'],generation=1,expected_revision=0,worker_id='synthetic_identity_worker');args.update(overrides)
    with no_effects(c):return identity.claim(c['conn'],c['checked_fence'],c['root_fd'],**args)


def checked(c,h=None):
    h=claimed(c) if h is None else h
    with no_effects(c):return identity.check(c['conn'],c['checked_fence'],c['root_fd'],h)


def rollback(c,p,**overrides):
    args={'binding_sha256':p['binding_sha256'],'generation':1,
          'expected_forward_revision':1,'expected_forward_sha256':c['manifest']['source_sha256']};args.update(overrides)
    with no_effects(c):return identity.check_rollback_binding(c['conn'],c['checked_fence'],c['root_fd'],**args)


def test_real_descriptor_identity_and_planned_rollback_bind_without_effects(identity_case):
    c=identity_case;p=prepared(c);assert prepared(c)==p;h=claimed(c,p);done=checked(c,h)
    b=json.loads(c['conn'].execute('SELECT binding_json FROM destination_identity_bindings').fetchone()[0])
    assert b['source']==c['plan']['plan']['source']
    assert b['source']['scope']=={'kind':'selected_seasons','seasons':[0,2]}
    assert b['source']['body_sha256']==c['plan']['plan']['source']['body_sha256']
    assert len({(x['chain'][-1]['device'],x['chain'][-1]['inode']) for x in b['files'].values()})==3
    assert b['files']['destination']['sha256']==b['files']['rollback']['sha256']==c['manifest']['preimage_sha256']
    assert b['files']['source']['sha256']==c['manifest']['source_sha256']
    assert not done['qualified_for_effects'] and not done['qualified_for_rollback']
    proof=rollback(c,done);assert proof['rollback_binding_consistent']
    assert not proof['actual_forward_receipt_available'] and not proof['qualified_for_rollback']
    with pytest.raises(plans.EffectsUnqualified):
        identity.require_effect_authority(c['conn'],done)
        c['effects']+=1
    assert done['revision']==2
    c['case']['env'][5].update(identity_binding_sha256=done['binding_sha256'],actual_observation_root=b['files']['source']['root'],rollback_binding_consistent=True,actual_forward_receipt_available=False)


@pytest.mark.parametrize('kind',['empty','duplicate','different_path'])
def test_missing_or_multiple_source_candidates_refused(identity_case,kind):
    c=identity_case;m=copy.deepcopy(c['manifest'])
    if kind=='empty':m['source_candidates']=[]
    else:
        extra=copy.deepcopy(m['source_candidates'][0])
        if kind=='different_path':extra['path']='source/another.bin'
        m['source_candidates'].append(extra)
    with pytest.raises(plans.PlanConflict):prepared(c,m)
    assert c['conn'].execute('SELECT COUNT(*) FROM destination_identity_bindings').fetchone()[0]==0


@pytest.mark.parametrize('path',['../outside','source/../candidate.bin','/source/candidate.bin','source//candidate.bin','source/./candidate.bin','source/%2e%2e/candidate.bin',r'source\candidate.bin','source:candidate.bin','source/candidate.bin/','source/\x00candidate'])
def test_noncanonical_traversal_encoded_or_absolute_paths_refused(identity_case,path):
    c=identity_case;m=copy.deepcopy(c['manifest']);m['source_candidates'][0]['path']=path
    with pytest.raises(plans.PlanConflict):prepared(c,m)


@pytest.mark.parametrize('role',ROLES)
@pytest.mark.parametrize('kind',['leaf_symlink','ancestor_symlink','hardlink'])
def test_actual_symlink_components_and_hardlinks_refused(identity_case,role,kind):
    c=identity_case;p=role_path(c,role)
    if kind=='leaf_symlink':
        target=p.with_name(p.name+'.real');p.rename(target);p.symlink_to(target.name)
    elif kind=='ancestor_symlink':
        directory=p.parent;target=directory.with_name(directory.name+'.real');directory.rename(target);directory.symlink_to(target.name,target_is_directory=True)
    else:os.link(p,c['root']/(role+'_hardlink.bin'))
    c['input_changes'].append(role+':'+kind)
    with pytest.raises(plans.PlanConflict):prepared(c)
    assert c['conn'].execute('SELECT COUNT(*) FROM destination_identity_bindings').fetchone()[0]==0


@pytest.mark.parametrize('kind',['case_leaf','case_ancestor','unicode_input','unicode_siblings'])
def test_case_and_unicode_alias_ambiguity_not_normalized(identity_case,kind):
    c=identity_case;m=copy.deepcopy(c['manifest']);p=role_path(c,'source')
    if kind=='case_leaf':p.with_name('Candidate.bin').write_bytes(p.read_bytes())
    elif kind=='case_ancestor':
        d=c['root']/'Source';d.mkdir();(d/'candidate.bin').write_bytes(p.read_bytes())
    else:
        (p.parent/'café.bin').write_bytes(p.read_bytes())
        m['source_candidates'][0]['path']='source/cafe\u0301.bin' if kind=='unicode_input' else 'source/café.bin'
        if kind=='unicode_siblings':(p.parent/'cafe\u0301.bin').write_bytes(p.read_bytes())
    c['input_changes'].append(kind)
    with pytest.raises(plans.PlanConflict):prepared(c,m)


@pytest.mark.parametrize('role,other',[('rollback','destination'),('destination','source'),('source','destination')])
def test_cross_role_alias_is_not_an_independent_rollback_or_source(identity_case,role,other):
    c=identity_case;m=copy.deepcopy(c['manifest']);path=m['source_candidates'][0]['path'] if other=='source' else m[other+'_path']
    if role=='source':m['source_candidates'][0]['path']=path
    else:m[role+'_path']=path
    with pytest.raises(plans.PlanConflict):prepared(c,m)


@pytest.mark.parametrize('field,value',[('media_type','movie'),('tmdb_id',102),('tmdb_id',True),('imdb_id','tt2000999'),('season',1),('season',True),('episode',0),('tmdb_episode_id',12)])
def test_typed_source_episode_must_match_owned_immutable_plan(identity_case,field,value):
    c=identity_case;m=copy.deepcopy(c['manifest']);m['identity'][field]=value;m['source_candidates'][0]['identity']=copy.deepcopy(m['identity'])
    with pytest.raises(plans.PlanConflict):prepared(c,m)


def test_boolean_equivalent_candidate_identity_is_not_a_typed_match(identity_case):
    c=identity_case;m=copy.deepcopy(c['manifest']);m['source_candidates'][0]['identity']['season']=False
    with pytest.raises(plans.PlanConflict):prepared(c,m)


@pytest.mark.parametrize('field,value',[('version',True),('mode','production'),('source_sha256','0'*64),('preimage_sha256','0'*64),('expected_forward_revision',True),('expected_forward_revision',0),('expected_forward_revision',2)])
def test_source_preimage_and_planned_forward_revision_pins_are_exact(identity_case,field,value):
    c=identity_case;m={**c['manifest'],field:value}
    with pytest.raises(plans.PlanConflict):prepared(c,m)


@pytest.mark.parametrize('role',ROLES)
@pytest.mark.parametrize('phase',['claim','check','rollback'])
@pytest.mark.parametrize('change',['bytes','inode'])
def test_changed_source_target_or_backup_refuses_each_binding_boundary(identity_case,role,phase,change):
    c=identity_case;p=prepared(c)
    h=claimed(c,p) if phase in ('check','rollback') else None
    if phase=='rollback':checked(c,h)
    path=role_path(c,role);original=path.read_bytes()
    if change=='bytes':path.write_bytes(original+b' changed synthetic fixture input')
    else:
        path.rename(path.with_name(path.name+'.previous'));path.write_bytes(original)
    c['input_changes'].append(role+':'+change+':'+phase)
    with pytest.raises(plans.PlanConflict):
        if phase=='claim':claimed(c,p)
        elif phase=='check':checked(c,h)
        else:rollback(c,p)
    expected={'claim':0,'check':1,'rollback':2}[phase]
    assert c['conn'].execute('SELECT revision FROM destination_identity_bindings').fetchone()[0]==expected


def test_changed_ancestor_or_root_descriptor_is_not_same_namespace(identity_case):
    c=identity_case;p=prepared(c);directory=c['root']/'source';directory.rename(c['root']/'previous_source');directory.mkdir()
    (directory/'candidate.bin').write_bytes((c['root']/'previous_source/candidate.bin').read_bytes())
    c['input_changes'].append('changed source ancestor')
    with pytest.raises(plans.PlanConflict):claimed(c,p)
    other=c['case']['env'][6]/'other_root';other.mkdir()
    for role in ROLES:
        original=role_path(c,role);dest=other/original.relative_to(c['root']);dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(original.read_bytes())
    fd=os.open(other,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        with no_effects(c),pytest.raises(plans.PlanConflict):identity.claim(c['conn'],c['checked_fence'],fd,binding_sha256=p['binding_sha256'],generation=1,expected_revision=0,worker_id='synthetic')
    finally:os.close(fd)


@pytest.mark.parametrize('field,value',[('generation',0),('generation',2),('generation',True),('expected_revision',True),('expected_revision',1),('binding_sha256','0'*64),('worker_id',True)])
def test_claim_generation_hash_revision_and_worker_are_strict(identity_case,field,value):
    c=identity_case;p=prepared(c)
    with pytest.raises(plans.PlanConflict):claimed(c,p,**{field:value})
    assert c['conn'].execute('SELECT revision FROM destination_identity_bindings').fetchone()[0]==0


@pytest.mark.parametrize('field,value',[('generation',0),('generation',True),('revision',True),('binding_sha256','0'*64),('worker_id',True),('worker_id','stale'),('domain_id','synthetic_other')])
def test_checked_claim_cannot_substitute_binding_or_generation(identity_case,field,value):
    c=identity_case;h=claimed(c)
    with no_effects(c),pytest.raises(plans.PlanConflict):identity.check(c['conn'],c['checked_fence'],c['root_fd'],{**h,field:value})
    assert c['conn'].execute('SELECT revision FROM destination_identity_bindings').fetchone()[0]==1


@pytest.mark.parametrize('field,value',[('generation',True),('generation',2),('expected_forward_revision',True),('expected_forward_revision',0),('expected_forward_revision',2),('expected_forward_sha256','0'*64)])
def test_rollback_cannot_use_stale_generation_or_foreign_postimage(identity_case,field,value):
    c=identity_case;p=checked(c)
    with pytest.raises(plans.PlanConflict):rollback(c,p,**{field:value})
    assert c['conn'].execute('SELECT revision FROM destination_identity_bindings').fetchone()[0]==2


@pytest.mark.parametrize('change',['disabled','denied','unknown','reader_generation','writer_generation','fence_generation','new_metadata_head'])
def test_current_owner_plan_and_reader_writer_fence_rechecked(identity_case,change):
    c=identity_case;p=prepared(c);env=c['case']['env'];db,conn,uid,rid,fixture,_,_=env
    if change=='disabled':db.update_user(uid,enabled=0)
    elif change=='denied':db.update_user_request_status(rid,'denied')
    elif change=='unknown':conn.execute("UPDATE native_request_dispatch SET state='unknown',attempts=1 WHERE request_id=?",(rid,))
    elif change in ('reader_generation','writer_generation'):conn.execute('UPDATE destination_model_domains SET '+change+'='+change+'+1')
    elif change=='fence_generation':conn.execute('UPDATE destination_model_domains SET fence_revision=fence_revision+1')
    else:
        md=copy.deepcopy(fixture['metadata']['selected']);md['episodes'][0]['name']='Synthetic later metadata'
        refresh.refresh(conn,rid,uid,expected_generation=0,expected_plan_sha256=c['plan']['plan_sha256'],expected_revision=2,policy_version=1,metadata=md,metadata_sha256=digest(md),current_preconditions=c['plan']['plan']['preconditions'])
    with pytest.raises(plans.PlanConflict):claimed(c,p)
    assert conn.execute('SELECT revision FROM destination_identity_bindings').fetchone()[0]==0


def test_observation_rejects_file_that_changes_during_bounded_read(identity_case,monkeypatch):
    c=identity_case;path=role_path(c,'source');real_read=os.read;changed=False
    def changing_read(fd,count):
        nonlocal changed
        data=real_read(fd,count)
        if data and not changed:
            changed=True;path.write_bytes(path.read_bytes()+b' synthetic concurrent input mutation')
        return data
    monkeypatch.setattr(identity.os,'read',changing_read)
    # This deliberately mutates the fixture input, so use explicit postconditions
    # instead of claiming no setup/adversary write occurred during observation.
    with pytest.raises(plans.PlanConflict):identity.observe_file(c['root_fd'],'source/candidate.bin')
    assert changed and c['conn'].execute('SELECT COUNT(*) FROM destination_identity_bindings').fetchone()[0]==0
    c['input_changes'].append('declared concurrent source mutation during read')


def test_binding_history_and_claim_revision_cannot_be_erased_or_reclaimed(identity_case):
    c=identity_case;p=prepared(c);h=claimed(c,p)
    with pytest.raises(plans.PlanConflict):claimed(c,p,worker_id='replacement')
    with pytest.raises(plans.PlanConflict):rollback(c,p)
    checked(c,h)
    for sql in ["UPDATE destination_identity_bindings SET binding_json='{}'",'DELETE FROM destination_identity_bindings',"UPDATE destination_identity_bindings SET revision=0,state='prepared',worker_id=NULL"]:
        with pytest.raises(sqlite3.IntegrityError):c['conn'].execute(sql)
    with pytest.raises(plans.PlanConflict):checked(c,h)
    assert prepared(c)['revision']==2


def test_two_processes_have_one_identity_claim_winner_and_no_effects(identity_case):
    c=identity_case;p=prepared(c);env=c['case']['env'];go=env[6]/'identity-go';children=[]
    script=r'''
import json,os,sqlite3,sys,time
from pathlib import Path
import destination_identity as identity
import request_execution_plans as plans
path,go,root,parent,digest,worker=sys.argv[1:]
conn=sqlite3.connect(path,isolation_level=None,timeout=5);conn.row_factory=sqlite3.Row;conn.execute('PRAGMA foreign_keys=ON');conn.execute('PRAGMA busy_timeout=5000')
fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);parent=json.loads(parent);end=time.monotonic()+10
while not Path(go).exists():
 if time.monotonic()>end:raise RuntimeError('bounded start deadline')
 time.sleep(.01)
try:
 h=identity.claim(conn,parent,fd,binding_sha256=digest,generation=1,expected_revision=0,worker_id=worker)
 checked=identity.check(conn,parent,fd,h)
 print(json.dumps({'outcome':'winner','result':checked,'effects':0}))
except plans.PlanConflict:print(json.dumps({'outcome':'loser','effects':0}))
finally:os.close(fd);conn.close()
'''
    environment={k:v for k,v in os.environ.items() if k in ('PATH','HOME','TMPDIR')};environment['PYTHONDONTWRITEBYTECODE']='1'
    with no_effects(c):
        try:
            for worker in ('synthetic_identity_one','synthetic_identity_two'):
                children.append(subprocess.Popen([sys.executable,'-c',script,env[0].DB_PATH,str(go),str(c['root']),json.dumps(c['checked_fence']),p['binding_sha256'],worker],cwd=Path(__file__).resolve().parents[1],env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
            go.write_text('start');outputs=[]
            for child in children:
                out,err=child.communicate(timeout=15);assert child.returncode==0,err[-1000:];outputs.append(json.loads(out))
        finally:
            for child in children:
                if child.poll() is None:child.terminate();child.wait(timeout=5)
    assert sorted(x['outcome'] for x in outputs)==['loser','winner']
    assert c['conn'].execute('SELECT revision FROM destination_identity_bindings').fetchone()[0]==2
    env[5].update(identity_process_pids=[p.pid for p in children],identity_process_results=outputs,identity_winners=1,identity_losers=1)


def test_fifo_candidate_is_rejected_without_blocking_or_reading(identity_case):
    c=identity_case;path=role_path(c,'source');path.rename(path.with_name(path.name+'.original'));os.mkfifo(path)
    c['input_changes'].append('own synthetic FIFO input')
    with pytest.raises(plans.PlanConflict):prepared(c)
    assert c['conn'].execute('SELECT COUNT(*) FROM destination_identity_bindings').fetchone()[0]==0


@pytest.mark.parametrize('fd',[True,-1])
def test_invalid_directory_descriptor_is_a_refusal(identity_case,fd):
    c=identity_case
    with no_effects(c),pytest.raises(plans.PlanConflict):identity.observe_file(fd,'source/candidate.bin')


@pytest.mark.parametrize('field,value',[('qualified_for_effects',True),('model_coverage_checked',False),('fence_revision',0),('plan_generation',1),('owner_id',True)])
def test_checked_fence_marker_cannot_substitute_actual_authority(identity_case,field,value):
    c=identity_case;m=c['manifest'];parent={**c['checked_fence'],field:value}
    with no_effects(c),pytest.raises(plans.PlanConflict):identity.prepare(c['conn'],parent,c['root_fd'],m,digest(m))
    assert c['conn'].execute('SELECT COUNT(*) FROM destination_identity_bindings').fetchone()[0]==0
