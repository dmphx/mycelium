"""media_servers.py — targeted library scans for newly-added content.

When mycelium writes a *new* .strm (only path through _write_strm — the bulk
Spore backfill never calls it), notify each media server to scan ONLY the
affected show/movie folder instead of firing a blanket /Library/Refresh.

  Jellyfin: POST /Library/Media/Updated  (mycelium holds the API key)
  Plex:     append the target folder to a queue file; a host-side cron
            (plex_targeted_scan.sh) drains it and runs the Plex Media Scanner
            CLI per folder — Plex's HTTP /sections/N/refresh is a no-op here,
            and mycelium has neither Plex creds nor docker access.

Path translation (container -> server-visible):
  MEDIA_PATH (/data/media)      -> JELLYFIN_LIBRARY_ROOT (/mnt/library-mycelium)
  series -> Plex PLEX_TV_ROOT    (/mnt/library/shows,  section PLEX_SECTION_TV)
  movies -> Plex PLEX_MOVIE_ROOT (/mnt/library/movies, section PLEX_SECTION_MOVIE)

The blanket jellyfin.refresh_library() backstop stays in place, so a missed
targeted scan is still caught by the periodic full refresh.
"""
import json
import logging
import os
import threading
import time
from pathlib import Path

import requests

import config
try:
    import settings
except Exception:
    settings = None

log = logging.getLogger(__name__)

import re
# Matches a Jellyfin/Plex 'Season NN' folder so targeted scans can be scoped to
# the season rather than the whole (possibly 1000-episode) show.
_SEASON_RE = re.compile(r"^Season \d+$", re.IGNORECASE)


def _cfg(key, default=""):
    if settings is not None:
        try:
            v = settings.get(key)
            if v:
                return v
        except Exception:
            pass
    env = os.environ.get(key)
    if env:
        return env
    return getattr(config, key, default)


MEDIA_PATH         = getattr(config, "MEDIA_PATH", "/data/media")
JF_LIBRARY_ROOT    = _cfg("JELLYFIN_LIBRARY_ROOT", "/mnt/library-mycelium")
PLEX_QUEUE         = _cfg("PLEX_SCAN_QUEUE", "/data/plex-scan-queue")
PLEX_TV_ROOT       = _cfg("PLEX_TV_ROOT", "/mnt/library/shows")
PLEX_MOVIE_ROOT    = _cfg("PLEX_MOVIE_ROOT", "/mnt/library/movies")
PLEX_SECTION_TV    = str(_cfg("PLEX_SECTION_TV", "8"))
PLEX_SECTION_MOVIE = str(_cfg("PLEX_SECTION_MOVIE", "7"))
try:
    _DEBOUNCE = float(_cfg("TARGETED_SCAN_DEBOUNCE_SEC", "20"))
except (TypeError, ValueError):
    _DEBOUNCE = 20.0
try:
    _SCAN_PACE = float(_cfg("TARGETED_SCAN_PACE_SEC", "0.5"))
except (TypeError, ValueError):
    _SCAN_PACE = 0.5

_lock = threading.Lock()
_pending = set()       # {(kind, folder)}
_timer = None


def _top_folder(strm_path):
    """Return ('series'|'movies', '<scan folder>') for a .strm under MEDIA_PATH, else None.

    For series, scope the targeted scan to the SEASON folder (e.g. \"One Piece/Season 14\")
    instead of the whole show. A single new/changed episode otherwise makes Jellyfin and
    Plex refresh the ENTIRE series; on 1000-episode anime (One Piece, Bleach, Naruto
    Shippuden) that full-series refresh recreates+removes a phantom \"Season Unknown\",
    removes+re-adds episodes, pegs Jellyfin CPU, and runs >15s so the /Library/Media/Updated
    POST times out (the 'Unexpected end of request content' errors). Season scope keeps the
    refresh to one season. Falls back to the show folder for movies and flat (no 'Season NN')
    layouts; the periodic full-refresh backstop still catches anything a narrower scan misses.
    """
    try:
        parts = Path(strm_path).relative_to(Path(MEDIA_PATH)).parts
    except Exception:
        return None
    if len(parts) < 2 or parts[0] not in ("series", "movies"):
        return None
    if parts[0] == "series" and len(parts) >= 4 and _SEASON_RE.match(parts[2]):
        return ("series", "%s/%s" % (parts[1], parts[2]))
    return (parts[0], parts[1])


def mark(strm_path):
    """Record a newly-written .strm so its folder gets a debounced targeted scan."""
    _enqueue(strm_path, "scan")


def mark_removed(strm_path):
    """Record a removed item so both libraries rescan its season or folder."""
    _enqueue(strm_path, "remove")


def request_reanalyze(strm_path):
    """Queue a Plex re-analyze (and Jellyfin refresh) for an item whose media
    changed on disk, e.g. a Spore stub rewritten with a corrected codec.

    Plex caches the codec at scan time, so a rewritten stub stays stale until it
    is re-analyzed; a plain scan only detects new files. The queue line carries an
    'analyze' marker so the host-side scanner runs Plex Media Scanner --analyze
    instead of --scan. Mycelium has no Plex creds, so this host queue is the only
    lever (see module docstring)."""
    _enqueue(strm_path, "analyze")


def _enqueue(strm_path, mode):
    info = _top_folder(strm_path)
    if not info:
        return
    kind, folder = info
    global _timer
    with _lock:
        _pending.add((kind, folder, mode))
        if _timer is None:
            _timer = threading.Timer(_DEBOUNCE, _flush)
            _timer.daemon = True
            _timer.start()


def _flush():
    global _timer
    with _lock:
        batch = sorted(_pending)
        _pending.clear()
        _timer = None
    if not batch:
        return
    # Jellyfin is a single batched POST -- fire it up front so the Plex scan
    # pacing below never delays it.
    _scan_jellyfin([{"Path": "%s/%s/%s" % (JF_LIBRARY_ROOT, kind, folder),
                     "UpdateType": ("Deleted" if mode == "remove" else
                                    "Modified" if mode == "analyze" else "Created")}
                    for kind, folder, mode in batch])
    plex_queue_lines = []
    scanned = 0
    for kind, folder, mode in batch:
        section = PLEX_SECTION_TV if kind == "series" else PLEX_SECTION_MOVIE
        root    = PLEX_TV_ROOT if kind == "series" else PLEX_MOVIE_ROOT
        plex_path = "%s/%s" % (root, folder)
        # Pace successive Plex partial scans so a large batch (e.g. a bulk Spore
        # backfill marking hundreds of show folders) trickles into Plex's scan
        # queue instead of bursting. A burst of scanner spawns is what piles up
        # in D-state and deadlocks SQLite; a small gap keeps Plex responsive.
        if scanned and _SCAN_PACE > 0:
            time.sleep(_SCAN_PACE)
        # A direct Plex API partial scan re-reads changed files, so a stub
        # rewritten with a corrected codec lands in Plex's DB. Falls back to the
        # host-drained queue file when no Plex token is configured.
        if _plex_api_scan(section, plex_path):
            scanned += 1
        else:
            plex_queue_lines.append("%s\t%s" % (section, plex_path))
    if plex_queue_lines:
        _queue_plex(plex_queue_lines)


def _scan_jellyfin(updates):
    url = _cfg("JELLYFIN_URL", "")
    key = _cfg("JELLYFIN_API_KEY", "")
    if not url:
        return
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-Emby-Token"] = key
    try:
        resp = requests.post("%s/Library/Media/Updated" % url.rstrip("/"),
                             headers=headers, data=json.dumps({"Updates": updates}),
                             timeout=15)
        if resp.status_code >= 400:
            log.warning("Targeted Jellyfin scan HTTP %s: %s", resp.status_code, resp.text[:120])
        else:
            log.info("Targeted Jellyfin scan: %d folder(s)", len(updates))
    except Exception as exc:
        log.warning("Targeted Jellyfin scan failed (%s); full-refresh backstop will catch it", exc)


def _plex_api_scan(section, plex_path):
    """Trigger a Plex partial scan of one folder over the HTTP API.

    Returns True only when Plex actually accepted the scan, so the caller skips the
    host-queue fallback; returns False when no Plex creds are set OR when the request
    failed/was rejected, letting the caller queue the folder for plex_targeted_scan.sh
    (Scanner CLI, no token needed). Reporting an HTTP failure as success used to
    silently disable that fallback whenever the token went stale.
    A partial scan re-reads changed files, so a stub rewritten with a
    corrected codec updates Plex's cached media info."""
    url   = _cfg("PLEX_URL", "")
    token = _cfg("PLEX_TOKEN", "")
    if not url or not token:
        return False
    try:
        resp = requests.get("%s/library/sections/%s/refresh" % (url.rstrip("/"), section),
                            params={"path": plex_path, "X-Plex-Token": token},
                            timeout=15)
        if resp.status_code >= 400:
            log.warning("Plex API scan HTTP %s for %s; falling back to host scan queue",
                        resp.status_code, plex_path)
            return False
        log.info("Plex API partial scan: section %s path %s", section, plex_path)
        return True
    except Exception as exc:
        log.warning("Plex API scan failed for %s (%s); falling back to host scan queue",
                    plex_path, exc)
        return False


def _queue_plex(lines):
    try:
        with open(PLEX_QUEUE, "a", encoding="utf-8") as fh:
            for ln in lines:
                fh.write(ln + "\n")
        log.info("Queued %d Plex targeted scan(s) -> %s", len(lines), PLEX_QUEUE)
    except Exception as exc:
        log.warning("Plex targeted-scan queue write failed: %s", exc)
