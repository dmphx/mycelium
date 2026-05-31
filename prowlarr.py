"""Prowlarr scraper (Newznab passthrough).

Prowlarr aggregates many torrent + usenet indexers. Its `/api/v1/search`
endpoint is a UI-test utility that quietly omits results from the per-app
search path, so downstream callers must hit each enabled indexer's
Newznab-style passthrough URL `/{id}/api?t={movie|tvsearch}&imdbid=...`
to get the real result set (the same shape Sonarr/Radarr consume).

For each enabled indexer we fan out a parallel query, parse the Newznab
XML, and return TorrentioStream objects:
  - torrent indexers -> TorrentioStream(protocol='torrent', info_hash=...)
  - usenet indexers  -> TorrentioStream(protocol='usenet', nzb_url=...,
                          info_hash=sha1(nzb_url) as dedup key)

Failures degrade silently to an empty list so an unreachable Prowlarr
never blocks the other scrapers.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import re
import time
from xml.etree import ElementTree as ET

import requests

from config import PROWLARR_API_KEY, PROWLARR_BASE_URL, PROWLARR_ENABLED
from torrentio import (
    TorrentioStream,
    _classify_quality,
    _detect_languages,
    _looks_like_season_pack,
)

log = logging.getLogger(__name__)

_BYTES_PER_GB = 1024 ** 3
_HASH_RE = re.compile(r"btih:([a-fA-F0-9]{40})", re.IGNORECASE)
_NEWZNAB_NS = "{http://www.newznab.com/DTD/2010/feeds/attributes/}"

# Cache the enabled-indexer list so we aren't hitting /api/v1/indexer on every
# search; refreshed every _INDEXER_CACHE_TTL_SEC.
_INDEXER_CACHE_TTL_SEC = 600
_indexer_cache: dict = {"at": 0.0, "items": []}


def _enabled_indexers() -> list[dict]:
    """Return [{id, name, protocol}, ...] for currently-enabled Prowlarr indexers."""
    now = time.monotonic()
    if (now - _indexer_cache["at"]) < _INDEXER_CACHE_TTL_SEC and _indexer_cache["items"]:
        return _indexer_cache["items"]
    if not PROWLARR_BASE_URL or not PROWLARR_API_KEY:
        return []
    try:
        resp = requests.get(
            f"{PROWLARR_BASE_URL.rstrip('/')}/api/v1/indexer",
            headers={"X-Api-Key": PROWLARR_API_KEY}, timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json() or []
    except Exception as exc:
        log.warning("Prowlarr indexer list failed: %s", exc)
        return _indexer_cache["items"] or []
    items = [
        {"id": r["id"], "name": r.get("name", "?"),
         "protocol": (r.get("protocol") or "torrent").lower()}
        for r in rows if r.get("enable")
    ]
    _indexer_cache["items"] = items
    _indexer_cache["at"] = now
    return items


def _torrent_stream(item: ET.Element, season: int | None,
                    indexer_name: str) -> TorrentioStream | None:
    title = (item.findtext("title") or "").strip()
    guid = (item.findtext("guid") or "").strip()
    link = (item.findtext("link") or "").strip()

    info_hash: str | None = None
    for blob in (guid, link):
        if not blob:
            continue
        m = _HASH_RE.search(blob)
        if m:
            info_hash = m.group(1).lower()
            break
    if not info_hash:
        # Look in the newznab:attr elements (some indexers expose infohash there)
        for attr in item.findall(f"{_NEWZNAB_NS}attr"):
            if attr.get("name", "").lower() == "infohash":
                v = (attr.get("value") or "").strip().lower()
                if len(v) == 40:
                    info_hash = v
                    break
    if not info_hash:
        return None

    size_bytes = 0
    try:
        size_bytes = int(item.findtext("size") or "0")
    except (ValueError, TypeError):
        pass
    if size_bytes == 0:
        for attr in item.findall(f"{_NEWZNAB_NS}attr"):
            if attr.get("name", "").lower() == "size":
                try:
                    size_bytes = int(attr.get("value") or 0)
                except (ValueError, TypeError):
                    pass
                break
    size_gb = round(size_bytes / _BYTES_PER_GB, 2) if size_bytes else 0.0

    seeders = 0
    for attr in item.findall(f"{_NEWZNAB_NS}attr"):
        if attr.get("name", "").lower() == "seeders":
            try:
                seeders = int(attr.get("value") or 0)
            except (ValueError, TypeError):
                pass
            break

    name = f"{title} [{indexer_name}]" if indexer_name else title
    blob = {"name": name, "title": title}
    return TorrentioStream(
        name=name, title=title, info_hash=info_hash,
        quality=_classify_quality(blob),
        seeders=seeders, size_gb=size_gb,
        is_season_pack=_looks_like_season_pack(title, season),
        languages=_detect_languages(name),
        source=f"prowlarr/{indexer_name.lower()}" if indexer_name else "prowlarr",
        protocol="torrent",
    )


def _usenet_stream(item: ET.Element, season: int | None,
                    indexer_name: str) -> TorrentioStream | None:
    title = (item.findtext("title") or "").strip()
    # Newznab puts the actual NZB download URL in <link>, the human-facing
    # detail URL in <guid>. TorBox fetches the link via its createusenet
    # endpoint, so prefer <link>; fall back to <guid> if link is missing.
    nzb_url = (item.findtext("link") or item.findtext("guid") or "").strip()
    enclosure = item.find("enclosure")
    if enclosure is not None:
        url_attr = enclosure.get("url")
        if url_attr and url_attr.startswith("http"):
            nzb_url = url_attr
    if not nzb_url or not nzb_url.startswith("http"):
        return None

    # Synthetic dedup key: sha1 of the canonical URL truncated to 40 chars to
    # share the info_hash slot. Won't collide with torrent btih (different
    # prefix space) for practical purposes.
    info_hash = hashlib.sha1(nzb_url.encode()).hexdigest()

    size_bytes = 0
    try:
        size_bytes = int(item.findtext("size") or "0")
    except (ValueError, TypeError):
        pass
    if size_bytes == 0:
        for attr in item.findall(f"{_NEWZNAB_NS}attr"):
            if attr.get("name", "").lower() == "size":
                try:
                    size_bytes = int(attr.get("value") or 0)
                except (ValueError, TypeError):
                    pass
                break
    size_gb = round(size_bytes / _BYTES_PER_GB, 2) if size_bytes else 0.0

    grabs = 0
    for attr in item.findall(f"{_NEWZNAB_NS}attr"):
        if attr.get("name", "").lower() == "grabs":
            try:
                grabs = int(attr.get("value") or 0)
            except (ValueError, TypeError):
                pass
            break

    name = f"{title} [{indexer_name} NZB]" if indexer_name else title
    blob = {"name": name, "title": title}
    return TorrentioStream(
        name=name, title=title, info_hash=info_hash,
        quality=_classify_quality(blob),
        # Use "grabs" as the seeder-equivalent ranking signal for usenet:
        # rank_streams sorts by it just the same.
        seeders=grabs, size_gb=size_gb,
        is_season_pack=_looks_like_season_pack(title, season),
        languages=_detect_languages(name),
        source=f"prowlarr/{indexer_name.lower()}" if indexer_name else "prowlarr",
        protocol="usenet",
        nzb_url=nzb_url,
    )


def _query_one_indexer(idx: dict, search_type: str, imdb_id: str,
                        season: int | None, episode: int | None,
                        timeout: int) -> list[TorrentioStream]:
    numeric_imdb = imdb_id.lstrip("t")
    params = {
        "t": search_type,
        "imdbid": numeric_imdb,
        "apikey": PROWLARR_API_KEY,
    }
    if search_type == "tvsearch" and season is not None:
        params["season"] = season
        if episode is not None:
            params["ep"] = episode
    if search_type == "movie":
        params["cat"] = "2000"
    else:
        params["cat"] = "5000"
    url = f"{PROWLARR_BASE_URL.rstrip('/')}/{idx['id']}/api"
    try:
        resp = requests.get(
            url, params=params, timeout=timeout,
            headers={"X-Api-Key": PROWLARR_API_KEY},
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Prowlarr [%s] query failed: %s", idx["name"], exc)
        return []
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        log.warning("Prowlarr [%s] XML parse failed: %s", idx["name"], exc)
        return []
    items = root.findall(".//item")
    parser = _usenet_stream if idx["protocol"] == "usenet" else _torrent_stream
    parsed = [s for s in (parser(it, season, idx["name"]) for it in items) if s is not None]
    if items:
        log.info("Prowlarr [%s/%s] %d items -> %d parsed",
                 idx["name"], idx["protocol"], len(items), len(parsed))
    return parsed


def fetch_streams(
    media_type: str,
    imdb_id: str,
    season: int | None = None,
    episode: int | None = None,
    timeout: int = 25,
) -> list[TorrentioStream]:
    """Return parsed TorrentioStream objects from every enabled Prowlarr
    indexer in parallel. Both torrent and usenet sources contribute."""
    if not PROWLARR_ENABLED or not PROWLARR_BASE_URL or not PROWLARR_API_KEY:
        return []
    indexers = _enabled_indexers()
    if not indexers:
        return []
    search_type = "movie" if media_type == "movie" else "tvsearch"
    log.info("Prowlarr: fanning out %s search across %d enabled indexers",
             search_type, len(indexers))
    out: list[TorrentioStream] = []
    seen_keys: set[str] = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(indexers))) as ex:
        futures = {
            ex.submit(_query_one_indexer, idx, search_type, imdb_id,
                       season, episode, timeout): idx
            for idx in indexers
        }
        for fut in concurrent.futures.as_completed(futures):
            try:
                streams = fut.result()
            except Exception as exc:
                log.warning("Prowlarr [%s] thread crashed: %s", futures[fut]["name"], exc)
                continue
            for s in streams:
                if s.info_hash in seen_keys:
                    continue
                seen_keys.add(s.info_hash)
                out.append(s)
    log.info("Prowlarr fan-out total: %d unique streams", len(out))
    return out
