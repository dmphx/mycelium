"""Keep series episodes on TMDB's numbering when a season counts absolutely.

Long anime (Naruto Shippuden, One Piece) are numbered on TMDB by a running
count inside each season: season 2 holds episodes 33-53, not 1-21. Items
registered before TMDB switched, and release files tagged S02E01..S02E21, carry
the per-season numbers. Both sets then sit in the same season folder, and Plex
(which follows TMDB) matches the absolute set while the per-season set shows as
duplicates or as the only copy of a season.

`tmdb_episode_number` maps a per-season number onto the season's absolute
range for new registrations. `discover`/`reconcile` repair existing items:
a per-season item whose absolute twin exists is a duplicate and is quarantined;
one without a twin is renamed to the absolute number, so no episode is lost.
Discovery is read only; applying is playback gated and serialized with other
media maintenance, and every touched row and file is recorded in a manifest.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import db
import playback_guard
import strm_generator
import tmdb
from config import MEDIA_PATH, SPORE_MEDIA_PATH
from io_utils import atomic_write_text

log = logging.getLogger(__name__)

_CACHE_TTL = 6 * 3600
_cache: dict[tuple[str, int], tuple[float, tuple[int, ...]]] = {}
_cache_lock = threading.Lock()
_EPISODE_SUFFIX_RE = re.compile(r"\s*S\d{1,3}E\d{1,4}$", re.IGNORECASE)


def _season_numbers(imdb_id: str, season: int, tmdb_client=None) -> tuple[int, ...]:
    """Sorted TMDB episode numbers (>= 1) of one season; () when unknown."""
    key = (imdb_id, int(season))
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    client = tmdb_client or tmdb
    numbers: tuple[int, ...] = ()
    try:
        tmdb_id = client.find_by_imdb(imdb_id, kind="tv")
        rows = client.get_season_episodes(tmdb_id, int(season)) if tmdb_id else []
        numbers = tuple(sorted({
            int(row["episode_number"]) for row in rows or []
            if row.get("episode_number") is not None and int(row["episode_number"]) >= 1
        }))
    except Exception as exc:
        log.debug("TMDB season numbers unavailable for %s S%s: %s", imdb_id, season, exc)
        return ()
    if numbers:
        with _cache_lock:
            _cache[key] = (now, numbers)
    return numbers


def absolute_offset(numbers: tuple[int, ...]) -> int | None:
    """First episode number of a season numbered by a running count, else None.

    Only a contiguous range starting above 1 qualifies; anything irregular is
    left alone rather than guessed at.
    """
    if not numbers or numbers[0] <= 1:
        return None
    first = numbers[0]
    if list(numbers) != list(range(first, first + len(numbers))):
        return None
    return first


def map_per_season(numbers: tuple[int, ...], episode: int) -> int | None:
    """The absolute number for per-season `episode`, or None when it is not one."""
    first = absolute_offset(numbers)
    if first is None or episode < 1 or episode >= first or episode > len(numbers):
        return None
    return first + episode - 1


def tmdb_episode_number(imdb_id: str | None, season: int, episode: int,
                        tmdb_client=None) -> int:
    """Translate a per-season episode number to TMDB's absolute numbering.

    Returns `episode` unchanged unless TMDB numbers this season by a running
    count and `episode` is a per-season number of it.
    """
    if not imdb_id or season is None or episode is None or int(season) < 1:
        return episode
    mapped = map_per_season(_season_numbers(imdb_id, int(season), tmdb_client), int(episode))
    if mapped is not None:
        log.info("Episode numbering: %s S%02dE%02d is TMDB episode %d",
                 imdb_id, int(season), int(episode), mapped)
        return mapped
    return episode


def discover(imdb_ids: set[str] | None = None, items: list[dict] | None = None,
             tmdb_client=None) -> dict:
    """Plan per-season items on absolutely numbered seasons.

    Returns {"duplicates": [...], "renumber": [...], "skipped": [...]}. Each
    entry carries the item token, show, season, old and absolute episode.
    """
    wanted = {value for value in (imdb_ids or set()) if value}
    rows = items if items is not None else db.get_all_virtual_items()
    grouped: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for item in rows:
        imdb_id = str(item.get("imdb_id") or "")
        if item.get("media_type") != "series" or not imdb_id.startswith("tt"):
            continue
        if wanted and imdb_id not in wanted:
            continue
        try:
            season, episode = int(item.get("season")), int(item.get("episode"))
        except (TypeError, ValueError):
            continue
        if season >= 1 and episode >= 1:
            grouped[(imdb_id, season)].append(item)

    duplicates, renumber, skipped = [], [], []
    for (imdb_id, season), season_items in sorted(grouped.items()):
        numbers = _season_numbers(imdb_id, season, tmdb_client)
        first = absolute_offset(numbers)
        if first is None:
            continue
        by_episode = {int(item["episode"]): item for item in season_items}
        for item in sorted(season_items, key=lambda row: int(row["episode"])):
            episode = int(item["episode"])
            if episode >= first:
                continue
            target = map_per_season(numbers, episode)
            entry = {"token": item.get("token"), "imdb_id": imdb_id,
                     "title": item.get("title"), "season": season,
                     "episode": episode, "absolute": target,
                     "strm_path": item.get("strm_path"),
                     "play_count": item.get("play_count") or 0}
            if target is None:
                skipped.append({**entry, "reason": "beyond the season's episode count"})
                continue
            twin = by_episode.get(target)
            if twin is not None:
                duplicates.append({**entry, "twin_token": twin.get("token")})
            else:
                renumber.append(entry)
    return {"duplicates": duplicates, "renumber": renumber, "skipped": skipped}


def _media_files(strm_path: Path) -> list[Path]:
    return [strm_path, strm_path.with_suffix(".nfo")]


def _thumb(strm_path: Path) -> Path:
    return Path(f"{strm_path.with_suffix('')}-thumb.jpg")


def _spore_files(strm_path: Path) -> list[Path]:
    stub_dir = strm_generator._spore_stub_dir(strm_path)
    return [stub_dir / (strm_path.stem + ".mkv"), stub_dir / (strm_path.stem + ".minfo")]


def _renamed_strm(strm_path: Path, season: int, absolute: int) -> Path:
    show = _EPISODE_SUFFIX_RE.sub("", strm_path.stem)
    return strm_path.with_name(f"{show} S{season:02d}E{absolute:02d}{strm_path.suffix}")


def _quarantine_move(path: Path, root: Path, quarantine: Path, area: str,
                     moved: list[tuple[Path, Path]]) -> None:
    destination = quarantine / area / path.relative_to(root)
    if destination.exists():
        raise FileExistsError(f"quarantine target already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(destination))
    moved.append((path, destination))


def _notify(removed: Path | None = None, added: Path | None = None) -> None:
    try:
        import media_servers
        if removed is not None:
            media_servers.mark_removed(removed)
        if added is not None:
            media_servers.mark(added)
    except Exception as exc:
        log.warning("Episode numbering: scan notification failed: %s", exc)


def reconcile(imdb_ids: set[str] | None = None, dry_run: bool = True,
              quarantine_root: str | Path | None = None, limit: int | None = None,
              tmdb_client=None) -> dict:
    """Discover, or apply: quarantine duplicates and rename the rest.

    `limit` bounds how many items one run touches.
    """
    plan = discover(imdb_ids=imdb_ids, tmdb_client=tmdb_client)
    if dry_run:
        return {"status": "dry_run", **plan}
    if playback_guard.active(force=True):
        return {"status": "deferred_playback", **plan}
    if not strm_generator._maintenance_lock.acquire(blocking=False):
        return {"status": "deferred_maintenance", **plan}

    media_root = Path(MEDIA_PATH)
    spore_root = Path(SPORE_MEDIA_PATH)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine = Path(quarantine_root or media_root.parent / "quarantine"
                      / "episode-numbering") / stamp
    done_duplicates, done_renumber, failed = [], [], []
    budget = limit if limit is not None else len(plan["duplicates"]) + len(plan["renumber"])
    try:
        for entry in plan["duplicates"]:
            if budget <= 0:
                break
            budget -= 1
            row = db.get_virtual_item(entry["token"])
            if not row:
                continue
            strm = Path(entry["strm_path"] or "")
            moved: list[tuple[Path, Path]] = []
            try:
                # Delete the row first: the catbox repair job recreates any
                # missing .strm whose row still exists.
                db.delete_virtual_item(entry["token"])
                if strm.is_absolute() and _under(strm, media_root):
                    for path in _media_files(strm) + [_thumb(strm)]:
                        if path.is_file():
                            _quarantine_move(path, media_root, quarantine, "media", moved)
                    for path in _spore_files(strm):
                        if path.is_file():
                            _quarantine_move(path, spore_root, quarantine, "spore", moved)
                done_duplicates.append({**entry, "row": row,
                                        "files": [str(d.relative_to(quarantine))
                                                  for _, d in moved]})
                _notify(removed=strm)
            except Exception as exc:
                for source, destination in reversed(moved):
                    if destination.exists() and not source.exists():
                        shutil.move(str(destination), str(source))
                db.restore_virtual_item(row)
                failed.append({**entry, "reason": str(exc)})

        for entry in plan["renumber"]:
            if budget <= 0:
                break
            budget -= 1
            row = db.get_virtual_item(entry["token"])
            if not row:
                continue
            old = Path(entry["strm_path"] or "")
            if not old.is_absolute() or not _under(old, media_root):
                failed.append({**entry, "reason": "strm path outside the media root"})
                continue
            new = _renamed_strm(old, entry["season"], entry["absolute"])
            pairs = list(zip(_media_files(old), _media_files(new))) + \
                list(zip(_spore_files(old), _spore_files(new)))
            if any(target.exists() for _, target in pairs):
                failed.append({**entry, "reason": f"target already exists: {new.name}"})
                continue
            renamed: list[tuple[Path, Path]] = []
            moved: list[tuple[Path, Path]] = []
            try:
                # The old episode's still image shows another episode.
                if _thumb(old).is_file():
                    _quarantine_move(_thumb(old), media_root, quarantine, "media", moved)
                for source, target in pairs:
                    if source.is_file():
                        shutil.move(str(source), str(target))
                        renamed.append((source, target))
                db.renumber_virtual_episode(
                    entry["token"], entry["absolute"], new.stem, str(new))
                db.mark_episode_status(entry["imdb_id"], entry["season"],
                                       entry["absolute"], "found")
                done_renumber.append({**entry, "row": row, "new_strm_path": str(new)})
                _notify(removed=old, added=new)
            except Exception as exc:
                for source, target in reversed(renamed):
                    if target.exists() and not source.exists():
                        shutil.move(str(target), str(source))
                for source, destination in reversed(moved):
                    if destination.exists() and not source.exists():
                        shutil.move(str(destination), str(source))
                db.restore_virtual_item(row)
                failed.append({**entry, "reason": str(exc)})

        if done_duplicates or done_renumber or failed:
            atomic_write_text(quarantine / "manifest.json", json.dumps({
                "created_at": datetime.now(timezone.utc).isoformat(),
                "duplicates": done_duplicates,
                "renumbered": done_renumber,
                "failed": failed,
            }, indent=2, default=str))
    finally:
        strm_generator._maintenance_lock.release()

    return {"status": "complete",
            "manifest": str(quarantine / "manifest.json")
            if (done_duplicates or done_renumber or failed) else None,
            "duplicates_removed": len(done_duplicates),
            "renumbered": len(done_renumber),
            "failed": failed,
            "skipped": plan["skipped"]}


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
