"""Bounded receiver-owned request preparation, without admission or publication.

The caller supplies the native actor and exact body after its existing receiver
authentication. This module is not an authentication mechanism. It issues an
in-process value, never accepts exported plans, and cannot qualify library grants,
publisher policy, destination revision, reader coverage or filesystem authority.
"""
import contextlib
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
import weakref

import integration_auth as ia
import integration_catalog_service as catalog
import intent_metadata
import intent_snapshots
import request_intents
import tmdb_checked as tc

MAX_LIVE = 128
MAX_SECONDS = 60
MAX_FETCHES = 101
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PREPARATION_BYTES = 4 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}")
_ISSUED = weakref.WeakKeyDictionary()
_LOCK = threading.RLock()
_SLOTS = threading.BoundedSemaphore(MAX_LIVE)
_POLICY_COLUMNS = ("id", "role", "enabled", "quota_monthly", "auto_approve")


def _fail(code="request_preparation_unavailable", status=503, retryable=False):
    raise ia.IntegrationError(code, status, "request preparation unavailable", retryable)


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except (ValueError, TypeError, RecursionError, OverflowError):
        _fail()


def _hash(domain, value):
    return hashlib.sha256(b"request-preparation:v1:" + domain.encode("ascii") + b"\0" + _canonical(value)).hexdigest()


def _now(clock):
    try:
        value = clock()
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except Exception:
        _fail()
    if not valid:
        _fail()
    return value


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _day(clock):
    try:
        value = clock()
        if type(value) is not datetime.datetime or value.tzinfo is None or value.utcoffset() is None:
            _fail()
        return value.astimezone(datetime.timezone.utc).date().isoformat()
    except ia.IntegrationError:
        raise
    except Exception:
        _fail()


def _policy_value(row):
    if type(row) is not dict or set(row) != set(_POLICY_COLUMNS) \
            or type(row["id"]) is not int or not 1 <= row["id"] <= 9223372036854775807 \
            or row["role"] not in ("user", "admin") \
            or type(row["enabled"]) is not int or row["enabled"] != 1 \
            or type(row["quota_monthly"]) is not int or not 0 <= row["quota_monthly"] <= 2147483647 \
            or type(row["auto_approve"]) is not int or row["auto_approve"] not in (0, 1):
        _fail()
    return row


@contextlib.contextmanager
def _read_connection():
    """A missing database must not be created by a preparation or its check."""
    import db
    connection = None
    try:
        cached = getattr(db._tls, "conn", None)
        if cached is not None and cached.in_transaction:
            _fail()  # Never fetch metadata while the normal caller holds a transaction.
        path = Path(db.DB_PATH).absolute()
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                     isolation_level=None, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection
        connection.execute("COMMIT")
    except sqlite3.Error as error:
        code = (getattr(error, "sqlite_errorcode", 0) or 0) & 255
        _fail(retryable=code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
                                sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_FULL))
    except (OSError, ValueError, TypeError):
        _fail()
    finally:
        if connection is not None:
            connection.close()


def _policy(owner):
    if type(owner) is not int or not 1 <= owner <= 9223372036854775807:
        _fail("disabled", 403)
    with _read_connection() as connection:
        row = connection.execute("SELECT " + ",".join(_POLICY_COLUMNS) +
                                 " FROM users WHERE id=?", (owner,)).fetchone()
        if row is None or row["enabled"] != 1:
            _fail("disabled", 403)
        import request_preparation_tokens
        request_preparation_tokens._ready(connection)
        return _policy_value(dict(row))


def _input(body, key):
    if type(body) is not bytes or not body or len(body) > ia.MAX_BODY_BYTES:
        _fail("bad_request", 400)
    try:
        key = request_intents._check_key(key)
        normalized = request_intents.normalize_payload(ia.strict_json_object(body))
    except (request_intents.IntentError, ia.IntegrationError):
        _fail("bad_request", 400)
    return normalized, key, hashlib.sha256(body).hexdigest()


class _Source:
    def __init__(self, normalized, fetch, budget):
        media = normalized["media"]
        self.root = f"/{media['type']}/{media['tmdb_id']}"
        self.kind, self.tid = media["type"], media["tmdb_id"]
        self.fetch, self.budget = fetch or tc.fetch, budget
        self.root_response = None
        self.responses = []
        self.byte_count = 0
        self.retryable = False
        self.failure = None

    def __call__(self, path):
        try:
            return self._get(path)
        except ia.IntegrationError as error:
            self.failure = error
            raise

    def _get(self, path):
        self.budget()
        if path == self.root + "/external_ids":
            if self.root_response is None:
                _fail()
            return dict(self.root_response["external_ids"])
        if path != self.root and not re.fullmatch(re.escape(self.root) + r"/season/(0|[1-9][0-9]{0,2})", path):
            _fail()
        if len(self.responses) >= MAX_FETCHES:
            _fail()
        params = {"append_to_response": "external_ids"} if path == self.root else {}
        try:
            raw = self.fetch(path, params)
        except tc.TmdbUnavailable as error:
            self.retryable = str(error) in catalog.RETRYABLE_REASONS
            _fail(retryable=self.retryable)
        except Exception:
            _fail()
        self.budget()
        if type(raw) is not dict:
            _fail()
        encoded = _canonical(raw)
        self.byte_count += len(encoded)
        if len(encoded) > tc.MAX_BYTES or self.byte_count > MAX_RESPONSE_BYTES:
            _fail()
        raw = tc.parse_json_object(encoded)  # Own a copy, never retain an injected caller's mutable dictionary.
        if path == self.root:
            external = raw.get("external_ids")
            if type(raw.get("id")) is not int or raw["id"] != self.tid \
                    or raw.get("media_type", self.kind) != self.kind \
                    or type(external) is not dict or type(external.get("imdb_id")) is not str \
                    or not intent_metadata._IMDB.fullmatch(external["imdb_id"]) \
                    or "id" in external and (type(external["id"]) is not int or external["id"] != self.tid) \
                    or raw.get("imdb_id") is not None and raw["imdb_id"] != external["imdb_id"]:
                _fail()
            self.root_response = raw
        elif "show_id" in raw and (type(raw["show_id"]) is not int or raw["show_id"] != self.tid):
            _fail()
        self.responses.append({"path": path, "params": params,
                               "response_hash": _hash("metadata-json", raw)})
        return raw


class PreparedRequest:
    """Receiver-issued immutable value; reports and hashes cannot recreate it."""
    __slots__ = ("_body", "__weakref__")

    def __new__(cls):
        raise TypeError("use prepare")

    def __setattr__(self, name, value):
        raise AttributeError("immutable preparation")

    @property
    def preparation_hash(self):
        document, _ = _issued(self)
        return _hash("prepared", document)


def _validate(document):
    if type(document) is not dict or set(document) != {"format_version", "preparation_id", "owner_id", "idempotency_key",
            "raw_body_sha256", "normalized_input", "native_policy", "native_policy_hash",
            "resolved", "hashes", "metadata_sources"} or type(document["format_version"]) is not int \
            or document["format_version"] != 1:
        _fail()
    if type(document["preparation_id"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", document["preparation_id"]):
        _fail()
    try:
        normalized = request_intents.normalize_payload(document["normalized_input"])
        request_intents._check_key(document["idempotency_key"])
        built = intent_snapshots.build(document["resolved"], normalized,
                                        request_intents.semantic_hash(normalized))
    except (request_intents.IntentError, intent_snapshots.SnapshotError):
        _fail()
    policy = _policy_value(document["native_policy"])
    if document["owner_id"] != policy["id"] or type(document["owner_id"]) is not int \
            or document["native_policy_hash"] != _hash("native-policy", policy) \
            or type(document["raw_body_sha256"]) is not str or not _HASH.fullmatch(document["raw_body_sha256"]) \
            or document["hashes"] != {key: built[key] for key in ("input_hash", "predicate_hash", "snapshot_hash", "accepted_hash")}:
        _fail()
    sources = document["metadata_sources"]
    root = f"/{normalized['media']['type']}/{normalized['media']['tmdb_id']}"
    if type(sources) is not list or not 1 <= len(sources) <= MAX_FETCHES:
        _fail()
    paths = set()
    for index, source in enumerate(sources):
        if type(source) is not dict or set(source) != {"path", "params", "response_hash"} \
                or type(source["path"]) is not str or source["path"] in paths \
                or type(source["response_hash"]) is not str or not _HASH.fullmatch(source["response_hash"]):
            _fail()
        if index == 0:
            if source["path"] != root or source["params"] != {"append_to_response": "external_ids"}:
                _fail()
        elif source["params"] != {} or not re.fullmatch(re.escape(root) + r"/season/(0|[1-9][0-9]{0,2})", source["path"]):
            _fail()
        paths.add(source["path"])
    return built


def _issued(prepared):
    if type(prepared) is not PreparedRequest:
        _fail()
    # Refuse a forked copy before taking an inherited lock or touching SQLite.
    state = _ISSUED.get(prepared)
    if state is None or state["pid"] != os.getpid():
        _fail()
    with _LOCK:
        state = _ISSUED.get(prepared)
        if state is None or state["pid"] != os.getpid() or state["body"] != getattr(prepared, "_body", None):
            _fail()
        if state["status"] != "prepared":
            _fail("request_preparation_retired", 409)
        now = _now(state["clock"])
        if now < state["last"] or now - state["started"] > MAX_SECONDS:
            state["status"] = "expired"
            _fail("request_preparation_expired", 409)
        state["last"] = now
        document = ia.strict_json_object(state["body"], MAX_PREPARATION_BYTES)
        if _day(state["utc_clock"]) != document["resolved"]["cutoff"]:
            state["status"] = "expired"
            _fail("request_preparation_expired", 409)
        return document, _validate(document)


def prepare(user_id, raw_body, idempotency_key, *, fetch=None, clock=time.monotonic, utc_clock=_utc_now):
    """Read-only preparation after receiver auth; not permission to execute work."""
    normalized, key, raw_hash = _input(raw_body, idempotency_key)
    if not _SLOTS.acquire(blocking=False):
        _fail(retryable=True)
    issued = False
    try:
        started = last = _now(clock)
        cutoff = _day(utc_clock)
        before = _policy(user_id)
        # Readiness is checked without legacy settings fallback, outside any transaction.
        catalog._require_key(catalog._read_setting)
        def budget():
            nonlocal last
            now = _now(clock)
            if now < last or now - started > MAX_SECONDS or _day(utc_clock) != cutoff:
                _fail("request_preparation_expired", 409)
            last = now
        source = _Source(normalized, fetch, budget)
        try:
            resolved = intent_metadata.resolve(normalized, today=cutoff, fetch=source)
        except intent_metadata.MetadataError as error:
            if source.failure is not None:
                raise source.failure from None
            _fail("scope_not_found" if error.status == 404 else "request_preparation_unavailable",
                  error.status, source.retryable)
        budget()
        if _policy(user_id) != before:
            _fail("request_policy_changed", 409)
        try:
            built = intent_snapshots.build(resolved, normalized, request_intents.semantic_hash(normalized))
        except intent_snapshots.SnapshotError:
            _fail()
        document = {"format_version": 1, "preparation_id": uuid.uuid4().hex, "owner_id": user_id, "idempotency_key": key,
                    "raw_body_sha256": raw_hash, "normalized_input": normalized,
                    "native_policy": before, "native_policy_hash": _hash("native-policy", before),
                    "resolved": resolved, "hashes": {k: built[k] for k in (
                        "input_hash", "predicate_hash", "snapshot_hash", "accepted_hash")},
                    "metadata_sources": source.responses}
        _validate(document)
        body = _canonical(document)
        if len(body) > MAX_PREPARATION_BYTES:
            _fail()
        budget()
        prepared = object.__new__(PreparedRequest)
        object.__setattr__(prepared, "_body", body)
        with _LOCK:
            _ISSUED[prepared] = {"body": body, "started": started, "last": last,
                                 "clock": clock, "utc_clock": utc_clock, "pid": os.getpid(), "status": "prepared"}
        weakref.finalize(prepared, _SLOTS.release)
        issued = True
        return prepared
    finally:
        if not issued:
            _SLOTS.release()


def check_prepared(prepared, *, user_id, raw_body, idempotency_key):
    document, built = _issued(prepared)
    normalized, key, raw_hash = _input(raw_body, idempotency_key)
    if type(user_id) is not int or user_id != document["owner_id"] \
            or key != document["idempotency_key"] or raw_hash != document["raw_body_sha256"] \
            or normalized != document["normalized_input"]:
        _fail("request_preparation_mismatch", 409)
    try:
        if _policy(user_id) != document["native_policy"]:
            _fail("request_policy_changed", 409)
    except ia.IntegrationError:
        retire(prepared)
        raise
    import request_preparation_tokens
    with _read_connection() as connection:
        request_preparation_tokens._check_absent(connection, document)
    _issued(prepared)  # Cancellation/expiry during policy or ledger reads must win before returning.
    return {"prepared": True, "admission_qualified": False,
            "preparation_hash": _hash("prepared", document), "cutoff": built["cutoff"],
            "raw_body_sha256": raw_hash, **document["hashes"]}


def retire(prepared):
    """In-process cancellation only; no admission, retry or durable tombstone claim."""
    if type(prepared) is not PreparedRequest:
        _fail()
    state = _ISSUED.get(prepared)
    if state is None or state["pid"] != os.getpid():
        _fail()
    with _LOCK:
        state = _ISSUED.get(prepared)
        if state is None or state["body"] != getattr(prepared, "_body", None):
            _fail()
        state["status"] = "retired"


def _original(prepared):
    """Original issuance for cancellation only; never makes expired work usable."""
    if type(prepared) is not PreparedRequest:
        _fail()
    state = _ISSUED.get(prepared)
    if state is None or state["pid"] != os.getpid():
        _fail()
    with _LOCK:
        if _ISSUED.get(prepared) is not state or state["body"] != getattr(prepared, "_body", None):
            _fail()
        document = ia.strict_json_object(state["body"], MAX_PREPARATION_BYTES)
        _validate(document)
        return document
