"""Shared cross-provider rate-limit backoff for the scraper pool.

Torrentio's public instance returns HTTP 429 when we query it too fast.
Previously fetch_streams swallowed the 429 as an empty result set, so the series
monitor never slowed down: it burned through the whole ~290k wanted-episode queue
at full speed, every call bouncing off a 429 wall — pinning CPU and, worse,
exhausting Torrentio's rate budget so genuinely available content also failed to
resolve.

This module records a global "cooldown until" timestamp whenever a provider
reports a 429 (honoring Retry-After when it is a plain seconds value). The series
monitor loop consults ``remaining()`` and sleeps it off between episodes, so it
backs off to the provider's tolerance instead of hammering. The interactive /
webhook search path records 429s too but never sleeps on them, so user-facing
requests stay responsive.

Only Torrentio (the shared public aggregator we are clearly rate-limiting) arms
the cooldown. Individual Prowlarr indexers 429 on their own schedule and must not
be able to pause the whole pool; Torrentio-paced backoff already slows every
per-episode cycle, prowlarr fan-out included.
"""
import logging
import time

log = logging.getLogger(__name__)

# Bounds on how long a single 429 parks the scraper pool.
_MIN_COOLDOWN_SEC = 30.0
_MAX_COOLDOWN_SEC = 300.0
_DEFAULT_COOLDOWN_SEC = 120.0

_cooldown_until = 0.0  # time.monotonic() deadline; 0.0 == not throttled


def note_rate_limit(provider: str, retry_after=None) -> None:
    """Record a 429 from ``provider``, arming (or extending) a global cooldown.

    ``retry_after`` is the response's Retry-After header if present. Only a plain
    seconds value is honored; an HTTP-date (or anything unparseable) falls back
    to the default. The result is clamped to [_MIN, _MAX] so a hostile or absurd
    header can neither disable the backoff nor wedge the monitor for hours.
    """
    global _cooldown_until
    secs = _DEFAULT_COOLDOWN_SEC
    if retry_after is not None:
        try:
            secs = float(retry_after)
        except (TypeError, ValueError):
            secs = _DEFAULT_COOLDOWN_SEC
    secs = max(_MIN_COOLDOWN_SEC, min(_MAX_COOLDOWN_SEC, secs))
    until = time.monotonic() + secs
    if until > _cooldown_until:
        _cooldown_until = until
        log.warning("Indexer backoff: %s returned 429  -  pausing scraper pool for %.0fs",
                    provider, secs)


def remaining() -> float:
    """Seconds left in the current cooldown (0.0 when not throttled)."""
    return max(0.0, _cooldown_until - time.monotonic())


def in_cooldown() -> bool:
    return remaining() > 0.0
