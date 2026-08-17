"""Find and quarantine legacy episodes beyond a show's real season boundary.

Older Mycelium builds could register a fixed 24 episodes when TMDB metadata was
temporarily unavailable. This module removes only that narrow failure shape.
Discovery is read only. Applying a cleanup is playback gated, serialized with
other media maintenance, and moves files into quarantine before deleting the
corresponding virtual item row.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
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
_SEASON_FOLDER_RE = re.compile(r"^Season (\d+)$", re.IGNORECASE)
_EPISODE_FILE_RE = re.compile(r"S(\d{1,3})E(\d{1,4})$", re.IGNORECASE)


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _official_last(tmdb_id: int | None, show_info: dict | None, season_number: int,
                   tmdb_client) -> int | None:
    declared_count = next((
        _int_or_none(season.get("episode_count"))
        for season in ((show_info or {}).get("seasons") or [])
        if _int_or_none(season.get("season_number")) == season_number
    ), None)
    episodes = (tmdb_client.get_season_episodes(tmdb_id, season_number)
                if tmdb_id is not None and declared_count else [])
    episode_numbers = {
        number for number in (_int_or_none(ep.get("episode_number")) for ep in episodes)
        if number is not None and number >= 1
    }
    last = max(episode_numbers) if episode_numbers else None
    if (not declared_count or last != declared_count
            or len(episode_numbers) != declared_count):
        return None
    return last


def discover(imdb_ids: set[str] | None = None, items: list[dict] | None = None,
             tmdb_client=None, orphan_show_ids: dict[str, str] | None = None,
             spore_root: str | Path | None = None) -> dict:
    """Return never-played episodes beyond a double-confirmed TMDB boundary.

    A season is authoritative only when the show's declared episode count and
    the detailed season payload agree on the final episode number. Empty,
    partial, or unusual metadata is skipped rather than treated as evidence for
    removal.
    """
    tmdb_client = tmdb_client or tmdb
    wanted_ids = {value for value in (imdb_ids or set()) if value}
    rows = items if items is not None else db.get_all_virtual_items()
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in rows:
        imdb_id = str(item.get("imdb_id") or "")
        season = _int_or_none(item.get("season"))
        episode = _int_or_none(item.get("episode"))
        if item.get("media_type") != "series" or not imdb_id.startswith("tt"):
            continue
        if wanted_ids and imdb_id not in wanted_ids:
            continue
        if season is None or episode is None or episode < 1:
            continue
        grouped[imdb_id].append(item)

    candidates: list[dict] = []
    skipped: list[dict] = []
    for imdb_id, show_items in sorted(grouped.items()):
        tmdb_id = tmdb_client.find_by_imdb(imdb_id, kind="tv")
        show_info = tmdb_client.get_show_info(tmdb_id) if tmdb_id else None
        for season_number in sorted({_int_or_none(item.get("season")) for item in show_items}):
            season_items = [
                item for item in show_items
                if _int_or_none(item.get("season")) == season_number
            ]
            official_last = _official_last(
                tmdb_id, show_info, season_number, tmdb_client
            )
            if official_last is None:
                skipped.append({
                    "imdb_id": imdb_id,
                    "season": season_number,
                    "reason": "TMDB season boundary not authoritative",
                })
                continue
            for item in season_items:
                episode = _int_or_none(item.get("episode"))
                if episode is None or episode <= official_last:
                    continue
                if item.get("last_played"):
                    skipped.append({
                        "imdb_id": imdb_id,
                        "season": season_number,
                        "episode": episode,
                        "reason": "episode has playback history",
                    })
                    continue
                candidates.append({
                    "token": item.get("token"),
                    "imdb_id": imdb_id,
                    "title": item.get("title"),
                    "season": season_number,
                    "episode": episode,
                    "official_last": official_last,
                    "strm_path": item.get("strm_path"),
                })

    known_tokens = {str(item.get("token") or "") for item in rows}
    root = Path(spore_root or SPORE_MEDIA_PATH)
    for show_folder, imdb_id in sorted((orphan_show_ids or {}).items()):
        if wanted_ids and imdb_id not in wanted_ids:
            continue
        folder = root / "series" / show_folder
        if not folder.is_dir() or not _under(folder, root):
            continue
        tmdb_id = tmdb_client.find_by_imdb(imdb_id, kind="tv")
        show_info = tmdb_client.get_show_info(tmdb_id) if tmdb_id else None
        for season_folder in sorted(path for path in folder.iterdir() if path.is_dir()):
            season_match = _SEASON_FOLDER_RE.match(season_folder.name)
            if not season_match:
                continue
            season_number = int(season_match.group(1))
            official_last = _official_last(
                tmdb_id, show_info, season_number, tmdb_client
            )
            if official_last is None:
                skipped.append({
                    "imdb_id": imdb_id,
                    "season": season_number,
                    "reason": "TMDB season boundary not authoritative",
                })
                continue
            for stub in sorted(season_folder.glob("*.mkv")):
                episode_match = _EPISODE_FILE_RE.search(stub.stem)
                if not episode_match:
                    continue
                parsed_season = int(episode_match.group(1))
                episode = int(episode_match.group(2))
                if parsed_season != season_number or episode <= official_last:
                    continue
                minfo = stub.with_suffix(".minfo")
                token = ""
                if minfo.is_file():
                    token = next((line.split("=", 1)[1].strip()
                                  for line in minfo.read_text(encoding="utf-8").splitlines()
                                  if line.startswith("token=")), "")
                if token and token in known_tokens:
                    skipped.append({
                        "imdb_id": imdb_id,
                        "season": season_number,
                        "episode": episode,
                        "reason": "Spore token still has a virtual item",
                    })
                    continue
                relative = stub.relative_to(root).with_suffix(".strm")
                candidates.append({
                    "token": None,
                    "imdb_id": imdb_id,
                    "title": show_folder,
                    "season": season_number,
                    "episode": episode,
                    "official_last": official_last,
                    "strm_path": str(Path(MEDIA_PATH) / relative),
                    "orphan_stub": True,
                })

    candidates.sort(key=lambda row: (
        str(row.get("title") or ""), row["season"], row["episode"]
    ))
    return {"candidates": candidates, "skipped": skipped}


def _files_for(candidate: dict, media_root: Path, spore_root: Path) -> list[tuple[Path, str]]:
    raw_path = candidate.get("strm_path")
    if not raw_path:
        return []
    strm_path = Path(raw_path)
    if not _under(strm_path, media_root):
        return []
    relative = strm_path.relative_to(media_root)
    stub_base = spore_root / relative
    return [
        (strm_path, "media"),
        (strm_path.with_suffix(".nfo"), "media"),
        (stub_base.with_suffix(".mkv"), "spore"),
        (stub_base.with_suffix(".minfo"), "spore"),
    ]


def cleanup(imdb_ids: set[str] | None = None, dry_run: bool = True,
            quarantine_root: str | Path | None = None,
            orphan_show_ids: dict[str, str] | None = None) -> dict:
    """Discover or quarantine qualified legacy phantom episodes."""
    report = discover(imdb_ids=imdb_ids, orphan_show_ids=orphan_show_ids)
    if dry_run:
        return {"status": "dry_run", **report}
    if playback_guard.active(force=True):
        return {"status": "deferred_playback", **report}
    if not strm_generator._maintenance_lock.acquire(blocking=False):
        return {"status": "deferred_maintenance", **report}

    media_root = Path(MEDIA_PATH)
    spore_root = Path(SPORE_MEDIA_PATH)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine = Path(quarantine_root or media_root.parent / "quarantine" / "phantom-episodes") / stamp
    quarantined: list[dict] = []
    failed: list[dict] = []
    try:
        for candidate in report["candidates"]:
            sources = [entry for entry in _files_for(candidate, media_root, spore_root)
                       if entry[0].is_file()]
            if not sources:
                failed.append({**candidate, "reason": "no exact source or Spore files found"})
                continue
            moved: list[tuple[Path, Path]] = []
            try:
                for source, area in sources:
                    root = media_root if area == "media" else spore_root
                    destination = quarantine / area / source.relative_to(root)
                    if destination.exists():
                        raise FileExistsError(f"quarantine target already exists: {destination}")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(destination))
                    moved.append((source, destination))
                if candidate.get("token"):
                    db.delete_virtual_item(candidate["token"])
                quarantined.append({
                    **candidate,
                    "files": [str(destination.relative_to(quarantine)) for _, destination in moved],
                })
                try:
                    import media_servers
                    media_servers.mark_removed(Path(candidate["strm_path"]))
                except Exception as exc:
                    log.warning("Phantom cleanup scan notification failed for %s: %s",
                                candidate.get("strm_path"), exc)
            except Exception as exc:
                for source, destination in reversed(moved):
                    if destination.exists() and not source.exists():
                        source.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(destination), str(source))
                failed.append({**candidate, "reason": str(exc)})

        if quarantined or failed:
            manifest = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "quarantined": quarantined,
                "failed": failed,
            }
            atomic_write_text(quarantine / "manifest.json", json.dumps(manifest, indent=2))
    finally:
        strm_generator._maintenance_lock.release()

    return {
        "status": "complete",
        "quarantine": str(quarantine) if quarantined else None,
        "quarantined": quarantined,
        "failed": failed,
        "skipped": report["skipped"],
    }
