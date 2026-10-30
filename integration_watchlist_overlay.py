"""Internal immutable watchlist metadata preparation. No HTTP activation.

Native membership and its legacy consumers are never changed. Fingerprints prove
current source equality, not an event history or absence of intermediate changes.
Only an in-process issued object can be applied; reports/hashes are not credentials.
"""
import contextlib
import datetime
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import weakref

import integration_auth as ia
import integration_catalog as ic
import integration_catalog_service as catalog
import tmdb_checked as tc

MAX_NATIVE_ROWS = 100
MAX_FETCHES = 32
MAX_SECONDS = 60
MAX_PREPARATION_BYTES = 1024 * 1024
MAX_OWNER_PLANS = 16
MAX_GLOBAL_PLANS = 10000
MAX_GLOBAL_ENTRIES = 1000000
_COLUMNS = ("id", "user_id", "imdb_id", "tmdb_id", "media_type", "title", "poster_path", "added_at")
_IDENTITY = ("id", "user_id", "media_type", "imdb_id", "tmdb_id", "added_at")
_HASH = re.compile(r"[0-9a-f]{64}")
_IMDB = re.compile(r"tt[0-9]{6,10}")
_DATE_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")
_IMAGE = r"/[A-Za-z0-9_-]{1,100}\.(?:jpg|jpeg|png)"
_ISSUED = weakref.WeakKeyDictionary()
_ISSUED_LOCK = threading.Lock()
_TABLES = ("integration_watchlist_preparations", "integration_watchlist_overlay_entries")
_SELECTION_GUARDS = ("integration_watchlist_selection_no_delete",
                     "integration_watchlist_selection_no_replace",
                     "integration_watchlist_selection_revision")

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS integration_watchlist_preparations (
        plan_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        format_version INTEGER NOT NULL,
        identity_set_hash TEXT NOT NULL,
        source_snapshot_hash TEXT NOT NULL,
        row_count INTEGER NOT NULL,
        applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )""",
    """CREATE INDEX IF NOT EXISTS idx_watchlist_preparations_owner
        ON integration_watchlist_preparations(user_id)""",
    """CREATE TABLE IF NOT EXISTS integration_watchlist_overlay_entries (
        plan_hash TEXT NOT NULL REFERENCES integration_watchlist_preparations(plan_hash) ON DELETE CASCADE,
        watchlist_id INTEGER NOT NULL,
        row_source_hash TEXT NOT NULL,
        native_identity_json TEXT NOT NULL,
        media_type TEXT NOT NULL,
        tmdb_id INTEGER NOT NULL,
        imdb_id TEXT NOT NULL,
        title_json TEXT NOT NULL,
        title_hash TEXT NOT NULL,
        identity_proof_json TEXT NOT NULL,
        PRIMARY KEY(plan_hash,watchlist_id)
    )""",
    """CREATE TABLE IF NOT EXISTS integration_watchlist_selection (
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        plan_hash TEXT NOT NULL REFERENCES integration_watchlist_preparations(plan_hash),
        revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 9223372036854775807)
    )""",
    """CREATE TRIGGER IF NOT EXISTS integration_watchlist_selection_no_delete
        BEFORE DELETE ON integration_watchlist_selection
        WHEN EXISTS(SELECT 1 FROM users WHERE id=OLD.user_id)
        BEGIN SELECT RAISE(ABORT,'retained watchlist selection'); END""",
    """CREATE TRIGGER IF NOT EXISTS integration_watchlist_selection_no_replace
        BEFORE INSERT ON integration_watchlist_selection
        WHEN EXISTS(SELECT 1 FROM integration_watchlist_selection WHERE user_id=NEW.user_id)
        BEGIN SELECT RAISE(ABORT,'retained watchlist selection revision'); END""",
    """CREATE TRIGGER IF NOT EXISTS integration_watchlist_selection_revision
        BEFORE UPDATE ON integration_watchlist_selection
        WHEN NEW.user_id != OLD.user_id OR NEW.plan_hash = OLD.plan_hash
             OR NEW.revision != OLD.revision + 1
        BEGIN SELECT RAISE(ABORT,'invalid watchlist selection revision'); END""",
)


def init_schema(conn):
    """Additive only; caller owns connection. No FK to native watchlist rows."""
    for sql in _SCHEMA:
        conn.execute(sql)
    for table in _TABLES:
        for action, when, message in (
            ("UPDATE", "", "immutable watchlist overlay"),
            ("DELETE", "WHEN EXISTS(SELECT 1 FROM users WHERE id=" +
             ("OLD.user_id)" if table == _TABLES[0] else
              "(SELECT user_id FROM integration_watchlist_preparations WHERE plan_hash=OLD.plan_hash))"),
             "retained watchlist overlay"),
            ("INSERT", "WHEN EXISTS(SELECT 1 FROM " + table + " WHERE plan_hash=NEW.plan_hash" +
             (" AND watchlist_id=NEW.watchlist_id" if table == _TABLES[1] else "") + ")",
             "immutable watchlist overlay"),
        ):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} "
                         f"ON {table} {when} BEGIN SELECT RAISE(ABORT,'{message}'); END")


def _fail(code="watchlist_overlay_unavailable", status=503, retryable=False):
    raise ia.IntegrationError(code, status, "watchlist metadata preparation unavailable", retryable)


def _exact(obj, keys):
    if type(obj) is not dict or set(obj) != set(keys):
        _fail()


def _integer(value, high=ic.MAX_ID):
    if type(value) is not int or not 1 <= value <= high:
        _fail()
    return value


def _hash_value(value):
    if type(value) is not str or not _HASH.fullmatch(value):
        _fail()


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except (ValueError, TypeError, RecursionError, OverflowError):
        _fail()


def _hash(domain, value):
    return hashlib.sha256(b"watchlist-overlay:v1:" + domain.encode("ascii") + b"\0" + _canonical(value)).hexdigest()


def _decode(value):
    if type(value) is not str or len(value) > MAX_PREPARATION_BYTES:
        _fail()
    try:
        return tc.parse_json_object(value.encode("utf-8"))
    except (UnicodeError, tc.TmdbUnavailable):
        _fail()


@contextlib.contextmanager
def _transaction(write=False):
    import db
    try:
        with db._connect() as conn:
            if conn.in_transaction:
                _fail()
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
        code = (getattr(exc, "sqlite_errorcode", 0) or 0) & 255
        _fail(retryable=code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
                                 sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_FULL))


def _enabled(conn, user_id):
    if type(user_id) is not int or not 1 <= user_id <= 9223372036854775807:
        _fail("disabled", 403)
    row = conn.execute("SELECT enabled FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None or row["enabled"] != 1:
        _fail("disabled", 403)


def _identity(row, user_id):
    _integer(row["id"], 9223372036854775807)
    if type(row["user_id"]) is not int or row["user_id"] != user_id \
            or row["media_type"] not in ("movie", "tv") \
            or type(row["imdb_id"]) is not str or not _IMDB.fullmatch(row["imdb_id"]):
        _fail()
    if row["tmdb_id"] is not None:
        _integer(row["tmdb_id"])
    when = row["added_at"]
    if type(when) is not str or not _DATE_TIME.fullmatch(when):
        _fail()
    try:
        datetime.datetime.strptime(when, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        _fail()
    return {key: row[key] for key in _IDENTITY}


def _snapshot(conn, user_id):
    _enabled(conn, user_id)
    # Bound source copying before reading potentially large legacy text values.
    lengths = conn.execute("SELECT " + "+".join(
        f"COALESCE(length(CAST({key} AS BLOB)),0)" for key in _COLUMNS) +
        " AS bytes FROM watchlist WHERE user_id=? ORDER BY id LIMIT ?",
        (user_id, MAX_NATIVE_ROWS + 1)).fetchall()
    if len(lengths) > MAX_NATIVE_ROWS or sum(row["bytes"] for row in lengths) > MAX_PREPARATION_BYTES:
        _fail()
    rows = [dict(row) for row in conn.execute("SELECT " + ",".join(_COLUMNS) +
        " FROM watchlist WHERE user_id=? ORDER BY id LIMIT ?", (user_id, MAX_NATIVE_ROWS + 1))]
    identities, source = [], []
    for row in rows:
        identities.append(_identity(row, user_id))
        if type(row["title"]) is not str or row["poster_path"] is not None and type(row["poster_path"]) is not str:
            _fail()
        source.append({"id": row["id"], "row_source_hash": _hash("source-row", row)})
    return rows, _hash("identity-set", identities), _hash("source-set", source)


def _title(dto, kind, tid):
    """Reconstruct only raw Title fields, then require exact canonical DTO equality."""
    required = {"tmdb_id", "media_type", "title", "overview"}
    allowed = required | {"poster_url", "backdrop_url", "vote_average",
                          "release_date" if kind == "movie" else "first_air_date"}
    if type(dto) is not dict or not required <= dto.keys() or dto.keys() - allowed \
            or type(dto["tmdb_id"]) is not int or dto["tmdb_id"] != tid or dto["media_type"] != kind \
            or type(dto["overview"]) is not str:
        _fail()
    raw = {"id": tid, "media_type": kind, "title" if kind == "movie" else "name": dto["title"],
           "overview": dto["overview"]}
    for key, size, raw_key in (("poster_url", "w500", "poster_path"),
                               ("backdrop_url", "w1280", "backdrop_path")):
        if key in dto:
            prefix = "https://image.tmdb.org/t/p/" + size
            if type(dto[key]) is not str or not re.fullmatch(re.escape(prefix) + _IMAGE, dto[key]):
                _fail()
            raw[raw_key] = dto[key][len(prefix):]
    for key in ("vote_average", "release_date", "first_air_date"):
        if key in dto:
            raw[key] = dto[key]
    try:
        built = ic.title(raw, kind)
    except ic.CatalogUnavailable:
        _fail()
    if _canonical(dto) != _canonical(built):
        _fail()
    return built


def _fingerprints(entries):
    return (_hash("identity-set", [e["native_identity"] for e in entries]),
            _hash("source-set", [{"id": e["watchlist_id"], "row_source_hash": e["row_source_hash"]}
                                  for e in entries]))


def _validate(document):
    _exact(document, {"format_version", "user_id", "identity_set_hash", "source_snapshot_hash",
                      "row_count", "entries", "plan_hash"})
    if type(document["format_version"]) is not int or document["format_version"] != 1:
        _fail()
    owner = _integer(document["user_id"], 9223372036854775807)
    entries = document["entries"]
    if type(entries) is not list or len(entries) > MAX_NATIVE_ROWS \
            or type(document["row_count"]) is not int or document["row_count"] != len(entries):
        _fail()
    previous, seen = 0, set()
    for entry in entries:
        _exact(entry, {"watchlist_id", "row_source_hash", "native_identity", "media_type", "tmdb_id",
                       "imdb_id", "title", "title_hash", "identity_proof"})
        wid = _integer(entry["watchlist_id"], 9223372036854775807)
        if wid <= previous:
            _fail()
        previous = wid
        native = entry["native_identity"]
        _exact(native, _IDENTITY)
        _identity(native, owner)
        kind, tid = entry["media_type"], _integer(entry["tmdb_id"])
        if native["id"] != wid or kind != native["media_type"] or entry["imdb_id"] != native["imdb_id"] \
                or native["tmdb_id"] is not None and native["tmdb_id"] != tid or (kind, tid) in seen:
            _fail()
        seen.add((kind, tid))
        _hash_value(entry["row_source_hash"])
        _title(entry["title"], kind, tid)
        if entry["title_hash"] != _hash("title", entry["title"]):
            _fail()
        proof = entry["identity_proof"]
        _exact(proof, {"media_type", "detail_id", "external_parent_id", "external_imdb_id",
                       "detail_hash", "find_candidate_id", "find_hash"})
        if proof["media_type"] != kind or type(proof["detail_id"]) is not int or proof["detail_id"] != tid \
                or proof["external_imdb_id"] != entry["imdb_id"]:
            _fail()
        if proof["external_parent_id"] is not None and (
                type(proof["external_parent_id"]) is not int or proof["external_parent_id"] != tid):
            _fail()
        _hash_value(proof["detail_hash"])
        if native["tmdb_id"] is None:
            if type(proof["find_candidate_id"]) is not int or proof["find_candidate_id"] != tid:
                _fail()
            _hash_value(proof["find_hash"])
        elif proof["find_candidate_id"] is not None or proof["find_hash"] is not None:
            _fail()
    identity_hash, source_hash = _fingerprints(entries)
    if document["identity_set_hash"] != identity_hash or document["source_snapshot_hash"] != source_hash \
            or document["plan_hash"] != _hash("plan", {k: v for k, v in document.items() if k != "plan_hash"}) \
            or len(_canonical(document)) > MAX_PREPARATION_BYTES:
        _fail()
    return document


class PreparedWatchlist:
    """Issued in-process only; immutable bytes, no public deserializer or constructor."""
    __slots__ = ("_body", "__weakref__")

    def __new__(cls):
        raise TypeError("use prepare_owner")

    def __setattr__(self, name, value):
        raise AttributeError("immutable preparation")

    @property
    def plan_hash(self):
        return _issued(self)["plan_hash"]


def _issued(prepared):
    if type(prepared) is not PreparedWatchlist:
        _fail()
    with _ISSUED_LOCK:
        original = _ISSUED.get(prepared)
    if original is None or original != getattr(prepared, "_body", None):
        _fail()
    return _validate(_decode(original.decode("ascii")))


def _now(clock):
    value = clock()
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        _fail()
    return value


def prepare_owner(user_id, *, fetch=None, clock=time.monotonic):
    started = _now(clock)
    with _transaction() as conn:
        rows, identity_hash, source_hash = _snapshot(conn, user_id)
    if sum(2 if row["tmdb_id"] is None else 1 for row in rows) > MAX_FETCHES:
        _fail()
    # Reject known duplicate typed identities before spending provider calls.
    known = [(row["media_type"], row["tmdb_id"]) for row in rows if row["tmdb_id"] is not None]
    if len(known) != len(set(known)):
        _fail()
    if rows:
        import config
        import db
        try:
            key = db.get_setting("TMDB_API_KEY")
            key = config.TMDB_API_KEY if key is None else key
        except (sqlite3.Error, OSError):
            _fail(retryable=True)  # Fixed safe failure; no legacy settings fallback.
        if type(key) is not str or not key or len(key) > 2048 or not key.isascii() \
                or not key.isprintable() or key != key.strip():
            _fail()
    calls, last = 0, started

    def budget():
        nonlocal last
        now = _now(clock)
        if now < last or now - started > MAX_SECONDS:
            _fail()
        last = now

    def get(path, params):
        nonlocal calls
        budget()
        if calls >= MAX_FETCHES:
            _fail()
        calls += 1
        try:
            raw = (fetch or tc.fetch)(path, params)
        except tc.TmdbNotFound:
            _fail()  # Unresolved legacy row, never removed or turned into empty data.
        except tc.TmdbUnavailable as exc:
            _fail(retryable=str(exc) in catalog.RETRYABLE_REASONS)
        except Exception:
            _fail()
        budget()
        return raw

    entries = []
    try:
        for row in rows:
            kind, tid, imdb = row["media_type"], row["tmdb_id"], row["imdb_id"]
            find_hash, candidate_id = None, None
            if tid is None:
                found = get(f"/find/{imdb}", {"external_source": "imdb_id"})
                candidates = found.get("movie_results" if kind == "movie" else "tv_results") if type(found) is dict else None
                if type(candidates) is not list or len(candidates) != 1 or type(candidates[0]) is not dict:
                    _fail()
                candidate = candidates[0]
                tid = candidate_id = _integer(candidate.get("id"))
                if "media_type" in candidate and candidate["media_type"] != kind:
                    _fail()
                find_hash = _hash("find-response", found)
            raw = get(f"/{kind}/{tid}", {"append_to_response": "external_ids"})
            if type(raw) is not dict or type(raw.get("id")) is not int or raw["id"] != tid \
                    or type(raw.get("overview")) is not str:
                _fail()
            external = raw.get("external_ids")
            if type(external) is not dict or external.get("imdb_id") != imdb:
                _fail()
            if "id" in external and (type(external["id"]) is not int or external["id"] != tid):
                _fail()
            if "imdb_id" in raw and raw["imdb_id"] is not None and raw["imdb_id"] != imdb:
                _fail()
            dto = ic.title(raw, kind)
            entries.append({"watchlist_id": row["id"], "row_source_hash": _hash("source-row", row),
                "native_identity": _identity(row, user_id), "media_type": kind, "tmdb_id": tid,
                "imdb_id": imdb, "title": dto, "title_hash": _hash("title", dto),
                "identity_proof": {"media_type": kind, "detail_id": tid,
                    "external_parent_id": external.get("id"), "external_imdb_id": imdb,
                    "detail_hash": _hash("detail-response", raw), "find_candidate_id": candidate_id,
                    "find_hash": find_hash}})
            budget()
    except ic.CatalogUnavailable:
        _fail()
    budget()
    document = {"format_version": 1, "user_id": user_id, "identity_set_hash": identity_hash,
                "source_snapshot_hash": source_hash, "row_count": len(rows), "entries": entries}
    document["plan_hash"] = _hash("plan", document)
    _validate(document)
    # Owner/source changes during resolution invalidate even a dry-run result.
    with _transaction() as conn:
        _current(conn, document)
    budget()
    prepared = object.__new__(PreparedWatchlist)
    body = _canonical(document)
    object.__setattr__(prepared, "_body", body)
    with _ISSUED_LOCK:
        _ISSUED[prepared] = body
    return prepared


def _current(conn, document):
    rows, identity_hash, source_hash = _snapshot(conn, document["user_id"])
    if identity_hash != document["identity_set_hash"] or source_hash != document["source_snapshot_hash"] \
            or len(rows) != document["row_count"]:
        _fail("watchlist_source_changed", 409)


def _summary(document):
    return {"ready": True, "plan_hash": document["plan_hash"], "row_count": document["row_count"]}


def check_prepared(prepared):
    document = _issued(prepared)
    with _transaction() as conn:
        _current(conn, document)
    return _summary(document)


def _stored(conn, user_id, plan_hash):
    row = conn.execute("SELECT * FROM integration_watchlist_preparations WHERE plan_hash=? AND user_id=?",
                       (plan_hash, user_id)).fetchone()
    if row is None:
        return None
    lengths = conn.execute("SELECT length(CAST(native_identity_json AS BLOB)) + "
        "length(CAST(title_json AS BLOB)) + length(CAST(identity_proof_json AS BLOB)) AS bytes "
        "FROM integration_watchlist_overlay_entries WHERE plan_hash=? LIMIT ?",
        (plan_hash, MAX_NATIVE_ROWS + 1)).fetchall()
    if len(lengths) > MAX_NATIVE_ROWS or any(type(r["bytes"]) is not int for r in lengths) \
            or sum(r["bytes"] for r in lengths) > MAX_PREPARATION_BYTES:
        _fail()
    document = {key: row[key] for key in ("format_version", "user_id", "identity_set_hash",
                                         "source_snapshot_hash", "row_count", "plan_hash")}
    entries = []
    for entry in conn.execute("SELECT * FROM integration_watchlist_overlay_entries WHERE plan_hash=? "
                               "ORDER BY watchlist_id LIMIT ?", (plan_hash, MAX_NATIVE_ROWS + 1)):
        entries.append({"watchlist_id": entry["watchlist_id"], "row_source_hash": entry["row_source_hash"],
            "native_identity": _decode(entry["native_identity_json"]), "media_type": entry["media_type"],
            "tmdb_id": entry["tmdb_id"], "imdb_id": entry["imdb_id"], "title": _decode(entry["title_json"]),
            "title_hash": entry["title_hash"], "identity_proof": _decode(entry["identity_proof_json"])})
    document["entries"] = entries
    return _validate(document)


def check_overlay(user_id, plan_hash):
    _hash_value(plan_hash)
    with _transaction() as conn:
        _enabled(conn, user_id)
        document = _stored(conn, user_id, plan_hash)
        if document is None:
            return {"ready": False, "reason": "not_prepared"}
        _guards(conn)
        _current(conn, document)
        return _summary(document)


def _retention(conn, user_id, count):
    owner = conn.execute("SELECT COUNT(*) FROM integration_watchlist_preparations WHERE user_id=?",
                         (user_id,)).fetchone()[0]
    plans = conn.execute("SELECT COUNT(*) FROM integration_watchlist_preparations").fetchone()[0]
    entries = conn.execute("SELECT COUNT(*) FROM integration_watchlist_overlay_entries").fetchone()[0]
    if owner >= MAX_OWNER_PLANS or plans >= MAX_GLOBAL_PLANS or entries + count > MAX_GLOBAL_ENTRIES:
        _fail()


def _guards(conn):
    expected = {f"{table}_no_{action}" for table in _TABLES for action in ("insert", "update", "delete")}
    actual = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    if not expected <= actual:
        _fail()


def apply_prepared(prepared, *, expected_plan_hash):
    document = _issued(prepared)
    _hash_value(expected_plan_hash)
    if expected_plan_hash != document["plan_hash"]:
        _fail("watchlist_plan_changed", 409)
    with _transaction(write=True) as conn:
        return _install_document(conn, document)


def _install_document(conn, document):
    """Internal caller owns its write transaction and constructs trusted metadata."""
    _validate(document)
    _current(conn, document)
    _guards(conn)
    existing = _stored(conn, document["user_id"], document["plan_hash"])
    if existing is not None:
        if _canonical(existing) != _canonical(document):
            _fail()
        return _summary(existing)
    _retention(conn, document["user_id"], document["row_count"])
    conn.execute("""INSERT INTO integration_watchlist_preparations
        (plan_hash,user_id,format_version,identity_set_hash,source_snapshot_hash,row_count)
        VALUES (?,?,?,?,?,?)""", tuple(document[k] for k in ("plan_hash", "user_id", "format_version",
                                    "identity_set_hash", "source_snapshot_hash", "row_count")))
    for entry in document["entries"]:
        conn.execute("""INSERT INTO integration_watchlist_overlay_entries
            (plan_hash,watchlist_id,row_source_hash,native_identity_json,media_type,tmdb_id,
             imdb_id,title_json,title_hash,identity_proof_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (document["plan_hash"], entry["watchlist_id"], entry["row_source_hash"],
             _canonical(entry["native_identity"]).decode("ascii"), entry["media_type"], entry["tmdb_id"],
             entry["imdb_id"], _canonical(entry["title"]).decode("ascii"), entry["title_hash"],
             _canonical(entry["identity_proof"]).decode("ascii")))
    return _summary(document)

def _selection(conn, user_id):
    _guards(conn)
    guards = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    if not set(_SELECTION_GUARDS) <= guards:
        _fail()
    row = conn.execute("SELECT plan_hash,revision FROM integration_watchlist_selection WHERE user_id=?",
                       (user_id,)).fetchone()
    if row is None:
        return None
    _hash_value(row["plan_hash"])
    _integer(row["revision"], 9223372036854775807)
    return dict(row)


def _selected_document(conn, user_id):
    """Caller owns the read transaction; no provider access or implicit selection."""
    _enabled(conn, user_id)
    selection = _selection(conn, user_id)
    if selection is None:
        return None, None
    document = _stored(conn, user_id, selection["plan_hash"])
    if document is None:
        _fail()
    _current(conn, document)
    return selection, document


def check_selection(user_id):
    """Readiness of the explicitly selected, complete current-owner metadata only."""
    with _transaction() as conn:
        selection, document = _selected_document(conn, user_id)
        if selection is None:
            return {"ready": False, "reason": "not_selected", "revision": 0}
        return {**_summary(document), "revision": selection["revision"]}


def list_selected(user_id, *, limit=50, before_id=None):
    """Owned bounded metadata page, checked against the complete native source.

    The cursor is only a row boundary, never authority or a snapshot credential.
    Every page rechecks the entire selected source in its own read transaction.
    A changed source fails rather than returning a partial or invented empty list.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_NATIVE_ROWS \
            or before_id is not None and (type(before_id) is not int or not 1 <= before_id <= 9223372036854775807):
        raise ia.IntegrationError("bad_request", 400, "invalid watchlist page")
    with _transaction() as conn:
        selection, document = _selected_document(conn, user_id)
        if selection is None:
            _fail("watchlist_not_selected")
        entries = [e for e in reversed(document["entries"])
                   if before_id is None or e["watchlist_id"] < before_id]
        items = [{**e["title"], "watchlist_id": e["watchlist_id"],
                  "added_at": e["native_identity"]["added_at"].replace(" ", "T") + "Z"}
                 for e in entries[:limit]]
        data = {"items": items,
                "next_before_id": items[-1]["watchlist_id"] if len(entries) > limit else None}
        try:
            ic.serialize(data)
        except ic.CatalogUnavailable:
            _fail()
        return data


def select_prepared(prepared, *, expected_plan_hash, expected_revision):
    """Internal explicit CAS of applied metadata. No HTTP or membership activation.

    A named hash is not a selection credential. The resolver-issued object and
    exact reviewed hash are required, and all current source checks run again in
    the same write transaction as the selection change. Revisions never reset.
    """
    document = _issued(prepared)
    _hash_value(expected_plan_hash)
    if expected_plan_hash != document["plan_hash"]:
        _fail("watchlist_plan_changed", 409)
    if type(expected_revision) is not int or not 0 <= expected_revision <= 9223372036854775807:
        _fail()
    with _transaction(write=True) as conn:
        return _select_document(conn, document, expected_revision)


def _select_document(conn, document, expected_revision):
    """Internal caller owns its transaction; never accepts an exported plan."""
    _current(conn, document)
    selection = _selection(conn, document["user_id"])
    revision = 0 if selection is None else selection["revision"]
    if expected_revision != revision:
        _fail("watchlist_selection_changed", 409)
    stored = _stored(conn, document["user_id"], document["plan_hash"])
    if stored is None or _canonical(stored) != _canonical(document):
        _fail()
    if selection is not None and selection["plan_hash"] == document["plan_hash"]:
        return {**_summary(document), "revision": revision}
    if revision == 9223372036854775807:
        _fail()
    if selection is None:
        conn.execute("INSERT INTO integration_watchlist_selection(user_id,plan_hash,revision) VALUES (?,?,1)",
                     (document["user_id"], document["plan_hash"]))
    else:
        changed = conn.execute("UPDATE integration_watchlist_selection SET plan_hash=?,revision=? "
                               "WHERE user_id=? AND revision=?",
                               (document["plan_hash"], revision + 1, document["user_id"], revision))
        if changed.rowcount != 1:
            _fail("watchlist_selection_changed", 409)
    return {**_summary(document), "revision": revision + 1}
