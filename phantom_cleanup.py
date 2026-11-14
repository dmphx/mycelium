"""Find and quarantine legacy episodes beyond a show's real season boundary.

Older Mycelium builds could register a fixed 24 episodes when TMDB metadata was
temporarily unavailable. This module removes only that narrow failure shape,
plus slots that carry no payload at all and exact duplicates of a live
canonical episode. Discovery is read only. Applying a cleanup is playback
gated, serialized with other media maintenance, and moves files into
quarantine before deleting the corresponding virtual item row.

TMDB agreeing with itself is not enough evidence (2026-11-13: 17 of 59
quarantined entries were real). Every candidate above the TMDB boundary is
classified:

  phantom    no second-provider listing, no provider title or air date in its
             own sidecar, and either no payload or the legacy blind-24 shape;
  duplicate  its sidecar title equals a live canonical slot in the same show;
  review     anything else. Reported, never moved.

TVmaze (keyless) is the second provider. A season it can't confirm is skipped.
A run over the per-run or per-show cap applies nothing and writes a proposal.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests as req_lib

import db
import playback_guard
import strm_generator
import tmdb
from config import MEDIA_PATH, SPORE_MEDIA_PATH
from io_utils import atomic_write_text

log = logging.getLogger(__name__)
_SEASON_FOLDER_RE = re.compile(r"^Season (\d+)$", re.IGNORECASE)
_EPISODE_FILE_RE = re.compile(r"S(\d{1,3})E(\d{1,4})$", re.IGNORECASE)
_PLACEHOLDER_TITLE_RE = re.compile(r"^(?:episode|ep\.?)\s*\d+$", re.IGNORECASE)
_NFO_MAX_BYTES = 64 * 1024
LEGACY_FIXED_COUNT = 24
_AMBIGUOUS: dict = {}


def _env_cap(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return max(0, value)


MAX_QUARANTINE = _env_cap("EPISODE_HYGIENE_MAX_QUARANTINE", 10)
MAX_PER_SHOW = _env_cap("EPISODE_HYGIENE_MAX_PER_SHOW", 5)


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


class TvmazeClient:
    """Keyless TVmaze reader: regular episode numbers per season, cached per run.

    Any failure returns None so the caller skips the season (fail closed)."""

    BASE = "https://api.tvmaze.com"

    def __init__(self, session=None, min_interval: float = 1.0, timeout: float = 15.0):
        self._session = session or req_lib
        self._min_interval = min_interval
        self._timeout = timeout
        self._last = 0.0
        self._cache: dict[str, list | None] = {}
        self._lock = threading.Lock()

    def _get(self, path: str):
        for attempt in range(2):
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self._session.get(f"{self.BASE}{path}", timeout=self._timeout)
            except Exception:
                return None
            if resp.status_code == 429 and attempt == 0:
                retry = _int_or_none(resp.headers.get("Retry-After")) or 2
                time.sleep(min(max(retry, 1), 10))
                continue
            if resp.status_code != 200:
                return None
            try:
                return resp.json()
            except ValueError:
                return None
        return None

    def _episodes(self, imdb_id: str) -> list | None:
        with self._lock:
            if imdb_id in self._cache:
                return self._cache[imdb_id]
            episodes = None
            show = self._get(f"/lookup/shows?imdb={imdb_id}")
            show_id = _int_or_none((show or {}).get("id")) if isinstance(show, dict) else None
            if show_id is not None:
                payload = self._get(f"/shows/{show_id}/episodes?specials=1")
                if isinstance(payload, list):
                    episodes = payload
            self._cache[imdb_id] = episodes
            return episodes

    def regular_numbers(self, imdb_id: str, season_number: int) -> set[int] | None:
        episodes = self._episodes(imdb_id)
        if episodes is None:
            return None
        numbers = set()
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            if _int_or_none(episode.get("season")) != season_number:
                continue
            if (episode.get("type") or "regular") != "regular":
                continue
            number = episode.get("number")
            if isinstance(number, int) and number >= 1:
                numbers.add(number)
        return numbers


tvmaze = TvmazeClient()


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


def _second_provider(imdb_id: str, season_number: int, tvmaze_client) -> set[int] | None:
    """TVmaze regular numbers for the season, or None when it can't confirm it."""
    try:
        numbers = tvmaze_client.regular_numbers(imdb_id, season_number)
    except Exception:
        return None
    return numbers or None


def _sidecar(strm_path) -> dict:
    """Title and air date from the item's own .nfo, written from provider metadata."""
    if not strm_path:
        return {}
    nfo = Path(strm_path).with_suffix(".nfo")
    try:
        with nfo.open("rb") as handle:
            text = handle.read(_NFO_MAX_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return {}
    found = {}
    for key in ("title", "aired"):
        match = re.search(rf"<{key}>(.*?)</{key}>", text, re.S)
        value = html.unescape(match.group(1)).strip() if match else ""
        if value:
            found[key] = value
    return found


def _normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", html.unescape(value or "").casefold())


def _is_placeholder(title: str | None) -> bool:
    return not title or bool(_PLACEHOLDER_TITLE_RE.match(title.strip()))


def _no_payload(item: dict) -> bool:
    size = item.get("size_gb")
    try:
        return size is None or float(size) <= 0
    except (TypeError, ValueError):
        return True


def _legacy_shape(item: dict, season_items: list[dict], official_last: int) -> bool:
    """True only for the blind 1..24 registration: one pack hash across the season."""
    numbers = [_int_or_none(row.get("episode")) for row in season_items]
    if max((n for n in numbers if n is not None), default=0) != LEGACY_FIXED_COUNT:
        return False
    hashes = {str(row.get("info_hash") or "") for row in season_items}
    if len(hashes) != 1 or "" in hashes:
        return False
    sources = {row.get("source") for row in season_items}
    sizes = {row.get("size_gb") for row in season_items}
    if len(sources) != 1 or len(sizes) != 1:
        return False
    above = sorted(n for n in numbers if n is not None and n > official_last)
    return above == list(range(official_last + 1, LEGACY_FIXED_COUNT + 1))


def _canonical_slot(media_root: Path, spore_root: Path, row: dict) -> bool:
    """A canonical duplicate target must have its source and Spore stub on disk."""
    raw = row.get("strm_path")
    if not raw:
        return False
    strm = Path(raw)
    if not strm.is_file() or not _under(strm, media_root):
        return False
    return (spore_root / strm.relative_to(media_root)).with_suffix(".mkv").is_file()


def _classify(candidate: dict, item: dict | None, season_items: list[dict],
              official_last: int, provider_numbers: set[int],
              canonical: dict[str, dict], media_root: Path, spore_root: Path) -> tuple[str, str]:
    episode = candidate["episode"]
    if episode in provider_numbers:
        return "review", "second provider lists episode"
    sidecar = _sidecar(candidate.get("strm_path"))
    title = sidecar.get("title")
    if not _is_placeholder(title):
        target = canonical.get(_normalize_title(title))
        if target is None:
            return "review", "sidecar carries a provider title"
        if target is _AMBIGUOUS:
            return "review", "title matches more than one canonical slot"
        target_aired = target.get("_aired")
        if sidecar.get("aired") and target_aired and sidecar["aired"] != target_aired:
            return "review", "air date differs from canonical slot"
        if not _canonical_slot(media_root, spore_root, target):
            return "review", "canonical slot missing"
        candidate["canonical"] = {
            "token": target.get("token"),
            "season": _int_or_none(target.get("season")),
            "episode": _int_or_none(target.get("episode")),
            "strm_path": target.get("strm_path"),
        }
        return "duplicate", "duplicate of live canonical slot"
    if sidecar.get("aired"):
        return "review", "sidecar carries an air date"
    if item is None:
        return "phantom", "orphan file without a virtual item"
    if _no_payload(item):
        return "phantom", "no payload"
    if _legacy_shape(item, season_items, official_last):
        return "phantom", "legacy blind registration"
    return "review", "not legacy blind-registration shape"


def _canonical_titles(show_items: list[dict], boundaries: dict[int, int | None]) -> dict[str, dict]:
    """Normalized title to its one canonical row; a title on several slots is ambiguous."""
    titles: dict[str, dict] = {}
    for row in show_items:
        season = _int_or_none(row.get("season"))
        episode = _int_or_none(row.get("episode"))
        last = boundaries.get(season)
        if season is None or episode is None or last is None or episode > last:
            continue
        sidecar = _sidecar(row.get("strm_path"))
        title = sidecar.get("title")
        if _is_placeholder(title):
            continue
        key = _normalize_title(title)
        titles[key] = _AMBIGUOUS if key in titles else {**row, "_aired": sidecar.get("aired")}
    return titles


def discover(imdb_ids: set[str] | None = None, items: list[dict] | None = None,
             tmdb_client=None, orphan_show_ids: dict[str, str] | None = None,
             spore_root: str | Path | None = None,
             media_root: str | Path | None = None,
             tvmaze_client=None) -> dict:
    """Return classified never-played episodes beyond a double-confirmed boundary.

    A season is considered only when TMDB's declared count and detailed season
    payload agree on the final episode number and TVmaze returns that season.
    Empty, partial, or unusual metadata is skipped rather than treated as
    evidence for removal. Only phantom and duplicate rows are candidates.
    """
    tmdb_client = tmdb_client or tmdb
    tvmaze_client = tvmaze_client or tvmaze
    wanted_ids = {value for value in (imdb_ids or set()) if value}
    rows = items if items is not None else db.get_all_virtual_items()
    source_root = Path(media_root or MEDIA_PATH)
    root = Path(spore_root or SPORE_MEDIA_PATH)
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
    review: list[dict] = []
    skipped: list[dict] = []

    def admit(candidate, item, season_items, official_last, provider_numbers, canonical):
        kind, reason = _classify(candidate, item, season_items, official_last,
                                 provider_numbers, canonical, source_root, root)
        candidate["class"] = kind
        candidate["reason"] = reason
        (review if kind == "review" else candidates).append(candidate)

    for imdb_id, show_items in sorted(grouped.items()):
        tmdb_id = tmdb_client.find_by_imdb(imdb_id, kind="tv")
        show_info = tmdb_client.get_show_info(tmdb_id) if tmdb_id else None
        seasons = sorted({_int_or_none(item.get("season")) for item in show_items})
        boundaries = {
            season_number: _official_last(tmdb_id, show_info, season_number, tmdb_client)
            for season_number in seasons
        }
        canonical = None
        for season_number in seasons:
            season_items = [
                item for item in show_items
                if _int_or_none(item.get("season")) == season_number
            ]
            official_last = boundaries[season_number]
            if official_last is None:
                skipped.append({
                    "imdb_id": imdb_id,
                    "season": season_number,
                    "reason": "TMDB season boundary not authoritative",
                })
                continue
            above = [item for item in season_items
                     if _int_or_none(item.get("episode")) > official_last]
            if not above:
                continue
            provider_numbers = _second_provider(imdb_id, season_number, tvmaze_client)
            if provider_numbers is None:
                skipped.append({
                    "imdb_id": imdb_id,
                    "season": season_number,
                    "reason": "second provider could not confirm season",
                })
                continue
            if canonical is None:
                canonical = _canonical_titles(show_items, boundaries)
            for item in above:
                episode = _int_or_none(item.get("episode"))
                if item.get("last_played"):
                    skipped.append({
                        "imdb_id": imdb_id,
                        "season": season_number,
                        "episode": episode,
                        "reason": "episode has playback history",
                    })
                    continue
                admit({
                    "token": item.get("token"),
                    "imdb_id": imdb_id,
                    "title": item.get("title"),
                    "season": season_number,
                    "episode": episode,
                    "official_last": official_last,
                    "strm_path": item.get("strm_path"),
                    "row": dict(item),
                }, item, season_items, official_last, provider_numbers, canonical)

    known_tokens = {str(item.get("token") or "") for item in rows}
    seen_orphans: set[tuple[str, int, int]] = set()
    for show_folder, imdb_id in sorted((orphan_show_ids or {}).items()):
        if wanted_ids and imdb_id not in wanted_ids:
            continue
        tmdb_id = tmdb_client.find_by_imdb(imdb_id, kind="tv")
        show_info = tmdb_client.get_show_info(tmdb_id) if tmdb_id else None
        show_items = grouped.get(imdb_id, [])
        boundaries: dict[int, int | None] = {}
        canonical = None

        def season_gate(season_number: int):
            nonlocal canonical
            if season_number not in boundaries:
                boundaries[season_number] = _official_last(
                    tmdb_id, show_info, season_number, tmdb_client)
            official_last = boundaries[season_number]
            if official_last is None:
                skipped.append({
                    "imdb_id": imdb_id,
                    "season": season_number,
                    "reason": "TMDB season boundary not authoritative",
                })
                return None, None
            provider_numbers = _second_provider(imdb_id, season_number, tvmaze_client)
            if provider_numbers is None:
                skipped.append({
                    "imdb_id": imdb_id,
                    "season": season_number,
                    "reason": "second provider could not confirm season",
                })
                return None, None
            if canonical is None:
                for number in {_int_or_none(row.get("season")) for row in show_items}:
                    if number not in boundaries:
                        boundaries[number] = _official_last(
                            tmdb_id, show_info, number, tmdb_client)
                canonical = _canonical_titles(show_items, boundaries)
            return official_last, provider_numbers

        # A legacy source file can outlive its virtual row and Spore stub. Scan
        # the canonical .strm tree too, using the same double-confirmed TMDB
        # boundary. When a matching orphan stub exists, _files_for() will move
        # both exact files from this one candidate.
        source_folder = source_root / "series" / show_folder
        if source_folder.is_dir() and _under(source_folder, source_root):
            for season_folder in sorted(path for path in source_folder.iterdir() if path.is_dir()):
                season_match = _SEASON_FOLDER_RE.match(season_folder.name)
                if not season_match:
                    continue
                season_number = int(season_match.group(1))
                official_last, provider_numbers = season_gate(season_number)
                if official_last is None:
                    continue
                for strm in sorted(season_folder.glob("*.strm")):
                    episode_match = _EPISODE_FILE_RE.search(strm.stem)
                    if not episode_match:
                        continue
                    parsed_season = int(episode_match.group(1))
                    episode = int(episode_match.group(2))
                    if parsed_season != season_number or episode <= official_last:
                        continue
                    token = ""
                    try:
                        match = re.search(r"/stream/([A-Za-z0-9]+)", strm.read_text(encoding="utf-8"))
                        token = match.group(1) if match else ""
                    except Exception:
                        pass
                    if token and token in known_tokens:
                        skipped.append({
                            "imdb_id": imdb_id,
                            "season": season_number,
                            "episode": episode,
                            "reason": "source token still has a virtual item",
                        })
                        continue
                    key = (show_folder, season_number, episode)
                    seen_orphans.add(key)
                    admit({
                        "token": None,
                        "imdb_id": imdb_id,
                        "title": show_folder,
                        "season": season_number,
                        "episode": episode,
                        "official_last": official_last,
                        "strm_path": str(strm),
                        "orphan_source": True,
                    }, None, [], official_last, provider_numbers, canonical)

        folder = root / "series" / show_folder
        if not folder.is_dir() or not _under(folder, root):
            continue
        for season_folder in sorted(path for path in folder.iterdir() if path.is_dir()):
            season_match = _SEASON_FOLDER_RE.match(season_folder.name)
            if not season_match:
                continue
            season_number = int(season_match.group(1))
            official_last, provider_numbers = season_gate(season_number)
            if official_last is None:
                continue
            for stub in sorted(season_folder.glob("*.mkv")):
                episode_match = _EPISODE_FILE_RE.search(stub.stem)
                if not episode_match:
                    continue
                parsed_season = int(episode_match.group(1))
                episode = int(episode_match.group(2))
                if parsed_season != season_number or episode <= official_last:
                    continue
                if (show_folder, season_number, episode) in seen_orphans:
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
                admit({
                    "token": None,
                    "imdb_id": imdb_id,
                    "title": show_folder,
                    "season": season_number,
                    "episode": episode,
                    "official_last": official_last,
                    "strm_path": str(Path(MEDIA_PATH) / relative),
                    "orphan_stub": True,
                }, None, [], official_last, provider_numbers, canonical)

    def order(row):
        return (str(row.get("title") or ""), row["season"], row["episode"])

    candidates.sort(key=order)
    review.sort(key=order)
    return {"candidates": candidates, "review": review, "skipped": skipped}


def _over_cap(candidates: list[dict], max_total: int, max_per_show: int) -> str | None:
    if len(candidates) > max_total:
        return f"{len(candidates)} candidates exceed the per-run cap of {max_total}"
    per_show: dict[str, int] = defaultdict(int)
    for candidate in candidates:
        per_show[candidate.get("imdb_id") or ""] += 1
    worst = max(per_show.items(), key=lambda pair: pair[1], default=("", 0))
    if worst[1] > max_per_show:
        return f"{worst[1]} candidates for {worst[0]} exceed the per-show cap of {max_per_show}"
    return None


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

    media_root = Path(MEDIA_PATH)
    spore_root = Path(SPORE_MEDIA_PATH)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine = Path(quarantine_root or media_root.parent / "quarantine" / "phantom-episodes") / stamp

    over = _over_cap(report["candidates"], MAX_QUARANTINE, MAX_PER_SHOW)
    if over:
        # Nothing moves and no scan is queued. A person reviews the proposal.
        atomic_write_text(quarantine / "proposal.json", json.dumps({
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reason": over,
            **report,
        }, indent=2, default=str))
        log.warning("Phantom cleanup over cap, proposal only: %s", over)
        return {"status": "over_cap", "reason": over,
                "proposal": str(quarantine / "proposal.json"), **report}

    if playback_guard.active(force=True):
        return {"status": "deferred_playback", **report}
    if not strm_generator._maintenance_lock.acquire(blocking=False):
        return {"status": "deferred_maintenance", **report}

    quarantined: list[dict] = []
    failed: list[dict] = []
    review = list(report.get("review") or [])
    try:
        for candidate in report["candidates"]:
            target = candidate.get("canonical")
            if (candidate.get("class") == "duplicate"
                    and (not target or not _canonical_slot(media_root, spore_root, target))):
                review.append({**candidate, "class": "review", "reason": "canonical slot missing"})
                continue
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
                "review": review,
            }
            atomic_write_text(quarantine / "manifest.json",
                              json.dumps(manifest, indent=2, default=str))
    finally:
        strm_generator._maintenance_lock.release()

    return {
        "status": "complete",
        "quarantine": str(quarantine) if quarantined else None,
        "quarantined": quarantined,
        "failed": failed,
        "review": review,
        "skipped": report["skipped"],
    }
