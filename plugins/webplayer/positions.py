"""Native-owner resume positions bound to one logical target and provider file.

This records viewing progress only. It grants no library or provider authority.
"""
import hashlib
import json
import math

import db


def init_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS webplayer_positions (
        user_id INTEGER NOT NULL,
        binding_hash TEXT NOT NULL,
        position_s REAL NOT NULL CHECK(position_s >= 0 AND position_s <= 604800),
        duration_s REAL CHECK(duration_s >= 0 AND duration_s <= 604800),
        PRIMARY KEY(user_id, binding_hash),
        CHECK(duration_s IS NULL OR position_s <= duration_s)
    )""")


def _binding(session):
    if (type(session.owner_id) is not int or session.owner_id <= 0
            or not isinstance(session.content_hash, str)
            or len(session.content_hash) != 40
            or any(c not in "0123456789abcdef" for c in session.content_hash)
            or type(session.torrent_id) is not int or session.torrent_id < 0
            or type(session.file_id) is not int or session.file_id < 0
            or not isinstance(session.target, tuple) or len(session.target) != 4):
        raise ValueError("Unqualified playback binding")
    document = [session.content_hash, session.torrent_id, session.file_id, *session.target]
    return hashlib.sha256(json.dumps(document, separators=(",", ":")).encode()).hexdigest()


def _owner(conn, user_id):
    row = conn.execute("SELECT enabled, webplayer_enabled FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or not row[0] or not row[1]:
        raise PermissionError("Player is unavailable")


def save(session, position_s, duration_s=None):
    binding = _binding(session)
    for value in (position_s, duration_s):
        if value is not None and (type(value) not in (int, float)
                or not math.isfinite(value) or not 0 <= value <= 604800):
            raise ValueError("Invalid position")
    if position_s is None or (duration_s is not None and position_s > duration_s):
        raise ValueError("Invalid position")
    with db._connect() as conn:
        if conn.in_transaction:
            raise RuntimeError("Position write cannot join another transaction")
        conn.execute("BEGIN IMMEDIATE")
        try:
            _owner(conn, session.owner_id)
            conn.execute("""INSERT INTO webplayer_positions(user_id,binding_hash,position_s,duration_s)
                VALUES(?,?,?,?) ON CONFLICT(user_id,binding_hash) DO UPDATE SET
                position_s=excluded.position_s,duration_s=excluded.duration_s""",
                (session.owner_id, binding, position_s, duration_s))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def read(session):
    binding = _binding(session)
    with db._connect() as conn:
        _owner(conn, session.owner_id)
        row = conn.execute("SELECT position_s,duration_s FROM webplayer_positions WHERE user_id=? AND binding_hash=?",
                           (session.owner_id, binding)).fetchone()
    return {"position_s": row[0], "duration_s": row[1]} if row else None
