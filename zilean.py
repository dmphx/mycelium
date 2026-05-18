import logging

import requests

from config import ZILEAN_URL
from torrentio import TorrentioStream, _classify_quality, _looks_like_season_pack

log = logging.getLogger(__name__)

_BYTES_PER_GB = 1024 ** 3


def _to_stream(raw: dict, season: int | None) -> TorrentioStream | None:
    info_hash = raw.get("infoHash")
    if not info_hash:
        return None
    title = raw.get("title", "") or ""
    size_bytes = raw.get("size") or 0
    size_gb = round(size_bytes / _BYTES_PER_GB, 2) if size_bytes else 0.0
    blob = {"name": title, "title": title}
    return TorrentioStream(
        name="Zilean",
        title=title,
        info_hash=info_hash.lower(),
        quality=_classify_quality(blob),
        seeders=0,  # Zilean doesn't expose seeder counts
        size_gb=size_gb,
        is_season_pack=_looks_like_season_pack(title, season),
    )


def fetch_streams(
    query: str,
    season: int | None = None,
    episode: int | None = None,
    timeout: int = 10,
) -> list[TorrentioStream]:
    params: dict[str, object] = {"Query": query}
    if season is not None:
        params["Season"] = season
    if episode is not None:
        params["Episode"] = episode
    url = f"{ZILEAN_URL.rstrip('/')}/torrents/search"
    log.info("Querying Zilean: %s params=%s", url, params)
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Zilean unavailable: %s", exc)
        return []
    raw_list = resp.json() or []
    parsed = [s for s in (_to_stream(r, season) for r in raw_list) if s is not None]
    log.info("Zilean returned %d results (%d parsed)", len(raw_list), len(parsed))
    return parsed
