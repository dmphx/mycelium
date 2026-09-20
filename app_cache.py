"""
Gunicorn entrypoint (thin override over app:app).

Imports the real Flask app unchanged. Nothing else: this module is only a
named entrypoint, kept because the gunicorn CMD in 15b-media-automation.yml
says `app_cache:app`.

── queue prioritisation patch RETIRED 2026-09-20 ─────────────────────────────
This file used to wrap db.get_wanted_episodes so the queue ran fewest
attempts first, then newest aired. db.get_wanted_episodes orders that way
itself now, and it takes a `limit` that the wrapper never accepted. Since
d107142 (2026-08-09) monitor passes that limit, so every hourly wanted
episode search died in TypeError before searching anything. Only the
fresh-release lane (get_fresh_wanted_episodes, never wrapped) kept running,
so new episodes still arrived while 203k older wanted rows sat untouched
for six weeks. Do not wrap a db helper from here: this module cannot see a
signature change in the module it patches.

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

The core application now has a separate demand-only block cache in
spore_readthrough.py. It fetches no bytes until a viewer asks for them, shares
same-title reads, and is not the retired whole-file prefetcher described here.

Deploy: gunicorn ... app_cache:app   (instead of app:app)
"""
from app import app  # noqa: F401
