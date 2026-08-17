"""Conservative identity repair for virtual media items."""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

import db
import playback_guard
import tmdb

log = logging.getLogger(__name__)

_IMDB_RE = re.compile(r"\btt\d{6,10}\b", re.IGNORECASE)
_SXXEXX_RE = re.compile(r"\bS(\d{1,3})E(\d{1,4})\b", re.IGNORECASE)
_X_RE = re.compile(r"\b(\d{1,3})x(\d{1,4})\b", re.IGNORECASE)
_SEASON_DIR_RE = re.compile(r"^Season\s+(\d{1,3})$", re.IGNORECASE)
_EP_ONLY_RE = re.compile(r"(?:^|[^a-z])E(?:pisode)?[ ._-]?(\d{1,4})(?:[^0-9]|$)", re.IGNORECASE)
_YEAR_RE = re.compile(r"\((\d{4})\)\s*$")
_repair_lock = threading.Lock()


def _valid_imdb(value: str | None) -> bool:
    return bool(value and re.fullmatch(r"tt\d{6,10}", value.strip(), re.IGNORECASE))


def _norm_title(value: str) -> str:
    value = _YEAR_RE.sub("", value or "")
    value = re.sub(r"\b(?:season|s)\s*\d+.*$", "", value, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _path_identity(path_value: str | None) -> tuple[int | None, int | None]:
    if not path_value:
        return None, None
    path = Path(path_value)
    for text in (path.stem, path.name, str(path)):
        match = _SXXEXX_RE.search(text) or _X_RE.search(text)
        if match:
            return int(match.group(1)), int(match.group(2))
    season_match = _SEASON_DIR_RE.match(path.parent.name)
    episode_match = _EP_ONLY_RE.search(path.stem)
    if season_match and episode_match:
        return int(season_match.group(1)), int(episode_match.group(1))
    return None, None


def _folder_name(item: dict) -> str:
    path_value = item.get("strm_path") or ""
    if path_value:
        parts = Path(path_value).parts
        for marker in ("series", "movies"):
            if marker in parts:
                idx = parts.index(marker)
                if idx + 1 < len(parts):
                    return parts[idx + 1]
    return item.get("title") or ""


def _nfo_imdb(path_value: str | None) -> str | None:
    if not path_value:
        return None
    path = Path(path_value)
    folders = [path.parent, path.parent.parent]
    for folder in folders:
        try:
            for nfo in folder.glob("*.nfo"):
                match = _IMDB_RE.search(nfo.read_text(encoding="utf-8", errors="ignore"))
                if match:
                    return match.group(0).lower()
        except OSError:
            continue
    return None


def _reference_map() -> dict[tuple[str, str], set[str]]:
    refs: dict[tuple[str, str], set[str]] = {}
    with db._connect() as conn:
        queries = (
            ("series", "SELECT title, imdb_id FROM monitored_series"),
            (None, "SELECT title, imdb_id, media_type FROM requests"),
            (None, "SELECT title, imdb_id, media_type FROM media_items"),
        )
        for forced_kind, query in queries:
            for row in conn.execute(query).fetchall():
                imdb_id = row["imdb_id"]
                if not _valid_imdb(imdb_id):
                    continue
                kind = forced_kind or row["media_type"]
                key = ("movie" if kind == "movie" else "series", _norm_title(row["title"]))
                if key[1]:
                    refs.setdefault(key, set()).add(imdb_id.lower())
    return refs


def _strict_tmdb_imdb(title: str, media_type: str,
                      year: int | None) -> tuple[str | None, str]:
    kind = "movie" if media_type == "movie" else "tv"
    params = {"query": title}
    if year and kind == "movie":
        params["year"] = year
    payload = tmdb._get(f"/search/{kind}", params=params) or {}
    wanted = _norm_title(title)
    exact: list[dict] = []
    for hit in payload.get("results") or []:
        hit_title = hit.get("title") or hit.get("name") or hit.get("original_title") or hit.get("original_name") or ""
        if _norm_title(hit_title) != wanted:
            continue
        hit_date = hit.get("release_date") or hit.get("first_air_date") or ""
        if year and hit_date[:4].isdigit() and int(hit_date[:4]) != year:
            continue
        exact.append(hit)
    if len(exact) != 1:
        return None, f"TMDB exact candidates={len(exact)}"
    imdb_id = tmdb.tmdb_to_imdb(exact[0]["id"], media_type=kind)
    return imdb_id, f"TMDB {kind} id={exact[0]['id']}"


def run(batch: int = 500, allow_tmdb: bool = True) -> dict:
    """Repair deterministic gaps and queue ambiguous matches for review."""
    if not _repair_lock.acquire(blocking=False):
        return {"status": "already_running"}
    try:
        if playback_guard.defer("identity_repair"):
            return {"status": "deferred_for_playback"}
        before = db.get_identity_gap_counts()
        items = db.get_virtual_items_missing_identity(limit=batch)
        refs = _reference_map()
        resolved_cache: dict[tuple[str, str, int | None], tuple[str | None, str]] = {}
        applied = 0
        reviewed = 0
        unresolved = 0
        for item in items:
            # Rotate through the entire library instead of letting unresolved
            # recent items permanently monopolize every scheduled batch.
            db.touch_virtual_identity_check(item["token"])
            old_imdb = item.get("imdb_id")
            old_season = item.get("season")
            old_episode = item.get("episode")
            new_season, new_episode = _path_identity(item.get("strm_path"))
            if old_season is not None:
                new_season = old_season
            if old_episode is not None:
                new_episode = old_episode

            new_imdb = old_imdb if _valid_imdb(old_imdb) else None
            method = "path"
            confidence = 1.0
            detail = "episode coordinates parsed from path"
            if not new_imdb:
                new_imdb = _nfo_imdb(item.get("strm_path"))
                if new_imdb:
                    method = "nfo"
                    detail = "IMDb id read from nearby NFO"
            folder = _folder_name(item)
            year_match = _YEAR_RE.search(folder)
            year = int(year_match.group(1)) if year_match else item.get("year")
            clean_title = _YEAR_RE.sub("", folder).strip()
            media_type = "movie" if item.get("media_type") == "movie" else "series"
            if not new_imdb:
                candidates = refs.get((media_type, _norm_title(clean_title)), set())
                if len(candidates) == 1:
                    new_imdb = next(iter(candidates))
                    method = "library_reference"
                    detail = "unique normalized title mapping in local library"
                elif len(candidates) > 1:
                    db.record_identity_repair(
                        item["token"], old_imdb, None, old_season, new_season,
                        old_episode, new_episode, "library_reference", 0.5,
                        "review", f"multiple IMDb ids: {sorted(candidates)}")
                    reviewed += 1
                    continue
            if not new_imdb and allow_tmdb and clean_title:
                cache_key = (media_type, clean_title, year)
                if cache_key not in resolved_cache:
                    try:
                        resolved_cache[cache_key] = _strict_tmdb_imdb(
                            clean_title, media_type, year)
                    except Exception as exc:
                        resolved_cache[cache_key] = (None, f"TMDB error: {exc}")
                    time.sleep(0.05)
                new_imdb, detail = resolved_cache[cache_key]
                method = "tmdb_exact"
                confidence = 0.95 if new_imdb else 0.0

            imdb_changed = bool(new_imdb and new_imdb != old_imdb)
            coords_changed = bool(
                item.get("media_type") == "series" and
                ((new_season is not None and new_season != old_season) or
                 (new_episode is not None and new_episode != old_episode)))
            if imdb_changed or coords_changed:
                db.update_virtual_item_identity(
                    item["token"], new_imdb, new_season, new_episode)
                db.record_identity_repair(
                    item["token"], old_imdb, new_imdb, old_season, new_season,
                    old_episode, new_episode, method, confidence, "applied", detail)
                applied += 1
            else:
                unresolved += 1
        after = db.get_identity_gap_counts()
        result = {
            "status": "complete",
            "scanned": len(items),
            "applied": applied,
            "review": reviewed,
            "unresolved": unresolved,
            "before": before,
            "after": after,
        }
        log.info("Identity repair: %s", result)
        return result
    finally:
        _repair_lock.release()
