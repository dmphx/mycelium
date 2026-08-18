"""Read-only, token-authenticated data surface for the Onyx Discord bot."""

from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone


SCHEMA_VERSION = 1
MAX_EVENTS = 100


class IntegrationAuthError(Exception):
    """Raised when the Discord integration token is absent or invalid."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


def authorize(header: str | None) -> None:
    """Validate an Authorization bearer token without leaking token state."""
    expected = (os.getenv("MYCELIUM_BOT_TOKEN") or "").strip()
    if not expected:
        raise IntegrationAuthError(503, "integration unavailable")
    prefix = "Bearer "
    provided = header[len(prefix):].strip() if header and header.startswith(prefix) else ""
    if not provided or not hmac.compare_digest(provided, expected):
        raise IntegrationAuthError(401, "unauthorized")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _group_counts(conn, table: str, column: str = "status") -> dict[str, int]:
    allowed = {
        ("requests", "status"),
        ("wanted_episodes", "status"),
        ("playability_state", "status"),
    }
    if (table, column) not in allowed:
        raise ValueError("unsupported counter")
    rows = conn.execute(
        f"SELECT {column}, COUNT(*) AS count FROM {table} GROUP BY {column}"
    ).fetchall()
    return {str(row[column] or "unknown"): int(row["count"]) for row in rows}


def get_summary(app_version: str) -> dict:
    """Return a sanitized operational snapshot for Discord status feeds."""
    import db
    import health
    import torbox

    with db._connect() as conn:
        requests = _group_counts(conn, "requests")
        wanted = _group_counts(conn, "wanted_episodes")
        playability = _group_counts(conn, "playability_state")
        latest = conn.execute("SELECT COALESCE(MAX(id), 0) AS id FROM activity_log").fetchone()

    dependencies = []
    try:
        for item in health.check_all():
            dependencies.append({
                "name": str(item.get("name") or "unknown"),
                "status": str(item.get("status") or "unknown"),
                "code": item.get("code"),
            })
    except Exception:
        dependencies.append({"name": "dependency checks", "status": "down", "code": None})

    try:
        torbox_usage = torbox.get_usage_summary()
        torbox_safe = {
            "torrent_count": int(torbox_usage.get("torrent_count") or 0),
            "total_gb": float(torbox_usage.get("total_gb") or 0),
            "states": {
                str(key): int(value)
                for key, value in (torbox_usage.get("states") or {}).items()
            },
        }
    except Exception:
        torbox_safe = {"status": "unavailable"}

    degraded = int(playability.get("degraded", 0))
    failed = int(requests.get("failed", 0))
    overall = "degraded" if degraded or failed or any(
        item["status"] == "down" for item in dependencies
    ) else "ok"

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "version": app_version,
        "status": overall,
        "requests": requests,
        "wanted_episodes": wanted,
        "playability": playability,
        "torbox": torbox_safe,
        "dependencies": dependencies,
        "latest_event_id": int(latest["id"]),
    }


def get_events(after_id: int = 0, limit: int = 50) -> dict:
    """Return cursor-based activity without paths, hashes, URLs, or requester data."""
    import db

    safe_limit = max(1, min(int(limit), MAX_EVENTS))
    safe_after = max(0, int(after_id))
    with db._connect() as conn:
        rows = conn.execute(
            """SELECT id, event, title, success, created_at
               FROM activity_log
               WHERE id > ?
               ORDER BY id ASC
               LIMIT ?""",
            (safe_after, safe_limit),
        ).fetchall()

    events = [{
        "id": int(row["id"]),
        "type": str(row["event"]),
        "title": str(row["title"] or "Untitled")[:200],
        "status": "ok" if bool(row["success"]) else "failed",
        "occurred_at": str(row["created_at"]),
    } for row in rows]
    next_cursor = events[-1]["id"] if events else safe_after
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "events": events,
        "next_cursor": next_cursor,
    }
