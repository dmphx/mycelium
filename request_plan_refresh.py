"""Explicit offline metadata generations. No scheduler, route or effect authority."""
import json

import request_execution_plans as plans

POLICY_VERSION = 1
MAX_GENERATION = 64
_HANDLE = {'request_id','owner_id','generation','plan_sha256','revision','worker_id'}


def init_schema(conn):
    """Owned disposable connections only. Normal db.init never calls this."""
    for statement in (
        """CREATE TABLE IF NOT EXISTS request_plan_metadata_versions (
          request_id INTEGER NOT NULL REFERENCES request_execution_plans(request_id),
          owner_id INTEGER NOT NULL REFERENCES users(id),
          generation INTEGER NOT NULL CHECK(generation BETWEEN 1 AND 64),
          plan_json TEXT NOT NULL, plan_sha256 TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'planned' CHECK(state IN ('planned','claimed','validated')),
          revision INTEGER NOT NULL DEFAULT 0 CHECK(revision BETWEEN 0 AND 2),
          worker_id TEXT, PRIMARY KEY(request_id,generation)
        )""",
        """CREATE TABLE IF NOT EXISTS request_plan_metadata_heads (
          request_id INTEGER PRIMARY KEY REFERENCES request_execution_plans(request_id),
          owner_id INTEGER NOT NULL REFERENCES users(id),
          generation INTEGER NOT NULL CHECK(generation BETWEEN 1 AND 64),
          FOREIGN KEY(request_id,generation) REFERENCES request_plan_metadata_versions(request_id,generation)
        )""",
        """CREATE TRIGGER IF NOT EXISTS request_plan_metadata_immutable
          BEFORE UPDATE OF request_id,owner_id,generation,plan_json,plan_sha256 ON request_plan_metadata_versions
          BEGIN SELECT RAISE(ABORT,'immutable metadata generation'); END""",
        """CREATE TRIGGER IF NOT EXISTS request_plan_metadata_no_delete
          BEFORE DELETE ON request_plan_metadata_versions
          BEGIN SELECT RAISE(ABORT,'retained metadata generation'); END""",
        """CREATE TRIGGER IF NOT EXISTS request_plan_metadata_head_identity
          BEFORE UPDATE OF request_id,owner_id ON request_plan_metadata_heads
          BEGIN SELECT RAISE(ABORT,'immutable metadata head identity'); END""",
        """CREATE TRIGGER IF NOT EXISTS request_plan_metadata_head_monotonic
          BEFORE UPDATE OF generation ON request_plan_metadata_heads
          WHEN NEW.generation != OLD.generation+1 OR NOT EXISTS (
            SELECT 1 FROM request_plan_metadata_versions
            WHERE request_id=OLD.request_id AND owner_id=OLD.owner_id
              AND generation=OLD.generation AND state='validated' AND revision=2)
          BEGIN SELECT RAISE(ABORT,'metadata head generation CAS'); END""",
        """CREATE TRIGGER IF NOT EXISTS request_plan_metadata_state_CAS
          BEFORE UPDATE OF state,revision,worker_id ON request_plan_metadata_versions
          WHEN NOT ((OLD.state='planned' AND OLD.revision=0 AND OLD.worker_id IS NULL
                     AND NEW.state='claimed' AND NEW.revision=1 AND NEW.worker_id IS NOT NULL)
                 OR (OLD.state='claimed' AND OLD.revision=1
                     AND NEW.state='validated' AND NEW.revision=2 AND NEW.worker_id IS OLD.worker_id))
          BEGIN SELECT RAISE(ABORT,'metadata state revision CAS'); END""",
        """CREATE TRIGGER IF NOT EXISTS request_plan_metadata_head_no_delete
          BEFORE DELETE ON request_plan_metadata_heads
          BEGIN SELECT RAISE(ABORT,'retained metadata head'); END""",
    ):
        conn.execute(statement)


def _generation(value):
    if type(value) is not int or not 0 <= value <= MAX_GENERATION:
        raise plans.PlanConflict('invalid metadata generation')
    return value


def _current(conn, request_id, owner_id):
    # Generation zero is the immutable original, and must finish its offline
    # claim/validation before it can seed the versioned refresh policy.
    base, original = plans._load(conn,request_id,owner_id)
    plans._check_source(conn,original)
    head = conn.execute('SELECT * FROM request_plan_metadata_heads WHERE request_id=?',(request_id,)).fetchone()
    if head is None:
        return {**base,'generation':0},original
    if head['owner_id'] != owner_id:
        raise plans.PlanConflict('metadata head owner changed')
    row = conn.execute('SELECT * FROM request_plan_metadata_versions WHERE request_id=? AND owner_id=? AND generation=?',
                       (request_id,owner_id,head['generation'])).fetchone()
    if row is None:
        raise plans.PlanConflict('metadata head unavailable')
    try:
        plan = json.loads(row['plan_json'])
        if (plans.scopes.canonical(plan) != row['plan_json'] or plans._sha(row['plan_json']) != row['plan_sha256']
            or plan['version'] != 2 or plan['policy_version'] != POLICY_VERSION
            or plan['generation'] != row['generation'] or plan['source'] != original['source']
            or plan['preconditions'] != original['preconditions']):
            raise ValueError('metadata generation integrity')
    except (ValueError,TypeError,KeyError):
        raise plans.PlanConflict('metadata generation integrity changed') from None
    plans._check_source(conn,plan)
    return dict(row),plan


def _public(row,plan):
    return {**plans._public(row,plan),'generation':row['generation']}


def get(conn, request_id, owner_id):
    row,plan = _current(conn,request_id,owner_id)
    return _public(row,plan)


def _transition(previous, built):
    # Conservative version1 policy: append eligible pairs, resolve unknown dates,
    # and retain descriptive corrections. Never erase or remap an observed pair.
    before = {(e['season'],e['episode']):e for e in previous['snapshot']['episodes']}
    after = {(e['season'],e['episode']):e for e in built['snapshot']['episodes']}
    if not before.keys() <= after.keys():
        raise plans.PlanConflict('metadata refresh would remove an observed episode')
    for pair,old in before.items():
        new = after[pair]
        if (new['tmdb_episode_id'] != old['tmdb_episode_id']
            or (old['air_date'] is not None and new['air_date'] != old['air_date'])):
            raise plans.PlanConflict('metadata episode identity or known air date changed')
    if built['cutoff'] != previous['snapshot']['cutoff']:
        raise plans.PlanConflict('original metadata cutoff changed')


def refresh(conn, request_id, owner_id, *, expected_generation, expected_plan_sha256,
            expected_revision, policy_version, metadata, metadata_sha256, current_preconditions):
    """One explicit CAS append. No in-place replacement, merge, replay or effects."""
    _generation(expected_generation)
    if expected_generation == MAX_GENERATION:
        raise plans.PlanConflict('metadata generation budget exhausted')
    if type(expected_revision) is not int or expected_revision != 2:
        raise plans.PlanConflict('validated revision required')
    if type(policy_version) is not int or policy_version != POLICY_VERSION:
        raise plans.PlanConflict('unsupported metadata refresh policy')
    plans._hash(expected_plan_sha256)
    pins = plans._preconditions(current_preconditions)
    with plans._tx(conn):
        row,old = _current(conn,request_id,owner_id)
        if (row['generation'] != expected_generation or row['plan_sha256'] != expected_plan_sha256
            or row['state'] != 'validated' or row['revision'] != expected_revision):
            raise plans.PlanConflict('stale or unfinished metadata generation')
        if old['preconditions'] != pins:
            raise plans.PlanConflict('destination or reader/writer generation changed')
        built = plans._metadata(old['source'],metadata,metadata_sha256)
        if metadata_sha256 == old['metadata_sha256']:
            raise plans.PlanConflict('metadata unchanged, no successor required')
        _transition(old,built)
        generation = expected_generation+1
        plan = {'version':2,'policy_version':POLICY_VERSION,'generation':generation,
                'parent_plan_sha256':row['plan_sha256'],'source':old['source'],
                'metadata_sha256':metadata_sha256,'snapshot':built['snapshot'],
                'predicate':built['predicate'],'snapshot_sha256':built['snapshot_hash'],
                'predicate_sha256':built['predicate_hash'],'accepted_metadata_sha256':built['accepted_hash'],
                'preconditions':pins}
        text = plans.scopes.canonical(plan)
        conn.execute('INSERT INTO request_plan_metadata_versions(request_id,owner_id,generation,plan_json,plan_sha256) VALUES (?,?,?,?,?)',
                     (request_id,owner_id,generation,text,plans._sha(text)))
        if expected_generation == 0:
            conn.execute('INSERT INTO request_plan_metadata_heads(request_id,owner_id,generation) VALUES (?,?,?)',
                         (request_id,owner_id,generation))
        else:
            changed = conn.execute('UPDATE request_plan_metadata_heads SET generation=? WHERE request_id=? AND owner_id=? AND generation=?',
                                   (generation,request_id,owner_id,expected_generation)).rowcount
            if changed != 1:
                raise plans.PlanConflict('metadata head CAS lost')
        current,stored = _current(conn,request_id,owner_id)
        return _public(current,stored)


def claim(conn, request_id, owner_id, *, expected_generation, expected_revision, plan_sha256, worker_id):
    _generation(expected_generation)
    if expected_generation == 0 or type(expected_revision) is not int or expected_revision != 0:
        raise plans.PlanConflict('planned successor generation required')
    plans._hash(plan_sha256)
    if type(worker_id) is not str or not plans._WORKER.fullmatch(worker_id):
        raise plans.PlanConflict('invalid worker identity')
    with plans._tx(conn):
        row,plan = _current(conn,request_id,owner_id)
        if row['generation'] != expected_generation or row['plan_sha256'] != plan_sha256:
            raise plans.PlanConflict('stale metadata generation')
        changed = conn.execute("UPDATE request_plan_metadata_versions SET state='claimed',revision=1,worker_id=? WHERE request_id=? AND owner_id=? AND generation=? AND state='planned' AND revision=0 AND plan_sha256=?",
                               (worker_id,request_id,owner_id,expected_generation,plan_sha256)).rowcount
        if changed != 1:
            raise plans.PlanConflict('metadata generation already claimed')
        return {'request_id':request_id,'owner_id':owner_id,'generation':expected_generation,
                'plan_sha256':plan_sha256,'revision':1,'worker_id':worker_id}


def validate(conn, handle, *, metadata, metadata_sha256, current_preconditions):
    if (type(handle) is not dict or set(handle) != _HANDLE
        or not plans._positive(handle['request_id']) or not plans._positive(handle['owner_id'])
        or type(handle['revision']) is not int or handle['revision'] != 1
        or type(handle['worker_id']) is not str or not plans._WORKER.fullmatch(handle['worker_id'])):
        raise plans.PlanConflict('invalid metadata claim')
    _generation(handle['generation']);plans._hash(handle['plan_sha256'])
    pins = plans._preconditions(current_preconditions)
    with plans._tx(conn):
        row,plan = _current(conn,handle['request_id'],handle['owner_id'])
        if row['generation'] != handle['generation'] or row['plan_sha256'] != handle['plan_sha256']:
            raise plans.PlanConflict('stale metadata claimant')
        built = plans._metadata(plan['source'],metadata,metadata_sha256)
        if (plan['metadata_sha256'] != metadata_sha256 or plan['accepted_metadata_sha256'] != built['accepted_hash']
            or plan['preconditions'] != pins):
            raise plans.PlanConflict('current metadata or destination/reader/writer pins changed')
        changed = conn.execute("UPDATE request_plan_metadata_versions SET state='validated',revision=2 WHERE request_id=? AND owner_id=? AND generation=? AND state='claimed' AND revision=? AND plan_sha256=? AND worker_id=?",
                               (handle['request_id'],handle['owner_id'],handle['generation'],handle['revision'],handle['plan_sha256'],handle['worker_id'])).rowcount
        if changed != 1:
            raise plans.PlanConflict('stale metadata claim revision')
        current,stored = _current(conn,handle['request_id'],handle['owner_id'])
        return _public(current,stored)


def require_effect_authority(*args, **kwargs):
    plans.require_effect_authority(*args, **kwargs)
