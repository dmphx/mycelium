"""Durable native review/dispatch boundary. No scheduler or retry loop is activated."""
import hashlib
import re

import native_request_scopes as scopes

MAX_ATTEMPTS = 3


class DispatchConflict(ValueError):
    pass


def init_schema(conn):
    conn.executescript('''
CREATE TABLE IF NOT EXISTS native_request_dispatch (
 request_id INTEGER PRIMARY KEY REFERENCES user_requests(id),
 binding_json TEXT NOT NULL, binding_sha256 TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('ready','preparing','pre_effect_failed','unknown','accepted')),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
 pre_effect_proof TEXT, error_code TEXT,
 updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now'))
);
CREATE TRIGGER IF NOT EXISTS native_request_dispatch_identity_immutable
 BEFORE UPDATE OF request_id,binding_json,binding_sha256 ON native_request_dispatch
 BEGIN SELECT RAISE(ABORT,'immutable native dispatch identity'); END;
''')


def _binding(conn, row):
    try:
        scope = scopes.read(conn, row)
    except scopes.ScopeError as exc:
        raise DispatchConflict(str(exc)) from None
    source = 'recorded'
    if scope is None:
        # Old movie requests have one unambiguous scope, but no original body proof.
        if row['media_type'] != 'movie':
            raise DispatchConflict('original TV scope unavailable')
        source, scope = 'legacy_movie', {'kind': 'movie'}
    snapshot = conn.execute('SELECT raw_body_sha256 FROM native_request_scopes WHERE request_id=?',
                            (row['id'],)).fetchone()
    value = {k: row[k] for k in ('id','user_id','imdb_id','tmdb_id','media_type','title')}
    value.update(scope=scope, scope_source=source,
                 raw_body_sha256=snapshot['raw_body_sha256'] if snapshot else None)
    text = scopes.canonical(value)
    return text, hashlib.sha256(text.encode()).hexdigest(), scope


def enqueue(conn, row):
    """Called inside the same transaction that commits native approval."""
    text, digest, _ = _binding(conn, row)
    conn.execute('INSERT OR IGNORE INTO native_request_dispatch(request_id,binding_json,binding_sha256,state) VALUES (?,?,?,?)',
                 (row['id'], text, digest, 'ready'))
    saved = conn.execute('SELECT binding_json,binding_sha256 FROM native_request_dispatch WHERE request_id=?',
                         (row['id'],)).fetchone()
    if (saved['binding_json'], saved['binding_sha256']) != (text, digest):
        raise DispatchConflict('dispatch identity changed')


def _public(saved):
    if saved is None:
        return {'state': 'unavailable', 'attempt': 0, 'can_retry': False}
    return {'state': saved['state'], 'attempt': saved['attempts'],
            'can_retry': (saved['state'] == 'ready' and saved['attempts'] == 0)
                         or (saved['state'] == 'pre_effect_failed'
                             and saved['pre_effect_proof'] == 'handoff_not_entered'
                             and saved['attempts'] < MAX_ATTEMPTS),
            'error_code': saved['error_code']}


def read(conn, row):
    saved = conn.execute('SELECT * FROM native_request_dispatch WHERE request_id=?', (row['id'],)).fetchone()
    if saved is not None:
        try:
            text, digest, _ = _binding(conn, row)
            if (saved['binding_json'], saved['binding_sha256']) != (text, digest):
                raise DispatchConflict('dispatch identity changed')
        except (ValueError, TypeError):
            return {'state': 'unavailable', 'attempt': saved['attempts'], 'can_retry': False,
                    'error_code': 'identity_changed'}
    return _public(saved)


def _current(conn, request_id):
    raw = conn.execute('SELECT * FROM user_requests WHERE id=?', (request_id,)).fetchone()
    if raw is None:
        raise DispatchConflict('request not found')
    row = dict(raw)
    user = conn.execute('SELECT enabled FROM users WHERE id=?', (row['user_id'],)).fetchone()
    if row['status'] != 'approved' or user is None or not user['enabled']:
        raise DispatchConflict('request no longer eligible')
    return row


def _claim(request_id, expected_attempt):
    import db
    with db._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = _current(conn, request_id)
            text, digest, scope = _binding(conn, row)
            saved = conn.execute('SELECT * FROM native_request_dispatch WHERE request_id=?', (request_id,)).fetchone()
            if saved is None or (saved['binding_json'], saved['binding_sha256']) != (text, digest):
                raise DispatchConflict('dispatch identity unavailable')
            initial = ((expected_attempt is None or (type(expected_attempt) is int and expected_attempt == 0))
                       and saved['state'] == 'ready' and saved['attempts'] == 0)
            retry = (type(expected_attempt) is int and expected_attempt > 0
                     and expected_attempt == saved['attempts'] and saved['state'] == 'pre_effect_failed'
                     and saved['pre_effect_proof'] == 'handoff_not_entered')
            if not (initial or retry) or saved['attempts'] >= MAX_ATTEMPTS:
                raise DispatchConflict('dispatch retry not permitted')
            attempt = saved['attempts'] + 1
            changed = conn.execute("UPDATE native_request_dispatch SET state='preparing',attempts=?,pre_effect_proof=NULL,error_code=NULL,updated_at=strftime('%Y-%m-%d %H:%M:%S','now') WHERE request_id=? AND state=? AND attempts=?",
                                   (attempt, request_id, saved['state'], saved['attempts'])).rowcount
            if changed != 1:
                raise DispatchConflict('dispatch attempt changed')
            conn.execute('COMMIT')
            return row, scope, attempt, digest
        except BaseException:
            conn.execute('ROLLBACK')
            raise


def _prepare(row, scope):
    """Pure local preparation only. It must never call an adapter or write a monitor."""
    if not re.fullmatch(r'tt[0-9]{7,10}', row['imdb_id'] or ''):
        raise ValueError('metadata identity unresolved')
    if not isinstance(row['title'], str) or not row['title'].strip():
        raise ValueError('title unavailable')
    mode, selected, cutoff = scopes.dispatch_args(scopes.validate(scope, row['media_type']))
    return (row['title'], row['imdb_id'], row['media_type'], row['tmdb_id'], mode, selected, cutoff)


def _before_handoff(request_id, attempt, digest):
    import db
    with db._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = _current(conn, request_id)
            _, current_digest, _ = _binding(conn, row)
            if current_digest != digest:
                raise DispatchConflict('dispatch identity changed')
            changed = conn.execute("UPDATE native_request_dispatch SET state='unknown',updated_at=strftime('%Y-%m-%d %H:%M:%S','now') WHERE request_id=? AND state='preparing' AND attempts=? AND binding_sha256=?",
                                   (request_id, attempt, digest)).rowcount
            if changed != 1:
                raise DispatchConflict('dispatch attempt changed')
            # Persist unknown BEFORE invoking anything that can have effects.
            conn.execute('COMMIT')
        except BaseException:
            conn.execute('ROLLBACK')
            raise


def _finish(request_id, attempt, previous, state, error=None, proof=None):
    import db
    with db._connect() as conn:
        changed = conn.execute("UPDATE native_request_dispatch SET state=?,error_code=?,pre_effect_proof=?,updated_at=strftime('%Y-%m-%d %H:%M:%S','now') WHERE request_id=? AND attempts=? AND state=?",
                               (state, error, proof, request_id, attempt, previous)).rowcount
        return changed == 1


def run(request_id, dispatcher, *, expected_attempt=None):
    """One handoff at most; no automatic retry, lease expiry or unknown reclamation."""
    import db
    row, scope, attempt, digest = _claim(request_id, expected_attempt)
    try:
        args = _prepare(row, scope)
        _before_handoff(request_id, attempt, digest)
    except Exception:
        # This invocation never entered dispatcher. CAS cannot downgrade unknown.
        _finish(request_id, attempt, 'preparing', 'pre_effect_failed',
                'preparation_failed', 'handoff_not_entered')
    else:
        try:
            dispatcher(*args)
        except Exception:
            # Even a seemingly early dispatcher exception may follow partial effects.
            _finish(request_id, attempt, 'unknown', 'unknown', 'handoff_failed')
        else:
            _finish(request_id, attempt, 'unknown', 'accepted')
    with db._connect() as conn:
        current = conn.execute('SELECT * FROM user_requests WHERE id=?', (request_id,)).fetchone()
        return read(conn, dict(current))
