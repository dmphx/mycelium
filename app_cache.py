"""
Gunicorn entrypoint (thin override over app:app).

Imports the real Flask app unchanged, then applies the wanted-episode queue
prioritisation patch below. The gunicorn CMD stays `app_cache:app` SOLELY to
carry that patch — see 15b-media-automation.yml.

── SSD prefetch cache RETIRED 2026-07-22 ────────────────────────────────────
This file used to also install a local-SSD prefetch cache (spore_cache) in
front of /spore-stream. It was removed. Reasons (measured, not assumed):
  * Cold start is already fast: a materialized token's CDN path measured
    TTFB 0.2s / 48 MB/s (383 Mbit/s) sustained. The Watch-Together buffering
    was the prefetch COLLISION (4x8MB prefetch storm sharing one TorBox
    byte-rate bucket with the playback it was "helping"), not TorBox latency.
  * The Direct Play byte path never used the cache anyway: spore_server.py
    (:8089, the Plex .so interceptor's byte server) _fetch_range()s straight
    from the CDN — zero spore_cache reference. So re-watches bypassed it.
  * For MKV the cache was write-only regardless: app.py 302s MKV to the CDN
    before mp4_faststart._get (the only cache read site). Measured 24h:
    14x "302 to CDN" vs 0x "proxying bytes".
  * At 3-5 users on distinct titles (distinct CDN urls = distinct rate
    buckets) TorBox never bottlenecks, so the cache's founding premise
    ("~40MB/s ceiling, collapses >32 concurrent") does not apply here.
The only way to FILL the cache was prefetch, which was the hazard itself, so
retiring removes the collision risk permanently rather than gating it.
Old override backups (app_cache.py.bak-*) retain the full cache implementation
if it is ever needed for a different (higher-concurrency) deployment.

Deploy: gunicorn ... app_cache:app   (instead of app:app)
"""
import logging

from app import app          # runs the full mycelium init, exactly like app:app

log = logging.getLogger("spore_cache")

# ── wanted-episode queue prioritisation (Onyx patch 2026-07-11) ──────────────
# Root cause: db.get_wanted_episodes() ordered by (title, season, episode), so
# the newest episode of every ongoing show sat at the very back of a ~290k-row
# queue and monitor.run_series_check never reached it (alphabetical + oldest-
# first). Re-order to fewest-attempts-then-newest-aired so current episodes of
# ongoing shows (e.g. American Dad S22) are processed first each pass, and the
# high-attempt un-gettable back-catalog naturally sinks to the back.
try:
    import db as _qp_db

    _qp_orig_get_wanted = _qp_db.get_wanted_episodes

    def _qp_get_wanted(max_attempts: int = 10):
        rows = _qp_orig_get_wanted(max_attempts)
        # stable sort: secondary key first (air_date DESC = newest), then
        # primary key (attempt_count ASC = never-tried first).
        rows.sort(key=lambda e: (e.get("air_date") or ""), reverse=True)
        rows.sort(key=lambda e: (e.get("attempt_count") or 0))
        return rows

    _qp_get_wanted.__name__ = _qp_orig_get_wanted.__name__
    _qp_db.get_wanted_episodes = _qp_get_wanted
    log.info("queue-priority: get_wanted_episodes re-ordered (attempt asc, air_date desc)")
except Exception as exc:  # never block startup
    log.warning("queue-priority: patch failed, using default order: %s", exc)
