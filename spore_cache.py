"""
spore_cache.py — local SSD prefetch cache for spore-stream raw passthrough.

Goal: high-bitrate titles (1080p 20+ Mbit/s, 4K 100+ Mbit/s) play smoothly by
downloading the whole file to a fast local SSD (/mnt/spore-cache) once, then
serving byte ranges from disk instead of re-hitting TorBox per read.

Only RAW-passthrough tokens are cached (cold MKV / already-fast MP4) — the bytes
served == the raw CDN file, so a 1:1 local copy is byte-correct. moov-relocated
MP4 (mycelium's virtual fast-start layout) is NEVER cached here; the wrapper only
calls us when info is None or info.already_fast.

Safety: every public entry point returns None / falls through on any error, and
all downloads run in detached daemon threads — a cache fault can never block a
gunicorn request thread or break playback.
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time

import requests as _req

log = logging.getLogger("spore_cache")

CACHE_DIR   = os.environ.get("SPORE_CACHE_DIR", "/mnt/spore-cache")
BUDGET      = int(os.environ.get("SPORE_CACHE_BUDGET_GB", "820")) * (1 << 30)
SEG         = 8 << 20            # download segment size
# Rate limited per URL by byte rate, so extra workers buy no extra throughput and
# only multiply 429s. Default follows the deployed SPORE_CACHE_WORKERS=4.
WORKERS     = int(os.environ.get("SPORE_CACHE_WORKERS", "4"))      # torbox fetchers / file
MAX_JOBS    = int(os.environ.get("SPORE_CACHE_MAX_JOBS", "2"))     # files downloading at once
MIN_SIZE    = 32 << 20          # don't cache tiny files
CONNECT_TO  = 10
READ_TO     = 60
_ORPHAN_PART_MIN_AGE = 3600     # only reap a jobless .part once it is this stale

# -- CDN 429 retry policy -----------------------------------------------------
# TorBox's CDN rate limits a presigned URL on BYTE RATE, not concurrency, and
# answers over-rate requests with 429 + `X-Ratelimit-After: <seconds>` (measured
# 2026-07-16: 12 concurrent 1KB Range GETs all return 206, while 4 workers pulling
# 8MB segments back to back get 429 with a hint of 7s and the body "Too many
# requests, retry in 7s"). So a 429 here means "you are pulling too fast, pause",
# not "this request is doomed": the only correct response is to wait and re-issue.
#
# Waiting the hinted interval is what actually lets the bucket refill; retrying
# faster just keeps it drained. Jitter de-synchronises the workers, which all trip
# the limit at nearly the same instant and would otherwise wake together and
# re-collide. Same policy as mp4_faststart._get() against this same CDN.
#
# The per-segment 429 budget has to be DEEP, and the reason is arithmetic rather
# than taste. Under load roughly half of all range GETs come back 429 (measured
# 429/success ~= 1.0), and workers compete for one shared bucket, so a given
# segment losing the race N times running has probability ~2^-N. That is tiny per
# segment but there are ~400 segments x WORKERS fetches in one 3GB title, and ANY
# single segment exhausting its budget aborts the entire job (see _download). At a
# budget of 8, ~0.4% per segment made a whole-file prefetch fail reliably: observed
# 2026-07-16, this exact title died at 912MB/3052MB after 3.5 min. At 40 the odds
# become ~1e-12 per segment, so the job now only gives up when the URL is genuinely
# throttled for minutes on end, which is exactly when the breaker SHOULD trip.
# Cost of patience is bounded and only paid when actually stuck: this is a
# background prefetch with no deadline, and a slow fill still beats no fill.
_RATE_WAIT_DEFAULT = float(os.environ.get("SPORE_CACHE_429_WAIT", "5"))   # no usable hint
_RATE_WAIT_CAP     = float(os.environ.get("SPORE_CACHE_429_WAIT_CAP", "12"))  # per 429
_MAX_RATE_WAITS    = int(os.environ.get("SPORE_CACHE_429_TRIES", "40"))   # waits per segment
_HEAD_RATE_WAITS   = 5           # waits for the sizing HEAD in _boot
_FETCH_TRIES       = 4           # attempts per segment for non-429 transient errors

_lock       = threading.Lock()
_jobs: dict[str, "Job"] = {}     # token -> Job (in-flight prefetch)
_job_sem    = threading.Semaphore(MAX_JOBS)

# -- per-token CDN 429 circuit breaker ----------------------------------------
# A single throttled TorBox CDN URL 429s every range GET for a minute or two.
# Without this, each /spore-stream hit re-spawns a fresh WORKERS-wide prefetch
# that hammers that one URL and re-trips the throttle, so it never gets the idle
# gap it needs to clear (observed: one title 429-looping ~30min with no viewer,
# driven by Plex re-probing). After a prefetch fails with a 429 we mark the token
# "cooling down": ensure_prefetch skips it, and the wrapped view returns a fast
# 503 + Retry-After instead of piling more requests onto the throttled URL.
_BREAKER_COOLDOWN = int(os.environ.get("SPORE_CACHE_429_COOLDOWN", "90"))  # seconds
_breaker: dict[str, float] = {}          # token -> cooldown_until (monotonic clock)
_breaker_lock = threading.Lock()


def note_cdn_429(token: str) -> None:
    """Record that this token's CDN URL is rate limited; start/extend cooldown."""
    until = time.monotonic() + _BREAKER_COOLDOWN
    with _breaker_lock:
        _breaker[token] = until
    log.warning("spore_cache: token=%s CDN 429, cooling down %ds", token, _BREAKER_COOLDOWN)


def note_cdn_ok(token: str) -> None:
    """Clear a token's cooldown after a confirmed successful fetch."""
    with _breaker_lock:
        _breaker.pop(token, None)


def cooldown_remaining(token: str) -> int:
    """Seconds left in this token's 429 cooldown, or 0 if not cooling down."""
    with _breaker_lock:
        until = _breaker.get(token)
        if until is None:
            return 0
        rem = until - time.monotonic()
        if rem <= 0:
            _breaker.pop(token, None)
            return 0
    return int(rem) + 1


def _is_429(exc: Exception) -> bool:
    return "CDN status 429" in str(exc)


def _rate_limit_pause(resp, attempt: int) -> float:
    """Seconds to wait after a 429, from the server's hint or backoff.

    Prefers what the CDN actually tells us (`X-Ratelimit-After`, or `Retry-After`
    if TorBox ever switches to the standard header) since that is the authoritative
    refill time. Falls back to exponential backoff when the hint is missing or
    unparseable. Jittered to 75-100% so that workers which trip the limit together
    do not wake together and immediately re-trip it.
    """
    hint = None
    try:
        hint = resp.headers.get("X-Ratelimit-After") or resp.headers.get("Retry-After")
    except Exception:
        pass
    wait = None
    if hint:
        try:
            wait = float(hint) + 0.5      # small margin past the stated refill
        except (TypeError, ValueError):
            wait = None                   # e.g. an HTTP-date Retry-After
    if wait is None:
        wait = _RATE_WAIT_DEFAULT * (2 ** max(attempt, 0))
    wait = min(max(wait, 0.5), _RATE_WAIT_CAP)
    return wait * (0.75 + random.random() * 0.25)


def _part(t: str) -> str: return os.path.join(CACHE_DIR, t + ".part")
def _done(t: str) -> str: return os.path.join(CACHE_DIR, t)


class Job:
    __slots__ = ("token", "size", "frontier", "failed")
    def __init__(self, token: str, size: int):
        self.token = token
        self.size = size
        self.frontier = 0        # contiguous bytes complete from offset 0
        self.failed = False


# ── cache-aware byte fetch (drop-in for mp4_faststart._get) ──────────────────
def read_range(token: str, start: int, end: int):
    """Return the RAW CDN bytes [start, end] inclusive from the local cache, or
    None if not (yet) available.

    The cached file is a byte-identical copy of the CDN file, so these bytes are
    interchangeable with what _get() would fetch from TorBox — the served layout
    (cold passthrough or moov-first virtual) is unchanged; only the source moves
    to the SSD. Returns exactly (end-start+1) bytes or None.
    """
    want = end - start + 1
    if want <= 0:
        return b""
    try:
        done = _done(token)
        if os.path.exists(done):
            _touch(done)
            return _read(done, start, want)
        with _lock:
            job = _jobs.get(token)
        # partial file: only serve ranges fully inside the downloaded frontier
        if job and job.size and not job.failed and end < job.frontier:
            return _read(_part(token), start, want)
    except Exception as exc:
        log.warning("spore_cache.read_range error token=%s: %s", token, exc)
    return None


def _read(path: str, start: int, want: int):
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(want)
    return data if len(data) == want else None


# ── prefetch ─────────────────────────────────────────────────────────────────
def ensure_prefetch(token: str, cdn_url: str):
    """Kick off a background full-file download if not cached/in-flight."""
    try:
        if os.path.exists(_done(token)):
            return
        if cooldown_remaining(token):
            return   # CDN throttled this token recently; don't re-storm it
        with _lock:
            if token in _jobs:
                return
            _jobs[token] = Job(token, 0)   # reserve slot; size resolved in thread
        threading.Thread(target=_boot, args=(token, cdn_url),
                         daemon=True, name=f"pf-{token[:8]}").start()
    except Exception as exc:
        log.warning("spore_cache.ensure_prefetch error token=%s: %s", token, exc)


def _boot(token: str, cdn_url: str):
    job = _jobs.get(token)
    if job is None:
        return
    try:
        # A 429 on the sizing HEAD used to kill the prefetch before it started;
        # wait out the throttle here too rather than surrendering the whole title.
        # Shallower budget than _fetch: a HEAD costs no bandwidth so it rarely
        # trips the rate limit, and if it does the URL is thoroughly throttled and
        # there is no point starting a job that would only crawl.
        size = 0
        for waits in range(_HEAD_RATE_WAITS + 1):
            r = _req.head(cdn_url, timeout=CONNECT_TO, allow_redirects=True)
            if r.status_code != 429:
                size = int(r.headers.get("Content-Length", 0))
                break
            if waits == _HEAD_RATE_WAITS:
                note_cdn_429(token)
                _drop(token)
                return
            time.sleep(_rate_limit_pause(r, waits))
    except Exception as exc:
        log.warning("spore_cache: HEAD failed token=%s: %s", token, exc)
        _drop(token)
        return
    if size < MIN_SIZE:
        _drop(token)
        return
    job.size = size
    with _job_sem:                 # bound concurrent whole-file downloads
        _download(job, cdn_url)


def _download(job: Job, cdn_url: str):
    token, size = job.token, job.size
    part = _part(token)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        _evict(size)
        with open(part, "wb") as f:
            f.truncate(size)
        nseg = (size + SEG - 1) // SEG
        nxt = [0]
        done_segs: set[int] = set()
        seg_lock = threading.Lock()
        fh = open(part, "r+b")
        fh_lock = threading.Lock()
        err: list = []

        def worker():
            while not err:
                with seg_lock:
                    i = nxt[0]
                    if i >= nseg:
                        return
                    nxt[0] += 1
                s = i * SEG
                e = min(s + SEG, size) - 1
                try:
                    data = _fetch(cdn_url, s, e)
                except Exception as exc:
                    err.append(exc)
                    return
                with fh_lock:
                    fh.seek(s)
                    fh.write(data)
                with seg_lock:
                    done_segs.add(i)
                    fr = job.frontier // SEG
                    while fr in done_segs:
                        fr += 1
                    job.frontier = min(fr * SEG, size)

        ts = [threading.Thread(target=worker, name=f"pfw-{token[:6]}-{k}")
              for k in range(WORKERS)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        fh.flush()
        os.fsync(fh.fileno())
        fh.close()
        if err:
            raise err[0]
        job.frontier = size
        os.replace(part, _done(token))
        _touch(_done(token))
        note_cdn_ok(token)
        log.info("spore_cache: CACHED token=%s size=%.2fGB", token, size / (1 << 30))
    except Exception as exc:
        job.failed = True
        if _is_429(exc):
            note_cdn_429(token)
        log.warning("spore_cache: prefetch failed token=%s: %s", token, exc)
        try:
            os.remove(part)
        except OSError:
            pass
    finally:
        _drop(token)


def _fetch(cdn_url: str, start: int, end: int) -> bytes:
    """Fetch [start, end] inclusive, validated against short reads / ignored Range.

    Retries rather than failing on the two things that are routinely survivable
    against TorBox's CDN, because either one killing the fetch aborts the entire
    whole-file prefetch (see _download) and leaves the title permanently uncached:

      * 429 rate limit: waits the hinted refill interval and re-issues the SAME
        offset without burning a normal attempt (a throttle is not a failure).
      * transient faults (timeout, 5xx, dropped connection): backoff + retry.

    Only gives up once a segment has burned its whole 429 budget or its retries,
    and a 429 give-up is still raised as "CDN status 429" so _is_429 / the caller's
    circuit breaker keep recognising it.

    Returns exactly (end-start+1) bytes or raises: never a short buffer, since the
    caller writes the result straight into the sparse .part file and a silently
    short segment would leave a hole of zeros in a cache file that then gets
    promoted to complete.
    """
    want = end - start + 1
    if want <= 0:
        return b""
    out = bytearray()
    attempts = 0
    rate_waits = 0
    rounds = 0
    max_rounds = _FETCH_TRIES + _MAX_RATE_WAITS + 32
    while len(out) < want:
        rounds += 1
        if rounds > max_rounds:
            raise IOError(f"too many partial reads {len(out)}/{want}")
        pos = start + len(out)
        try:
            r = _req.get(cdn_url, headers={"Range": f"bytes={pos}-{end}"},
                         timeout=(CONNECT_TO, READ_TO), stream=True)
            try:
                if r.status_code == 429:
                    rate_waits += 1
                    if rate_waits > _MAX_RATE_WAITS:
                        # Sustained throttle: report as a 429 so the per-token
                        # breaker trips and stops re-storming this URL.
                        raise IOError("CDN status 429")
                    wait = _rate_limit_pause(r, rate_waits - 1)
                    log.debug("spore_cache: 429 at %d, waiting %.1fs (%d/%d)",
                              pos, wait, rate_waits, _MAX_RATE_WAITS)
                    time.sleep(wait)
                    continue          # same offset; a throttle is not an attempt
                if r.status_code == 200 and pos != 0:
                    raise IOError("CDN ignored Range")
                if r.status_code not in (200, 206):
                    raise IOError(f"CDN status {r.status_code}")
                got_before = len(out)
                for chunk in r.iter_content(1 << 20):
                    if chunk:
                        out += chunk
                        if len(out) >= want:
                            break
                if len(out) == got_before:
                    raise IOError("CDN returned no data")
                # Progress made. A short read (connection dropped mid-range) just
                # re-requests the remaining tail without burning an attempt.
            finally:
                r.close()
        # NB: requests.RequestException subclasses OSError (== IOError), so this
        # clause MUST stay above the IOError one or it would never be reached.
        except _req.RequestException as exc:
            attempts += 1
            if attempts >= _FETCH_TRIES:
                raise IOError(f"CDN fetch error: {exc}") from exc
            time.sleep(min(0.3 * (2 ** (attempts - 1)), 2.0))
        except IOError as exc:
            if _is_429(exc):
                raise                 # budget exhausted; do not retry-storm
            attempts += 1
            if attempts >= _FETCH_TRIES:
                raise
            time.sleep(min(0.3 * (2 ** (attempts - 1)), 2.0))
    return bytes(out[:want])


def _drop(token: str):
    with _lock:
        _jobs.pop(token, None)


def _touch(path: str):
    try:
        os.utime(path, None)
    except OSError:
        pass


def _reap_orphan_parts():
    """Delete .part files that no live Job is writing.

    A .part is only ever readable while its Job is in _jobs (read_range checks the
    frontier), and _jobs is in-memory, so every restart orphans the .part of any
    prefetch that was still running: unreachable dead bytes that nothing can serve
    and nothing retries. _evict deliberately ignores .part when totalling the
    budget, so orphans were never reclaimed and grew OUTSIDE it, eating the disk
    headroom between BUDGET and the real disk size (found 2026-07-16: two orphans
    from Jul 11, 3.3GB, still sitting there).

    Safe by construction: a token is put in _jobs before its thread starts and only
    dropped in _download's finally, so "not in _jobs" means nobody is writing it.
    The age floor is just belt and braces against a boot race.
    """
    try:
        names = os.listdir(CACHE_DIR)
    except OSError:
        return
    now = time.time()
    for n in names:
        if not n.endswith(".part") or n.startswith("."):
            continue
        with _lock:
            if n[:-len(".part")] in _jobs:
                continue                      # a live prefetch owns this file
        p = os.path.join(CACHE_DIR, n)
        try:
            st = os.stat(p)
            if now - st.st_mtime < _ORPHAN_PART_MIN_AGE:
                continue
            os.remove(p)
            log.info("spore_cache: reaped orphan %s (%.2fGB, age %.1fh)", n,
                     st.st_size / (1 << 30), (now - st.st_mtime) / 3600)
        except OSError:
            pass


# ── LRU eviction ─────────────────────────────────────────────────────────────
def _evict(incoming: int):
    try:
        _reap_orphan_parts()   # free dead .part bytes before evicting live cache
        files = []
        total = 0
        for n in os.listdir(CACHE_DIR):
            if n.startswith(".") or n.endswith(".part"):
                continue
            p = os.path.join(CACHE_DIR, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append((p, st.st_mtime, st.st_size))
            total += st.st_size
        if total + incoming <= BUDGET:
            return
        files.sort(key=lambda x: x[1])           # oldest access first
        for p, _, sz in files:
            if total + incoming <= BUDGET:
                break
            try:
                os.remove(p)
                total -= sz
                log.info("spore_cache: evicted %s (%.2fGB)", os.path.basename(p),
                         sz / (1 << 30))
            except OSError:
                pass
    except Exception as exc:
        log.warning("spore_cache: eviction error: %s", exc)
