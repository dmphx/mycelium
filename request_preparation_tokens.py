"""Durable cancellation only. No token consumption, admission or execution authority."""
import contextlib
import sqlite3

import request_preparation as preparation

MAX_OWNER_TOMBSTONES = 1000
MAX_GLOBAL_TOMBSTONES = 100000
_GUARDS = ("request_preparation_tombstones_no_update", "request_preparation_tombstones_no_delete",
           "request_preparation_tombstones_no_replace")
_COLUMNS = ("preparation_hash", "user_id", "key_hash", "raw_body_sha256", "input_hash", "state")
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS request_preparation_tombstones (
        preparation_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        key_hash TEXT NOT NULL,
        raw_body_sha256 TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state='cancelled'),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )""",
    """CREATE INDEX IF NOT EXISTS request_preparation_tombstones_owner
        ON request_preparation_tombstones(user_id)""",
    """CREATE TRIGGER IF NOT EXISTS request_preparation_tombstones_no_update
        BEFORE UPDATE ON request_preparation_tombstones
        BEGIN SELECT RAISE(ABORT,'immutable request preparation retirement'); END""",
    """CREATE TRIGGER IF NOT EXISTS request_preparation_tombstones_no_delete
        BEFORE DELETE ON request_preparation_tombstones
        BEGIN SELECT RAISE(ABORT,'retained request preparation retirement'); END""",
    """CREATE TRIGGER IF NOT EXISTS request_preparation_tombstones_no_replace
        BEFORE INSERT ON request_preparation_tombstones
        WHEN EXISTS(SELECT 1 FROM request_preparation_tombstones WHERE preparation_hash=NEW.preparation_hash)
        BEGIN SELECT RAISE(ABORT,'immutable request preparation retirement'); END""",
)


def init_schema(connection):
    """Schema only; no native-row migration, copied data or commits."""
    for statement in _SCHEMA:
        connection.execute(statement)


def _ready(connection):
    connection.execute("SELECT " + ",".join(_COLUMNS) + " FROM request_preparation_tombstones LIMIT 0")
    guards = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    if not set(_GUARDS) <= guards:
        preparation._fail()


def _binding(document):
    return {"preparation_hash": preparation._hash("prepared", document), "user_id": document["owner_id"],
            "key_hash": preparation._hash("idempotency-key", document["idempotency_key"]),
            "raw_body_sha256": document["raw_body_sha256"], "input_hash": document["hashes"]["input_hash"],
            "state": "cancelled"}


def _record(connection, document):
    _ready(connection)
    expected = _binding(document)
    row = connection.execute("SELECT " + ",".join(_COLUMNS) +
                             " FROM request_preparation_tombstones WHERE preparation_hash=?",
                             (expected["preparation_hash"],)).fetchone()
    if row is None:
        return None
    if dict(row) != expected:
        preparation._fail()
    return expected


def _check_absent(connection, document):
    if _record(connection, document) is not None:
        preparation._fail("request_preparation_retired", 409)


@contextlib.contextmanager
def _transaction():
    import db
    try:
        with db._connect() as connection:
            if connection.in_transaction:
                preparation._fail()
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
    except (sqlite3.Error, OSError):
        preparation._fail()


def cancel(prepared, *, user_id, raw_body, idempotency_key):
    """Retire the genuine local issuance and atomically retain its cancellation.

    Matching replays are free, including at capacity. Storage failure still retires
    the local object but never reports durable success. Nothing admits a request.
    """
    document = preparation._original(prepared)
    normalized, key, digest = preparation._input(raw_body, idempotency_key)
    if type(user_id) is not int or user_id != document["owner_id"] \
            or normalized != document["normalized_input"] or digest != document["raw_body_sha256"] \
            or key != document["idempotency_key"]:
        preparation._fail("request_preparation_mismatch", 409)
    # The same lock order is required by a future admission consumer: value then DB.
    with preparation._LOCK:
        preparation.retire(prepared)
        with _transaction() as connection:
            owner = connection.execute("SELECT enabled FROM users WHERE id=?", (user_id,)).fetchone()
            if owner is None or owner["enabled"] != 1:
                preparation._fail("disabled", 403)
            record = _record(connection, document)
            if record is None:
                owner_count = connection.execute("SELECT COUNT(*) FROM request_preparation_tombstones WHERE user_id=?",
                                                 (user_id,)).fetchone()[0]
                total = connection.execute("SELECT COUNT(*) FROM request_preparation_tombstones").fetchone()[0]
                if owner_count >= MAX_OWNER_TOMBSTONES or total >= MAX_GLOBAL_TOMBSTONES:
                    preparation._fail()
                record = _binding(document)
                connection.execute("INSERT INTO request_preparation_tombstones(" + ",".join(_COLUMNS) +
                                   ") VALUES (?,?,?,?,?,?)", tuple(record[k] for k in _COLUMNS))
            return {"retired": True, "durable": True, "preparation_hash": record["preparation_hash"]}
