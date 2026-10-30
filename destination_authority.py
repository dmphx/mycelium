"""Offline destination/reader/all-writer fence model, never runtime authority.

Only SQLite model state is written. No filesystem destination, callback, provider,
clock-based reader inference, route or shared writer is enrolled by this module.
"""
import json
import re

import request_execution_plans as plans
import request_plan_refresh as refresh

_MODE = 'isolated_model_no_external_entrypoints'
_ID = re.compile(r'[a-z][a-z0-9_]{0,63}')
_MANIFEST = {'version','mode','domain_id','source_inventory_sha256','writers','readers',
             'gateway_sha256','unknown_domains','preimage','reader_generation','writer_generation'}
_SNAPSHOT = {'destination_sha256','destination_revision','reader_generation','writer_generation','fence_revision'}
_HANDLE = {'domain_id','request_id','owner_id','plan_generation','plan_sha256','fence_revision','revision','worker_id','binding_sha256'}
_EVENT = {'domain_id','kind','member_id','generation'}
_MAX = 2**63-2


def init_schema(conn):
    """Explicit owned fixture schema only; no normal db.init or migration hook."""
    for statement in (
        """CREATE TABLE IF NOT EXISTS destination_model_domains (
          domain_id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
          phase TEXT NOT NULL CHECK(phase IN ('enrolling','open','fenced','checked')),
          closure_sha256 TEXT, preimage TEXT NOT NULL, destination_revision INTEGER NOT NULL DEFAULT 0,
          reader_generation INTEGER NOT NULL, writer_generation INTEGER NOT NULL,
          fence_revision INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS destination_model_members (
          domain_id TEXT NOT NULL REFERENCES destination_model_domains(domain_id),
          kind TEXT NOT NULL CHECK(kind IN ('writer','reader')), member_id TEXT NOT NULL,
          gateway_sha256 TEXT NOT NULL, PRIMARY KEY(domain_id,kind,member_id)
        )""",
        """CREATE TABLE IF NOT EXISTS destination_model_events (
          domain_id TEXT NOT NULL REFERENCES destination_model_domains(domain_id),
          kind TEXT NOT NULL, member_id TEXT NOT NULL, generation INTEGER NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('active','unknown','closed')),
          PRIMARY KEY(domain_id,kind,generation),
          FOREIGN KEY(domain_id,kind,member_id) REFERENCES destination_model_members(domain_id,kind,member_id)
        )""",
        """CREATE TABLE IF NOT EXISTS destination_model_fences (
          domain_id TEXT PRIMARY KEY REFERENCES destination_model_domains(domain_id),
          binding_json TEXT NOT NULL, binding_sha256 TEXT NOT NULL, worker_id TEXT NOT NULL,
          fence_revision INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
          state TEXT NOT NULL DEFAULT 'reserved' CHECK(state IN ('reserved','checked'))
        )""",
        """CREATE TRIGGER IF NOT EXISTS destination_model_manifest_immutable
          BEFORE UPDATE OF domain_id,manifest_json,manifest_sha256 ON destination_model_domains
          BEGIN SELECT RAISE(ABORT,'immutable model domain'); END""",
        """CREATE TRIGGER IF NOT EXISTS destination_model_member_closed
          BEFORE INSERT ON destination_model_members
          WHEN (SELECT phase FROM destination_model_domains WHERE domain_id=NEW.domain_id)!='enrolling'
          BEGIN SELECT RAISE(ABORT,'closed model membership'); END""",
        """CREATE TRIGGER IF NOT EXISTS destination_model_member_immutable
          BEFORE UPDATE ON destination_model_members
          BEGIN SELECT RAISE(ABORT,'immutable gateway membership'); END""",
        """CREATE TRIGGER IF NOT EXISTS destination_model_member_retained
          BEFORE DELETE ON destination_model_members
          BEGIN SELECT RAISE(ABORT,'retained gateway membership'); END""",
        """CREATE TRIGGER IF NOT EXISTS destination_model_event_identity
          BEFORE UPDATE OF domain_id,kind,member_id,generation ON destination_model_events
          BEGIN SELECT RAISE(ABORT,'immutable participant event identity'); END""",
        """CREATE TRIGGER IF NOT EXISTS destination_model_event_retained
          BEFORE DELETE ON destination_model_events
          BEGIN SELECT RAISE(ABORT,'retained participant completion evidence'); END""",
        """CREATE TRIGGER IF NOT EXISTS destination_model_event_transition
          BEFORE UPDATE OF state ON destination_model_events
          WHEN NOT (OLD.state='active' AND NEW.state IN ('closed','unknown'))
          BEGIN SELECT RAISE(ABORT,'unknown or completed participant cannot be rewritten'); END""",
        """CREATE TRIGGER IF NOT EXISTS destination_model_fence_immutable
          BEFORE UPDATE OF domain_id,binding_json,binding_sha256,worker_id,fence_revision ON destination_model_fences
          BEGIN SELECT RAISE(ABORT,'immutable model fence'); END""",
        """CREATE TRIGGER IF NOT EXISTS destination_model_fence_retained
          BEFORE DELETE ON destination_model_fences
          BEGIN SELECT RAISE(ABORT,'retained model fence'); END""",
        """CREATE TRIGGER IF NOT EXISTS destination_model_fence_CAS
          BEFORE UPDATE OF revision,state ON destination_model_fences
          WHEN NOT (OLD.revision=0 AND OLD.state='reserved' AND NEW.revision=1 AND NEW.state='checked')
          BEGIN SELECT RAISE(ABORT,'model fence revision CAS'); END""",
    ):
        conn.execute(statement)


def _number(value):
    if type(value) is not int or not 0 <= value <= _MAX:
        raise plans.PlanConflict('invalid model generation')
    return value


def _id(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise plans.PlanConflict('invalid model identity')
    return value


def _preimage(value):
    if type(value) is not str or len(value.encode())>65536:
        raise plans.PlanConflict('bounded model preimage required')
    return plans._sha(value)


def enroll(conn, manifest, manifest_sha256):
    """Pin a closed synthetic model declaration, not a live completeness proof."""
    plans._hash(manifest_sha256)
    if type(manifest) is not dict or set(manifest)!=_MANIFEST:
        raise plans.PlanConflict('exact synthetic model manifest required')
    if type(manifest['version']) is not int or manifest['version']!=1 or manifest['mode']!=_MODE:
        raise plans.PlanConflict('live or unsupported authority cannot be enrolled')
    _id(manifest['domain_id'])
    if not manifest['domain_id'].startswith('synthetic_'):
        raise plans.PlanConflict('synthetic model domain required')
    for field in ('writers','readers'):
        members=manifest[field]
        if type(members) is not list or not 1<=len(members)<=128:
            raise plans.PlanConflict('explicit nonempty participant set required')
        for member in members:_id(member)
        if members!=sorted(set(members)):raise plans.PlanConflict('unique sorted participant set required')
    if (type(manifest['unknown_domains']) is not list or len(manifest['unknown_domains'])>128
        or any(type(x) is not str or not x or len(x)>200 for x in manifest['unknown_domains'])):
        raise plans.PlanConflict('invalid unknown authority domains')
    for field in ('gateway_sha256','source_inventory_sha256'):plans._hash(manifest[field])
    for field in ('reader_generation','writer_generation'):_number(manifest[field])
    _preimage(manifest['preimage']);text=plans.scopes.canonical(manifest)
    if plans._sha(text)!=manifest_sha256:raise plans.PlanConflict('model manifest pin changed')
    with plans._tx(conn):
        if conn.execute('SELECT 1 FROM destination_model_domains WHERE domain_id=?',(manifest['domain_id'],)).fetchone():
            raise plans.PlanConflict('model already enrolled')
        conn.execute("INSERT INTO destination_model_domains(domain_id,manifest_json,manifest_sha256,phase,preimage,reader_generation,writer_generation) VALUES (?,?,?,'enrolling',?,?,?)",
                     (manifest['domain_id'],text,manifest_sha256,manifest['preimage'],manifest['reader_generation'],manifest['writer_generation']))


def _domain(conn, domain_id):
    _id(domain_id)
    row=conn.execute('SELECT * FROM destination_model_domains WHERE domain_id=?',(domain_id,)).fetchone()
    if row is None:raise plans.PlanConflict('model domain absent')
    try:
        m=json.loads(row['manifest_json'])
        if plans.scopes.canonical(m)!=row['manifest_json'] or plans._sha(row['manifest_json'])!=row['manifest_sha256'] or m['mode']!=_MODE:
            raise ValueError('model integrity')
    except (ValueError,TypeError,KeyError):raise plans.PlanConflict('model integrity changed') from None
    return dict(row),m


def register(conn, domain_id, kind, member_id, gateway_sha256):
    _id(member_id);plans._hash(gateway_sha256)
    with plans._tx(conn):
        row,m=_domain(conn,domain_id)
        if (kind not in ('writer','reader') or row['phase']!='enrolling'
            or member_id not in m[kind+'s'] or gateway_sha256!=m['gateway_sha256']):
            raise plans.PlanConflict('participant gateway not covered')
        if conn.execute('SELECT 1 FROM destination_model_members WHERE domain_id=? AND kind=? AND member_id=?',(domain_id,kind,member_id)).fetchone():
            raise plans.PlanConflict('participant already registered')
        conn.execute('INSERT INTO destination_model_members VALUES (?,?,?,?)',(domain_id,kind,member_id,gateway_sha256))


def _coverage(conn,row,m):
    if m['unknown_domains']:raise plans.PlanConflict('reader or writer completeness unknown')
    expected=sorted((kind,member,m['gateway_sha256']) for kind in ('writer','reader') for member in m[kind+'s'])
    actual=[tuple(x) for x in conn.execute('SELECT kind,member_id,gateway_sha256 FROM destination_model_members WHERE domain_id=? ORDER BY kind,member_id',(row['domain_id'],))]
    if actual!=expected:raise plans.PlanConflict('not every model entrypoint is registered')
    return plans._sha(plans.scopes.canonical({'manifest_sha256':row['manifest_sha256'],'gateways':actual}))


def seal(conn, domain_id):
    with plans._tx(conn):
        row,m=_domain(conn,domain_id);proof=_coverage(conn,row,m)
        if row['phase']!='enrolling':raise plans.PlanConflict('model membership already closed')
        conn.execute("UPDATE destination_model_domains SET phase='open',closure_sha256=? WHERE domain_id=? AND phase='enrolling'",(proof,domain_id))
    return observe(conn,domain_id)


def _closed(conn,row,m):
    if row['phase']=='enrolling' or row['closure_sha256']!=_coverage(conn,row,m):
        raise plans.PlanConflict('model coverage not closed')


def _snapshot(row):
    return {'destination_sha256':_preimage(row['preimage']),'destination_revision':row['destination_revision'],
            'reader_generation':row['reader_generation'],'writer_generation':row['writer_generation'],
            'fence_revision':row['fence_revision']}


def observe(conn, domain_id):
    row,m=_domain(conn,domain_id);_closed(conn,row,m)
    return _snapshot(row)


def enter(conn, domain_id, kind, member_id, *, expected_generation):
    """Every modeled reader/writer admission uses the same durable gate."""
    _number(expected_generation);_id(member_id)
    with plans._tx(conn):
        row,m=_domain(conn,domain_id);_closed(conn,row,m)
        if kind not in ('reader','writer') or member_id not in m[kind+'s'] or row['phase']!='open':
            raise plans.PlanConflict('new entry blocked by model fence or coverage')
        field=kind+'_generation'
        if row[field]!=expected_generation or row[field]>=_MAX:raise plans.PlanConflict('stale participant generation')
        count=conn.execute('SELECT COUNT(*) FROM destination_model_events WHERE domain_id=?',(domain_id,)).fetchone()[0]
        if count>=256:raise plans.PlanConflict('model event budget exhausted')
        busy=conn.execute("SELECT 1 FROM destination_model_events WHERE domain_id=? AND kind=? AND member_id=? AND state!='closed'",(domain_id,kind,member_id)).fetchone()
        if busy:raise plans.PlanConflict('participant already active or unknown')
        generation=row[field]+1
        conn.execute('UPDATE destination_model_domains SET '+field+'=? WHERE domain_id=? AND '+field+'=?',(generation,domain_id,expected_generation))
        conn.execute("INSERT INTO destination_model_events VALUES (?,?,?,?,'active')",(domain_id,kind,member_id,generation))
        return {'domain_id':domain_id,'kind':kind,'member_id':member_id,'generation':generation}


def finish(conn, handle, *, unknown=False):
    """Explicit owned completion only. No expiry, timestamp or idle inference."""
    if type(handle) is not dict or set(handle)!=_EVENT or type(unknown) is not bool:
        raise plans.PlanConflict('exact participant event handle required')
    _number(handle['generation'])
    if handle['kind'] not in ('reader','writer'):raise plans.PlanConflict('invalid participant kind')
    _id(handle['member_id'])
    with plans._tx(conn):
        row,m=_domain(conn,handle['domain_id']);_closed(conn,row,m)
        if row['phase']!='open':raise plans.PlanConflict('participant completion behind a fence')
        field=handle['kind']+'_generation'
        if row[field]>=_MAX:raise plans.PlanConflict('generation exhausted')
        changed=conn.execute("UPDATE destination_model_events SET state=? WHERE domain_id=? AND kind=? AND member_id=? AND generation=? AND state='active'",
                             ('unknown' if unknown else 'closed',handle['domain_id'],handle['kind'],handle['member_id'],handle['generation'])).rowcount
        if changed!=1:raise plans.PlanConflict('stale or uncertain participant completion')
        conn.execute('UPDATE destination_model_domains SET '+field+'='+field+'+1 WHERE domain_id=?',(handle['domain_id'],))


def _idle(conn,row,m):
    _closed(conn,row,m)
    if conn.execute("SELECT 1 FROM destination_model_events WHERE domain_id=? AND state!='closed'",(row['domain_id'],)).fetchone():
        raise plans.PlanConflict('active or uncertain reader/writer remains')


def _validated_plan(conn,request_id,owner_id,generation,plan_sha256):
    refresh._generation(generation);plans._hash(plan_sha256)
    row,plan=refresh._current(conn,request_id,owner_id)
    if row['generation']!=generation or row['plan_sha256']!=plan_sha256 or row['state']!='validated' or row['revision']!=2:
        raise plans.PlanConflict('stale or unfinished owned plan')
    return plan


def _expected(value):
    if type(value) is not dict or set(value)!=_SNAPSHOT:raise plans.PlanConflict('exact destination and generation snapshot required')
    plans._hash(value['destination_sha256'])
    for key in _SNAPSHOT-{'destination_sha256'}:_number(value[key])
    return dict(value)


def reserve(conn, domain_id, request_id, owner_id, *, plan_generation, plan_sha256,
            expected, observed_preimage, worker_id):
    expected=_expected(expected);observed=_preimage(observed_preimage)
    if type(worker_id) is not str or not plans._WORKER.fullmatch(worker_id):raise plans.PlanConflict('invalid fence worker')
    with plans._tx(conn):
        plan=_validated_plan(conn,request_id,owner_id,plan_generation,plan_sha256)
        row,m=_domain(conn,domain_id);_idle(conn,row,m)
        if (row['phase']!='open' or _snapshot(row)!=expected or observed!=expected['destination_sha256']
            or row['fence_revision']>=_MAX or plan['preconditions']!={k:expected[k] for k in plans._PRECONDITIONS}):
            raise plans.PlanConflict('preimage, destination revision or reader/writer generation changed')
        binding={'domain_id':domain_id,'request_id':request_id,'owner_id':owner_id,'plan_generation':plan_generation,
                 'plan_sha256':plan_sha256,'source':plan['source'],'expected':expected,
                 'closure_sha256':row['closure_sha256'],'manifest_sha256':row['manifest_sha256']}
        text=plans.scopes.canonical(binding);fence_revision=row['fence_revision']+1
        changed=conn.execute("UPDATE destination_model_domains SET phase='fenced',fence_revision=? WHERE domain_id=? AND phase='open' AND fence_revision=?",(fence_revision,domain_id,expected['fence_revision'])).rowcount
        if changed!=1:raise plans.PlanConflict('destination fence CAS lost')
        conn.execute('INSERT INTO destination_model_fences(domain_id,binding_json,binding_sha256,worker_id,fence_revision) VALUES (?,?,?,?,?)',
                     (domain_id,text,plans._sha(text),worker_id,fence_revision))
        return {'domain_id':domain_id,'request_id':request_id,'owner_id':owner_id,'plan_generation':plan_generation,
                'plan_sha256':plan_sha256,'fence_revision':fence_revision,'revision':0,'worker_id':worker_id,'binding_sha256':plans._sha(text)}


def check(conn, handle, *, observed_preimage):
    if type(handle) is not dict or set(handle)!=_HANDLE or type(handle['revision']) is not int or handle['revision']!=0:
        raise plans.PlanConflict('exact model fence handle required')
    _number(handle['fence_revision']);_preimage(observed_preimage);plans._hash(handle['binding_sha256'])
    with plans._tx(conn):
        plan=_validated_plan(conn,handle['request_id'],handle['owner_id'],handle['plan_generation'],handle['plan_sha256'])
        row,m=_domain(conn,handle['domain_id']);_idle(conn,row,m)
        fence=conn.execute('SELECT * FROM destination_model_fences WHERE domain_id=?',(handle['domain_id'],)).fetchone()
        if fence is None:raise plans.PlanConflict('model fence absent')
        try:
            b=json.loads(fence['binding_json'])
            if plans.scopes.canonical(b)!=fence['binding_json'] or plans._sha(fence['binding_json'])!=fence['binding_sha256']:raise ValueError('binding hash')
        except (ValueError,TypeError):raise plans.PlanConflict('model fence integrity') from None
        if (row['phase']!='fenced' or fence['revision']!=0 or fence['state']!='reserved'
            or handle['domain_id']!=b['domain_id'] or handle['binding_sha256']!=fence['binding_sha256']
            or handle['worker_id']!=fence['worker_id'] or handle['fence_revision']!=row['fence_revision']
            or handle['fence_revision']!=fence['fence_revision']
            or any(handle[k]!=b[k] or type(handle[k]) is not type(b[k]) for k in ('request_id','owner_id','plan_generation','plan_sha256'))
            or plan['source']!=b['source'] or row['closure_sha256']!=b['closure_sha256'] or row['manifest_sha256']!=b['manifest_sha256']
            or _snapshot(row)!={**b['expected'],'fence_revision':handle['fence_revision']}
            or _preimage(observed_preimage)!=b['expected']['destination_sha256']):
            raise plans.PlanConflict('stale fence, plan, preimage or reader/writer generation')
        changed=conn.execute("UPDATE destination_model_fences SET state='checked',revision=1 WHERE domain_id=? AND state='reserved' AND revision=0 AND worker_id=? AND fence_revision=?",
                             (handle['domain_id'],handle['worker_id'],handle['fence_revision'])).rowcount
        if changed!=1:raise plans.PlanConflict('model fence check CAS lost')
        conn.execute("UPDATE destination_model_domains SET phase='checked' WHERE domain_id=? AND phase='fenced'",(handle['domain_id'],))
        return {**handle,'revision':1,'model_coverage_checked':True,'qualified_for_effects':False}


def require_effect_authority(*args, **kwargs):
    plans.require_effect_authority(*args, **kwargs)
