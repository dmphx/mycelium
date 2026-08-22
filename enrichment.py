"""Progression-aware native Plex analysis for Spore virtual media.

Plex cannot generate intro/credit markers or preview images from Mycelium's
tiny stub MKVs. This worker downloads a bounded season batch to the dedicated
Spore disk, swaps the corresponding stubs for local-file symlinks only while
Plex is idle, runs targeted native analysis, and restores the exact stubs.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

import catbox
import config
import db
import playback_guard
import strm_generator

log = logging.getLogger(__name__)

_run_lock = threading.Lock()
_EPISODE_SUFFIX = re.compile(r"\s+S\d{1,2}E\d{1,3}.*$", re.IGNORECASE)
_ANALYSIS_SETTINGS = (
    "GenerateBIFBehavior",
    "GenerateIntroMarkerBehavior",
    "GenerateCreditsMarkerBehavior",
    "GenerateChapterThumbBehavior",
)


class EnrichmentDeferred(RuntimeError):
    pass


def enabled() -> bool:
    return bool(config.ENRICHMENT_ENABLED and config.SPORE_ENABLED)


def queue_from_playback(token: str) -> int:
    """Queue the played season and four episodes from the next season."""
    if not enabled():
        return 0
    items = db.get_series_enrichment_items(
        token,
        season_cap=config.ENRICHMENT_SEASON_CAP,
        next_count=config.ENRICHMENT_NEXT_SEASON_EPISODES,
    )
    changed = db.queue_media_enrichment(items, reason="playback")
    if changed:
        played = db.get_virtual_item(token) or {}
        log.info(
            "Enrichment queued from playback: %s season=%s episode=%s items=%d cap=%d next=%d",
            played.get("title", token), played.get("season"), played.get("episode"),
            len(items), config.ENRICHMENT_SEASON_CAP,
            config.ENRICHMENT_NEXT_SEASON_EPISODES,
        )
    return changed


def seed(token: str, reason: str = "manual") -> int:
    """Explicitly seed progression enrichment for a known token."""
    if not enabled():
        raise RuntimeError("media enrichment is disabled")
    items = db.get_series_enrichment_items(
        token,
        season_cap=config.ENRICHMENT_SEASON_CAP,
        next_count=config.ENRICHMENT_NEXT_SEASON_EPISODES,
    )
    return db.queue_media_enrichment(items, reason=reason)


def _cache_dir() -> Path:
    path = Path(config.ENRICHMENT_CACHE_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(token: str) -> Path:
    return _cache_dir() / f"{token}.media"


def _stub_path(item: dict) -> Path:
    strm_path = Path(item["strm_path"])
    return strm_generator._spore_stub_dir(strm_path) / (strm_path.stem + ".mkv")


def _backup_path(stub: Path) -> Path:
    return stub.with_name("." + stub.name + ".enrichment-stub")


def recover_overlays() -> int:
    """Restore stubs left swapped by a killed worker."""
    root = Path(config.SPORE_MEDIA_PATH)
    restored = 0
    if not root.exists():
        return 0
    for backup in root.rglob(".*.mkv.enrichment-stub"):
        original_name = backup.name[1:-len(".enrichment-stub")]
        stub = backup.with_name(original_name)
        try:
            if stub.is_symlink() or stub.exists():
                stub.unlink()
            os.replace(backup, stub)
            restored += 1
        except Exception as exc:
            log.error("Enrichment could not restore %s: %s", stub, exc)
    if restored:
        log.warning("Enrichment restored %d interrupted stub overlay(s)", restored)
    return restored


def _local_media_valid(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 1024 * 1024:
        return False
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, timeout=60,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


def _download(item: dict, batch_bytes: int) -> int:
    token = item["token"]
    final = _cache_path(token)
    if _local_media_valid(final):
        db.set_enrichment_state([token], "staged")
        return final.stat().st_size

    if playback_guard.active(force=True):
        raise EnrichmentDeferred("playback became active before download")

    db.set_enrichment_state([token], "downloading")
    url = catbox.materialize(token, allow_readd=True, record_playback=False)
    if not url:
        raise RuntimeError(f"materialize failed for {item['title']}")

    part = final.with_suffix(".part")
    try:
        with requests.get(url, stream=True, timeout=(15, 120)) as response:
            if response.status_code >= 400:
                raise RuntimeError(
                    f"download HTTP {response.status_code} for {item['title']}"
                )
            expected = int(response.headers.get("Content-Length") or 0)
            max_bytes = int(config.ENRICHMENT_MAX_BATCH_GB) * 1024 ** 3
            if expected and batch_bytes + expected > max_bytes:
                raise RuntimeError("enrichment batch exceeds configured byte cap")
            written = 0
            check_at = 64 * 1024 * 1024
            with part.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    if written >= check_at:
                        check_at += 64 * 1024 * 1024
                        if playback_guard.active(force=True):
                            raise EnrichmentDeferred("playback started during download")
                handle.flush()
                os.fsync(handle.fileno())
            if expected and written != expected:
                raise RuntimeError(
                    f"download size mismatch for {item['title']}: {written}/{expected}"
                )
        os.chmod(part, 0o644)
        os.replace(part, final)
        if not _local_media_valid(final):
            raise RuntimeError(f"ffprobe rejected downloaded media for {item['title']}")
        db.set_enrichment_state([token], "staged")
        log.info("Enrichment staged %s (%d bytes)", item["title"], final.stat().st_size)
        return final.stat().st_size
    except Exception:
        part.unlink(missing_ok=True)
        raise


def _overlay(item: dict) -> tuple[Path, Path]:
    stub = _stub_path(item)
    backup = _backup_path(stub)
    media = _cache_path(item["token"])
    if not _local_media_valid(media):
        raise RuntimeError(f"staged media missing for {item['title']}")
    if backup.exists():
        if stub.is_symlink() or stub.exists():
            stub.unlink()
        os.replace(backup, stub)
    if not stub.is_file() or stub.stat().st_size > 1024 * 1024:
        raise RuntimeError(f"refusing to replace non-stub path {stub}")
    os.replace(stub, backup)
    try:
        os.symlink(str(media), stub)
    except Exception:
        os.replace(backup, stub)
        raise
    return stub, backup


def _restore_overlay(stub: Path, backup: Path) -> None:
    if not backup.exists():
        return
    if stub.is_symlink() or stub.exists():
        stub.unlink()
    os.replace(backup, stub)


def _plex_config() -> tuple[str, str]:
    url = os.environ.get("PLEX_URL", "").rstrip("/")
    token = os.environ.get("PLEX_TOKEN", "")
    if not (url and token):
        raise RuntimeError("PLEX_URL and PLEX_TOKEN are required for enrichment")
    return url, token


def _plex_request(method: str, path: str, **kwargs) -> requests.Response:
    url, token = _plex_config()
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["X-Plex-Token"] = token
    response = requests.request(method, url + path, headers=headers, timeout=30, **kwargs)
    return response


def _set_analysis(mode: str) -> None:
    for key in _ANALYSIS_SETTINGS:
        response = _plex_request("PUT", "/:/prefs", params={key: mode})
        if response.status_code >= 400:
            raise RuntimeError(f"Plex preference {key} failed: HTTP {response.status_code}")
    for key in ("ButlerTaskDeepMediaAnalysis", "ButlerTaskUpgradeMediaAnalysis"):
        _plex_request("PUT", "/:/prefs", params={key: "0"})


def _show_title(item: dict) -> str:
    return _EPISODE_SUFFIX.sub("", item.get("title") or "").strip()


def _plex_episode_map(item: dict) -> tuple[dict[tuple[int, int], str], set[str]]:
    title = _show_title(item)
    response = _plex_request("GET", "/search", params={"query": title})
    response.raise_for_status()
    root = ET.fromstring(response.content)
    show_key = None
    for node in root:
        if node.get("type") == "show" and (node.get("title") or "").casefold() == title.casefold():
            show_key = node.get("ratingKey")
            break
    if not show_key:
        raise RuntimeError(f"Plex show not found: {title}")
    leaves = _plex_request("GET", f"/library/metadata/{show_key}/allLeaves")
    leaves.raise_for_status()
    mapping: dict[tuple[int, int], str] = {}
    season_keys: set[str] = set()
    for node in ET.fromstring(leaves.content):
        try:
            key = (int(node.get("parentIndex") or 0), int(node.get("index") or 0))
        except ValueError:
            continue
        if key[0] and key[1] and node.get("ratingKey"):
            mapping[key] = node.get("ratingKey")
        if node.get("parentRatingKey"):
            season_keys.add(node.get("parentRatingKey"))
    return mapping, season_keys


def _analysis_active() -> bool:
    response = _plex_request("GET", "/activities")
    if response.status_code != 200:
        return False
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        return False
    return any(
        "media.generate" in (node.get("type") or "")
        or "media.analyze" in (node.get("type") or "")
        for node in root
    )


def _wait_for_analysis() -> None:
    deadline = time.monotonic() + config.ENRICHMENT_ANALYZE_TIMEOUT_SECONDS
    idle_polls = 0
    time.sleep(3)
    while time.monotonic() < deadline:
        if _analysis_active():
            idle_polls = 0
        else:
            idle_polls += 1
            if idle_polls >= 3:
                return
        time.sleep(5)
    raise RuntimeError("Plex analysis timeout")


def _analyze_batch(batch: list[dict]) -> None:
    mapping, _ = _plex_episode_map(batch[0]) if batch[0]["media_type"] == "series" else ({}, set())
    season_keys: set[str] = set()
    for item in batch:
        if playback_guard.active(force=True):
            raise EnrichmentDeferred("playback started during analysis batch")
        if item["media_type"] == "series":
            pair = (int(item["season"]), int(item["episode"]))
            rating_key = mapping.get(pair)
            if not rating_key:
                raise RuntimeError(f"Plex episode not found for {item['title']}")
            metadata = _plex_request("GET", f"/library/metadata/{rating_key}")
            if metadata.status_code == 200:
                root = ET.fromstring(metadata.content)
                for node in root:
                    if node.get("parentRatingKey"):
                        season_keys.add(node.get("parentRatingKey"))
        else:
            raise RuntimeError("movie enrichment lookup is not implemented yet")
        response = _plex_request("PUT", f"/library/metadata/{rating_key}/analyze")
        if response.status_code != 200:
            raise RuntimeError(
                f"Plex analyze failed for {item['title']}: HTTP {response.status_code}"
            )
        _wait_for_analysis()
        log.info("Enrichment analyzed %s", item["title"])

    # A final season-level pass lets Plex compare episode audio for intro markers.
    for season_key in sorted(season_keys):
        if playback_guard.active(force=True):
            raise EnrichmentDeferred("playback started before season marker pass")
        response = _plex_request("PUT", f"/library/metadata/{season_key}/analyze")
        if response.status_code != 200:
            raise RuntimeError(f"Plex season analyze failed: HTTP {response.status_code}")
        _wait_for_analysis()


def run_once() -> dict:
    """Run at most one queued season batch."""
    if not enabled():
        return {"status": "disabled"}
    if not _run_lock.acquire(blocking=False):
        return {"status": "already-running"}
    batch: list[dict] = []
    overlays: list[tuple[Path, Path]] = []
    tokens: list[str] = []
    try:
        recover_overlays()
        if playback_guard.defer("media_enrichment"):
            return {"status": "deferred-playback"}
        batch = db.get_enrichment_batch()
        if not batch:
            return {"status": "idle"}
        tokens = [item["token"] for item in batch]
        estimated_gb = sum(float(item.get("size_gb") or 0) for item in batch)
        if estimated_gb > config.ENRICHMENT_MAX_BATCH_GB:
            raise RuntimeError(
                f"estimated batch size {estimated_gb:.1f} GiB exceeds cap"
            )
        batch_bytes = 0
        for item in batch:
            batch_bytes += _download(item, batch_bytes)
        if playback_guard.active(force=True):
            raise EnrichmentDeferred("playback active after staging")

        for item in batch:
            overlays.append(_overlay(item))
        db.set_enrichment_state(tokens, "analyzing")
        try:
            _set_analysis("scheduled")
            _analyze_batch(batch)
        finally:
            try:
                _set_analysis("never")
            except Exception as exc:
                log.error("Enrichment could not restore Plex analysis preferences: %s", exc)
        for stub, backup in reversed(overlays):
            _restore_overlay(stub, backup)
        overlays.clear()
        db.set_enrichment_state(tokens, "complete")
        for item in batch:
            _cache_path(item["token"]).unlink(missing_ok=True)
        log.info(
            "Enrichment completed batch: title=%s season=%s items=%d bytes=%d",
            _show_title(batch[0]), batch[0].get("season"), len(batch), batch_bytes,
        )
        return {"status": "complete", "items": len(batch), "bytes": batch_bytes}
    except EnrichmentDeferred as exc:
        if tokens:
            db.set_enrichment_state(tokens, "staged", str(exc))
        log.info("Enrichment deferred: %s", exc)
        return {"status": "deferred-playback", "items": len(batch)}
    except Exception as exc:
        if tokens:
            db.set_enrichment_state(tokens, "failed", str(exc))
        log.exception("Enrichment batch failed: %s", exc)
        return {"status": "failed", "error": str(exc)[:200]}
    finally:
        for stub, backup in reversed(overlays):
            try:
                _restore_overlay(stub, backup)
            except Exception as exc:
                log.critical("Enrichment stub restore failed for %s: %s", stub, exc)
        _run_lock.release()
