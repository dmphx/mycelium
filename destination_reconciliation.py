"""Offline staged receipts and durable quarantine. No filesystem effects or wiring."""
import json

import destination_identity as identity
import request_execution_plans as plans

_HANDLE = {'domain_id','revision','event_sha256'}
_TERMINAL = {'unknown','invalidated','inverse_staged'}
_REASONS = {'forward_outcome_unknown','rollback_outcome_unknown','preimage_unavailable'}


def init_schema(conn):
    """Explicit owned-fixture initialization, never normal db.init or migration."""
    for statement in (
        """CREATE TABLE IF NOT EXISTS destination_reconciliation_bindings (
          domain_id TEXT PRIMARY KEY REFERENCES destination_identity_bindings(domain_id),
          binding_json TEXT NOT NULL, binding_sha256 TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS destination_reconciliation_events (
          domain_id TEXT NOT NULL REFERENCES destination_reconciliation_bindings(domain_id),
          revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0),
          state TEXT NOT NULL CHECK(state IN ('prepared','forward_staged','inverse_staged','unknown','invalidated')),
          event_json TEXT NOT NULL, event_sha256 TEXT NOT NULL, previous_sha256 TEXT,
          PRIMARY KEY(domain_id,revision)
        )""",
        """CREATE TRIGGER IF NOT EXISTS reconciliation_binding_immutable BEFORE UPDATE ON destination_reconciliation_bindings
          BEGIN SELECT RAISE(ABORT,'immutable reconciliation binding'); END""",
        """CREATE TRIGGER IF NOT EXISTS reconciliation_binding_retained BEFORE DELETE ON destination_reconciliation_bindings
          BEGIN SELECT RAISE(ABORT,'retained reconciliation binding'); END""",
        """CREATE TRIGGER IF NOT EXISTS reconciliation_event_immutable BEFORE UPDATE ON destination_reconciliation_events
          BEGIN SELECT RAISE(ABORT,'immutable reconciliation event'); END""",
        """CREATE TRIGGER IF NOT EXISTS reconciliation_event_retained BEFORE DELETE ON destination_reconciliation_events
          BEGIN SELECT RAISE(ABORT,'retained reconciliation event'); END""",
        """CREATE TRIGGER IF NOT EXISTS reconciliation_event_CAS BEFORE INSERT ON destination_reconciliation_events
          WHEN NOT (
            (NEW.revision=0 AND NEW.state='prepared' AND NEW.previous_sha256 IS NULL
             AND NOT EXISTS(SELECT 1 FROM destination_reconciliation_events WHERE domain_id=NEW.domain_id))
            OR EXISTS(SELECT 1 FROM destination_reconciliation_events p
              WHERE p.domain_id=NEW.domain_id AND p.revision=NEW.revision-1
              AND p.revision=(SELECT MAX(revision) FROM destination_reconciliation_events WHERE domain_id=NEW.domain_id)
              AND p.event_sha256=NEW.previous_sha256
              AND ((p.state='prepared' AND NEW.state='forward_staged')
                OR (p.state='forward_staged' AND NEW.state='inverse_staged')
                OR (p.state IN ('prepared','forward_staged') AND NEW.state IN ('unknown','invalidated')))))
          BEGIN SELECT RAISE(ABORT,'reconciliation CAS or terminal quarantine'); END""",
    ):
        conn.execute(statement)


def _canonical(value):
    return plans.scopes.canonical(value)


def _read(conn, domain_id):
    binding=conn.execute('SELECT * FROM destination_reconciliation_bindings WHERE domain_id=?',(domain_id,)).fetchone()
    events=conn.execute('SELECT * FROM destination_reconciliation_events WHERE domain_id=? ORDER BY revision',(domain_id,)).fetchall()
    if binding is None or not events:raise plans.PlanConflict('reconciliation binding absent')
    try:
        b=json.loads(binding['binding_json'])
        if _canonical(b)!=binding['binding_json'] or plans._sha(binding['binding_json'])!=binding['binding_sha256']:raise ValueError()
        previous=None
        for n,event in enumerate(events):
            payload=json.loads(event['event_json'])
            if (event['revision']!=n or event['previous_sha256']!=previous
                or _canonical(payload)!=event['event_json'] or plans._sha(event['event_json'])!=event['event_sha256']
                or payload['domain_id']!=domain_id or payload['revision']!=n or payload['state']!=event['state']
                or payload['binding_sha256']!=binding['binding_sha256'] or payload['previous_sha256']!=previous
                or payload['mode']!='offline_staged_only' or payload['actual_forward_receipt_available'] is not False
                or payload['qualified_for_effects'] is not False or payload['qualified_for_rollback'] is not False):raise ValueError()
            previous=event['event_sha256']
    except (KeyError,ValueError,TypeError):raise plans.PlanConflict('reconciliation history integrity') from None
    return binding,b,events[-1]


def _public(event):
    return {'domain_id':event['domain_id'],'revision':event['revision'],'event_sha256':event['event_sha256']}


def _append(conn,binding,previous,state,detail):
    revision=0 if previous is None else previous['revision']+1
    previous_hash=None if previous is None else previous['event_sha256']
    payload={'version':1,'mode':'offline_staged_only','domain_id':binding['domain_id'],
             'binding_sha256':binding['binding_sha256'],'revision':revision,'previous_sha256':previous_hash,
             'state':state,'detail':detail,'actual_forward_receipt_available':False,
             'qualified_for_effects':False,'qualified_for_rollback':False}
    text=_canonical(payload)
    conn.execute('INSERT INTO destination_reconciliation_events VALUES (?,?,?,?,?,?)',
        (binding['domain_id'],revision,state,text,plans._sha(text),previous_hash))
    return _public(conn.execute('SELECT * FROM destination_reconciliation_events WHERE domain_id=? AND revision=?',
                               (binding['domain_id'],revision)).fetchone())


def enroll(conn,checked_fence,root_fd,checked_identity):
    """Bind checked originals. A recorded uncertain attempt cannot be re-enrolled."""
    if (type(checked_identity) is not dict or set(checked_identity)!=identity._HANDLE|{'qualified_for_effects','qualified_for_rollback'}
        or type(checked_identity['revision']) is not int or checked_identity['revision']!=2
        or checked_identity['qualified_for_effects'] is not False or checked_identity['qualified_for_rollback'] is not False):
        raise plans.PlanConflict('exact checked identity required')
    with plans._tx(conn):
        row,b=identity._load(conn,checked_fence,root_fd,checked_identity['binding_sha256'],checked_identity['generation'])
        if identity._public(row)!=checked_identity or row['state']!='checked':raise plans.PlanConflict('stale checked identity')
        text=_canonical({'version':1,'mode':'offline_staged_only','identity':checked_identity,
                         'identity_binding':b,'checked_fence':checked_fence})
        conn.execute('INSERT INTO destination_reconciliation_bindings VALUES (?,?,?)',(row['domain_id'],text,plans._sha(text)))
        binding=conn.execute('SELECT * FROM destination_reconciliation_bindings WHERE domain_id=?',(row['domain_id'],)).fetchone()
        return _append(conn,binding,None,'prepared',{'physical_destination':'original_preimage','effects':0})


def _current(conn,handle):
    if (type(handle) is not dict or set(handle)!=_HANDLE or type(handle['domain_id']) is not str
        or type(handle['revision']) is not int or not 0<=handle['revision']<=3):raise plans.PlanConflict('strict reconciliation handle required')
    plans._hash(handle['event_sha256'])
    binding,b,event=_read(conn,handle['domain_id'])
    if _public(event)!=handle:raise plans.PlanConflict('stale reconciliation revision or receipt')
    if event['state'] in _TERMINAL:raise plans.PlanConflict('terminal reconciliation; no retry or reclaim')
    return binding,b,event


def _validate(conn,b,root_fd):
    h=b['identity']
    row,current=identity._load(conn,b['checked_fence'],root_fd,h['binding_sha256'],h['generation'])
    if identity._public(row)!=h or current!=b['identity_binding']:raise plans.PlanConflict('identity authority changed')


def _advance(conn,root_fd,handle,operation,forward_receipt=None,expected_forward_revision=None):
    # Commit a negative authority observation before raising. Raising inside the
    # transaction would roll back the quarantine and permit an ABA restoration.
    stale=False;result=None
    with plans._tx(conn):
        binding,b,event=_current(conn,handle)
        if operation=='forward' and event['state']!='prepared':raise plans.PlanConflict('forward already staged')
        if operation=='inverse':
            if (event['state']!='forward_staged' or forward_receipt!=event['event_sha256']
                or type(expected_forward_revision) is not int
                or expected_forward_revision!=b['identity_binding']['rollback_guard']['planned_forward_revision']):
                raise plans.PlanConflict('exact forward staged receipt and generation required')
        try:_validate(conn,b,root_fd)
        except plans.PlanConflict:
            result=_append(conn,binding,event,'invalidated',{'reason':'observed_authority_or_preimage_mismatch','effects':0})
            stale=True
        else:
            guard=b['identity_binding']['rollback_guard'];forward_revision=guard['planned_forward_revision']
            if operation=='forward':
                detail={'from_revision':forward_revision-1,'to_revision':forward_revision,
                        'from_sha256':guard['preimage_sha256'],'to_sha256':guard['planned_forward_sha256'],
                        'rollback_sha256':guard['preimage_sha256'],'physical_destination':'original_preimage','effects':0}
                result=_append(conn,binding,event,'forward_staged',detail)
            elif operation=='inverse':
                detail={'forward_staged_receipt_sha256':event['event_sha256'],
                        'from_revision':forward_revision,'to_revision':forward_revision+1,
                        'from_sha256':guard['planned_forward_sha256'],'to_sha256':guard['preimage_sha256'],
                        'physical_destination':'original_preimage','effects':0}
                result=_append(conn,binding,event,'inverse_staged',detail)
            else:result=_public(event)
    if stale:raise plans.PlanConflict('durably invalidated; restored inputs cannot revive this attempt')
    return result


def stage_forward(conn,root_fd,handle):
    return _advance(conn,root_fd,handle,'forward')


def stage_inverse(conn,root_fd,handle,*,forward_receipt_sha256,expected_forward_revision):
    plans._hash(forward_receipt_sha256)
    return _advance(conn,root_fd,handle,'inverse',forward_receipt_sha256,expected_forward_revision)


def revalidate(conn,root_fd,handle):
    return _advance(conn,root_fd,handle,'revalidate')


def report_unknown(conn,handle,*,reason):
    """Persist explicit uncertainty without inferring success/failure from bytes."""
    if type(reason) is not str or reason not in _REASONS:raise plans.PlanConflict('explicit uncertainty reason required')
    with plans._tx(conn):
        binding,b,event=_current(conn,handle)
        return _append(conn,binding,event,'unknown',{'reason':reason,'effects':0})


def stored_status(conn,domain_id):
    """Historical record only. Does not revalidate current authority."""
    binding,b,event=_read(conn,domain_id)
    return json.loads(event['event_json'])|{'event_sha256':event['event_sha256'],'current_authority_checked':False}


def require_effect_authority(*args,**kwargs):
    plans.require_effect_authority(*args,**kwargs)
