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
from pathlib import Path

import requests

import config
try:
    import settings
except Exception:
    settings = None

log = logging.getLogger(__name__)


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

_lock = threading.Lock()
_pending = set()       # {(kind, folder)}
_timer = None


def _top_folder(strm_path):
    """Return ('series'|'movies', '<top folder>') for a .strm under MEDIA_PATH, else None."""
    try:
        parts = Path(strm_path).relative_to(Path(MEDIA_PATH)).parts
    except Exception:
        return None
    if len(parts) < 2 or parts[0] not in ("series", "movies"):
        return None
    return (parts[0], parts[1])


def mark(strm_path):
    """Record a newly-written .strm so its folder gets a debounced targeted scan."""
    info = _top_folder(strm_path)
    if not info:
        return
    global _timer
    with _lock:
        _pending.add(info)
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
    jf_updates, plex_lines = [], []
    for kind, folder in batch:
        jf_updates.append({"Path": "%s/%s/%s" % (JF_LIBRARY_ROOT, kind, folder),
                           "UpdateType": "Created"})
        if kind == "series":
            plex_lines.append("%s\t%s/%s" % (PLEX_SECTION_TV, PLEX_TV_ROOT, folder))
        else:
            plex_lines.append("%s\t%s/%s" % (PLEX_SECTION_MOVIE, PLEX_MOVIE_ROOT, folder))
    _scan_jellyfin(jf_updates)
    _queue_plex(plex_lines)


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


def _queue_plex(lines):
    try:
        with open(PLEX_QUEUE, "a", encoding="utf-8") as fh:
            for ln in lines:
                fh.write(ln + "\n")
        log.info("Queued %d Plex targeted scan(s) -> %s", len(lines), PLEX_QUEUE)
    except Exception as exc:
        log.warning("Plex targeted-scan queue write failed: %s", exc)
