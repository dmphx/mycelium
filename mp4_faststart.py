"""
MP4 fast-start proxy for Mycelium.

CDN-served MP4 files have moov at the END (mdat-before-moov).
This makes Plex/FFmpeg seek 15GB before knowing the codec.

Solution: fetch ftyp (32 bytes) + moov (~15MB) from the CDN once,
rewrite chunk offsets (stco/co64) so moov appears first, cache on disk.

Virtual fast-start layout:
  [ftyp][moov_rewritten][mdat_content...]

Original CDN layout:
  [ftyp][mdat_content...][moov]

Offset mapping (virtual → CDN):
  [0, ftyp_size)                    → CDN [0, ftyp_size)  (ftyp unchanged)
  [ftyp_size, ftyp_size+moov_size)  → serve from cached rewritten moov
  [ftyp_size+moov_size, ...)        → CDN [virtual - moov_size, ...)

stco/co64 delta: +moov_size  (mdat shifted right by moov_size in virtual file)
"""
from __future__ import annotations

import logging
import struct
import threading
import time
from pathlib import Path

import requests as req_lib

log = logging.getLogger(__name__)


class SporeFetchError(Exception):
    """A CDN byte-range fetch could not be satisfied correctly.

    Raised instead of silently returning wrong/partial bytes, so callers can
    re-materialize a fresh CDN URL and retry rather than streaming garbage
    (misaligned bytes) into FFmpeg's decoder.

    `status` carries the CDN's HTTP status when one was received, so callers
    can tell a retired URL (400/403/404: invalidate and re-resolve) from a
    throttled one (429: the same URL is fine, re-resolving amplifies load).
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

_CONNECT_TIMEOUT = 10
_READ_TIMEOUT    = 60
_MAX_MOOV_BYTES  = 128 * 1024 * 1024  # refuse to buffer a moov bigger than this
_MAX_FTYP_BYTES  = 1024 * 1024        # real ftyp boxes are a few dozen bytes; same cap idea as moov
_CACHE_DIR: Path | None = None # set by init()
# Per-token locks instead of one global lock, so building the fast-start
# cache for one movie doesn't block every other concurrent cold-start build.
# Not swept/evicted - same accepted tradeoff as catbox.py's _token_locks
# (unbounded but slow growth vs. the risk of deleting a lock while it's
# being handed out to another caller).
_cache_locks: dict[str, threading.Lock] = {}
_cache_locks_registry_lock = threading.Lock()


def _token_lock(token: str) -> threading.Lock:
    with _cache_locks_registry_lock:
        lock = _cache_locks.get(token)
        if lock is None:
            lock = threading.Lock()
            _cache_locks[token] = lock
        return lock

# TorBox's CDN enforces a per-file download rate limit and answers 429 with an
# `X-Ratelimit-After: <seconds>` hint (typically 5). Retrying faster than that
# just keeps the bucket drained — a single transcode's request burst can trip it
# indefinitely. So on 429 we WAIT the hinted interval (capped) and retry, and we
# bound how many such waits we tolerate before giving up on a chunk.
_RATE_LIMIT_WAIT_DEFAULT = 5.0   # used when the server sends no usable hint
_RATE_LIMIT_WAIT_CAP     = 10.0  # never sleep longer than this on one 429
_MAX_RATE_WAITS          = 5     # ~ up to 5 paced retries before giving up
# serve_bytes() calls _get() inline on a live gunicorn request thread while
# streaming a Plex response, and gunicorn runs only a handful of threads for
# the whole app (see Dockerfile --threads). A sustained 429 that pins those
# threads waiting would starve other requests (incl. /health -> the container
# watchdog restart). We therefore let a live fetch WAIT OUT a throttle
# patiently -- turning a transient rate-limit into a brief buffer instead of a
# truncated read -- but BOUND how many threads may be parked on a 429-wait at
# once with a semaphore, so free threads are always left for /health and other
# streams. A live fetch that finds no free permit gives up immediately (the
# client re-requests the range). build_and_cache()'s calls run backgrounded and
# wait unbounded (bounded_waits stays False). See _get(bounded_waits=...).
_LIVE_REQUEST_MAX_RATE_WAITS = 8          # patient, now that waits are bounded
_MAX_LIVE_429_WAITERS        = 8          # concurrent live 429-waiters allowed
_live_429_wait_sem = threading.BoundedSemaphore(_MAX_LIVE_429_WAITERS)


def init(cache_dir: str | Path) -> None:
    global _CACHE_DIR
    _CACHE_DIR = Path(cache_dir)
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(token: str) -> Path:
    assert _CACHE_DIR is not None, "mp4_faststart.init() not called"
    return _CACHE_DIR / f"{token}.fsh"


# ── Box parsing ───────────────────────────────────────────────────────────────

def _box_header(data: bytes | bytearray, pos: int) -> tuple[bytes, int, int]:
    """Return (box_type, box_size, header_size) at pos, or raise ValueError."""
    if pos + 8 > len(data):
        raise ValueError("truncated box header")
    size = struct.unpack_from(">I", data, pos)[0]
    typ  = bytes(data[pos + 4 : pos + 8])
    if size == 1:
        if pos + 16 > len(data):
            raise ValueError("truncated extended box header")
        size   = struct.unpack_from(">Q", data, pos + 8)[0]
        hdr    = 16
    elif size == 0:
        size   = len(data) - pos
        hdr    = 8
    else:
        hdr    = 8
    return typ, size, hdr


def _rewrite_offsets(moov: bytearray, delta: int, moov_offset: int) -> None:
    """Add delta to every stco/co64 chunk offset inside moov that points into
    mdat1 (the data before moov in the CDN file), in-place.

    Offsets >= moov_offset point into a second mdat block that comes AFTER
    moov in the CDN layout ([ftyp][mdat1][moov][mdat2]) - that region doesn't
    move in the virtual fast-start layout, so those offsets must stay
    untouched. Raises ValueError if a 32-bit stco offset would overflow.

    Only the classic (non-fragmented) moov container hierarchy is handled.
    moof/traf (fragmented MP4) use tfhd/trun, not stco/co64, and can't appear
    nested inside moov anyway - deliberately excluded so a fragmented file
    falls through as untouched/no-op rather than being mis-rewritten."""
    _CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts"}

    def _walk(start: int, end: int) -> None:
        pos = start
        while pos < end - 8:
            try:
                typ, size, hdr = _box_header(moov, pos)
            except ValueError:
                break
            if size < 8:
                break
            box_end = pos + size

            if typ in _CONTAINERS:
                _walk(pos + hdr, box_end)
            elif typ == b"stco":
                n = struct.unpack_from(">I", moov, pos + 12)[0]
                for i in range(n):
                    p = pos + 16 + i * 4
                    old = struct.unpack_from(">I", moov, p)[0]
                    if old >= moov_offset:
                        continue  # points into mdat2, unchanged in the virtual layout
                    new = old + delta
                    if new > 0xFFFFFFFF:
                        raise ValueError(
                            f"stco offset overflow: {old}+{delta} exceeds 32-bit range - "
                            "file needs co64, fast-start unsupported"
                        )
                    struct.pack_into(">I", moov, p, new)
            elif typ == b"co64":
                n = struct.unpack_from(">I", moov, pos + 12)[0]
                for i in range(n):
                    p = pos + 16 + i * 8
                    old = struct.unpack_from(">Q", moov, p)[0]
                    if old >= moov_offset:
                        continue
                    struct.pack_into(">Q", moov, p, old + delta)
            pos = box_end

    _walk(0, len(moov))


def _find_box_in(data: bytes, typ: bytes) -> int:
    """Return offset of first top-level box with the given type, or -1."""
    pos = 0
    while pos + 8 <= len(data):
        try:
            t, size, _ = _box_header(data, pos)
        except ValueError:
            break
        if t == typ:
            return pos
        pos += size
    return -1


# ── Fetch + cache ─────────────────────────────────────────────────────────────

def _get(url: str, start: int, end: int, tries: int = 4,
         rate_waits_max: int | None = None, bounded_waits: bool = False) -> bytes:
    """Fetch CDN bytes [start, end] inclusive, *validated*.

    The naive version returned whatever the CDN sent — including a 500 error
    body, or a full-file HTTP 200 when the CDN ignored the Range header. Those
    bytes then got spliced into the video stream, misaligning FFmpeg's decoder
    (the "Invalid NAL unit size", "Could not find ref with POC", "Number of
    bands exceeds limit" corruption). This version guarantees the bytes it
    returns actually correspond to the requested range:

      * rejects any non-206 response for a mid-file range (a 200 means the CDN
        ignored Range and would serve from offset 0) and retries;
      * on a short read / mid-range connection drop, re-requests the remaining
        tail instead of returning a truncated buffer;
      * retries transient CDN failures (timeouts, 5xx, 429) with backoff.

    Returns exactly (end-start+1) bytes, or fewer only at genuine end-of-file
    (HTTP 416). Raises SporeFetchError if the range cannot be satisfied.
    """
    count = end - start + 1
    if count <= 0:
        return b""
    rw_max = _MAX_RATE_WAITS if rate_waits_max is None else rate_waits_max
    buf = bytearray()
    attempts = 0
    rate_waits = 0
    rounds = 0
    max_rounds = tries + rw_max + 16
    while len(buf) < count:
        rounds += 1
        if rounds > max_rounds:
            raise SporeFetchError(
                f"too many partial reads ({len(buf)}/{count}) for {url[:60]}"
            )
        want_start = start + len(buf)
        headers = {"Range": f"bytes={want_start}-{end}"}
        try:
            resp = req_lib.get(
                url,
                headers=headers,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                stream=True,
            )
            sc = resp.status_code
            if sc == 429:
                # Per-file CDN rate limit. Honor the server's hint and wait
                # rather than hammering (fast retries keep the bucket empty).
                hint = resp.headers.get("X-Ratelimit-After") \
                    or resp.headers.get("Retry-After")
                resp.close()
                rate_waits += 1
                if rate_waits > rw_max:
                    raise SporeFetchError(
                        "CDN rate-limited (429), gave up after waiting", status=429
                    )
                wait = _RATE_LIMIT_WAIT_DEFAULT
                if hint:
                    try:
                        wait = float(hint) + 0.5
                    except (TypeError, ValueError):
                        pass
                wait = min(max(wait, 0.5), _RATE_LIMIT_WAIT_CAP)
                if bounded_waits:
                    # Live request thread: only park on the wait if a permit is
                    # free, else give up now so we never pin the last few
                    # gunicorn threads and starve /health. The client re-requests
                    # the range; a stalled read is worse app-wide than one retry.
                    if not _live_429_wait_sem.acquire(blocking=False):
                        raise SporeFetchError(
                            "CDN rate-limited (429), no wait permit", status=429
                        )
                    try:
                        time.sleep(wait)
                    finally:
                        _live_429_wait_sem.release()
                else:
                    time.sleep(wait)
                continue  # retry the same offset; do not burn a normal attempt
            if sc == 416:
                resp.close()
                break  # requested past EOF — a legitimate short read
            if sc == 200 and want_start != 0:
                # CDN ignored the Range header and is serving from byte 0.
                # Streaming that as if it were bytes at `want_start` corrupts
                # the video — reject and retry.
                resp.close()
                raise SporeFetchError(
                    f"CDN ignored Range (HTTP 200) at offset {want_start}"
                )
            if sc not in (200, 206):
                resp.close()
                raise SporeFetchError(f"CDN HTTP {sc}", status=sc)
            got_before = len(buf)
            for chunk in resp.iter_content(1 << 17):
                if chunk:
                    buf += chunk
                    if len(buf) >= count:
                        break
            if len(buf) == got_before:
                raise SporeFetchError("CDN returned no data")
            # Progress made. If still short (connection dropped mid-range) the
            # while-loop re-requests the remaining tail without burning a retry.
        except SporeFetchError:
            attempts += 1
            if attempts >= tries:
                raise
            time.sleep(min(0.3 * (2 ** (attempts - 1)), 2.0))
        except req_lib.RequestException as exc:
            attempts += 1
            if attempts >= tries:
                raise SporeFetchError(f"CDN fetch error: {exc}") from exc
            time.sleep(min(0.3 * (2 ** (attempts - 1)), 2.0))
    return bytes(buf[:count])


# Public alias for callers outside this module (app.py cold/warm proxy paths).
fetch_range = _get


def _locate_moov(cdn_url: str, cdn_size: int) -> tuple[int, int] | None:
    """
    Scan top-level box headers to find moov offset and size.
    Reads only 16 bytes per box header, so it's cheap even for 17 GB files.
    Returns (moov_offset, moov_size) or None.
    """
    pos = 0
    while pos < cdn_size - 8:
        raw = _get(cdn_url, pos, pos + 15)
        if len(raw) < 8:
            break
        try:
            typ, size, _ = _box_header(raw, 0)
        except ValueError:
            break
        if typ == b"moov":
            return pos, size
        if size < 8 or size > cdn_size - pos:
            # A box smaller than the minimum header (or one claiming to run
            # past EOF) means the file is malformed - bail out instead of
            # crawling forward one byte at a time, one HTTP request per box.
            break
        pos += size
    return None


def build_and_cache(cdn_url: str, token: str) -> bool:
    """
    Fetch ftyp + moov from CDN, build fast-start header, write to .fsh cache.
    Scans box headers sequentially so moov is found regardless of its position.
    Returns True on success.
    """
    path = _cache_path(token)
    with _token_lock(token):
        if path.exists():
            return True

        try:
            head = req_lib.head(cdn_url, timeout=_CONNECT_TIMEOUT, allow_redirects=True)
            cdn_size = int(head.headers["Content-Length"])

            def _atomic_write(dest: Path, data: bytes) -> None:
                tmp = dest.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(dest)

            # ftyp: first box  -  read header to get actual size, then fetch full box.
            # An oversized "size" here isn't necessarily a malformed MP4 -- it's
            # the expected result of parsing a non-MP4 container's first bytes
            # as an MP4 box header. MKV's EBML magic (0x1A45DFA3 = 440786851)
            # parses this way every time, so this must fall through to the same
            # "not an MP4" redirect-sentinel path _locate_moov() uses below,
            # not bail out -- bailing out here left build_and_cache() failing
            # forever for these tokens (no .fsh ever written -> every request
            # stuck on the slow cold-proxy path indefinitely).
            ftyp_hdr = _get(cdn_url, 0, 15)
            _, ftyp_size, _ = _box_header(ftyp_hdr, 0)
            if ftyp_size > _MAX_FTYP_BYTES:
                log.info(
                    "FastStart: ftyp size %d for token=%s exceeds cap %d "
                    "(likely non-MP4, e.g. MKV's EBML magic) - redirect sentinel",
                    ftyp_size, token, _MAX_FTYP_BYTES,
                )
                meta = struct.pack(">QQQQ", 0, 0, cdn_size, 0)
                _atomic_write(path, meta)
                return True
            ftyp_raw = _get(cdn_url, 0, ftyp_size - 1)
            ftyp = ftyp_raw[:ftyp_size]

            # Locate moov by scanning box headers
            result = _locate_moov(cdn_url, cdn_size)

            if result is None:
                # Not an MP4 (likely MKV): write redirect sentinel so spore-stream
                # issues a 302 to CDN directly. FFmpeg reads MKV from byte 0, no seeking.
                meta = struct.pack(">QQQQ", 0, 0, cdn_size, 0)
                _atomic_write(path, meta)
                log.info("FastStart: non-MP4 CDN for token=%s, stored redirect sentinel", token)
                return True

            moov_offset, moov_size = result

            if moov_size > _MAX_MOOV_BYTES:
                log.warning(
                    "FastStart: moov size %d for token=%s exceeds cap %d - "
                    "refusing to buffer (malformed/hostile CDN response?)",
                    moov_size, token, _MAX_MOOV_BYTES,
                )
                return False

            if moov_offset == ftyp_size:
                # Already fast-start: sentinel with moov_size=0 signals direct CDN redirect
                meta = struct.pack(">QQQQ", ftyp_size, 0, cdn_size, moov_offset)
                _atomic_write(path, meta)
                log.info("FastStart: already fast-start for token=%s, stored sentinel", token)
                return True

            # Fetch and rewrite moov
            moov = bytearray(_get(cdn_url, moov_offset, moov_offset + moov_size - 1))

            # Chunk offsets delta = moov_size: mdat1 shifts right by moov_size in virtual layout
            _rewrite_offsets(moov, moov_size, moov_offset)

            header = ftyp + bytes(moov)

            # .fsh: [8B ftyp_size][8B moov_size][8B cdn_size][8B moov_offset][header...]
            meta = struct.pack(">QQQQ", ftyp_size, moov_size, cdn_size, moov_offset)
            _atomic_write(path, meta + header)

            log.info(
                "FastStart: cached token=%s ftyp=%d moov=%d moov_offset=%d cdn_size=%d",
                token, ftyp_size, moov_size, moov_offset, cdn_size,
            )
            return True

        except Exception as exc:
            log.warning("FastStart: build failed for %s: %s", token, exc)
            return False


def load(token: str) -> dict | None:
    """
    Load cached fast-start info for token.
    Returns dict with keys: ftyp_size, moov_size, cdn_size, header (bytes)
    or None if not cached.
    """
    path = _cache_path(token)
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
        if len(raw) < 32:
            # Legacy .fsh without moov_offset field (3-field header)
            ftyp_size, moov_size, cdn_size = struct.unpack_from(">QQQ", raw, 0)
            moov_offset = ftyp_size if moov_size == 0 else cdn_size - moov_size
            header = raw[24:]
        else:
            ftyp_size, moov_size, cdn_size, moov_offset = struct.unpack_from(">QQQQ", raw, 0)
            header = raw[32:]
        return {
            "ftyp_size":    ftyp_size,
            "moov_size":    moov_size,
            "moov_offset":  moov_offset,
            "cdn_size":     cdn_size,
            "header":       header,
            "header_size":  len(header),
            "already_fast": moov_size == 0,
        }
    except Exception as exc:
        log.warning("FastStart: load failed for %s: %s", token, exc)
        return None


def load_meta(token: str) -> dict | None:
    """Cheap header-only load: the four size fields without the moov bytes.

    Reads at most 32 bytes from the .fsh, so unlike load() (which pulls the full
    ftyp+moov header, up to ~32 MB, into memory) this is safe to call once per
    entry while listing a large directory. Returns None if the .fsh is absent or
    the cache dir was never initialised.
    """
    try:
        path = _cache_path(token)
        with path.open("rb") as fh:
            raw = fh.read(32)
    except FileNotFoundError:
        return None
    except Exception as exc:
        log.warning("FastStart: load_meta failed for %s: %s", token, exc)
        return None
    if len(raw) < 24:
        return None
    if len(raw) < 32:
        # Legacy .fsh without moov_offset field (3-field header)
        ftyp_size, moov_size, cdn_size = struct.unpack_from(">QQQ", raw, 0)
        moov_offset = ftyp_size if moov_size == 0 else cdn_size - moov_size
    else:
        ftyp_size, moov_size, cdn_size, moov_offset = struct.unpack_from(">QQQQ", raw, 0)
    return {
        "ftyp_size":    ftyp_size,
        "moov_size":    moov_size,
        "moov_offset":  moov_offset,
        "cdn_size":     cdn_size,
        "already_fast": moov_size == 0,
    }


def extract_codec_private(token: str) -> bytes | None:
    """Extract HEVC (hvcC) or AVC (avcC) decoder config bytes from the cached moov.
    Returns the box payload (without 8-byte header), or None if not found."""
    info = load(token)
    if not info:
        return None
    moov = info["header"][info["ftyp_size"]:]
    for tag in (b"hvcC", b"avcC"):
        i = moov.find(tag)
        if i >= 4:
            sz = struct.unpack_from(">I", moov, i - 4)[0]
            if 8 <= sz <= len(moov) - (i - 4):
                return bytes(moov[i + 4 : i - 4 + sz])
    return None


# ── Virtual offset mapping ────────────────────────────────────────────────────

def virtual_to_cdn(virtual_offset: int, info: dict) -> int | None:
    """
    Map a virtual fast-start file offset to the real CDN offset.
    Returns None if the offset is inside the cached header (no CDN fetch needed).
    """
    if virtual_offset < info["header_size"]:
        return None  # served from cached header
    return virtual_offset - info["moov_size"]


def serve_bytes(info: dict, cdn_url: str, v_start: int, v_end: int) -> bytes:
    """
    Return bytes [v_start, v_end] from the virtual fast-start file.

    Virtual layout: [ftyp][moov_rewritten][mdat1][mdat2]
    CDN layout:     [ftyp][mdat1][moov][mdat2]

    Offset mapping for CDN data regions:
      mdat1: virtual [hdr_size, moov_offset+moov_size) → CDN [ftyp_size, moov_offset)
             i.e. cdn = virtual - moov_size
      mdat2: virtual [moov_offset+moov_size, cdn_size) → CDN [moov_offset+moov_size, cdn_size)
             i.e. cdn = virtual  (unchanged)
    """
    header      = info["header"]
    hdr_size    = info["header_size"]
    moov_size   = info["moov_size"]
    moov_offset = info["moov_offset"]
    mdat2_start = moov_offset + moov_size  # virtual == CDN for mdat2

    out = bytearray()
    pos = v_start

    # Region 1: cached header (ftyp + rewritten moov)
    if pos < hdr_size:
        chunk_end = min(v_end, hdr_size - 1)
        out += header[pos : chunk_end + 1]
        pos = chunk_end + 1

    # Region 2: mdat1 (before moov in CDN), cdn = virtual - moov_size.
    # serve_bytes runs inline on a live request thread, so cap the 429 wait
    # budget (see _LIVE_REQUEST_MAX_RATE_WAITS) instead of holding the thread.
    if pos <= v_end and pos < mdat2_start:
        chunk_end = min(v_end, mdat2_start - 1)
        out += _get(cdn_url, pos - moov_size, chunk_end - moov_size,
                    rate_waits_max=_LIVE_REQUEST_MAX_RATE_WAITS, bounded_waits=True)
        pos = chunk_end + 1

    # Region 3: mdat2 (after moov in CDN), cdn = virtual
    if pos <= v_end:
        out += _get(cdn_url, pos, v_end,
                    rate_waits_max=_LIVE_REQUEST_MAX_RATE_WAITS, bounded_waits=True)

    return bytes(out)
