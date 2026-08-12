"""Shared Plex playback gate for background media work."""
from __future__ import annotations

import logging
import os
import threading
import time
import xml.etree.ElementTree as ET

import requests

log = logging.getLogger(__name__)

_lock = threading.Lock()
_cached_until = 0.0
_cached_active = False
_CACHE_SECONDS = 5.0
_RECENT_SECONDS = 600
_last_defer_log: dict[str, float] = {}


def _plex_active() -> bool | None:
    url = os.environ.get("PLEX_URL", "").rstrip("/")
    token = os.environ.get("PLEX_TOKEN", "")
    if not (url and token):
        try:
            import settings
            url = url or str(settings.get("PLEX_URL", "") or "").rstrip("/")
            token = token or str(settings.get("PLEX_TOKEN", "") or "")
        except Exception:
            pass
    if not (url and token):
        return None
    try:
        resp = requests.get(
            url + "/status/sessions",
            headers={"X-Plex-Token": token},
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        root = ET.fromstring(resp.content)
        return int(root.get("size") or 0) > 0
    except Exception as exc:
        log.debug("playback guard Plex query failed: %s", exc)
        return None


def _recent_play() -> bool:
    try:
        import db
        with db._connect() as conn:
            row = conn.execute(
                "select count(*) from virtual_items "
                "where last_played > datetime('now', ?)",
                (f"-{_RECENT_SECONDS} seconds",),
            ).fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def active(force: bool = False) -> bool:
    """Return true for playing, paused, buffering, or recently active media."""
    global _cached_active, _cached_until
    now = time.monotonic()
    with _lock:
        if not force and now < _cached_until:
            return _cached_active
    live = _plex_active()
    result = bool(live) or _recent_play()
    with _lock:
        _cached_active = result
        _cached_until = now + _CACHE_SECONDS
    return result


def defer(job_name: str) -> bool:
    """Return true and record a metric when background work should stop."""
    if not active():
        return False
    now = time.monotonic()
    with _lock:
        last_log = _last_defer_log.get(job_name, 0.0)
        should_log = now - last_log >= 300.0
        if should_log:
            _last_defer_log[job_name] = now
    if should_log:
        log.info("%s deferred while Plex playback is active", job_name)
    try:
        import metrics_prom
        metrics_prom.spore_background_deferred_total.labels(job=job_name).inc()
    except Exception:
        pass
    return True
