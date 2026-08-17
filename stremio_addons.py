"""Generic Stremio stream-addon search adapter.

The adapter intentionally consumes only metadata and info hashes. It does not
send debrid credentials to addons and never follows direct playback URLs.
Self-hosted Comet and other standard stream addons can therefore contribute
candidate hashes without becoming part of the playback trust boundary.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

import requests

from config import STREMIO_ADDON_TIMEOUT_SEC, STREMIO_ADDON_URLS
from torrentio import (
    TorrentioStream,
    _HTTP_HEADERS,
    _classify_quality,
    _detect_languages,
    _looks_like_season_pack,
    _parse_seeders,
    _parse_size_gb,
)

log = logging.getLogger(__name__)


def _addon_name(base_url: str) -> str:
    parsed = urlparse(base_url)
    host = parsed.hostname or "addon"
    return host.split(".")[0].lower()


def _build_url(base_url: str, media_type: str, imdb_id: str,
               season: int | None, episode: int | None) -> str:
    kind = "movie" if media_type == "movie" else "series"
    item_id = imdb_id
    if kind == "series":
        if season is None or episode is None:
            raise ValueError("season and episode are required for series")
        item_id = f"{imdb_id}:{season}:{episode}"
    return f"{base_url.rstrip('/')}/stream/{kind}/{item_id}.json"


def _to_stream(raw: dict, season: int | None, source: str) -> TorrentioStream | None:
    info_hash = str(raw.get("infoHash") or "").lower().strip()
    if len(info_hash) != 40:
        return None
    description = str(raw.get("description") or raw.get("title") or "")
    name = str(raw.get("name") or "")
    behavior = raw.get("behaviorHints") or {}
    binge_group = str(behavior.get("bingeGroup") or "").replace("|", " ")
    combined_name = f"{name} {binge_group}".strip()
    blob = {"name": combined_name, "title": description}
    return TorrentioStream(
        name=combined_name,
        title=description,
        info_hash=info_hash,
        quality=_classify_quality(blob),
        seeders=_parse_seeders(description),
        size_gb=_parse_size_gb(description),
        is_season_pack=_looks_like_season_pack(description, season),
        languages=_detect_languages(f"{combined_name} {description}"),
        source=f"stremio/{source}",
    )


def fetch_from(base_url: str, media_type: str, imdb_id: str,
               season: int | None = None, episode: int | None = None,
               timeout: int | None = None) -> list[TorrentioStream]:
    """Fetch one configured addon and return hash-backed candidates only."""
    try:
        url = _build_url(base_url, media_type, imdb_id, season, episode)
    except ValueError:
        return []
    source = _addon_name(base_url)
    try:
        if timeout is None:
            import settings
            timeout = int(settings.get(
                "STREMIO_ADDON_TIMEOUT_SEC", STREMIO_ADDON_TIMEOUT_SEC))
        resp = requests.get(
            url,
            timeout=timeout,
            headers=_HTTP_HEADERS,
        )
        resp.raise_for_status()
        raw_streams = (resp.json() or {}).get("streams") or []
    except (requests.RequestException, ValueError) as exc:
        log.warning("Stremio addon [%s] unavailable: %s", source, exc)
        return []
    parsed = [
        stream for stream in
        (_to_stream(raw, season, source) for raw in raw_streams)
        if stream is not None
    ]
    log.info("Stremio addon [%s] returned %d hash candidate(s)", source, len(parsed))
    return parsed


def configured_sources() -> list[tuple[str, str]]:
    """Return stable source-name and URL pairs for the configured addons."""
    import settings
    urls = settings.get("STREMIO_ADDON_URLS", STREMIO_ADDON_URLS) or []
    if isinstance(urls, str):
        urls = [value.strip() for value in urls.split(",") if value.strip()]
    return [(_addon_name(url), str(url).rstrip("/")) for url in urls]


def fetch_streams(media_type: str, imdb_id: str,
                  season: int | None = None,
                  episode: int | None = None) -> list[TorrentioStream]:
    """Query configured addons serially.

    The shared search coordinator normally calls fetch_from in parallel. This
    convenience entrypoint keeps the module useful to direct callers and tests.
    """
    out: list[TorrentioStream] = []
    seen: set[str] = set()
    for _, url in configured_sources():
        for stream in fetch_from(url, media_type, imdb_id, season, episode):
            if stream.info_hash not in seen:
                seen.add(stream.info_hash)
                out.append(stream)
    return out
