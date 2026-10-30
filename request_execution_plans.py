"""Unwired, request-scoped planning/CAS ledger. It grants no effect authority.

Connections and pinned metadata are explicitly supplied by an internal caller.
No app startup, route, worker, provider or shared monitor imports this module.
"""
from contextlib import contextmanager
import hashlib
import json
import re

import intent_snapshots
import native_request_scopes as scopes
import native_request_dispatch as dispatch


class PlanConflict(ValueError):
    pass


class EffectsUnqualified(PlanConflict):
    pass


_HASH = re.compile(r'[a-f0-9]{64}')
_WORKER = re.compile(r'[A-Za-z0-9_-]{1,96}')
_PRECONDITIONS = {'destination_sha256','reader_generation','writer_generation'}
_HANDLE = {'request_id','owner_id','plan_sha256','revision','worker_id'}


def init_schema(conn):
    """Explicit offline initialization only, never called by normal db.init()."""
    for statement in (
        """CREATE TABLE IF NOT EXISTS request_execution_plans (
          request_id INTEGER PRIMARY KEY REFERENCES user_requests(id),
          owner_id INTEGER NOT NULL REFERENCES users(id),
          plan_json TEXT NOT NULL, plan_sha256 TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'planned' CHECK(state IN ('planned','claimed','validated')),
          revision INTEGER NOT NULL DEFAULT 0 CHECK(revision BETWEEN 0 AND 2),
          worker_id TEXT,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now'))
        )""",
        """CREATE TRIGGER IF NOT EXISTS request_execution_plans_immutable
          BEFORE UPDATE OF request_id,owner_id,plan_json,plan_sha256 ON request_execution_plans
          BEGIN SELECT RAISE(ABORT,'immutable execution plan'); END""",
        """CREATE TRIGGER IF NOT EXISTS request_execution_plans_no_delete
          BEFORE DELETE ON request_execution_plans
          BEGIN SELECT RAISE(ABORT,'immutable execution plan'); END""",
    ):
        conn.execute(statement)


def _positive(value):
    return type(value) is int and 0 < value <= 9223372036854775807


def _hash(value):
    if type(value) is not str or not _HASH.fullmatch(value):
        raise PlanConflict('invalid hash pin')
    return value


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _preconditions(value):
    if type(value) is not dict or set(value) != _PRECONDITIONS:
        raise PlanConflict('exact precondition pins required')
    _hash(value['destination_sha256'])
    for name in ('reader_generation','writer_generation'):
        if type(value[name]) is not int or not 0 <= value[name] <= 2**63-1:
            raise PlanConflict('invalid generation pin')
    return dict(value)


@contextmanager
def _tx(conn):
    if conn.in_transaction:
        raise PlanConflict('planning requires an independent transaction')
    conn.execute('BEGIN IMMEDIATE')
    try:
        yield
        conn.execute('COMMIT')
    except BaseException:
        conn.execute('ROLLBACK')
        raise


def _source(conn, request_id, owner_id, expected_dispatch_attempt):
    if not _positive(request_id) or not _positive(owner_id):
        raise PlanConflict('invalid request owner identity')
    if type(expected_dispatch_attempt) is not int or not 0 <= expected_dispatch_attempt <= 3:
        raise PlanConflict('invalid dispatch attempt')
    raw = conn.execute('SELECT * FROM user_requests WHERE id=? AND user_id=?', (request_id,owner_id)).fetchone()
    user = conn.execute('SELECT enabled FROM users WHERE id=?',(owner_id,)).fetchone()
    if raw is None or user is None or not user['enabled'] or raw['status'] != 'approved':
        raise PlanConflict('request not eligible')
    row = dict(raw)
    try:
        scope = scopes.read(conn,row)
        text, binding_sha, _ = dispatch._binding(conn,row)
    except (ValueError, TypeError):
        raise PlanConflict('original request identity unavailable') from None
    if scope is None:
        raise PlanConflict('original immutable scope required')
    snapshot = conn.execute('SELECT * FROM native_request_scopes WHERE request_id=?',(request_id,)).fetchone()
    saved = conn.execute('SELECT * FROM native_request_dispatch WHERE request_id=?',(request_id,)).fetchone()
    if saved is None or saved['binding_json'] != text or saved['binding_sha256'] != binding_sha:
        raise PlanConflict('dispatch identity unavailable')
    if saved['attempts'] != expected_dispatch_attempt:
        raise PlanConflict('dispatch generation changed')
    ready = saved['state']=='ready' and saved['attempts']==0
    proven = saved['state']=='pre_effect_failed' and saved['pre_effect_proof']=='handoff_not_entered'
    if not (ready or proven):
        raise PlanConflict('handoff may already have effects')
    _hash(snapshot['raw_body_sha256'])
    return {**{k:row[k] for k in ('id','user_id','imdb_id','tmdb_id','media_type','title')},
            'scope':scope,'scope_sha256':snapshot['scope_sha256'],
            'body_sha256':snapshot['raw_body_sha256'],'dispatch_binding_sha256':binding_sha,
            'dispatch_attempt':expected_dispatch_attempt,'dispatch_state':saved['state']}


def _metadata(source, metadata, expected_hash):
    _hash(expected_hash)
    try:
        text = scopes.canonical(metadata)
    except (ValueError, TypeError):
        raise PlanConflict('invalid pinned metadata') from None
    if len(text.encode()) > 4*1024*1024 or _sha(text) != expected_hash:
        raise PlanConflict('metadata pin changed')
    scope = source['scope']
    normalized_scope = {'kind':'future'} if scope['kind']=='future' else scope
    payload = {'media':{'type':source['media_type'],'tmdb_id':source['tmdb_id']},'scope':normalized_scope}
    try:
        import request_intents
        normalized = request_intents.normalize_payload(payload)
        built = intent_snapshots.build(metadata,normalized,request_intents.semantic_hash(normalized))
    except (ValueError, TypeError, KeyError, request_intents.IntentError, intent_snapshots.SnapshotError):
        raise PlanConflict('metadata does not match original request') from None
    episodes = built['snapshot']['episodes']
    if source['media_type'] == 'tv' and not episodes:
        raise PlanConflict('episode metadata unavailable')
    if scope['kind'] == 'selected_seasons' and {e['season'] for e in episodes} != set(scope['seasons']):
        raise PlanConflict('selected season metadata unavailable')
    if built['title'] != source['title'] or built['imdb_id'] != source['imdb_id']:
        raise PlanConflict('metadata title identity changed')
    if scope['kind']=='future' and built['cutoff'] != scope['starts_on']:
        raise PlanConflict('original future cutoff changed')
    return built


def _load(conn, request_id, owner_id):
    if not _positive(request_id) or not _positive(owner_id):
        raise PlanConflict('invalid request owner identity')
    row = conn.execute('SELECT * FROM request_execution_plans WHERE request_id=? AND owner_id=?',
                       (request_id,owner_id)).fetchone()
    if row is None:
        raise PlanConflict('plan not found')
    try:
        plan = json.loads(row['plan_json'])
        if scopes.canonical(plan) != row['plan_json'] or _sha(row['plan_json']) != row['plan_sha256']:
            raise ValueError('plan hash mismatch')
    except (ValueError, TypeError):
        raise PlanConflict('plan integrity changed') from None
    return dict(row),plan


def _check_source(conn, plan):
    source = plan['source']
    if _source(conn,source['id'],source['user_id'],source['dispatch_attempt']) != source:
        raise PlanConflict('original request changed')


def _public(row,plan):
    return {'request_id':row['request_id'],'owner_id':row['owner_id'],
            'plan_sha256':row['plan_sha256'],'revision':row['revision'],'state':row['state'],
            'worker_id':row['worker_id'],'qualified_for_effects':False,'plan':plan}


def prepare(conn, request_id, owner_id, *, expected_dispatch_attempt, metadata, metadata_sha256, preconditions):
    """Create an immutable plan, or return that identical plan. No replacement/merge."""
    pins = _preconditions(preconditions)
    with _tx(conn):
        source = _source(conn,request_id,owner_id,expected_dispatch_attempt)
        built = _metadata(source,metadata,metadata_sha256)
        plan = {'version':1,'source':source,'metadata_sha256':metadata_sha256,
                'snapshot':built['snapshot'],'predicate':built['predicate'],
                'snapshot_sha256':built['snapshot_hash'],'predicate_sha256':built['predicate_hash'],
                'accepted_metadata_sha256':built['accepted_hash'],'preconditions':pins}
        text = scopes.canonical(plan)
        saved = conn.execute('SELECT plan_json FROM request_execution_plans WHERE request_id=?',(request_id,)).fetchone()
        if saved is not None and saved['plan_json'] != text:
            raise PlanConflict('immutable plan already exists')
        conn.execute('INSERT OR IGNORE INTO request_execution_plans(request_id,owner_id,plan_json,plan_sha256) VALUES (?,?,?,?)',
                     (request_id,owner_id,text,_sha(text)))
        row, stored = _load(conn,request_id,owner_id)
        return _public(row,stored)


def claim(conn, request_id, owner_id, *, expected_revision, plan_sha256, worker_id):
    if type(expected_revision) is not int or expected_revision != 0:
        raise PlanConflict('invalid expected revision')
    _hash(plan_sha256)
    if type(worker_id) is not str or not _WORKER.fullmatch(worker_id):
        raise PlanConflict('invalid worker identity')
    with _tx(conn):
        row, plan = _load(conn,request_id,owner_id)
        _check_source(conn,plan)
        if row['plan_sha256'] != plan_sha256:
            raise PlanConflict('plan pin changed')
        changed = conn.execute("UPDATE request_execution_plans SET state='claimed',revision=1,worker_id=? WHERE request_id=? AND owner_id=? AND state='planned' AND revision=? AND plan_sha256=?",
            (worker_id,request_id,owner_id,expected_revision,plan_sha256)).rowcount
        if changed != 1:
            raise PlanConflict('plan ownership changed')
        return {'request_id':request_id,'owner_id':owner_id,'plan_sha256':plan_sha256,
                'revision':1,'worker_id':worker_id}


def validate(conn, handle, *, metadata, metadata_sha256, current_preconditions):
    """CAS validate pinned inputs only. This is not destination/reader qualification."""
    if (type(handle) is not dict or set(handle) != _HANDLE
        or not _positive(handle['request_id']) or not _positive(handle['owner_id'])
        or type(handle['revision']) is not int or handle['revision'] != 1
        or type(handle['worker_id']) is not str or not _WORKER.fullmatch(handle['worker_id'])):
        raise PlanConflict('invalid claim identity')
    _hash(handle['plan_sha256'])
    pins = _preconditions(current_preconditions)
    with _tx(conn):
        row, plan = _load(conn,handle['request_id'],handle['owner_id'])
        _check_source(conn,plan)
        built = _metadata(plan['source'],metadata,metadata_sha256)
        if (plan['metadata_sha256'] != metadata_sha256 or plan['accepted_metadata_sha256'] != built['accepted_hash']
            or plan['preconditions'] != pins):
            raise PlanConflict('metadata or destination/reader/writer pins changed')
        changed = conn.execute("UPDATE request_execution_plans SET state='validated',revision=2 WHERE request_id=? AND owner_id=? AND state='claimed' AND revision=? AND plan_sha256=? AND worker_id=?",
            (handle['request_id'],handle['owner_id'],handle['revision'],handle['plan_sha256'],handle['worker_id'])).rowcount
        if changed != 1:
            raise PlanConflict('stale plan claimant')
        row,plan = _load(conn,handle['request_id'],handle['owner_id'])
        return _public(row,plan)


def get(conn, request_id, owner_id):
    row,plan = _load(conn,request_id,owner_id)
    _check_source(conn,plan)
    return _public(row,plan)


def require_effect_authority(*args, **kwargs):
    """Fail closed even for the CAS winner: all-writer/readers are unqualified."""
    raise EffectsUnqualified('execution/publication is not qualified by a planning ledger')
