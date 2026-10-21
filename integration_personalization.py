"""Owned, non-automatic saved people and historical personalization receipts.

This source is deliberately separate from legacy automatic favorites and watchlist
consumers. No requests, quota, outbox, library status or grants belong here.
"""
import contextlib
import hashlib
import json
import re
import sqlite3

import integration_auth as ia
import integration_catalog as ic
import integration_catalog_service as catalog

MAX_SAVED_PEOPLE = 1000
LIST_LIMIT = 200
MAX_OWNER_RESULTS = 100000
MAX_GLOBAL_RESULTS = 1000000
_KEY = re.compile(r"[A-Za-z0-9_-]{16,128}")
_HASH = re.compile(r"[0-9a-f]{64}")
_PROFILE = re.compile(r"https://image\.tmdb\.org/t/p/w185/[A-Za-z0-9_-]{1,100}\.(?:jpg|jpeg|png)")
_GUARDS = ("integration_results_no_update", "integration_results_no_delete",
           "integration_results_no_replace")

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS integration_saved_people (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        person_id INTEGER NOT NULL CHECK(person_id BETWEEN 1 AND 2147483647),
        name TEXT NOT NULL,
        profile_url TEXT,
        added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        PRIMARY KEY(user_id, person_id)
    )""",
    """CREATE TABLE IF NOT EXISTS integration_personalization_results (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        idem_key TEXT NOT NULL,
        resource TEXT NOT NULL CHECK(resource IN ('follows','watchlist')),
        action TEXT NOT NULL CHECK(action IN ('POST','DELETE')),
        identity_json TEXT NOT NULL,
        semantic_hash TEXT NOT NULL,
        raw_body_sha256 TEXT NOT NULL,
        response_status INTEGER NOT NULL CHECK(response_status=200),
        response_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        PRIMARY KEY(user_id, idem_key)
    )""",
    """CREATE TRIGGER IF NOT EXISTS integration_results_no_update
        BEFORE UPDATE ON integration_personalization_results
        BEGIN SELECT RAISE(ABORT,'immutable personalization result'); END""",
    """CREATE TRIGGER IF NOT EXISTS integration_results_no_delete
        BEFORE DELETE ON integration_personalization_results
        WHEN EXISTS(SELECT 1 FROM users WHERE id=OLD.user_id)
        BEGIN SELECT RAISE(ABORT,'retained personalization result'); END""",
    """CREATE TRIGGER IF NOT EXISTS integration_results_no_replace
        BEFORE INSERT ON integration_personalization_results
        WHEN EXISTS(SELECT 1 FROM integration_personalization_results
                    WHERE user_id=NEW.user_id AND idem_key=NEW.idem_key)
        BEGIN SELECT RAISE(ABORT,'immutable personalization result'); END""",
)


def init_schema(conn):
    """Additive only. Caller owns its connection; no commit, data copy or migration."""
    for statement in _SCHEMA:
        conn.execute(statement)


def _unavailable(retryable=False):
    raise ia.IntegrationError("personalization_unavailable", 503,
                              "personalization temporarily unavailable", retryable)


def _bad():
    raise ia.IntegrationError("bad_request", 400, "invalid personalization input")


def _id(value):
    if type(value) is not int or not 1 <= value <= ic.MAX_ID:
        _bad()
    return value


def _owner_id(value):
    if type(value) is not int or not 1 <= value <= 9223372036854775807:
        raise ia.IntegrationError("disabled", 403, "account disabled")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _binding(resource, action, identity, key):
    if type(key) is not str or not _KEY.fullmatch(key) or action not in ("POST", "DELETE"):
        _bad()
    if type(identity) is not dict:
        _bad()
    if resource == "follows":
        if set(identity) != {"person_id"}:
            _bad()
        _id(identity["person_id"])
    elif resource == "watchlist":  # Namespace reserved; no watchlist mutation is implemented.
        if set(identity) != {"media"} or type(identity["media"]) is not dict \
                or set(identity["media"]) != {"type", "tmdb_id"} \
                or identity["media"]["type"] not in ("movie", "tv"):
            _bad()
        _id(identity["media"]["tmdb_id"])
    else:
        _bad()
    semantic = _json({"resource": resource, "action": action, "identity": identity})
    return hashlib.sha256(semantic.encode("ascii")).hexdigest()


@contextlib.contextmanager
def _transaction(write=False):
    import db
    try:
        with db._connect() as conn:
            if conn.in_transaction:
                _unavailable()  # Never commit or roll back somebody else's transaction.
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
    except (sqlite3.Error, OSError) as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 255
        _unavailable(code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
                             sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_FULL))


def _enabled(conn, user_id):
    _owner_id(user_id)
    user = conn.execute("SELECT enabled FROM users WHERE id=?", (user_id,)).fetchone()
    if user is None or user["enabled"] != 1:
        raise ia.IntegrationError("disabled", 403, "account disabled")


def _person(row):
    """Validate persisted allowlisted fields as well as newly resolved metadata."""
    try:
        pid = row["person_id"]
        if type(pid) is not int or not 1 <= pid <= ic.MAX_ID:
            _unavailable()
        out = {"person_id": pid, "name": ic.short_text(row["name"], "bad_person")}
        profile = row.get("profile_url")
        if profile is not None:
            if type(profile) is not str or not _PROFILE.fullmatch(profile):
                _unavailable()
            out["profile_url"] = profile
        return out
    except (KeyError, TypeError, ic.CatalogUnavailable):
        _unavailable()


def _member(conn, user_id, person_id):
    row = conn.execute("SELECT person_id,name,profile_url FROM integration_saved_people "
                       "WHERE user_id=? AND person_id=?", (user_id, person_id)).fetchone()
    return None if row is None else _person(dict(row))


def _result(conn, user_id, resource, action, identity, key, semantic):
    row = conn.execute("SELECT * FROM integration_personalization_results WHERE user_id=? AND idem_key=?",
                       (user_id, key)).fetchone()
    if row is None:
        return None
    try:
        stored_identity = ia.strict_json_object(row["identity_json"].encode("utf-8"))
        stored_semantic = _binding(row["resource"], row["action"], stored_identity, row["idem_key"])
        if stored_semantic != row["semantic_hash"] or not _HASH.fullmatch(row["raw_body_sha256"]) \
                or row["response_status"] != 200:
            _unavailable()
        data = ia.strict_json_object(row["response_json"].encode("utf-8"))
        if row["resource"] == "follows":
            if set(data) != {"person_id", "following", "automatic_requests"} \
                    or type(data["person_id"]) is not int \
                    or data["person_id"] != stored_identity["person_id"] \
                    or type(data["following"]) is not bool \
                    or data["following"] != (row["action"] == "POST") \
                    or data["automatic_requests"] is not False:
                _unavailable()
        else:
            # Future watchlist writers must validate their own result shape before qualification.
            _unavailable()
    except (ValueError, TypeError, AttributeError, KeyError, IndexError, ia.IntegrationError) as exc:
        if isinstance(exc, ia.IntegrationError) and exc.status == 503:
            raise
        _unavailable()
    if stored_semantic != semantic:
        raise ia.IntegrationError("idempotency_conflict", 409, "idempotency key conflicts")
    return data


def replay_lookup(user_id, resource, action, identity, key):
    """Historical result in a native-enabled read transaction, without metadata I/O."""
    semantic = _binding(resource, action, identity, key)
    with _transaction() as conn:
        _enabled(conn, user_id)
        return _result(conn, user_id, resource, action, identity, key, semantic)


def list_saved_people(user_id):
    with _transaction() as conn:
        _enabled(conn, user_id)
        rows = conn.execute("SELECT person_id,name,profile_url FROM integration_saved_people "
                            "WHERE user_id=? ORDER BY person_id LIMIT ?",
                            (user_id, LIST_LIMIT + 1)).fetchall()
        people = [_person(dict(row)) for row in rows]
        data = {"items": people[:LIST_LIMIT], "complete": len(rows) <= LIST_LIMIT,
                "automatic_requests": False}
        ic.serialize(data)
        return data


def _capacity(conn, user_id):
    owner = conn.execute("SELECT COUNT(*) FROM integration_personalization_results WHERE user_id=?",
                         (user_id,)).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM integration_personalization_results").fetchone()[0]
    if owner >= MAX_OWNER_RESULTS or total >= MAX_GLOBAL_RESULTS:
        _unavailable()


def _membership_capacity(conn, user_id):
    count = conn.execute("SELECT COUNT(*) FROM integration_saved_people WHERE user_id=?",
                         (user_id,)).fetchone()[0]
    if count >= MAX_SAVED_PEOPLE:
        _unavailable()


def _ledger_health(conn):
    conn.execute("SELECT user_id,idem_key,resource,action,identity_json,semantic_hash,"
                 "raw_body_sha256,response_status,response_json,created_at "
                 "FROM integration_personalization_results LIMIT 0")
    guards = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    if not set(_GUARDS) <= guards:
        _unavailable()


def mutate_saved_person(user_id, action, person_id, payload, key, body_sha256, *, fetch=None):
    """Commit membership and immutable success receipt together. No automatic requests."""
    _id(person_id)
    if type(payload) is not dict or payload or type(body_sha256) is not str \
            or not _HASH.fullmatch(body_sha256):
        _bad()
    identity = {"person_id": person_id}
    semantic = _binding("follows", action, identity, key)
    with _transaction() as conn:
        _enabled(conn, user_id)
        replay = _result(conn, user_id, "follows", action, identity, key, semantic)
        if replay is not None:
            return replay
        _ledger_health(conn)
        member = _member(conn, user_id, person_id)
        _capacity(conn, user_id)
        if action == "POST" and member is None:
            _membership_capacity(conn, user_id)
    if action == "POST" and member is None:
        resolved = catalog.execute("people.read", f"/integration/v1/people/{person_id}", {},
                                   (str(person_id),), fetch=fetch)
        member = _person(resolved)
    with _transaction(write=True) as conn:
        _enabled(conn, user_id)
        replay = _result(conn, user_id, "follows", action, identity, key, semantic)
        if replay is not None:
            return replay
        _ledger_health(conn)
        current = _member(conn, user_id, person_id)
        _capacity(conn, user_id)
        if action == "POST" and current is None:
            _membership_capacity(conn, user_id)
            conn.execute("INSERT INTO integration_saved_people(user_id,person_id,name,profile_url) "
                         "VALUES (?,?,?,?)", (user_id, person_id, member["name"], member.get("profile_url")))
        elif action == "DELETE":
            conn.execute("DELETE FROM integration_saved_people WHERE user_id=? AND person_id=?",
                         (user_id, person_id))
        data = {"person_id": person_id, "following": action == "POST", "automatic_requests": False}
        conn.execute("""INSERT INTO integration_personalization_results
                     (user_id,idem_key,resource,action,identity_json,semantic_hash,
                      raw_body_sha256,response_status,response_json)
                     VALUES (?,?,'follows',?,?,?,?,200,?)""",
                     (user_id, key, action, _json(identity), semantic, body_sha256, _json(data)))
        return data


def operations(user_id, *, metadata_ready=False):
    """Local source readiness only; preserve unrelated capabilities on failure."""
    supported = set()
    try:
        list_saved_people(user_id)
        supported.add("follows.read")
        if metadata_ready:
            with _transaction() as conn:
                _enabled(conn, user_id)
                _ledger_health(conn)
                supported.add("follows.write")
    except ia.IntegrationError:
        pass
    return supported
