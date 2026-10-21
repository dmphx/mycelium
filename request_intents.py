"""Durable per-user request intents: strict payloads, idempotent admission, quota,
review and a leased dispatch outbox.

Foundation only. It has no routes, no metadata resolver and no executor. Callers
resolve trusted metadata (title, imdb_id) themselves, outside any transaction, and
pass it to admit(). Every multi-statement operation here runs in one explicit
BEGIN IMMEDIATE transaction because db._connect() is autocommit.

The request_id is the existing user_requests.id. Media, scope and the future
cutoff date are immutable once admitted (enforced by triggers as well as code).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone

MAX_TMDB_ID = 2**31 - 1
MAX_SELECTED_SEASONS = 100
MAX_EPISODES = 500
SCOPE_KINDS = ("movie", "all_seasons", "selected_seasons", "future", "episodes")
_KEY_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
_IMDB_RE = re.compile(r"tt[0-9]{6,10}")
FINISH_STAGES = ("dispatched", "registered", "watching")
DEFER_STAGES = ("awaiting_metadata", "waiting_release", "rate_limited")
_UNKNOWN = {"state": "unknown", "entitlement": "unknown", "lookup": "unknown",
            "playability": "unknown"}


class IntentError(Exception):
    def __init__(self, code: str, status: int, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


# ── schema ───────────────────────────────────────────────────────────────────

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS request_intents (
        request_id       INTEGER PRIMARY KEY REFERENCES user_requests(id) ON DELETE CASCADE,
        user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        media_type       TEXT    NOT NULL,
        tmdb_id          INTEGER NOT NULL,
        imdb_id          TEXT    NOT NULL,
        title            TEXT    NOT NULL,
        scope_kind       TEXT    NOT NULL,
        scope_json       TEXT    NOT NULL,
        scope_hash       TEXT    NOT NULL,
        future_cutoff    TEXT    NOT NULL,
        outcome          TEXT    NOT NULL DEFAULT 'created',
        active           INTEGER NOT NULL DEFAULT 1,
        processing_state TEXT    NOT NULL DEFAULT 'awaiting_approval',
        created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now'))
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_request_intents_active
        ON request_intents(user_id, media_type, tmdb_id, scope_hash) WHERE active = 1""",
    """CREATE INDEX IF NOT EXISTS idx_request_intents_user
        ON request_intents(user_id, request_id DESC)""",
    """CREATE TABLE IF NOT EXISTS request_idempotency (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        idem_key      TEXT    NOT NULL,
        semantic_hash TEXT    NOT NULL,
        raw_hash      TEXT,
        request_id    INTEGER NOT NULL REFERENCES user_requests(id) ON DELETE CASCADE,
        outcome       TEXT    NOT NULL,
        created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now')),
        UNIQUE(user_id, idem_key)
    )""",
    """CREATE TABLE IF NOT EXISTS request_outbox (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id       INTEGER NOT NULL UNIQUE REFERENCES user_requests(id) ON DELETE CASCADE,
        state            TEXT    NOT NULL DEFAULT 'ready',
        attempts         INTEGER NOT NULL DEFAULT 0,
        lease_token      TEXT,
        lease_expires_at REAL,
        next_attempt_at  REAL    NOT NULL DEFAULT 0,
        last_error       TEXT,
        created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now')),
        updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now'))
    )""",
    """CREATE INDEX IF NOT EXISTS idx_request_outbox_state ON request_outbox(state, next_attempt_at)""",
    """CREATE TRIGGER IF NOT EXISTS trg_request_intents_immutable
        BEFORE UPDATE ON request_intents
        WHEN NEW.request_id IS NOT OLD.request_id OR NEW.user_id IS NOT OLD.user_id
          OR NEW.media_type IS NOT OLD.media_type OR NEW.tmdb_id IS NOT OLD.tmdb_id
          OR NEW.imdb_id IS NOT OLD.imdb_id OR NEW.title IS NOT OLD.title
          OR NEW.scope_kind IS NOT OLD.scope_kind OR NEW.scope_json IS NOT OLD.scope_json
          OR NEW.scope_hash IS NOT OLD.scope_hash OR NEW.future_cutoff IS NOT OLD.future_cutoff
        BEGIN SELECT RAISE(ABORT, 'request intent is immutable'); END""",
)


def init_schema(conn) -> None:
    """Additive and idempotent: only CREATE ... IF NOT EXISTS, no data changes."""
    for stmt in _SCHEMA:
        conn.execute(stmt)


@contextlib.contextmanager
def _tx():
    import db
    with db._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


# ── payload ──────────────────────────────────────────────────────────────────

def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _bad(msg: str):
    raise IntentError("invalid_payload", 400, msg)


def _exact(obj, keys: set, label: str) -> None:
    if not isinstance(obj, dict):
        _bad(f"{label} must be an object")
    extra = set(obj) - keys
    if extra:
        _bad(f"{label} has unknown fields")


def normalize_payload(payload) -> dict:
    """Return the canonical {media, scope} dict or raise IntentError(400)."""
    _exact(payload, {"media", "scope"}, "payload")
    if "media" not in payload or "scope" not in payload:
        _bad("media and scope are required")
    media, scope = payload["media"], payload["scope"]
    _exact(media, {"type", "tmdb_id"}, "media")
    if media.get("type") not in ("movie", "tv"):
        _bad("media.type must be movie or tv")
    tid = media.get("tmdb_id")
    if not _is_int(tid) or not 1 <= tid <= MAX_TMDB_ID:
        _bad("media.tmdb_id must be a positive integer")
    _exact(scope, {"kind", "seasons", "episodes"}, "scope")
    kind = scope.get("kind")
    if kind not in SCOPE_KINDS:
        _bad("scope.kind is not supported")
    if (kind == "movie") != (media["type"] == "movie"):
        _bad("scope.kind does not fit media.type")
    if "seasons" in scope and kind != "selected_seasons":
        _bad("scope.seasons only applies to selected_seasons")
    if "episodes" in scope and kind != "episodes":
        _bad("scope.episodes only applies to episodes")
    out = {"kind": kind}
    if kind == "selected_seasons":
        seasons = scope.get("seasons")
        if not isinstance(seasons, list) or not seasons or len(seasons) > MAX_SELECTED_SEASONS:
            _bad("scope.seasons must be a list of 1 to 100 seasons")
        if not all(_is_int(s) and 0 <= s <= 999 for s in seasons):
            _bad("seasons must be integers from 0 to 999")
        if len(set(seasons)) != len(seasons):
            _bad("scope.seasons has duplicates")
        out["seasons"] = sorted(seasons)
    elif kind == "episodes":
        eps = scope.get("episodes")
        if not isinstance(eps, list) or not eps or len(eps) > MAX_EPISODES:
            _bad("scope.episodes must be a list of 1 to 500 episodes")
        pairs = []
        for e in eps:
            _exact(e, {"season", "episode"}, "episode")
            s, n = e.get("season"), e.get("episode")
            if not (_is_int(s) and 0 <= s <= 999 and _is_int(n) and 1 <= n <= 9999):
                _bad("episode season must be 0..999 and episode 1..9999")
            pairs.append((s, n))
        if len(set(pairs)) != len(pairs):
            _bad("scope.episodes has duplicates")
        out["episodes"] = [{"season": s, "episode": n} for s, n in sorted(pairs)]
    return {"media": {"type": media["type"], "tmdb_id": tid}, "scope": out}


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def semantic_hash(normalized: dict) -> str:
    return _sha(_canon(normalized))


def raw_signature_hash(raw_body: bytes) -> str:
    """SHA-256 of the exact request body bytes as received. Audit only."""
    if not isinstance(raw_body, (bytes, bytearray)):
        raise IntentError("invalid_payload", 400, "raw body must be bytes")
    return hashlib.sha256(bytes(raw_body)).hexdigest()


def _check_key(key) -> str:
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise IntentError("invalid_idempotency_key", 400,
                          "idempotency key must be 16 to 128 safe ASCII characters")
    return key


def _check_metadata(md) -> dict:
    if not isinstance(md, dict):
        raise IntentError("metadata_required", 400, "trusted metadata required")
    title, imdb = md.get("title"), md.get("imdb_id")
    if (not isinstance(title, str) or not title.strip() or len(title) > 300
            or not isinstance(imdb, str) or not _IMDB_RE.fullmatch(imdb)):
        raise IntentError("invalid_metadata", 400, "metadata needs title and imdb_id")
    return {"title": title.strip(), "imdb_id": imdb}


def _num(v, name: str, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
            or not lo <= v <= hi:
        raise IntentError("invalid_argument", 400, f"{name} must be a finite number in range")
    return float(v)


def _clock(now) -> float:
    """Wall clock. The now argument exists for tests only."""
    return time.time() if now is None else _num(now, "now", 0, 1e13)


def _rid(request_id) -> int:
    if not _is_int(request_id) or request_id < 1:
        raise IntentError("invalid_request_id", 400, "request id must be a positive integer")
    return request_id


# ── receipts ─────────────────────────────────────────────────────────────────

def _receipt(conn, request_id: int, outcome: str | None = None) -> dict | None:
    row = conn.execute(
        """SELECT i.*, r.status AS request_status
           FROM request_intents i JOIN user_requests r ON r.id = i.request_id
           WHERE i.request_id=?""", (request_id,)).fetchone()
    if row is None:
        return None
    scope = json.loads(row["scope_json"])
    if row["scope_kind"] == "future":
        scope["starts_on"] = row["future_cutoff"]
    status = row["request_status"]
    stage = {"pending": "awaiting_approval", "denied": "denied",
             "failed": "failed"}.get(status) or row["processing_state"]
    return {
        "request_id": row["request_id"],
        "media": {"type": row["media_type"], "tmdb_id": row["tmdb_id"], "title": row["title"]},
        "scope": scope,
        "request_status": row["request_status"],
        "processing": {"stage": stage},
        "availability": dict(_UNKNOWN),
        "outcome": outcome or row["outcome"],
    }


def _user(conn, user_id):
    if not _is_int(user_id):
        raise IntentError("user_required", 403, "native user required")
    u = conn.execute("SELECT id, role, enabled, quota_monthly, auto_approve FROM users WHERE id=?",
                     (user_id,)).fetchone()
    if u is None or not u["enabled"]:
        raise IntentError("user_required", 403, "native enabled user required")
    return u


def _replay(conn, user_id, key, sem) -> dict | None:
    row = conn.execute("SELECT * FROM request_idempotency WHERE user_id=? AND idem_key=?",
                       (user_id, key)).fetchone()
    if row is None:
        return None
    if row["semantic_hash"] != sem:
        raise IntentError("idempotency_conflict", 409,
                          "this idempotency key was used with a different request")
    return _receipt(conn, row["request_id"], row["outcome"])


def replay_lookup(user_id: int, payload, idempotency_key: str) -> dict | None:
    """Validate, check the owner is enabled, and return the original receipt for a
    semantic replay (None if the key is new). Safe to call before any network I/O;
    admit() repeats every check inside its transaction."""
    key = _check_key(idempotency_key)
    sem = semantic_hash(normalize_payload(payload))
    import db
    with db._connect() as conn:
        _user(conn, user_id)
        return _replay(conn, user_id, key, sem)


# ── admission ────────────────────────────────────────────────────────────────

def admit(user_id: int, payload, idempotency_key: str, metadata: dict | None = None,
          raw_body: bytes | None = None) -> dict:
    """Admit one intent. Returns a receipt; raises IntentError otherwise.

    metadata ({title, imdb_id}) must be server-resolved. It is only needed when a
    new request row is created, so replays and coalesced requests do not need it.
    raw_body is the exact verified request body bytes, or None when the caller has
    none; the stored raw hash is then NULL and is never derived from the payload."""
    key = _check_key(idempotency_key)
    norm = normalize_payload(payload)
    sem = semantic_hash(norm)
    raw = None if raw_body is None else raw_signature_hash(raw_body)
    media, scope = norm["media"], norm["scope"]
    scope_json = _canon(scope)
    scope_hash = _sha(scope_json)
    for attempt in range(3):
        try:
            with _tx() as conn:
                return _admit_locked(conn, user_id, key, sem, raw, media, scope, scope_json,
                                     scope_hash, metadata)
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" not in str(exc):
                raise
            if attempt == 2:
                raise IntentError("conflict", 409, "concurrent admission, retry") from None
    raise AssertionError("unreachable")


def _admit_locked(conn, user_id, key, sem, raw, media, scope, scope_json, scope_hash, metadata):
    user = _user(conn, user_id)
    replay = _replay(conn, user_id, key, sem)
    if replay is not None:
        return replay
    existing = conn.execute(
        """SELECT request_id FROM request_intents
           WHERE user_id=? AND media_type=? AND tmdb_id=? AND scope_hash=? AND active=1""",
        (user_id, media["type"], media["tmdb_id"], scope_hash)).fetchone()
    if existing is not None:
        conn.execute(
            """INSERT INTO request_idempotency (user_id, idem_key, semantic_hash, raw_hash,
                                                request_id, outcome)
               VALUES (?, ?, ?, ?, ?, 'existing_request')""",
            (user_id, key, sem, raw, existing["request_id"]))
        return _receipt(conn, existing["request_id"], "existing_request")
    md = _check_metadata(metadata)
    is_admin = user["role"] == "admin"
    quota = int(user["quota_monthly"] or 0)
    if quota > 0 and not is_admin:
        used = conn.execute(
            """SELECT COUNT(*) AS n FROM user_requests
               WHERE user_id=? AND status <> 'denied'
                 AND created_at >= strftime('%Y-%m-01 00:00:00','now')""",
            (user_id,)).fetchone()["n"]
        if used >= quota:
            raise IntentError("quota_exceeded", 429, "monthly request quota reached")
    approved = is_admin or bool(user["auto_approve"])
    status = "approved" if approved else "pending"
    seasons_csv = (",".join(str(s) for s in scope["seasons"])
                   if scope["kind"] == "selected_seasons" else None)
    cur = conn.execute(
        """INSERT INTO user_requests (user_id, imdb_id, tmdb_id, media_type, title, seasons, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, md["imdb_id"], media["tmdb_id"], media["type"], md["title"], seasons_csv, status))
    rid = int(cur.lastrowid)
    cutoff = datetime.now(timezone.utc).date().isoformat()
    conn.execute(
        """INSERT INTO request_intents (request_id, user_id, media_type, tmdb_id, imdb_id, title,
               scope_kind, scope_json, scope_hash, future_cutoff, processing_state)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (rid, user_id, media["type"], media["tmdb_id"], md["imdb_id"], md["title"],
         scope["kind"], scope_json, scope_hash, cutoff,
         "queued" if approved else "awaiting_approval"))
    conn.execute(
        """INSERT INTO request_idempotency (user_id, idem_key, semantic_hash, raw_hash,
                                            request_id, outcome)
           VALUES (?, ?, ?, ?, ?, 'created')""",
        (user_id, key, sem, raw, rid))
    if approved:
        conn.execute("INSERT INTO request_outbox (request_id) VALUES (?)", (rid,))
    return _receipt(conn, rid, "created")


# ── review ───────────────────────────────────────────────────────────────────

def review(request_id: int, admin_id: int, approve: bool, note: str | None = None) -> dict:
    """Admin decision on a pending intent. Exactly one outbox job on approval."""
    _rid(request_id)
    if not isinstance(approve, bool):
        raise IntentError("invalid_payload", 400, "approve must be a boolean")
    with _tx() as conn:
        admin = conn.execute("SELECT role, enabled FROM users WHERE id=?",
                             (admin_id if _is_int(admin_id) else -1,)).fetchone()
        if admin is None or not admin["enabled"] or admin["role"] != "admin":
            raise IntentError("forbidden", 403, "enabled native admin required")
        if conn.execute("SELECT 1 FROM request_intents WHERE request_id=?",
                        (request_id,)).fetchone() is None:
            raise IntentError("not_found", 404, "request not found")
        new = "approved" if approve else "denied"
        cur = conn.execute(
            """UPDATE user_requests SET status=?, reviewed_by=?,
                      reviewed_at=strftime('%Y-%m-%d %H:%M:%S','now'), note=COALESCE(?, note)
               WHERE id=? AND status='pending'""",
            (new, admin_id, note[:500] if isinstance(note, str) else None, request_id))
        if cur.rowcount != 1:
            raise IntentError("already_reviewed", 409, "request is not pending")
        if approve:
            conn.execute("INSERT INTO request_outbox (request_id) VALUES (?)", (request_id,))
            conn.execute("UPDATE request_intents SET processing_state='queued' WHERE request_id=?",
                         (request_id,))
        else:
            conn.execute("UPDATE request_intents SET active=0, processing_state='denied' "
                         "WHERE request_id=?", (request_id,))
        return _receipt(conn, request_id)


# ── owner reads ──────────────────────────────────────────────────────────────

def get(user_id: int, request_id: int) -> dict | None:
    """The owner's receipt, or None (identical for missing and foreign ids)."""
    _rid(request_id)
    import db
    with db._connect() as conn:
        _user(conn, user_id)
        row = conn.execute("SELECT 1 FROM request_intents WHERE request_id=? AND user_id=?",
                           (request_id, user_id)).fetchone()
        return _receipt(conn, request_id) if row else None


def list_for_user(user_id: int, limit: int = 50, before_id: int | None = None) -> list[dict]:
    limit = int(_num(limit, "limit", 1, 200))
    if before_id is not None:
        _rid(before_id)
    import db
    with db._connect() as conn:
        _user(conn, user_id)
        rows = conn.execute(
            """SELECT request_id FROM request_intents
               WHERE user_id=? AND (? IS NULL OR request_id < ?)
               ORDER BY request_id DESC LIMIT ?""",
            (user_id, before_id, before_id, limit)).fetchall()
        return [_receipt(conn, r["request_id"]) for r in rows]


# ── outbox leases ────────────────────────────────────────────────────────────

_LIVE_LEASE = """SELECT o.request_id, o.attempts FROM request_outbox o
    JOIN request_intents i ON i.request_id = o.request_id
    JOIN user_requests r ON r.id = o.request_id AND r.status = 'approved'
    JOIN users u ON u.id = i.user_id AND u.enabled = 1
    WHERE o.id=? AND o.state='leased' AND o.lease_token=? AND o.lease_expires_at > ?"""


def _lease_args(outbox_id, lease_token):
    if not _is_int(outbox_id) or outbox_id < 1 or not isinstance(lease_token, str) \
            or not lease_token:
        raise IntentError("invalid_argument", 400, "bad outbox id or lease token")


def claim_next(lease_seconds: float = 60.0, now: float | None = None) -> dict | None:
    """Lease the oldest dispatchable job. Jobs of disabled owners or of requests
    no longer approved are skipped."""
    lease = _num(lease_seconds, "lease_seconds", 1, 3600)
    now = _clock(now)
    with _tx() as conn:
        row = conn.execute(
            """SELECT o.id, o.request_id, o.attempts, i.user_id, i.media_type, i.tmdb_id,
                      i.imdb_id, i.title, i.scope_json, i.future_cutoff
               FROM request_outbox o
               JOIN request_intents i ON i.request_id = o.request_id
               JOIN user_requests r ON r.id = o.request_id AND r.status = 'approved'
               JOIN users u ON u.id = i.user_id AND u.enabled = 1
               WHERE (o.state='ready' AND o.next_attempt_at <= ?)
                  OR (o.state='leased' AND o.lease_expires_at <= ?)
               ORDER BY o.id LIMIT 1""", (now, now)).fetchone()
        if row is None:
            return None
        token = uuid.uuid4().hex
        expires = now + lease
        conn.execute(
            """UPDATE request_outbox SET state='leased', lease_token=?, lease_expires_at=?,
                      attempts=attempts+1, updated_at=strftime('%Y-%m-%d %H:%M:%S','now')
               WHERE id=?""", (token, expires, row["id"]))
        conn.execute("UPDATE request_intents SET processing_state='dispatching' WHERE request_id=?",
                     (row["request_id"],))
        return {"outbox_id": row["id"], "request_id": row["request_id"], "lease_token": token,
                "lease_expires_at": expires, "attempt": row["attempts"] + 1,
                "user_id": row["user_id"],
                "media": {"type": row["media_type"], "tmdb_id": row["tmdb_id"]},
                "imdb_id": row["imdb_id"], "title": row["title"],
                "scope": json.loads(row["scope_json"]), "future_cutoff": row["future_cutoff"]}


def renew(outbox_id: int, lease_token: str, lease_seconds: float = 60.0,
          now: float | None = None) -> bool:
    _lease_args(outbox_id, lease_token)
    lease = _num(lease_seconds, "lease_seconds", 1, 3600)
    now = _clock(now)
    with _tx() as conn:
        if conn.execute(_LIVE_LEASE, (outbox_id, lease_token, now)).fetchone() is None:
            return False
        conn.execute("UPDATE request_outbox SET lease_expires_at=? WHERE id=?",
                     (now + lease, outbox_id))
        return True


def finish(outbox_id: int, lease_token: str, stage: str = "dispatched",
           now: float | None = None) -> bool:
    """Mark dispatch complete (not that media is available). Needs a current,
    unexpired lease, an enabled owner and a still-approved request; otherwise
    nothing changes."""
    _lease_args(outbox_id, lease_token)
    if stage not in FINISH_STAGES:
        raise IntentError("invalid_argument", 400, "stage not allowed")
    now = _clock(now)
    with _tx() as conn:
        row = conn.execute(_LIVE_LEASE, (outbox_id, lease_token, now)).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE request_outbox SET state='done', lease_token=NULL, "
                     "lease_expires_at=NULL, updated_at=strftime('%Y-%m-%d %H:%M:%S','now') "
                     "WHERE id=?", (outbox_id,))
        conn.execute("UPDATE request_intents SET processing_state=? WHERE request_id=?",
                     (stage, row["request_id"]))
        return True


def defer(outbox_id: int, lease_token: str, retry_after: float, stage: str,
          now: float | None = None) -> bool:
    """Requeue without spending the finite attempt budget (rate limit, metadata or
    release not ready). Same lease, owner and approval requirements as finish()."""
    _lease_args(outbox_id, lease_token)
    after = _num(retry_after, "retry_after", 0, 604800)
    if stage not in DEFER_STAGES:
        raise IntentError("invalid_argument", 400, "stage not allowed")
    now = _clock(now)
    with _tx() as conn:
        row = conn.execute(_LIVE_LEASE, (outbox_id, lease_token, now)).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE request_outbox SET state='ready', lease_token=NULL, "
                     "lease_expires_at=NULL, next_attempt_at=?, attempts=MAX(attempts-1, 0), "
                     "updated_at=strftime('%Y-%m-%d %H:%M:%S','now') WHERE id=?",
                     (now + after, outbox_id))
        conn.execute("UPDATE request_intents SET processing_state=? WHERE request_id=?",
                     (stage, row["request_id"]))
        return True


def fail(outbox_id: int, lease_token: str, error: str = "", retry_after: float = 60.0,
         max_attempts: int = 5, now: float | None = None) -> str | None:
    """Back off ('retry') or give up ('failed'). None when the lease is not current
    and unexpired, the owner is disabled, or the request is no longer approved."""
    _lease_args(outbox_id, lease_token)
    after = _num(retry_after, "retry_after", 0, 604800)
    if not _is_int(max_attempts) or not 1 <= max_attempts <= 100:
        raise IntentError("invalid_argument", 400, "max_attempts must be 1..100")
    now = _clock(now)
    with _tx() as conn:
        row = conn.execute(_LIVE_LEASE, (outbox_id, lease_token, now)).fetchone()
        if row is None:
            return None
        msg = str(error)[:200]
        if row["attempts"] >= max_attempts:
            conn.execute("UPDATE request_outbox SET state='failed', lease_token=NULL, "
                         "lease_expires_at=NULL, last_error=?, "
                         "updated_at=strftime('%Y-%m-%d %H:%M:%S','now') WHERE id=?",
                         (msg, outbox_id))
            conn.execute("UPDATE user_requests SET status='failed' WHERE id=?", (row["request_id"],))
            conn.execute("UPDATE request_intents SET active=0, processing_state='failed' "
                         "WHERE request_id=?", (row["request_id"],))
            return "failed"
        conn.execute("UPDATE request_outbox SET state='ready', lease_token=NULL, "
                     "lease_expires_at=NULL, next_attempt_at=?, last_error=?, "
                     "updated_at=strftime('%Y-%m-%d %H:%M:%S','now') WHERE id=?",
                     (now + after, msg, outbox_id))
        conn.execute("UPDATE request_intents SET processing_state='queued' WHERE request_id=?",
                     (row["request_id"],))
        return "retry"
