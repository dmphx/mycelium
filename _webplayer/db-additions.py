# Parked — paste these into db.py when activating the web player.
#
# 1. Add both strings to the migrations list in _run_migrations():
#
# 2. Add both functions anywhere in db.py (e.g. after touch_virtual_item)

# ── Migrations (add to _run_migrations list) ──────────────────────────────────

MIGRATION_PLAYER_SOURCE = (
    "ALTER TABLE virtual_items ADD COLUMN player_source TEXT DEFAULT 'jellyfin'"
)

MIGRATION_PLAYBACK_SESSIONS = """
CREATE TABLE IF NOT EXISTS playback_sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    token      TEXT    NOT NULL,
    position_s REAL    NOT NULL DEFAULT 0,
    duration_s REAL,
    updated_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now')),
    UNIQUE(user_id, token)
)
"""

# ── Functions (add to db.py) ───────────────────────────────────────────────────

def save_playback_position(user_id: int, token: str,
                           position_s: float,
                           duration_s: float | None = None) -> None:
    with _conn() as c:
        c.execute(
            """
            INSERT INTO playback_sessions (user_id, token, position_s, duration_s)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, token) DO UPDATE
              SET position_s = excluded.position_s,
                  duration_s = COALESCE(excluded.duration_s, duration_s),
                  updated_at = strftime('%Y-%m-%d %H:%M:%S','now')
            """,
            (user_id, token, position_s, duration_s),
        )


def get_playback_position(user_id: int, token: str) -> float:
    with _conn() as c:
        row = c.execute(
            "SELECT position_s FROM playback_sessions WHERE user_id=? AND token=?",
            (user_id, token),
        ).fetchone()
    return row["position_s"] if row else 0.0
