"""
Cross-scheme episode numbering via TheXEM (thexem.info, free, no auth).

Some releases (anime especially) number episodes by a single absolute count
("Naruto Shippuden - 154") while requests arrive in TVDB season/episode order.
TheXEM maps tvdb <-> scene <-> absolute per show. This resolves a request's
(season, episode) to an absolute number so the file matcher can find the right
file regardless of the release's scheme.

Design: best-effort and fail-safe. Any miss (no tvdb id, no map, uncovered
show, network error) returns None and the caller falls back to normal matching.
Results are cached in SQLite, including negative results, so a show is fetched
from TheXEM at most once per TTL.
"""
import json
import logging
import sqlite3
import time
import urllib.request

from config import DB_PATH

log = logging.getLogger("numbering")

_XEM_URL = "https://thexem.info/map/all?id={tvdb}&origin=tvdb"
_UA = {"User-Agent": "curl/8.6.0"}        # thexem 403s the default urllib agent
_TTL = 30 * 24 * 3600                      # refresh real maps monthly
_NEG_TTL = 7 * 24 * 3600                   # remember "no map" for a week
_schema_ready = False


def _conn():
    global _schema_ready
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.execute("PRAGMA busy_timeout=8000")
    if not _schema_ready:
        c.execute("""CREATE TABLE IF NOT EXISTS numbering_cache (
            tvdb_id    INTEGER PRIMARY KEY,
            xem_json   TEXT,
            fetched_at INTEGER NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS tvdb_id_map (
            imdb_id     TEXT PRIMARY KEY,
            tvdb_id     INTEGER,
            resolved_at INTEGER NOT NULL)""")
        c.commit()
        _schema_ready = True
    return c


def _http_json(url):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=8) as r:   # in the resolve path; keep short
        return json.loads(r.read())


def resolve_tvdb_id(imdb_id, tmdb_id=None):
    """imdb_id -> tvdb_id (via mycelium metadata + TMDB external_ids), cached.

    Returns None (and caches that) when the show has no TVDB id or lookups fail.
    """
    if not imdb_id:
        return None
    now = int(time.time())
    with _conn() as c:
        row = c.execute("SELECT tvdb_id, resolved_at FROM tvdb_id_map WHERE imdb_id=?", (imdb_id,)).fetchone()
        if row and now - row[1] < (_NEG_TTL if row[0] is None else _TTL):
            return row[0]
    tvdb = None
    try:
        import tmdb as _tmdb
        if not tmdb_id:
            # prefer a tmdb_id mycelium already knows, else reverse-lookup
            with _conn() as c:
                r = c.execute("SELECT tmdb_id FROM monitored_series WHERE imdb_id=? AND tmdb_id IS NOT NULL", (imdb_id,)).fetchone()
            tmdb_id = (r[0] if r else None) or _tmdb.find_by_imdb(imdb_id, kind="tv")
        if tmdb_id:
            data = _tmdb._get(f"/tv/{tmdb_id}/external_ids")
            tvdb = (data or {}).get("tvdb_id") or None
    except Exception as exc:
        log.debug("tvdb resolve failed for %s: %s", imdb_id, exc)
        return None      # transient: do not cache a failure as authoritative
    with _conn() as c:
        c.execute("INSERT INTO tvdb_id_map (imdb_id, tvdb_id, resolved_at) VALUES (?,?,?) "
                  "ON CONFLICT(imdb_id) DO UPDATE SET tvdb_id=excluded.tvdb_id, resolved_at=excluded.resolved_at",
                  (imdb_id, tvdb, now))
        c.commit()
    return tvdb


def _index(entries):
    """Build {(tvdb_season, tvdb_episode): absolute} from a TheXEM map."""
    m = {}
    for e in (entries or []):
        t = e.get("tvdb")
        if t and t.get("absolute"):
            m[(t["season"], t["episode"])] = t["absolute"]
    return m


def _load_map(tvdb_id):
    """Return {(season, episode): absolute} for the tvdb scheme, cached. {} if none."""
    now = int(time.time())
    with _conn() as c:
        row = c.execute("SELECT xem_json, fetched_at FROM numbering_cache WHERE tvdb_id=?", (tvdb_id,)).fetchone()
    if row and now - row[1] < (_NEG_TTL if not row[0] else _TTL):
        try:
            return _index(json.loads(row[0]) if row[0] else None)
        except Exception:
            return {}
    data = None
    try:
        d = _http_json(_XEM_URL.format(tvdb=tvdb_id))
        if d.get("result") == "success" and isinstance(d.get("data"), list):
            data = d["data"]
    except Exception as exc:
        log.debug("thexem fetch failed for tvdb %s: %s", tvdb_id, exc)
        return {}        # transient: do not persist as "no map"
    with _conn() as c:
        c.execute("INSERT INTO numbering_cache (tvdb_id, xem_json, fetched_at) VALUES (?,?,?) "
                  "ON CONFLICT(tvdb_id) DO UPDATE SET xem_json=excluded.xem_json, fetched_at=excluded.fetched_at",
                  (tvdb_id, json.dumps(data) if data is not None else None, now))
        c.commit()
    return _index(data)


def to_absolute(imdb_id, season, episode, tmdb_id=None):
    """Resolve a TVDB (season, episode) to an absolute episode number.

    Returns None when there is no mapping (normal Western shows, uncovered
    shows, or any failure), so the caller keeps its default behaviour.
    """
    try:
        tvdb_id = resolve_tvdb_id(imdb_id, tmdb_id)
        if not tvdb_id:
            return None
        return _load_map(tvdb_id).get((int(season), int(episode)))
    except Exception as exc:
        log.debug("to_absolute failed (%s S%sE%s): %s", imdb_id, season, episode, exc)
        return None
