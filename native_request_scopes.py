"""Immutable native request scope snapshots, distinct from signed integration grants."""
import datetime as dt
import hashlib
import json


class ScopeError(ValueError):
    pass


def init_schema(conn):
    conn.executescript('''
CREATE TABLE IF NOT EXISTS native_request_scopes (
 request_id INTEGER PRIMARY KEY REFERENCES user_requests(id),
 user_id INTEGER NOT NULL, imdb_id TEXT NOT NULL, tmdb_id INTEGER NOT NULL,
 media_type TEXT NOT NULL, scope_json TEXT NOT NULL, scope_sha256 TEXT NOT NULL,
 raw_body_sha256 TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS native_request_scopes_immutable_update
 BEFORE UPDATE ON native_request_scopes BEGIN SELECT RAISE(ABORT,'immutable native scope'); END;
CREATE TRIGGER IF NOT EXISTS native_request_scopes_immutable_delete
 BEFORE DELETE ON native_request_scopes BEGIN SELECT RAISE(ABORT,'immutable native scope'); END;
''')


def utc_today():
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def canonical(scope):
    return json.dumps(scope, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def validate(scope, media_type):
    if type(scope) is not dict:
        raise ScopeError('native scope unavailable')
    kind = scope.get('kind')
    if media_type == 'movie' and scope == {'kind': 'movie'}:
        return dict(scope)
    if media_type != 'tv':
        raise ScopeError('native scope unavailable')
    if scope == {'kind': 'all_seasons'}:
        return dict(scope)
    if kind == 'future' and set(scope) == {'kind', 'starts_on'}:
        date = scope['starts_on']
        if type(date) is str:
            try:
                if dt.date.fromisoformat(date).isoformat() == date:
                    return dict(scope)
            except ValueError:
                pass
    if kind == 'selected_seasons' and set(scope) == {'kind', 'seasons'}:
        seasons = scope['seasons']
        if (type(seasons) is list and 1 <= len(seasons) <= 100
            and all(type(s) is int and 0 <= s <= 999 for s in seasons)
            and seasons == sorted(set(seasons))):
            return {'kind': kind, 'seasons': list(seasons)}
    raise ScopeError('native scope unavailable')


def prepare(payload):
    if type(payload) is not dict or set(payload) - {'tmdb_id','media_type','title','monitor_mode','seasons'}:
        raise ScopeError('invalid native request')
    tid = payload.get('tmdb_id')
    kind = payload.get('media_type') or 'movie'
    if type(tid) is not int or not 1 <= tid <= 9999999999 or kind not in ('tv','movie'):
        raise ScopeError('invalid native media')
    if type(payload.get('title')) is not str or not payload['title'].strip():
        raise ScopeError('invalid native title')
    if kind == 'movie':
        return {'kind':'movie'}
    mode = payload.get('monitor_mode', 'all')
    seasons = payload.get('seasons', [])
    if type(seasons) is not list or len(seasons) > 100 or any(type(s) is not int or not 0 <= s <= 999 for s in seasons) or len(set(seasons)) != len(seasons):
        raise ScopeError('invalid native seasons')
    if mode == 'selected':
        return validate({'kind':'selected_seasons','seasons':sorted(seasons)}, kind)
    if mode == 'all':
        return {'kind':'all_seasons'}
    if mode == 'future':
        return validate({'kind':'future','starts_on':utc_today()}, kind)
    raise ScopeError('invalid native monitor mode')


def store(conn, request_id, user_id, imdb_id, tmdb_id, media_type, scope, raw_body_sha256):
    normalized = validate(scope, media_type)
    text = canonical(normalized)
    if type(raw_body_sha256) is not str or len(raw_body_sha256) != 64 or any(c not in '0123456789abcdef' for c in raw_body_sha256):
        raise ScopeError('native body identity unavailable')
    conn.execute('INSERT INTO native_request_scopes VALUES (?,?,?,?,?,?,?,?)',
        (request_id,user_id,imdb_id,tmdb_id,media_type,text,hashlib.sha256(text.encode()).hexdigest(),raw_body_sha256))


def read(conn, request_row):
    record = conn.execute('SELECT * FROM native_request_scopes WHERE request_id=?',(request_row['id'],)).fetchone()
    if record is None:
        return None
    if any(record[key] != request_row[key] for key in ('user_id','imdb_id','tmdb_id','media_type')):
        raise ScopeError('native scope identity changed')
    text = record['scope_json']
    if hashlib.sha256(text.encode()).hexdigest() != record['scope_sha256']:
        raise ScopeError('native scope hash changed')
    try:
        scope = validate(json.loads(text), request_row['media_type'])
    except (ValueError, TypeError):
        raise ScopeError('native scope unavailable') from None
    if canonical(scope) != text:
        raise ScopeError('native scope encoding changed')
    return scope


def dispatch_args(scope):
    kind = scope['kind']
    return ('selected',list(scope['seasons']),None) if kind == 'selected_seasons' else (
        ('future',None,scope['starts_on']) if kind == 'future' else ('all',None,None))
