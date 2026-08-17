"""Shared search coordinator with durable traces and candidate history."""
from __future__ import annotations

import concurrent.futures
import logging
import time
from dataclasses import asdict
from typing import Callable

import blacklist
import db
import health_cache
import indexer_backoff
import mediafusion
import prowlarr
import settings
import stremio_addons
import torrentio
import tmdb
import zilean
from torrentio import TorrentioStream

log = logging.getLogger(__name__)


def content_key(imdb_id: str, season: int | None = None,
                episode: int | None = None) -> str:
    if season is not None and episode is not None:
        return f"{imdb_id}:S{int(season):02d}E{int(episode):02d}"
    return imdb_id


def _candidate_row(stream: TorrentioStream, rank_order: int) -> dict:
    row = asdict(stream)
    row["magnet"] = stream.magnet
    row["rank_order"] = rank_order
    return row


def _source_jobs(media_type: str, imdb_id: str, title: str,
                 season: int | None, episode: int | None,
                 include_prowlarr: bool) -> list[tuple[str, Callable[[], list]]]:
    kind = "movie" if media_type == "movie" else "series"
    jobs: list[tuple[str, Callable[[], list]]] = []
    if settings.get("ZILEAN_ENABLED", False) and health_cache.is_up("zilean"):
        jobs.append(("zilean", lambda: zilean.fetch_streams(
            imdb_id, season=season, episode=episode)))
    if health_cache.is_up("torrentio") and not indexer_backoff.in_cooldown():
        jobs.append(("torrentio", lambda: torrentio.fetch_streams(
            kind, imdb_id, season=season, episode=episode)))
    if settings.get("MEDIAFUSION_ENABLED", False):
        jobs.append(("mediafusion", lambda: mediafusion.fetch_streams(
            kind, imdb_id, season=season, episode=episode)))
    if include_prowlarr and settings.get("PROWLARR_ENABLED", False):
        jobs.append(("prowlarr", lambda: prowlarr.fetch_streams(
            kind, imdb_id, season=season, episode=episode, title=title,
            aliases=tmdb.search_aliases(imdb_id, kind, title))))
    for addon_name, addon_url in stremio_addons.configured_sources():
        jobs.append((f"stremio/{addon_name}", lambda url=addon_url: stremio_addons.fetch_from(
            url, kind, imdb_id, season=season, episode=episode)))
    return jobs


def search_candidates(media_type: str, imdb_id: str, title: str,
                      season: int | None = None,
                      episode: int | None = None,
                      prefer_season_pack: bool = False,
                      override: dict | None = None,
                      trigger: str = "unknown",
                      include_prowlarr: bool = True,
                      prowlarr_on_cache_miss: bool = False) -> list[TorrentioStream]:
    """Query all enabled catalogs concurrently and persist the search trace."""
    ckey = content_key(imdb_id, season, episode)
    run_id = db.start_search_run(
        ckey, title, media_type, season, episode, trigger)
    jobs = _source_jobs(
        media_type, imdb_id, title, season, episode,
        include_prowlarr and not prowlarr_on_cache_miss)
    source_counts: dict[str, int] = {}
    source_errors: dict[str, str] = {}
    groups: dict[str, list[TorrentioStream]] = {}

    def _run_source(name: str, fn: Callable[[], list]) -> tuple[str, list, float, str | None]:
        started = time.monotonic()
        try:
            result = fn() or []
            return name, result, time.monotonic() - started, None
        except Exception as exc:
            return name, [], time.monotonic() - started, str(exc)[:300]

    def _run_jobs(source_jobs: list[tuple[str, Callable[[], list]]]) -> None:
        if not source_jobs:
            return
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(8, len(source_jobs))) as pool:
            futures = [pool.submit(_run_source, name, fn) for name, fn in source_jobs]
            for future in concurrent.futures.as_completed(futures):
                name, streams, latency, error = future.result()
                groups[name] = streams
                source_counts[name] = len(streams)
                if error:
                    source_errors[name] = error
                db.record_source_query(name, len(streams), latency, error is None, error)

    _run_jobs(jobs)

    if include_prowlarr and prowlarr_on_cache_miss:
        # The direct indexer fan-out is the slowest and heaviest source. Keep it
        # out of the common path when a fast catalog already exposes a cached
        # release, but retain it as a deep fallback for actual misses.
        fast_streams = [stream for streams in groups.values() for stream in streams]
        torrent_hashes = list(dict.fromkeys(
            stream.info_hash.lower() for stream in fast_streams
            if not stream.is_usenet))
        cached = any(stream.is_usenet for stream in fast_streams)
        if torrent_hashes:
            try:
                import debrid
                cached = any(debrid.check_cached_multi(torrent_hashes).values())
            except Exception as exc:
                log.warning("Fast-source cache gate failed for %s: %s", ckey, exc)
        if not cached:
            deep_jobs = [job for job in _source_jobs(
                media_type, imdb_id, title, season, episode, True)
                if job[0] == "prowlarr"]
            jobs.extend(deep_jobs)
            _run_jobs(deep_jobs)

    seen: set[tuple[str, str]] = set()
    merged: list[TorrentioStream] = []
    for name, _ in jobs:
        for stream in groups.get(name, []):
            key = (stream.protocol, stream.info_hash.lower())
            if key in seen:
                continue
            seen.add(key)
            merged.append(stream)
    filtered = blacklist.filter_candidates(merged)
    ranked = torrentio.rank_streams(
        filtered,
        prefer_season_pack=prefer_season_pack,
        override=override,
        media_kind="movie" if media_type == "movie" else None,
    )
    rejected = db.get_rejected_candidate_hashes(ckey)
    ranked = [stream for stream in ranked if stream.info_hash.lower() not in rejected]
    db.upsert_release_candidates(
        ckey, [_candidate_row(stream, idx) for idx, stream in enumerate(ranked, 1)])
    counts = {
        "raw": sum(source_counts.values()),
        "unique": len(merged),
        "ranked": len(ranked),
        "rejected_previous": len(rejected),
    }
    status = "ok" if ranked else ("source_error" if source_errors else "empty")
    db.finish_search_run(
        run_id, status, source_counts, counts,
        error="; ".join(f"{k}: {v}" for k, v in source_errors.items()) or None,
    )
    log.info("Search [%s]: %d raw, %d unique, %d ranked for %s",
             trigger, counts["raw"], counts["unique"], counts["ranked"], ckey)
    return ranked


def cache_map(content_key_value: str,
              candidates: list[TorrentioStream]) -> dict[str, set[str]]:
    """Batch-check candidates across configured debrid providers and persist it."""
    import debrid
    torrents = [item for item in candidates if not item.is_usenet]
    hashes = list(dict.fromkeys(item.info_hash.lower() for item in torrents))
    result = debrid.check_cached_multi(hashes) if hashes else {}
    for provider, provider_hashes in result.items():
        for info_hash in provider_hashes:
            db.mark_candidate_cached(content_key_value, info_hash, provider)
    return result


def mark_selected(content_key_value: str, stream: TorrentioStream,
                  provider: str | None = None) -> None:
    if provider:
        db.mark_candidate_cached(content_key_value, stream.info_hash, provider)
    db.mark_candidate_selected(content_key_value, stream.info_hash, stream.source)


def reject(content_key_value: str, info_hash: str, reason: str) -> None:
    db.reject_release_candidate(content_key_value, info_hash, reason)


def next_cached_alternate(content_key_value: str, media_type: str,
                          imdb_id: str, season: int | None = None,
                          episode: int | None = None) -> tuple[str, str, str] | None:
    """Return a still-cached, sanity-checked alternate from durable history."""
    import release_sanity
    import torbox

    rows = db.get_alternate_candidates(content_key_value)
    if not rows:
        return None
    streams = [
        TorrentioStream(
            name=row.get("title") or "",
            title=row.get("title") or "",
            info_hash=row["info_hash"],
            quality=row.get("quality") or "unknown",
            seeders=int(row.get("seeders") or 0),
            size_gb=float(row.get("size_gb") or 0),
            is_season_pack=torrentio._looks_like_season_pack(
                row.get("title") or "", season),
            source=row.get("source") or "history",
        )
        for row in rows
    ]
    cached = torbox.check_cached([stream.info_hash for stream in streams])
    streams = [stream for stream in streams if stream.info_hash in cached]
    kind = ("movie" if media_type == "movie" else
            "episode" if season is not None and episode is not None else
            "season_pack")
    before_sanity = {stream.info_hash for stream in streams}
    streams = release_sanity.filter_cached(
        streams, kind=kind, season=season, episode=episode,
        imdb_id=imdb_id, label=content_key_value)
    after_sanity = {stream.info_hash for stream in streams}
    for info_hash in before_sanity - after_sanity:
        reject(content_key_value, info_hash, "SANITY_REJECTED")
    if not streams:
        return None
    selected = streams[0]
    mark_selected(content_key_value, selected, "torbox")
    return selected.info_hash.lower(), selected.magnet, "torbox"
