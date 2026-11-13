"""Run weekly episode hygiene outside the host's Plex mutation window.

Use the same existing lock files as host conversion, repair and scanner jobs.
Never create, replace or truncate them, and never wait or retry this interval.
The caller's scheduler owns the next attempt. Both locks span discovery and
both cleanup passes, including their existing playback and maintenance gates.
"""
from __future__ import annotations

import fcntl
import logging
import os
import re
import stat
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import config

log = logging.getLogger(__name__)
_LOCK_NAMES = ("convert-cached.lock", "plex-targeted-scan.lock")


class Deferred(Exception):
    """A fixed reason code safe to log without paths or provider errors."""


def _minute(value: str) -> int:
    if not isinstance(value, str) or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None:
        raise ValueError("invalid window")
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def _outside_nightly_window() -> None:
    try:
        start = _minute(config.EPISODE_HYGIENE_NIGHTLY_START)
        end = _minute(config.EPISODE_HYGIENE_NIGHTLY_END)
        if start == end:
            raise ValueError("empty window")
        now = datetime.now(ZoneInfo(config.EPISODE_HYGIENE_TIMEZONE))
        minute = now.hour * 60 + now.minute
    except (ValueError, TypeError, KeyError, OSError):
        raise Deferred("nightly_window_unknown") from None
    blocked = start <= minute < end if start < end else minute >= start or minute < end
    if blocked:
        raise Deferred("nightly_window")


@contextmanager
def _host_fence():
    """Nonblocking edit then scanner flocks on existing regular-file inodes."""
    _outside_nightly_window()
    descriptors = []
    directory = None
    try:
        try:
            directory = os.open(config.EPISODE_HYGIENE_HOST_LOCK_DIR,
                                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            for name in _LOCK_NAMES:
                before = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise Deferred("host_lock_identity_unknown")
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                             dir_fd=directory)
                descriptors.append(fd)
                opened = os.fstat(fd)
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    raise Deferred("host_lock_identity_changed")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise Deferred("host_lock_busy") from None
                after = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (not stat.S_ISREG(after.st_mode) or after.st_nlink != 1 or
                        (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)):
                    raise Deferred("host_lock_identity_changed")
            # The clock may have crossed the window boundary while opening locks.
            _outside_nightly_window()
        except OSError:
            raise Deferred("host_lock_unavailable") from None
        yield
    finally:
        # close releases flocks even when a cleanup pass or playback check raises.
        for fd in reversed(descriptors):
            os.close(fd)
        if directory is not None:
            os.close(directory)


def run() -> dict:
    """One scheduled attempt. Refusal leaves the normal interval unchanged."""
    try:
        with _host_fence():
            import playback_guard
            if playback_guard.active(force=True):
                raise Deferred("playback")
            import episode_scheme
            import phantom_cleanup
            phantoms = phantom_cleanup.cleanup(dry_run=False)
            numbering = episode_scheme.reconcile(
                dry_run=False, limit=config.EPISODE_HYGIENE_LIMIT)
            log.info("Episode hygiene: phantoms %s (%d quarantined), numbering %s "
                     "(%d duplicates, %d renumbered)",
                     phantoms.get("status"), len(phantoms.get("quarantined") or []),
                     numbering.get("status"), numbering.get("duplicates_removed", 0),
                     numbering.get("renumbered", 0))
            return {"status": "complete", "phantoms": phantoms, "numbering": numbering}
    except Deferred as refusal:
        log.info("Episode hygiene deferred: %s; next scheduled interval, no retry", refusal)
        return {"status": "deferred", "reason": str(refusal)}
