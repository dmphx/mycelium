"""Repair legacy series folders whose visible title is a TMDB placeholder.

Old request paths could create folders such as ``tmdb1434`` before resolving a
real show title. New additions are resolved before file creation, but existing
folders need a conservative migration. This module requires a confirmed IMDb
identity, checks every episode and Spore token for conflicts, and moves the old
folder to quarantine after copying data into the canonical folder.
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as _xml_escape

import backup
import db
import playback_guard
import strm_generator
from config import MEDIA_PATH, SPORE_MEDIA_PATH
from io_utils import atomic_write_text


_PLACEHOLDER_RE = re.compile(r"^tmdb[:_ -]?\d+$", re.IGNORECASE)
_EPISODE_RE = re.compile(r"S(\d{1,2})E(\d{1,3})", re.IGNORECASE)


def _nfo_imdb(folder: Path) -> str | None:
    nfo = folder / "tvshow.nfo"
    if not nfo.is_file():
        return None
    try:
        root = ET.parse(nfo).getroot()
        for node in root.findall("uniqueid"):
            if node.get("type") == "imdb" and node.text:
                return node.text.strip()
    except Exception:
        return None
    return None


def _minfo_token(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("token="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def _episode_destination(path: Path, target: Path, title: str, suffix: str) -> Path | None:
    match = _EPISODE_RE.search(path.name)
    if not match:
        return None
    season = int(match.group(1))
    episode = int(match.group(2))
    season_dir = path.parent.name if path.parent.name.lower().startswith("season ") else f"Season {season:02d}"
    return target / season_dir / f"{title} S{season:02d}E{episode:02d}{suffix}"


def _target_identity_conflict(target: Path, imdb_id: str) -> bool:
    if not target.exists():
        return False
    if target.is_symlink() or not target.is_dir():
        return True
    target_imdb = _nfo_imdb(target)
    return bool(target_imdb and target_imdb != imdb_id)


def discover() -> list[dict]:
    """Return confirmed placeholder folders or titles and any conflicts."""
    series_root = Path(MEDIA_PATH) / "series"
    spore_root = Path(SPORE_MEDIA_PATH) / "series"
    if not series_root.is_dir():
        return []

    plans = []
    planned_imdb_ids: set[str] = set()
    for source in sorted(series_root.iterdir()):
        if not source.is_dir() or source.is_symlink() or not _PLACEHOLDER_RE.match(source.name):
            continue

        nfo_imdb = _nfo_imdb(source)
        virtual_ids = db.get_virtual_item_imdb_ids_under_path(str(source))
        conflict_reasons = []
        if len(virtual_ids) > 1:
            conflict_reasons.append("multiple virtual IMDb identities")
        virtual_imdb = next(iter(virtual_ids)) if len(virtual_ids) == 1 else None
        if nfo_imdb and virtual_imdb and nfo_imdb != virtual_imdb:
            conflict_reasons.append("NFO and virtual IMDb identities disagree")
        imdb_id = virtual_imdb or nfo_imdb
        if not imdb_id:
            conflict_reasons.append("IMDb identity unavailable")

        canonical = strm_generator._canonical_series_folder(imdb_id) if imdb_id else ""
        if not canonical or _PLACEHOLDER_RE.match(canonical):
            conflict_reasons.append("canonical title unavailable")
        canonical = strm_generator._safe(canonical) if canonical else ""
        target = series_root / canonical if canonical else series_root / source.name
        if canonical and _target_identity_conflict(target, imdb_id):
            conflict_reasons.append("canonical folder has a different identity")

        source_files = []
        if canonical:
            for path in sorted(source.rglob("*.strm")):
                dest = _episode_destination(path, target, canonical, ".strm")
                if dest is None:
                    conflict_reasons.append(f"unparseable episode {path.name}")
                    continue
                if dest.exists() and dest.read_bytes() != path.read_bytes():
                    conflict_reasons.append(f"episode conflict {dest.parent.name}/{dest.name}")
                source_files.append((path, dest))

        stub_source = spore_root / source.name
        stub_target = spore_root / canonical if canonical else stub_source
        stub_files = []
        if canonical and stub_source.is_dir() and not stub_source.is_symlink():
            for minfo in sorted(stub_source.rglob("*.minfo")):
                dest_minfo = _episode_destination(minfo, stub_target, canonical, ".minfo")
                dest_mkv = _episode_destination(minfo, stub_target, canonical, ".mkv")
                source_mkv = minfo.with_suffix(".mkv")
                if dest_minfo is None or dest_mkv is None or not source_mkv.is_file():
                    conflict_reasons.append(f"incomplete Spore pair {minfo.name}")
                    continue
                if dest_minfo.exists() and _minfo_token(dest_minfo) != _minfo_token(minfo):
                    conflict_reasons.append(f"Spore token conflict {dest_minfo.parent.name}/{dest_minfo.name}")
                stub_files.append((minfo, source_mkv, dest_minfo, dest_mkv))

        plans.append({
            "source": source,
            "target": target,
            "stub_source": stub_source,
            "stub_target": stub_target,
            "imdb_id": imdb_id,
            "title": canonical,
            "source_files": source_files,
            "stub_files": stub_files,
            "conflicts": sorted(set(conflict_reasons)),
            "title_only": False,
        })
        if imdb_id:
            planned_imdb_ids.add(imdb_id)

    # Some older rows were given a canonical folder later while their database
    # title remained ``tmdb:NNN``. Those rows do not need a path migration, but
    # leaving the placeholder poisons future wanted-episode search queries and
    # can leak into media-server episode metadata.
    for series in db.get_all_monitored_series():
        current_title = str(series.get("title") or "").strip()
        imdb_id = str(series.get("imdb_id") or "").strip()
        if not _PLACEHOLDER_RE.match(current_title) or imdb_id in planned_imdb_ids:
            continue

        conflict_reasons = []
        if not re.fullmatch(r"tt\d+", imdb_id, re.IGNORECASE):
            conflict_reasons.append("IMDb identity unavailable")
        canonical = strm_generator._canonical_series_folder(imdb_id) if not conflict_reasons else ""
        if not canonical or _PLACEHOLDER_RE.match(canonical):
            conflict_reasons.append("canonical title unavailable")
        canonical = strm_generator._safe(canonical) if canonical else ""
        target = series_root / canonical if canonical else series_root / current_title
        if canonical and (not target.is_dir() or target.is_symlink()):
            conflict_reasons.append("canonical folder unavailable")
        elif canonical:
            target_imdb = _nfo_imdb(target)
            virtual_ids = db.get_virtual_item_imdb_ids_under_path(str(target))
            if target_imdb and target_imdb != imdb_id:
                conflict_reasons.append("canonical folder has a different identity")
            if virtual_ids and virtual_ids != {imdb_id}:
                conflict_reasons.append("canonical folder has mixed virtual identities")

        plans.append({
            "source": target,
            "target": target,
            "stub_source": spore_root / canonical if canonical else spore_root / current_title,
            "stub_target": spore_root / canonical if canonical else spore_root / current_title,
            "imdb_id": imdb_id or None,
            "title": canonical,
            "source_files": [],
            "stub_files": [],
            "conflicts": sorted(set(conflict_reasons)),
            "title_only": True,
        })
    return plans


def _update_identity_titles(imdb_id: str, title: str) -> None:
    tables = ("monitored_series", "requests", "wanted_episodes", "virtual_items",
              "watchlist", "user_requests")
    with db._connect() as conn:
        existing = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table in tables:
            if table in existing:
                conn.execute(f"UPDATE {table} SET title=? WHERE imdb_id=?", (title, imdb_id))
        conn.commit()


def cleanup(dry_run: bool = True) -> dict:
    """Apply conflict-free placeholder migrations, preserving originals."""
    plans = discover()
    result = {
        "found": len(plans),
        "eligible": sum(not plan["conflicts"] for plan in plans),
        "migrated": 0,
        "titles_repaired": 0,
        "held": sum(bool(plan["conflicts"]) for plan in plans),
        "details": [
            {
                "source": plan["source"].name,
                "target": plan["target"].name,
                "imdb_id": plan["imdb_id"],
                "title_only": plan["title_only"],
                "conflicts": plan["conflicts"],
            }
            for plan in plans
        ],
    }
    if dry_run or not plans:
        return result
    if playback_guard.active(force=True):
        return {**result, "blocked": "playback active"}
    if not strm_generator._maintenance_lock.acquire(blocking=False):
        return {**result, "blocked": "maintenance active"}

    try:
        snapshot = backup.run()
        if snapshot is None:
            return {**result, "blocked": "database backup failed"}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        quarantine = Path(MEDIA_PATH).parent / "quarantine" / "placeholder-series" / stamp

        for plan in plans:
            if plan["conflicts"]:
                continue
            source = plan["source"]
            target = plan["target"]
            target.mkdir(parents=True, exist_ok=True)

            if plan["title_only"]:
                atomic_write_text(
                    target / "tvshow.nfo",
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                    "<tvshow>\n"
                    f"  <title>{_xml_escape(plan['title'])}</title>\n"
                    f'  <uniqueid type="imdb" default="true">{_xml_escape(plan["imdb_id"])}</uniqueid>\n'
                    "</tvshow>\n",
                )
                _update_identity_titles(plan["imdb_id"], plan["title"])
                result["titles_repaired"] += 1
                continue

            for old_path, new_path in plan["source_files"]:
                new_path.parent.mkdir(parents=True, exist_ok=True)
                if not new_path.exists():
                    shutil.copy2(old_path, new_path)
                updated = db.update_virtual_item_strm_path(str(old_path), str(new_path))
                if updated > 1:
                    raise RuntimeError(f"multiple DB rows for {old_path}")
                old_nfo = old_path.with_suffix(".nfo")
                new_nfo = new_path.with_suffix(".nfo")
                if old_nfo.is_file() and not new_nfo.exists():
                    shutil.copy2(old_nfo, new_nfo)

            for old_minfo, old_mkv, new_minfo, new_mkv in plan["stub_files"]:
                new_minfo.parent.mkdir(parents=True, exist_ok=True)
                if not new_minfo.exists():
                    shutil.copy2(old_minfo, new_minfo)
                if not new_mkv.exists():
                    shutil.copy2(old_mkv, new_mkv)

            for name in ("poster.jpg", "fanart.jpg"):
                old_asset = source / name
                new_asset = target / name
                if old_asset.is_file() and not new_asset.exists():
                    shutil.copy2(old_asset, new_asset)
            atomic_write_text(
                target / "tvshow.nfo",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                "<tvshow>\n"
                f"  <title>{_xml_escape(plan['title'])}</title>\n"
                f'  <uniqueid type="imdb" default="true">{_xml_escape(plan["imdb_id"])}</uniqueid>\n'
                "</tvshow>\n",
            )
            _update_identity_titles(plan["imdb_id"], plan["title"])

            media_quarantine = quarantine / source.name / "media"
            media_quarantine.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(media_quarantine))
            if plan["stub_source"].is_dir():
                spore_quarantine = quarantine / source.name / "spore"
                spore_quarantine.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(plan["stub_source"]), str(spore_quarantine))
            result["migrated"] += 1
        result["backup"] = str(snapshot)
        result["quarantine"] = str(quarantine)
        return result
    finally:
        strm_generator._maintenance_lock.release()
