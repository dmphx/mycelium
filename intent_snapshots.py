"""Pure, strict admission snapshot for request intents (no database access).

Builds and validates the immutable metadata snapshot, the internal accepted
predicate and their hashes from a B2a resolved dict plus the B1 normalized input.
The resolved dict must come from the server-side resolver; this module checks its
shape and consistency, it cannot tell who produced it. Everything here is internal
and pending the final wire contract; nothing implies registration or playability.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date

MAX_EPISODES = 5000
MAX_TITLE = 300
MAX_RUNTIME_MIN = 1440
_IMDB = re.compile(r"tt[0-9]{6,10}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_BAD_TEXT = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
RESOLVED_KEYS = {"media", "scope", "title", "imdb_id", "year", "cutoff", "episodes"}
EPISODE_KEYS = {"tmdb_episode_id", "season", "episode", "name", "air_date", "runtime_min", "air_state"}

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS request_metadata_snapshot (
        request_id     INTEGER PRIMARY KEY REFERENCES request_intents(request_id) ON DELETE CASCADE,
        snapshot_json  TEXT    NOT NULL,
        predicate_json TEXT    NOT NULL,
        snapshot_hash  TEXT    NOT NULL,
        predicate_hash TEXT    NOT NULL,
        accepted_hash  TEXT    NOT NULL,
        input_hash     TEXT    NOT NULL,
        cutoff         TEXT    NOT NULL,
        catalog_count  INTEGER NOT NULL,
        created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now'))
    )""",
    """CREATE TABLE IF NOT EXISTS request_episode_state (
        request_id       INTEGER NOT NULL REFERENCES request_intents(request_id) ON DELETE CASCADE,
        season           INTEGER NOT NULL,
        episode          INTEGER NOT NULL,
        tmdb_episode_id  INTEGER NOT NULL,
        name             TEXT    NOT NULL,
        runtime_min      INTEGER,
        air_date         TEXT,
        state            TEXT    NOT NULL,
        updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now')),
        PRIMARY KEY (request_id, season, episode),
        UNIQUE (request_id, tmdb_episode_id)
    )""",
    """CREATE TABLE IF NOT EXISTS request_episode_observation (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id       INTEGER NOT NULL REFERENCES request_intents(request_id) ON DELETE CASCADE,
        season           INTEGER NOT NULL,
        episode          INTEGER NOT NULL,
        tmdb_episode_id  INTEGER NOT NULL,
        name             TEXT    NOT NULL,
        runtime_min      INTEGER,
        air_date         TEXT,
        source           TEXT    NOT NULL DEFAULT 'admission',
        observed_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now'))
    )""",
    """CREATE INDEX IF NOT EXISTS idx_request_episode_obs ON request_episode_observation(request_id, season, episode)""",
    """CREATE TRIGGER IF NOT EXISTS trg_req_snapshot_no_update BEFORE UPDATE ON request_metadata_snapshot
        BEGIN SELECT RAISE(ABORT, 'request snapshot is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_req_snapshot_guard_delete BEFORE DELETE ON request_metadata_snapshot
        WHEN EXISTS (SELECT 1 FROM request_intents WHERE request_id = OLD.request_id)
        BEGIN SELECT RAISE(ABORT, 'request snapshot is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_req_epstate_identity BEFORE UPDATE ON request_episode_state
        WHEN NEW.request_id IS NOT OLD.request_id OR NEW.season IS NOT OLD.season
          OR NEW.episode IS NOT OLD.episode OR NEW.tmdb_episode_id IS NOT OLD.tmdb_episode_id
          OR NEW.name IS NOT OLD.name OR NEW.runtime_min IS NOT OLD.runtime_min
          OR NEW.air_date IS NOT OLD.air_date
        BEGIN SELECT RAISE(ABORT, 'episode identity is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_req_epstate_guard_delete BEFORE DELETE ON request_episode_state
        WHEN EXISTS (SELECT 1 FROM request_intents WHERE request_id = OLD.request_id)
        BEGIN SELECT RAISE(ABORT, 'episode identity is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_req_obs_no_update BEFORE UPDATE ON request_episode_observation
        BEGIN SELECT RAISE(ABORT, 'observations are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_req_obs_guard_delete BEFORE DELETE ON request_episode_observation
        WHEN EXISTS (SELECT 1 FROM request_intents WHERE request_id = OLD.request_id)
        BEGIN SELECT RAISE(ABORT, 'observations are append-only'); END""",
)


class SnapshotError(Exception):
    def __init__(self, code: str, status: int, message: str = ""):
        super().__init__(message or code)
        self.code, self.status, self.message = code, status, message or code


def _bad(msg: str, code: str = "invalid_metadata", status: int = 400):
    raise SnapshotError(code, status, msg)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(v, what: str) -> str:
    if not isinstance(v, str) or not v.strip() or len(v.strip()) > MAX_TITLE \
            or _BAD_TEXT.search(v):
        _bad(f"bad {what}")
    return v.strip()


def _iso(v, what: str, nullable: bool = False) -> str | None:
    if v is None and nullable:
        return None
    if not isinstance(v, str) or not _DATE.fullmatch(v):
        _bad(f"bad {what}")
    try:
        date.fromisoformat(v)
    except ValueError:
        _bad(f"bad {what}")
    return v


def check_title_imdb(title, imdb_id) -> dict:
    if not isinstance(imdb_id, str) or not _IMDB.fullmatch(imdb_id):
        _bad("bad imdb_id")
    return {"title": _text(title, "title"), "imdb_id": imdb_id}


def predicate_for(normalized: dict, cutoff: str) -> dict:
    scope = normalized["scope"]
    kind = scope["kind"]
    if kind == "movie":
        return {"kind": "movie"}
    monitoring = {"episodes": "none", "selected_seasons": "selected_seasons"}.get(
        kind, "all_regular_seasons")
    pred = {"input_scope": scope, "numbering": "tmdb", "monitoring": monitoring,
            "specials": "explicit_only"}
    if kind == "future":
        pred["starts_on"] = cutoff
    return pred


def build(resolved, normalized: dict, input_hash: str) -> dict:
    """Validate `resolved` against `normalized` and return the internal snapshot.

    Returns {title, imdb_id, cutoff, snapshot, snapshot_json, predicate,
    predicate_json, snapshot_hash, predicate_hash, accepted_hash, input_hash,
    episodes:[... with initial state]}. Raises SnapshotError (400 invalid_metadata,
    503 catalog_too_large) before anything is written."""
    if not isinstance(resolved, dict) or set(resolved) != RESOLVED_KEYS:
        _bad("resolved metadata has the wrong shape")
    # Python equality treats True and 1 (and 1.0) as equal. Revalidate identities
    # with the strict input rules before comparing their normalized meaning.
    import request_intents
    try:
        observed_input = request_intents.normalize_payload({
            "media": resolved["media"], "scope": resolved["scope"]})
    except request_intents.IntentError:
        _bad("resolved metadata has invalid request identifiers")
    media, scope = normalized["media"], normalized["scope"]
    if observed_input != normalized:
        _bad("resolved metadata does not match the request")
    md = check_title_imdb(resolved["title"], resolved["imdb_id"])
    cutoff = _iso(resolved["cutoff"], "cutoff")
    year = resolved["year"]
    if year is not None and (not _is_int(year) or not 1 <= year <= 9999):
        _bad("bad year")
    eps = resolved["episodes"]
    if not isinstance(eps, list):
        _bad("episodes must be a list")
    if len(eps) > MAX_EPISODES:
        _bad("catalog too large", "catalog_too_large", 503)
    kind = scope["kind"]
    if media["type"] == "movie" and eps:
        _bad("a movie has no episodes")
    pairs, ids, rows = set(), set(), []
    for e in eps:
        if not isinstance(e, dict) or set(e) != EPISODE_KEYS:
            _bad("bad episode shape")
        eid, s, n = e["tmdb_episode_id"], e["season"], e["episode"]
        if not (_is_int(eid) and 0 < eid <= 2**31 - 1 and _is_int(s) and 0 <= s <= 999
                and _is_int(n) and 1 <= n <= 9999):
            _bad("bad episode identity")
        if (s, n) in pairs or eid in ids:
            _bad("duplicate episode")
        pairs.add((s, n))
        ids.add(eid)
        rt = e["runtime_min"]
        if rt is not None and (not _is_int(rt) or not 0 <= rt <= MAX_RUNTIME_MIN):
            _bad("bad runtime")
        ad = _iso(e["air_date"], "air_date", nullable=True)
        want = "unknown" if ad is None else ("aired" if ad <= cutoff else "unaired")
        if e["air_state"] != want:
            _bad("air_state does not match air_date")
        if kind == "episodes":
            pass
        elif kind == "selected_seasons":
            if s not in scope["seasons"]:
                _bad("episode outside the selected seasons")
        else:  # all_seasons, future: regular seasons only
            if s < 1:
                _bad("specials are not part of this scope")
        if kind == "future" and ad is not None and ad < cutoff:
            _bad("future scope never includes back catalog")
        state = {"unknown": "waiting_airdate", "unaired": "unaired", "aired": "pending"}[want]
        rows.append({"season": s, "episode": n, "tmdb_episode_id": eid,
                     "name": _text(e["name"], "episode name"), "runtime_min": rt,
                     "air_date": ad, "state": state})
    if kind == "episodes":
        if pairs != {(x["season"], x["episode"]) for x in scope["episodes"]}:
            _bad("resolved episodes differ from the requested pairs")
    rows.sort(key=lambda r: (r["season"], r["episode"]))
    snapshot = {"media": media, "title": md["title"], "imdb_id": md["imdb_id"], "year": year,
                "cutoff": cutoff,
                "episodes": [{k: r[k] for k in ("season", "episode", "tmdb_episode_id", "name",
                                                "runtime_min", "air_date")} for r in rows]}
    predicate = predicate_for(normalized, cutoff)
    snapshot_json, predicate_json = canon(snapshot), canon(predicate)
    snapshot_hash, predicate_hash = sha(snapshot_json), sha(predicate_json)
    accepted = sha(canon({"predicate_hash": predicate_hash, "snapshot_hash": snapshot_hash}))
    return {"title": md["title"], "imdb_id": md["imdb_id"], "cutoff": cutoff,
            "snapshot": snapshot, "snapshot_json": snapshot_json, "predicate": predicate,
            "predicate_json": predicate_json, "snapshot_hash": snapshot_hash,
            "predicate_hash": predicate_hash, "accepted_hash": accepted,
            "input_hash": input_hash, "episodes": rows}
