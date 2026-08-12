"""Demand-driven shared byte-block cache for Spore playback.

The TorBox CDN rate-limits each file URL by byte rate. Two Plex viewers of the
same title must therefore share upstream reads instead of independently pulling
the same bytes. This cache stores only blocks that a viewer actually requests.
It never prefetches, so it cannot compete with playback like the retired
whole-file SSD prefetcher did.

One fetch is allowed per title at a time. Callers asking for the same aligned
block share one in-flight result, then all later callers read it from SSD.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

BLOCK_SIZE = max(1 << 20, int(os.environ.get("SPORE_READ_CACHE_BLOCK_MB", "16")) << 20)
BUDGET = max(BLOCK_SIZE, int(os.environ.get("SPORE_READ_CACHE_BUDGET_GB", "400")) << 30)
WAIT_TIMEOUT = max(30.0, float(os.environ.get("SPORE_READ_CACHE_WAIT_SEC", "480")))
EVICT_INTERVAL = max(30.0, float(os.environ.get("SPORE_READ_CACHE_EVICT_SEC", "300")))
MAX_BLOCKING_REQUESTS = max(2, int(os.environ.get("SPORE_READ_CACHE_MAX_BLOCKING", "6")))

_root = Path(os.environ.get("SPORE_READ_CACHE_DIR", "/mnt/spore-cache/readthrough"))
_enabled_setting = os.environ.get("SPORE_READ_CACHE_ENABLED", "auto").lower()
_enabled = (
    _enabled_setting in ("1", "true", "yes")
    or (_enabled_setting == "auto" and os.path.ismount(_root.parent))
)


class CacheWaitError(Exception):
    """A shared block fetch did not finish before the bounded follower wait."""

    status = 503
    retry_after = 5


@dataclass
class _Flight:
    done: threading.Event = field(default_factory=threading.Event)
    data: bytes | None = None
    error: BaseException | None = None


_state_lock = threading.Lock()
_flights: dict[tuple[str, int], _Flight] = {}
_title_locks: dict[str, threading.Lock] = {}
_evict_lock = threading.Lock()
_last_evict = 0.0
_request_slots = threading.BoundedSemaphore(MAX_BLOCKING_REQUESTS)


def init(cache_dir: str | Path | None = None) -> None:
    """Initialize the cache directory. Failure disables disk persistence only."""
    global _root, _enabled
    if cache_dir is not None:
        _root = Path(cache_dir)
    if not _enabled:
        return
    try:
        _root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _enabled = False
        log.warning("spore read cache disabled: %s", exc)


def _metric(name: str, result: str) -> None:
    try:
        import metrics_prom
        getattr(metrics_prom, name).labels(result=result).inc()
    except Exception:
        pass


def _safe_key(source_key: str) -> str:
    return hashlib.sha256(source_key.encode("utf-8", "replace")).hexdigest()


def _block_path(key: str, start: int) -> Path:
    return _root / key[:2] / key / f"{start:016x}.blk"


def _read_complete(path: Path, expected: int) -> bytes | None:
    try:
        if path.stat().st_size != expected:
            return None
        data = path.read_bytes()
        if len(data) != expected:
            return None
        try:
            os.utime(path, None)
        except OSError:
            pass
        return data
    except OSError:
        return None


def _write_complete(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _title_lock(key: str) -> threading.Lock:
    with _state_lock:
        lock = _title_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _title_locks[key] = lock
        return lock


def _get_block(source_key: str, start: int, end: int,
               fetcher: Callable[[int, int], bytes]) -> bytes:
    key = _safe_key(source_key)
    expected = end - start + 1
    path = _block_path(key, start)

    if _enabled:
        cached = _read_complete(path, expected)
        if cached is not None:
            _metric("spore_block_cache_total", "hit")
            return cached

    flight_key = (key, start)
    with _state_lock:
        flight = _flights.get(flight_key)
        if flight is None:
            flight = _Flight()
            _flights[flight_key] = flight
            leader = True
        else:
            leader = False

    if not leader:
        _metric("spore_block_cache_total", "shared")
        if not _request_slots.acquire(blocking=False):
            raise CacheWaitError("Spore shared-fetch wait capacity is full")
        try:
            if not flight.done.wait(WAIT_TIMEOUT):
                raise CacheWaitError("shared Spore block fetch timed out")
            if flight.error is not None:
                raise flight.error
            if flight.data is None:
                raise CacheWaitError("shared Spore block fetch returned no data")
            return flight.data
        finally:
            _request_slots.release()

    _metric("spore_block_cache_total", "miss")
    if not _request_slots.acquire(blocking=False):
        error = CacheWaitError("Spore fetch capacity is full")
        flight.error = error
        flight.done.set()
        with _state_lock:
            _flights.pop(flight_key, None)
        raise error
    try:
        # Recheck after taking the title gate. A previous block leader may have
        # completed this block while this request waited behind another offset.
        with _title_lock(key):
            if _enabled:
                cached = _read_complete(path, expected)
                if cached is not None:
                    flight.data = cached
                    _metric("spore_block_cache_total", "hit_after_wait")
                    return cached
            try:
                data = fetcher(start, end)
            except BaseException as exc:
                status = getattr(exc, "status", None)
                _metric("spore_cdn_fetch_total", "rate_limited" if status == 429 else "error")
                raise
            if len(data) != expected:
                raise IOError(f"short Spore block fetch {len(data)}/{expected}")
            flight.data = data
            _metric("spore_cdn_fetch_total", "ok")
            if _enabled:
                try:
                    _write_complete(path, data)
                except OSError as exc:
                    log.warning("spore read cache write failed: %s", exc)
            _evict_later()
            return data
    except BaseException as exc:
        flight.error = exc
        raise
    finally:
        _request_slots.release()
        flight.done.set()
        with _state_lock:
            _flights.pop(flight_key, None)


def read_range(source_key: str, start: int, end: int, file_size: int,
               fetcher: Callable[[int, int], bytes]) -> bytes:
    """Return an inclusive byte range through the shared on-demand cache."""
    if start < 0 or end < start or file_size <= 0 or end >= file_size:
        raise ValueError(f"invalid Spore cache range {start}-{end}/{file_size}")

    out = bytearray()
    pos = start
    while pos <= end:
        block_start = (pos // BLOCK_SIZE) * BLOCK_SIZE
        block_end = min(block_start + BLOCK_SIZE - 1, file_size - 1)
        block = _get_block(source_key, block_start, block_end, fetcher)
        rel_start = pos - block_start
        take_end = min(end, block_end) - block_start + 1
        out += block[rel_start:take_end]
        pos = block_start + take_end
    return bytes(out)


def _evict_later() -> None:
    global _last_evict
    if not _enabled:
        return
    now = time.monotonic()
    with _evict_lock:
        if now - _last_evict < EVICT_INTERVAL:
            return
        _last_evict = now
    threading.Thread(target=_evict, daemon=True, name="spore-cache-evict").start()


def _evict() -> None:
    """LRU-evict completed blocks until usage is back under the disk budget."""
    try:
        files: list[tuple[float, int, Path]] = []
        total = 0
        for path in _root.rglob("*.blk"):
            try:
                st = path.stat()
            except OSError:
                continue
            total += st.st_size
            files.append((st.st_mtime, st.st_size, path))
        if total <= BUDGET:
            return
        target = int(BUDGET * 0.9)
        for _, size, path in sorted(files):
            if total <= target:
                break
            try:
                path.unlink()
                total -= size
            except OSError:
                pass
        log.info("spore read cache evicted to %.1f GiB", total / (1 << 30))
    except Exception as exc:
        log.warning("spore read cache eviction failed: %s", exc)


def status() -> dict:
    """Return cheap runtime state for health and diagnostics."""
    with _state_lock:
        inflight = len(_flights)
        titles = len(_title_locks)
    return {
        "enabled": _enabled,
        "block_size": BLOCK_SIZE,
        "budget": BUDGET,
        "max_blocking_requests": MAX_BLOCKING_REQUESTS,
        "inflight": inflight,
        "coordinated_titles": titles,
        "path": str(_root),
    }
