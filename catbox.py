"""Catbox-style lazy materialization for TorBox.

When CATBOX_MODE is enabled, .strm files contain a proxy URL pointing to
/stream/<token>. On playback the webhook ensures the torrent is in TorBox
(re-adding from the cached magnet if it has been released), fetches a fresh
CDN URL, and 307-redirects the client.

After CATBOX_IDLE_MINUTES of inactivity an item is removed from TorBox to
stay within TorBox's 30-day cache retention policy. The virtual entry stays
in the DB so playback works again on the next request.

Resolved CDN URLs are cached in-memory per token to avoid hammering TorBox's
60/hour createtorrent + 300/min general rate limits when Jellyfin sends
multiple probe/seek requests for the same item in quick succession.
"""
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta

import cachetools

import db
import settings as _settings
import torbox
from config import CATBOX_HOST, CATBOX_IDLE_MINUTES as _CATBOX_IDLE_MINUTES_DEFAULT

log = logging.getLogger(__name__)


# All module-level caches below are bounded with cachetools so a stuck or
# adversarial caller cannot grow them indefinitely. TTLs match the existing
# expiry semantics; maxsize caps are sized for ~10k concurrent tokens, well
# above the household library sizes Mycelium targets.
_URL_CACHE_TTL_SEC = 82800  # 23 hours  -  within TorBox CDN URL 24h validity
ON_PLAY_READY_TIMEOUT_SEC = 45  # max wait on-play before giving up (cached = seconds)
_url_cache: "cachetools.TTLCache[str, tuple[str, float]]" = cachetools.TTLCache(
    maxsize=10000, ttl=_URL_CACHE_TTL_SEC
)
_url_cache_lock = threading.Lock()

# Failure cooldown: after a failed materialize (429, timeout, no file found),
# block retries for a short window so Jellyfin's burst of probe requests doesn't
# hammer TorBox with repeated createtorrent calls.
_FAIL_COOLDOWN_SEC = 30        # standard failure (readd blocked, no file)
_FAIL_COOLDOWN_429_SEC = 120   # TorBox 429  -  back off longer
# A torrent that was added but is not "ready" yet is a transient cold-start state
# that usually clears within seconds. A hard 30s wall here turns a normal first
# play into a "transcoder crashed" for half a minute even though the CDN URL is
# about to exist, so give it a short cooldown and let the next play pick it up as
# soon as TorBox finishes readying the file.
_FAIL_COOLDOWN_READYING_SEC = 8   # added, waiting for TorBox/RD "ready"
# TTL on the cache equals the longest cooldown; per-entry expiries still
# enforced explicitly via the monotonic-timestamp value (older entries with
# shorter cooldowns get reported as expired sooner).
_fail_cache: "cachetools.TTLCache[str, float]" = cachetools.TTLCache(
    maxsize=10000, ttl=_FAIL_COOLDOWN_429_SEC
)
_fail_cache_lock = threading.Lock()

# ── Reason codes (structured, for playability_state + admin UI) ───────────────
REASON_UNKNOWN_TOKEN    = "UNKNOWN_TOKEN"
REASON_NO_IMDB          = "NO_IMDB"
REASON_TORRENTIO_EMPTY  = "TORRENTIO_EMPTY"
REASON_NO_CACHED        = "NO_CACHED_RELEASE"
REASON_WAIT_TIMEOUT     = "WAIT_TIMEOUT"
REASON_NO_FILE          = "NO_FILE"
REASON_RD_429           = "RD_429"
REASON_TB_429           = "TB_429"
REASON_ADD_FAILED       = "ADD_FAILED"
REASON_SEARCH_ERROR     = "SEARCH_UNAVAILABLE"

def _fail_get(token: str) -> bool:
    with _fail_cache_lock:
        exp = _fail_cache.get(token)
        if exp is None:
            return False
        if exp > time.monotonic():
            return True
        del _fail_cache[token]
        return False

def _fail_put(token: str, ttl: int = _FAIL_COOLDOWN_SEC) -> None:
    with _fail_cache_lock:
        _fail_cache[token] = time.monotonic() + ttl

# LRU because a Lock has no useful TTL; cap protects against unbounded growth
# when scan probes or malformed callers spray distinct tokens. Evicted locks
# are discarded (any waiter dies with the lock owner, not a problem in practice
# since the owner thread either finishes or holds a process-lifetime Python ref).
_token_locks: "cachetools.LRUCache[str, threading.Lock]" = cachetools.LRUCache(maxsize=10000)

# Per-content search cache so Zilean/Torrentio are called at most once per hour
# for the same (imdb_id, season, episode) combo, regardless of how many tokens share it.
# The (expiry, result) tuple keeps the historic explicit TTL behaviour; the
# cachetools wrapper exists only to bound size.
_search_cache: "cachetools.LRUCache[tuple, tuple[float, object]]" = cachetools.LRUCache(maxsize=10000)
_search_cache_lock = threading.Lock()
_SEARCH_HIT_TTL    = 300    # 5 min: re-check soon if a cached release was found
_SEARCH_MISS_TTL   = 21600  # 6 h:  nothing cached  -  back off (matches _fail_put below)
_SEARCH_ERROR_TTL  = 30     # transient source failure must remain quickly retryable
_next_prepare_cache: "cachetools.TTLCache[str, bool]" = cachetools.TTLCache(
    maxsize=10000, ttl=3600
)
_next_prepare_lock = threading.Lock()
_token_locks_lock = threading.Lock()

# ── scan/probe burst detection ────────────────────────────────────────────────
# A media-server library scan opens many DISTINCT .strm URLs in a short burst,
# whereas real playback touches a single token (plus seeks on that same token).
# When we see a burst of distinct tokens we treat the requests as scan probes and
# refuse to re-add idle-released torrents  -  re-materializing the whole library on
# every scan is slow and churns TorBox's createtorrent quota. Items already live
# in TorBox still resolve cheaply (mylist is cached), so they probe fine.
_SCAN_WINDOW_SEC = 25
# Deliberately above this deployment's realistic concurrent-user count (a
# handful of real users, see CLAUDE.md) so several people starting different
# titles within the same 25s window isn't misread as a library scan and
# denied re-add. A real scan opens far more than this many distinct items.
_SCAN_DISTINCT_THRESHOLD = 8
_recent_tokens: dict[str, float] = {}
_recent_lock = threading.Lock()


def _is_scan_burst(token: str) -> bool:
    """Record this token request and report whether we appear to be inside a
    library-scan burst (many distinct tokens within the recent window).

    Only counts tokens that actually exist in the virtual_items table so an
    attacker (or noisy probe) hitting /stream/<random> repeatedly cannot
    trip the burst threshold and force legitimate playbacks to be treated
    as scans.
    """
    if not db.get_virtual_item(token):
        return False
    now = time.monotonic()
    with _recent_lock:
        for t, ts in list(_recent_tokens.items()):
            if now - ts > _SCAN_WINDOW_SEC:
                del _recent_tokens[t]
        _recent_tokens[token] = now
        return len(_recent_tokens) >= _SCAN_DISTINCT_THRESHOLD


def _token_lock(token: str) -> threading.Lock:
    with _token_locks_lock:
        lock = _token_locks.get(token)
        if lock is None:
            lock = threading.Lock()
            _token_locks[token] = lock
        return lock


def _content_key(item: dict) -> str | None:
    imdb_id = item.get("imdb_id")
    if not imdb_id:
        return None
    season, episode = item.get("season"), item.get("episode")
    if season and episode:
        return f"{imdb_id}:S{season:02d}E{episode:02d}"
    return imdb_id


_TOUCH_DEBOUNCE_SEC = 60  # the last_played/play_count precision we actually need
_touch_cache: "cachetools.TTLCache[str, bool]" = cachetools.TTLCache(
    maxsize=10000, ttl=_TOUCH_DEBOUNCE_SEC
)
_touch_cache_lock = threading.Lock()
_materialize_context = threading.local()


def _touch_debounced(token: str) -> None:
    """db.touch_virtual_item() is a synchronous UPDATE + commit, and materialize's
    cache-hit path runs on every byte-range request, so one viewer fires dozens a
    minute. SQLite takes one writer at a time, so under concurrent playback those
    writes serialize against every other session's. play_count/last_played do not
    need per-chunk precision."""
    with _touch_cache_lock:
        if token in _touch_cache:
            return
        _touch_cache[token] = True
    db.touch_virtual_item(token)
    try:
        import enrichment
        enrichment.queue_from_playback(token)
    except Exception as exc:
        log.debug("Enrichment playback queue failed for %s: %s", token, exc)


def _record_playback_touch(token: str) -> None:
    """Touch real playback, but not background preparation reads."""
    if getattr(_materialize_context, "background", False):
        return
    _touch_debounced(token)


def _cache_get(token: str) -> str | None:
    with _url_cache_lock:
        entry = _url_cache.get(token)
        if entry and entry[1] > time.monotonic():
            return entry[0]
        if entry:
            del _url_cache[token]
    return None


def _cache_put(token: str, url: str) -> None:
    with _url_cache_lock:
        _url_cache[token] = (url, time.monotonic() + _URL_CACHE_TTL_SEC)


def cache_url(token: str, url: str) -> None:
    """Store a CDN URL in the in-memory cache (used by preload)."""
    _cache_put(token, url)


def invalidate_url_cache(token: str | None = None) -> None:
    with _url_cache_lock:
        if token is None:
            _url_cache.clear()
        else:
            _url_cache.pop(token, None)


def catbox_host() -> str:
    """Externally reachable host for the .strm proxy URL. Settings DB first,
    env/config fallback. Must be reachable from Jellyfin."""
    return (_settings.get("CATBOX_HOST", CATBOX_HOST) or "").strip()


def proxy_url(token: str) -> str:
    return f"{catbox_host().rstrip('/')}/stream/{token}"


def register(info_hash: str, magnet: str, title: str, media_type: str,
             strm_path: str | None = None, torbox_id: int | None = None,
             file_id: int | None = None, imdb_id: str | None = None,
             quality: str | None = None, source: str | None = None,
             size_gb: float | None = None, season: int | None = None,
             episode: int | None = None, year: int | None = None,
             protocol: str = "torrent", nzb_url: str | None = None,
             usenet_id: int | None = None) -> str:
    token = uuid.uuid4().hex[:16]
    db.insert_virtual_item(token, info_hash, magnet, title, media_type,
                            strm_path=strm_path, torbox_id=torbox_id, file_id=file_id,
                            imdb_id=imdb_id, quality=quality, source=source,
                            size_gb=size_gb, season=season, episode=episode, year=year,
                            protocol=protocol, nzb_url=nzb_url, usenet_id=usenet_id)
    return token


def materialize(token: str, allow_readd: bool | None = None,
                record_playback: bool = True) -> str | None:
    """Ensure the torrent is in TorBox and return a fresh stream URL.
    Cached URLs are served for up to 30 minutes to absorb Jellyfin's probe/seek
    bursts without spending TorBox createtorrent rate-limit slots.

    allow_readd controls whether an idle-released torrent may be re-added (which
    can block ~45s waiting for it to become ready). When None (default), it is
    auto-decided: during a scan-burst we skip the re-add so the scan stays fast.
    """
    previous_background = getattr(_materialize_context, "background", False)
    _materialize_context.background = previous_background or not record_playback
    try:
        return _materialize_inner(token, allow_readd)
    finally:
        _materialize_context.background = previous_background


def _materialize_inner(token: str, allow_readd: bool | None = None) -> str | None:
    cached = _cache_get(token)
    if cached:
        _record_playback_touch(token)
        return cached

    # Respect failure cooldown  -  don't spam TorBox after a recent failed attempt.
    if _fail_get(token):
        return None

    if allow_readd is None:
        allow_readd = not _is_scan_burst(token)

    with _token_lock(token):
        # Re-check inside the lock: another thread may have succeeded or set cooldown.
        cached = _cache_get(token)
        if cached:
            _record_playback_touch(token)
            return cached
        if _fail_get(token):
            return None
        url = _materialize_locked(token, allow_readd=allow_readd)
        if url:
            _cache_put(token, url)
            if not getattr(_materialize_context, "background", False):
                _schedule_next_episode_preload(token)
        else:
            # Do not clobber a more specific cooldown that _materialize_locked
            # already set (the short readying window, or a long 429 / no-cached
            # back-off)  -  only apply the default when none is active.
            if not _fail_get(token):
                _fail_put(token)
        return url


def _pick_usenet_file_id(item: dict, virtual_item: dict) -> int | None:
    """Pick the right file inside a completed TorBox usenet download.

    Movies: largest non-trailer video file.
    Episodes: the shared episode matcher, which only returns a confident match
    (never a file tagged as a different episode, and never a blind largest-file
    guess).  Returns None when it cannot match, so the caller can re-scrape.
    """
    import strm_generator
    files = item.get("files") or []
    if not files:
        return None
    # Some usenet entries expose only short_name; normalise to 'name'.
    norm = [{**f, "name": (f.get("name") or f.get("short_name") or "")} for f in files]
    if virtual_item.get("media_type") == "movie":
        main = strm_generator._pick_main_movie_file(norm)
        return main.get("id") if main else None
    s_num = virtual_item.get("season")
    e_num = virtual_item.get("episode")
    if s_num and e_num:
        absolute = None
        try:
            import numbering
            absolute = numbering.to_absolute(virtual_item.get("imdb_id"), s_num, e_num)
        except Exception:
            absolute = None
        main = strm_generator._pick_episode_file(norm, s_num, e_num, absolute=absolute)
        return main.get("id") if main else None
    vids = [f for f in norm if strm_generator._is_video(f["name"])]
    return vids[0].get("id") if len(vids) == 1 else None


def _materialize_usenet(token: str, item: dict) -> str | None:
    """On-play materialize for a usenet virtual_item.

    Path:
      1. If stored usenet_id exists and download is ready, just refresh the CDN URL.
      2. If usenet_id exists but item is still downloading, wait briefly.
      3. If no usenet_id (rare: lost between submit and persist), resubmit the
         stored NZB URL.
    """
    import strm_generator as _sg

    usenet_id = item.get("usenet_id")
    nzb_url = item.get("nzb_url") or item.get("magnet")  # magnet col was the fallback

    if not usenet_id and nzb_url:
        # Resubmit; happens only if we crashed between add_nzb returning and the
        # row being persisted (very rare). Costs 1 quota slot.
        try:
            log.info("Catbox/usenet: no usenet_id stored for %s  -  resubmitting NZB",
                     item.get("title"))
            result = torbox.add_nzb(nzb_url, name=item.get("title"),
                                     reason="catbox-usenet-resubmit")
            usenet_id = (result or {}).get("id")
            if usenet_id:
                db.update_virtual_usenet_id(token, usenet_id)
        except torbox.RateLimited:
            _fail_put(token, _FAIL_COOLDOWN_429_SEC)
            return None
        except Exception as exc:
            log.warning("Catbox/usenet: resubmit failed for %s: %s",
                        item.get("title"), exc)
            _fail_put(token, _FAIL_COOLDOWN_SEC)
            return None

    if not usenet_id:
        log.warning("Catbox/usenet: no usenet_id and no nzb_url for %s", item.get("title"))
        return None

    live = torbox.find_usenet_by_id(usenet_id)
    if not live or not torbox._is_ready(live):
        # Wait up to ON_PLAY_READY_TIMEOUT_SEC. Usenet downloads of a typical
        # movie complete in 30-120s; first playback may need this window.
        log.info("Catbox/usenet: %s not ready (id=%s)  -  waiting up to %ds",
                 item.get("title"), usenet_id, ON_PLAY_READY_TIMEOUT_SEC)
        live = torbox.wait_until_ready_usenet(usenet_id, timeout=ON_PLAY_READY_TIMEOUT_SEC)
        if not live or not torbox._is_ready(live):
            _fail_put(token, _FAIL_COOLDOWN_SEC)
            return None

    file_id = _pick_usenet_file_id(live, item)
    if file_id is None:
        log.warning("Catbox/usenet: no video file in TorBox usenet id=%s", usenet_id)
        _fail_put(token, _FAIL_COOLDOWN_SEC)
        return None

    url = _sg._get_usenet_stream_url(usenet_id, file_id)
    if not url:
        _fail_put(token, _FAIL_COOLDOWN_SEC)
        return None
    log.info("Catbox/usenet: served %s (usenet_id=%s, file=%s)",
             item.get("title"), usenet_id, file_id)
    return url


def _rd_get_url(item: dict, rd_id: str) -> str | None:
    """Get a playable URL from RealDebrid for this virtual item."""
    import realdebrid as _rd
    import strm_generator
    if item["media_type"] == "movie":
        return _rd.get_main_video_url(rd_id)
    # Episode: confident match only (SxxExx or NNxNN); never a blind largest-file
    # guess and never a file tagged as a different episode.
    pairs = _rd.get_video_files_with_urls(rd_id)
    if not pairs:
        return None
    s_num, e_num = item.get("season"), item.get("episode")
    name = lambda f: f.get("path") or f.get("name") or ""
    if s_num and e_num:
        want = (int(s_num), int(e_num))
        matched = [(f, u) for f, u in pairs if strm_generator._file_episode(name(f)) == want]
        if matched:
            return max(matched, key=lambda fu: fu[0].get("bytes") or 0)[1]
        untagged = [(f, u) for f, u in pairs if strm_generator._file_episode(name(f)) is None]
        if len(untagged) == 1:
            return untagged[0][1]
        return None
    return pairs[0][1] if len(pairs) == 1 else None


def _materialize_locked(token: str, allow_readd: bool = True,
                        alternate_attempts: int = 0) -> str | None:
    item = db.get_virtual_item(token)
    if not item:
        log.warning("Catbox: unknown token %s", token)
        _metrics_inc("failed")
        return None

    ckey = _content_key(item)
    debrid_provider = (item.get("debrid_provider") or "torbox").lower()
    protocol = (item.get("protocol") or "torrent").lower()
    rematerialized = False

    # ── TorBox usenet path ────────────────────────────────────────────────────
    if protocol == "usenet":
        url = _materialize_usenet(token, item)
        if url:
            _record_playback_touch(token)
            if ckey:
                db.update_playability_ok(ckey, "torbox-usenet")
        return url

    # ── RealDebrid path ───────────────────────────────────────────────────────
    if debrid_provider == "realdebrid":
        import realdebrid as _rd
        rd_id = item.get("rd_id")

        # Fast path: rd_id still live in RD library
        if rd_id:
            info = _rd.get_info(rd_id)
            if info and info.get("status") == "downloaded":
                url = _rd_get_url(item, rd_id)
                if url:
                    _record_playback_touch(token)
                    if ckey:
                        db.update_playability_ok(ckey, "realdebrid")
                    _metrics_inc("ok" if not rematerialized else "rematerialized")
                    return url
            log.info("Catbox/RD: %s no longer in RD library  -  will re-add", item["title"])
            db.update_virtual_rd_id(token, None)
            rd_id = None
            rematerialized = True

        if not allow_readd:
            log.debug("Catbox/RD: skipping re-add for %s during scan-burst probe", item["title"])
            return None

        rematerialized = True
        log.info("Catbox/RD: searching cached release for %s", item["title"])
        fresh = _search_cached_release(item)
        if fresh is _SEARCH_UNAVAILABLE:
            _fail_put(token, _FAIL_COOLDOWN_SEC)
            if ckey:
                db.update_playability_fail(ckey, REASON_SEARCH_ERROR)
            return None
        if not fresh:
            log.error("Catbox/RD: no cached release for %s  -  keeping .strm, retry in 6h",
                      item["title"])
            _fail_put(token, 21600)  # 6h  -  repair job will clean up if truly dead
            if ckey:
                db.update_playability_fail(ckey, REASON_NO_CACHED)
            return None

        new_hash, new_magnet, provider = fresh
        db.update_virtual_item_upgrade(token, new_hash, new_magnet, None, None)
        db.update_virtual_debrid_provider(token, provider)
        if provider == "torbox":
            # Search found TorBox  -  fall through to TorBox block below
            debrid_provider = "torbox"
            item["debrid_provider"] = "torbox"
            item["info_hash"] = new_hash
            item["file_id"] = None
        else:
            try:
                result = _rd.add_magnet(new_magnet)
                rd_id = result["id"]
                rd_info = _rd.wait_until_ready(rd_id)
                if not rd_info:
                    log.error("Catbox/RD: wait_until_ready timed out for %s", item["title"])
                    _fail_put(token, _FAIL_COOLDOWN_READYING_SEC)
                    if ckey:
                        db.update_playability_fail(ckey, REASON_WAIT_TIMEOUT)
                    return None
                db.update_virtual_rd_id(token, rd_id)
                url = _rd_get_url(item, rd_id)
                if url:
                    _record_playback_touch(token)
                    if ckey:
                        db.update_playability_ok(ckey, "realdebrid")
                    _metrics_inc("rematerialized")
                return url
            except Exception as exc:
                is_429 = "429" in str(exc)
                log.error("Catbox/RD: add_magnet failed for %s: %s", item["title"], exc)
                _fail_put(token, _FAIL_COOLDOWN_429_SEC if is_429 else _FAIL_COOLDOWN_SEC)
                if ckey:
                    db.update_playability_fail(ckey, REASON_RD_429 if is_429 else REASON_ADD_FAILED)
                return None

    # ── TorBox path ───────────────────────────────────────────────────────────
    torbox_id = item["torbox_id"]

    # Fast path: cached torbox_id still live in TorBox.
    if torbox_id:
        live = torbox.find_by_id(torbox_id)
        if not live or not torbox._is_ready(live):
            torbox_id = None
            rematerialized = True

    # Second chance: torrent may still be in TorBox library under its hash.
    if not torbox_id and item.get("info_hash"):
        existing = torbox.find_by_hash(item["info_hash"])
        if existing and torbox._is_ready(existing):
            torbox_id = existing["id"]
            db.update_virtual_torbox_id(token, torbox_id)
            log.info("Catbox: %s still in library (id=%s)  -  no re-add needed",
                     item["title"], torbox_id)

    # Third chance: use the stored magnet to add directly  -  covers both items that
    # previously had a torbox_id (fell out of mylist top-1000) and freshly lazy-
    # registered items (torbox_id=NULL, magnet already selected at request time).
    if not torbox_id and item.get("magnet") and allow_readd:
        try:
            log.info("Catbox: %s adding stored magnet", item["title"])
            added = torbox.add_magnet(item["magnet"], reason="catbox-readd")
            _tid = added.get("id") or added.get("torrent_id")
            existing = added if _tid and torbox._is_ready(added) else (
                torbox.find_by_id(_tid) if _tid else
                torbox.find_by_hash(item["info_hash"], force_refresh=True)
            )
            if not (existing and torbox._is_ready(existing)) and _tid:
                existing = torbox.wait_until_ready(
                    item["info_hash"], timeout=ON_PLAY_READY_TIMEOUT_SEC, torrent_id=_tid)
            if existing and torbox._is_ready(existing):
                torbox_id = existing["id"]
                db.update_virtual_torbox_id(token, torbox_id)
                log.info("Catbox: %s added via stored magnet (id=%s)", item["title"], torbox_id)
        except Exception as exc:
            exc_str = str(exc)
            is_rate_limited = (isinstance(exc, torbox.RateLimited)
                               or "429" in exc_str or "403" in exc_str)
            log.warning("Catbox: stored-magnet re-add failed for %s: %s", item["title"], exc)
            if is_rate_limited:
                # 429 = rate limited; 403 = API key/plan issue  -  either way
                # there is no point continuing to checkcached, it will also fail.
                _fail_put(token, _FAIL_COOLDOWN_429_SEC)
                if ckey:
                    db.update_playability_fail(ckey, REASON_TB_429)
                return None

    # Fourth chance: known hash may be cached on RD even if TorBox doesn't have it.
    # This avoids a full Torrentio search for items where Torrentio returns 0 results.
    if not torbox_id and item.get("info_hash") and allow_readd:
        try:
            import realdebrid as _rd
            if _rd.is_configured():
                known_hash = item["info_hash"].lower()
                rd_instant = _rd.check_cached([known_hash])
                if known_hash in {h.lower() for h in rd_instant}:
                    log.info("Catbox: known hash cached on RD for %s  -  switching to RD path",
                             item["title"])
                    db.update_virtual_debrid_provider(token, "realdebrid")
                    magnet = item.get("magnet") or f"magnet:?xt=urn:btih:{known_hash}"
                    rd_result = _rd.add_magnet(magnet)
                    rd_id = rd_result["id"]
                    rd_info = _rd.wait_until_ready(rd_id)
                    if rd_info:
                        db.update_virtual_rd_id(token, rd_id)
                        url = _rd_get_url(item, rd_id)
                        if url:
                            _record_playback_touch(token)
                            if ckey:
                                db.update_playability_ok(ckey, "realdebrid")
                            _metrics_inc("rematerialized")
                            return url
                    log.error("Catbox: RD wait_until_ready timed out for %s", item["title"])
                    _fail_put(token, _FAIL_COOLDOWN_READYING_SEC)
                    if ckey:
                        db.update_playability_fail(ckey, REASON_WAIT_TIMEOUT)
                    return None
        except Exception as exc:
            log.warning("Catbox: RD known-hash check failed for %s: %s", item["title"], exc)

    if not torbox_id and not allow_readd:
        log.debug("Catbox: skipping re-add for %s during scan-burst probe", item["title"])
        return None

    if not torbox_id:
        rematerialized = True
        log.info("Catbox: searching fresh cached release for %s", item["title"])
        fresh = _search_cached_release(item)
        if fresh is _SEARCH_UNAVAILABLE:
            _fail_put(token, _FAIL_COOLDOWN_SEC)
            if ckey:
                db.update_playability_fail(ckey, REASON_SEARCH_ERROR)
            return None
        if not fresh:
            log.error("Catbox: no cached release found for %s  -  keeping .strm, retry in 6h",
                      item["title"])
            _fail_put(token, 21600)  # 6h  -  repair job will clean up if truly dead
            if ckey:
                db.update_playability_fail(ckey, REASON_NO_CACHED)
            return None

        new_hash, new_magnet, provider = fresh
        db.update_virtual_debrid_provider(token, provider)
        if provider == "realdebrid":
            # Search found RD  -  switch provider and handle via RD
            import realdebrid as _rd
            db.update_virtual_item_upgrade(token, new_hash, new_magnet, None, None)
            try:
                result = _rd.add_magnet(new_magnet)
                rd_id = result["id"]
                rd_info = _rd.wait_until_ready(rd_id)
                if not rd_info:
                    _fail_put(token, _FAIL_COOLDOWN_READYING_SEC)
                    if ckey:
                        db.update_playability_fail(ckey, REASON_WAIT_TIMEOUT)
                    return None
                db.update_virtual_rd_id(token, rd_id)
                item["rd_id"] = rd_id
                url = _rd_get_url(item, rd_id)
                if url:
                    _record_playback_touch(token)
                    if ckey:
                        db.update_playability_ok(ckey, "realdebrid")
                    _metrics_inc("rematerialized")
                return url
            except Exception as exc:
                is_429 = "429" in str(exc)
                log.error("Catbox: RD add_magnet failed for %s: %s", item["title"], exc)
                _fail_put(token, _FAIL_COOLDOWN_429_SEC if is_429 else _FAIL_COOLDOWN_SEC)
                if ckey:
                    db.update_playability_fail(ckey, REASON_RD_429 if is_429 else REASON_ADD_FAILED)
                return None

        if new_hash != (item.get("info_hash") or "").lower():
            log.info("Catbox: swapping hash %s → %s", item["title"], new_hash)
            db.update_virtual_item_upgrade(token, new_hash, new_magnet, None, None)
            item["info_hash"] = new_hash
            item["file_id"] = None

        try:
            added = torbox.add_magnet(new_magnet, reason="catbox-search")
            # Use the ID from the add response to avoid a full mylist refresh.
            # TorBox returns "torrent_id" for cached adds, "id" for others.
            _tid = added.get("id") or added.get("torrent_id")
            live = added if _tid and torbox._is_ready(added) else None
            if not live:
                live = torbox.find_by_id(_tid) if _tid else None
            if not live or not torbox._is_ready(live):
                live = torbox.wait_until_ready(
                    new_hash, timeout=ON_PLAY_READY_TIMEOUT_SEC, torrent_id=_tid or None)
            if not live:
                log.error("Catbox: fresh release not ready for %s  -  keeping .strm, retry soon",
                          item["title"])
                _fail_put(token, _FAIL_COOLDOWN_READYING_SEC)
                if ckey:
                    db.update_playability_fail(ckey, REASON_WAIT_TIMEOUT)
                return None
            torbox_id = live["id"]
            db.update_virtual_torbox_id(token, torbox_id)
        except Exception as exc:
            is_429 = "429" in str(exc)
            log.error("Catbox: add_magnet failed for %s: %s", token, exc)
            _fail_put(token, _FAIL_COOLDOWN_429_SEC if is_429 else _FAIL_COOLDOWN_SEC)
            if ckey:
                db.update_playability_fail(ckey, REASON_TB_429 if is_429 else REASON_ADD_FAILED)
            return None

    file_id = item["file_id"]
    if not file_id:
        live = torbox.find_by_id(torbox_id)
        if live:
            # Backstop for the mislabeled-pack bug: if this torrent's real files
            # obviously aren't the requested item (a movie request resolving to a
            # season/complete-series pack, an oversized collection, or no video at
            # all), refuse rather than guessing a file and serving garbage. The
            # durable fix is at grab time  -  this only catches items whose bad
            # hash predates that guard.
            import release_sanity
            _kind = ("movie" if item["media_type"] == "movie"
                     else "episode" if (item.get("season") and item.get("episode"))
                     else "season_pack")
            _bad = release_sanity.verify_live_torrent(
                live, _kind, season=item.get("season"), episode=item.get("episode"),
                imdb_id=item.get("imdb_id"))
            if _bad:
                log.error("Catbox: release sanity rejected torrent %s for %s (%s)  -  "
                          "keeping .strm, not serving a mislabeled pack",
                          torbox_id, item["title"], _bad)
                alternate = _try_alternate_release(
                    token, item, ckey, f"sanity:{_bad}", alternate_attempts)
                if alternate:
                    return alternate
                _requeue_unplayable_episode(item, f"sanity:{_bad}")
                _fail_put(token, _FAIL_COOLDOWN_SEC)
                if ckey:
                    db.update_playability_fail(ckey, REASON_NO_FILE)
                return None
            import strm_generator
            if item["media_type"] == "movie":
                main = strm_generator._pick_main_movie_file(live.get("files") or [])
                if main:
                    file_id = main["id"]
                    db.update_virtual_file_id(token, file_id)
                    if ckey and item.get("info_hash"):
                        db.save_candidate_file_match(
                            ckey, item["info_hash"], file_id, main.get("name"))
                elif not (live.get("files")):
                    # TorBox returned the torrent without a files list (common for the
                    # ?id= single-item endpoint).  Use file_id=0 which tells TorBox to
                    # serve the largest file automatically  -  works for single-file movies.
                    log.info("Catbox: no files list for %s  -  using file_id=0 (auto)", item["title"])
                    file_id = 0
            else:
                files = live.get("files") or []
                s_num = item.get("season")
                e_num = item.get("episode")
                if s_num and e_num:
                    absolute = None
                    try:
                        import numbering
                        absolute = numbering.to_absolute(item.get("imdb_id"), s_num, e_num)
                    except Exception:
                        absolute = None
                    main = strm_generator._pick_episode_file(files, s_num, e_num, absolute=absolute)
                else:
                    vids = [f for f in files
                            if strm_generator._is_video(f.get("name") or "")
                            and not strm_generator._is_trailer(f)]
                    main = vids[0] if len(vids) == 1 else None
                if main:
                    file_id = main["id"]
                    db.update_virtual_file_id(token, file_id)
                    if ckey and item.get("info_hash"):
                        db.save_candidate_file_match(
                            ckey, item["info_hash"], file_id, main.get("name"))
                else:
                    # No confident match.  Do NOT guess (the old largest-file guess
                    # served the wrong episode).  Leave unresolved so it re-scrapes.
                    log.warning("Catbox: no confident file match for %s S%sE%s in torrent %s; "
                                "leaving unresolved", item["title"], s_num, e_num, torbox_id)

    if file_id is None or (not file_id and file_id != 0):
        log.error("Catbox: no playable file found for %s  -  keeping .strm, retry later", token)
        alternate = _try_alternate_release(
            token, item, ckey, REASON_NO_FILE, alternate_attempts)
        if alternate:
            return alternate
        _requeue_unplayable_episode(item, REASON_NO_FILE)
        _fail_put(token, _FAIL_COOLDOWN_SEC)
        if ckey:
            db.update_playability_fail(ckey, REASON_NO_FILE)
        return None

    import strm_generator
    url = strm_generator._get_stream_url(torbox_id, file_id)
    if url:
        _record_playback_touch(token)
        if ckey:
            db.update_playability_ok(ckey, "torbox")
        _metrics_inc("rematerialized" if rematerialized else "ok")
    else:
        _metrics_inc("failed")
    return url


def _requeue_unplayable_episode(item: dict, reason: str) -> None:
    """Return a found episode to the wanted queue after deterministic failure."""
    if item.get("media_type") != "series":
        return
    imdb_id = item.get("imdb_id")
    season = item.get("season")
    episode = item.get("episode")
    if not imdb_id or season is None or episode is None:
        return
    if db.requeue_wanted_episode(imdb_id, int(season), int(episode)):
        log.warning(
            "Catbox: requeued %s S%02dE%02d after %s",
            item.get("title") or imdb_id, int(season), int(episode), reason,
        )


def _try_alternate_release(token: str, item: dict, ckey: str | None,
                           reason: str, alternate_attempts: int) -> str | None:
    """Reject a bad content-release pairing and try a cached alternate once."""
    if not ckey or not item.get("info_hash") or alternate_attempts >= 2:
        return None
    import search_engine
    old_hash = item["info_hash"].lower()
    search_engine.reject(ckey, old_hash, reason)
    key = (item.get("imdb_id"), item.get("season"), item.get("episode"))
    with _search_cache_lock:
        _search_cache.pop(key, None)
    fresh = search_engine.next_cached_alternate(
        ckey, item.get("media_type") or "movie", item.get("imdb_id") or "",
        season=item.get("season"), episode=item.get("episode"))
    if not fresh:
        fresh = _search_cached_release(item)
    if not fresh or fresh is _SEARCH_UNAVAILABLE:
        return None
    new_hash, new_magnet, provider = fresh
    if new_hash.lower() == old_hash:
        return None
    log.warning("Catbox: switching %s from rejected hash %s to alternate %s",
                item.get("title"), old_hash, new_hash)
    db.update_virtual_item_upgrade(
        token, new_hash, new_magnet, None, f"alternate/{provider}")
    db.update_virtual_protocol(token, "torrent")
    db.update_virtual_debrid_provider(token, provider)
    invalidate_url_cache(token)
    return _materialize_locked(
        token, allow_readd=True, alternate_attempts=alternate_attempts + 1)


def _metrics_inc(result: str) -> None:
    try:
        import metrics_prom
        metrics_prom.catbox_stream_total.labels(result=result).inc()
    except Exception:
        pass


def _remove_strm(item: dict) -> None:
    """Delete the .strm file for a definitively dead item so Jellyfin stops showing it."""
    import os
    strm_path = item.get("strm_path")
    if not strm_path:
        return
    try:
        if os.path.exists(strm_path):
            os.remove(strm_path)
            log.info("Catbox: removed dead .strm %s", strm_path)
    except Exception as exc:
        log.warning("Catbox: could not remove .strm %s: %s", strm_path, exc)


_SEARCH_UNAVAILABLE = object()  # sentinel: search couldn't run (no imdb_id, network error)


def _search_cached_release(item: dict) -> object:
    """Thin cache layer around _search_best_cached_release.

    Deduplicates Zilean/Torrentio calls when multiple tokens share the same
    (imdb_id, season, episode).  A miss is cached for 6 h, a hit for 5 min
    (so a newly-cached release is picked up quickly on retry).
    """
    imdb_id = item.get("imdb_id")
    if not imdb_id:
        # No imdb_id → _search_best_cached_release will handle + log the warning.
        return _search_best_cached_release(item)
    key = (imdb_id, item.get("season"), item.get("episode"))
    now = time.monotonic()
    with _search_cache_lock:
        entry = _search_cache.get(key)
        if entry and entry[0] > now:
            result = entry[1]
            log.debug("Catbox search cache hit for %s %s  -  skipping Zilean/Torrentio",
                      imdb_id, key[1:])
            return result
    result = _search_best_cached_release(item)
    if result is _SEARCH_UNAVAILABLE:
        ttl = _SEARCH_ERROR_TTL
    else:
        ttl = _SEARCH_HIT_TTL if result else _SEARCH_MISS_TTL
    with _search_cache_lock:
        _search_cache[key] = (now + ttl, result)
    return result


def _search_best_cached_release(item: dict) -> tuple[str, str, str] | None | object:
    """Search all configured catalogs for the best cached release.

    Returns:
      (info_hash, magnet, provider) - found a cached release
      None                  -  searched OK, nothing cached right now
      _SEARCH_UNAVAILABLE   -  couldn't search (no imdb_id, network error)  -  do NOT remove .strm
    """
    imdb_id = item.get("imdb_id")
    if not imdb_id:
        log.warning("Catbox search: no imdb_id for %s  -  keeping .strm, will retry later",
                    item["title"])
        return _SEARCH_UNAVAILABLE
    try:
        import release_sanity
        import search_engine
        media_type = item["media_type"]
        season = item.get("season")
        episode = item.get("episode")
        ranked = search_engine.search_candidates(
            "movie" if media_type == "movie" else "series",
            imdb_id, item.get("title") or "", season=season, episode=episode,
            trigger="catbox_recovery")
        if not ranked:
            return None
        ckey = search_engine.content_key(imdb_id, season, episode)
        cache_results = search_engine.cache_map(ckey, ranked)
        rd_cached = cache_results.get("realdebrid", set())
        tb_cached = cache_results.get("torbox", set())
        log.info("Catbox search: RD=%d TB=%d cached out of %d for %s",
                 len(rd_cached), len(tb_cached), len(ranked), item.get("title"))

        torbox_candidates = [s for s in ranked if s.info_hash in tb_cached]
        if torbox_candidates:
            kind = ("movie" if media_type == "movie" else
                    "episode" if season is not None and episode is not None else
                    "season_pack")
            before_sanity = {s.info_hash for s in torbox_candidates}
            torbox_candidates = release_sanity.filter_cached(
                torbox_candidates, kind=kind, season=season, episode=episode,
                imdb_id=imdb_id, label=item.get("title") or ckey)
            after_sanity = {s.info_hash for s in torbox_candidates}
            for rejected_hash in before_sanity - after_sanity:
                search_engine.reject(ckey, rejected_hash, "SANITY_REJECTED")
        if torbox_candidates:
            selected = torbox_candidates[0]
            search_engine.mark_selected(ckey, selected, "torbox")
            return selected.info_hash.lower(), selected.magnet, "torbox"
        for stream in ranked:
            if stream.info_hash in rd_cached:
                search_engine.mark_selected(ckey, stream, "realdebrid")
                return stream.info_hash.lower(), stream.magnet, "realdebrid"
        return None
    except Exception as exc:
        log.warning("Catbox search: failed for %s: %s  -  keeping .strm", item["title"], exc)
        return _SEARCH_UNAVAILABLE


def _schedule_next_episode_preload(token: str) -> None:
    """Prepare one following episode without fetching any CDN media bytes."""
    try:
        import settings as _s
        import config as _cfg
        if not _s.get("SAFE_NEXT_EPISODE_PREPARE", _cfg.SAFE_NEXT_EPISODE_PREPARE):
            return
        item = db.get_virtual_item(token)
        if not item or item.get("media_type") != "series":
            return
        imdb_id = item.get("imdb_id")
        season = item.get("season")
        episode = item.get("episode")
        if not (imdb_id and season and episode):
            return
        prepare_key = f"{imdb_id}:{season}:{episode + 1}"
        with _next_prepare_lock:
            if prepare_key in _next_prepare_cache:
                return
            _next_prepare_cache[prepare_key] = True
        worker = threading.Thread(
            target=_prepare_next_episode,
            args=(imdb_id, item.get("title") or "", season, episode),
            name=f"next-episode-{imdb_id}-{season}-{episode}", daemon=True,
        )
        worker.start()
        log.debug("Catbox: scheduled safe next-episode preparation for %s", item.get("title"))
    except Exception as exc:
        log.debug("Catbox: next-episode preparation scheduling failed: %s", exc)


def _prepare_next_episode(imdb_id: str, title: str,
                          season: int, episode: int) -> None:
    """Search, cache-check, and validate one next episode without media reads."""
    try:
        import release_sanity
        import search_engine
        import strm_generator

        title = strm_generator._base_series_title(title)
        canonical = strm_generator._canonical_series_folder(
            imdb_id, fallback_title=title)
        if canonical:
            title = canonical

        next_season, next_episode = season, episode + 1
        wanted = db.get_wanted_episode(imdb_id, next_season, next_episode)
        nxt = db.get_virtual_item_by_episode(imdb_id, next_season, next_episode)
        if not wanted and not nxt:
            next_season, next_episode = season + 1, 1
            wanted = db.get_wanted_episode(imdb_id, next_season, next_episode)
            nxt = db.get_virtual_item_by_episode(imdb_id, next_season, next_episode)
        if not wanted and not nxt:
            return

        ckey = search_engine.content_key(imdb_id, next_season, next_episode)
        if nxt and nxt.get("info_hash"):
            cached_hashes = torbox.check_cached([nxt["info_hash"]])
            if nxt["info_hash"].lower() in cached_hashes:
                ok, reason = release_sanity.check_hash(
                    nxt["info_hash"], "episode", season=next_season,
                    episode=next_episode, imdb_id=imdb_id, label=ckey)
                if ok:
                    db.mark_candidate_cached(ckey, nxt["info_hash"], "torbox")
                    log.info("Next episode prepared: %s metadata mapping already valid", ckey)
                    return
                search_engine.reject(ckey, nxt["info_hash"], reason)

        streams = search_engine.search_candidates(
            "series", imdb_id, title, season=next_season,
            episode=next_episode, trigger="next_episode_prepare")
        cache_results = search_engine.cache_map(ckey, streams)
        cached = [stream for stream in streams
                  if not stream.is_usenet and
                  stream.info_hash in cache_results.get("torbox", set())]
        cached = release_sanity.filter_cached(
            cached, kind="episode", season=next_season,
            episode=next_episode, imdb_id=imdb_id, label=ckey)
        if not cached:
            log.info("Next episode prepare: no valid cached release for %s", ckey)
            return
        best = cached[0]
        written = strm_generator.create_lazy_episode_strm(
            best.info_hash, best.magnet, title, next_season, next_episode,
            imdb_id=imdb_id, quality=best.quality, source=best.source,
            size_gb=best.size_gb, preload_first=False)
        if written or db.get_virtual_item_by_episode(imdb_id, next_season, next_episode):
            search_engine.mark_selected(ckey, best, "torbox")
            db.mark_episode_status(imdb_id, next_season, next_episode, "found")
            log.info("Next episode prepared: %s lazy item ready", ckey)
    except Exception as exc:
        log.warning("Next episode prepare failed for %s: %s", imdb_id, exc)


def _sweep_caches() -> None:
    """Prune expired entries from the in-memory caches that are only cleaned
    on read (_url_cache, _fail_cache, _search_cache) or on next scan-burst
    check (_recent_tokens). A token that's cached once and never queried
    again would otherwise sit in memory forever on a long-running instance.

    _token_locks is deliberately NOT swept here: a lock object can be handed
    out to a caller and acquired moments after this check finds it free,
    so deleting it here could let two callers end up serialized on two
    different Lock objects for the same token instead of one - a real
    correctness bug, not just a leak. Left as a known, harmless memory growth."""
    now_mono = time.monotonic()

    with _url_cache_lock:
        for t in [t for t, (_, exp) in _url_cache.items() if exp <= now_mono]:
            del _url_cache[t]

    with _fail_cache_lock:
        for t in [t for t, exp in _fail_cache.items() if exp <= now_mono]:
            del _fail_cache[t]

    with _search_cache_lock:
        for k in [k for k, (exp, _) in _search_cache.items() if exp <= now_mono]:
            del _search_cache[k]

    with _recent_lock:
        for t in [t for t, ts in _recent_tokens.items() if now_mono - ts > _SCAN_WINDOW_SEC]:
            del _recent_tokens[t]


def release_idle() -> int:
    """Remove TorBox items idle longer than CATBOX_IDLE_MINUTES. Returns count released."""
    _sweep_caches()
    idle_minutes = _settings.get("CATBOX_IDLE_MINUTES", _CATBOX_IDLE_MINUTES_DEFAULT)
    cutoff = datetime.utcnow() - timedelta(minutes=idle_minutes)
    cutoff_iso = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    items = db.get_idle_virtual_items(cutoff_iso)
    released = 0
    for item in items:
        try:
            deleted = torbox.delete_torrent(item["torbox_id"])
            if not deleted:
                # Torrent may already be gone from TorBox (evicted or manually removed).
                # Still clear the local reference so catbox can re-add it on next play.
                still_there = torbox.find_by_id(item["torbox_id"])
                if still_there:
                    continue
            db.update_virtual_torbox_id(item["token"], None)
            log.info("Catbox: released idle torrent %s (%s)", item["torbox_id"], item["title"])
            released += 1
        except Exception as exc:
            log.warning("Catbox: failed to release idle torrent %s (%s): %s",
                        item["torbox_id"], item.get("title"), exc)
    if released:
        log.info("Catbox: released %d idle torrent(s)", released)
    return released
