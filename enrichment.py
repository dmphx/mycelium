"""Progression-aware native Plex analysis for Spore virtual media.

Only authoritative Plex sessions enqueue work. The worker claims a bounded,
leased batch, stages real media while Plex is idle, analyzes exact stable media
identities, restores every stub, and records positive completion evidence.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import uuid
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
_health_lock = threading.Lock()
_PROCESS_LOCK_PATH = "/data/media-enrichment.lock"
_EPISODE_SUFFIX = re.compile(r"\s+S\d{1,2}E\d{1,3}.*$", re.IGNORECASE)
_TRAILING_YEAR = re.compile(r"\s*\((19\d{2}|20\d{2}|21\d{2})\)\s*$")
_ANALYSIS_SETTINGS = (
    "GenerateBIFBehavior",
    "GenerateIntroMarkerBehavior",
    "GenerateCreditsMarkerBehavior",
    "GenerateChapterThumbBehavior",
)
_BUTLER_SETTINGS = (
    "ButlerTaskDeepMediaAnalysis",
    "ButlerTaskUpgradeMediaAnalysis",
)
_PLAY_STATES = {"playing", "paused", "buffering"}
_health = {
    "preferences_safe": None,
    "last_session_poll_ok": None,
    "last_run_status": "not-started",
}
_runtime_ready = False


class EnrichmentDeferred(RuntimeError):
    pass


class EnrichmentBatchFull(EnrichmentDeferred):
    pass


class EnrichmentItemTooLarge(RuntimeError):
    pass


def _set_health(**values) -> None:
    with _health_lock:
        _health.update(values)


def health_snapshot() -> dict:
    with _health_lock:
        result = dict(_health)
    cache = Path(config.ENRICHMENT_CACHE_DIR)
    try:
        staged = list(cache.glob("*.media")) if cache.is_dir() else []
        result["staged_files"] = len(staged)
        result["staged_bytes"] = sum(path.stat().st_size for path in staged)
    except OSError:
        result["staged_files"] = -1
        result["staged_bytes"] = -1
    try:
        result["overlay_backups"] = sum(
            1 for item in db.get_enrichment_recovery_items()
            if _backup_path(_stub_path(item)).exists()
        )
    except Exception:
        result["overlay_backups"] = -1
    return result


def enabled() -> bool:
    return bool(config.ENRICHMENT_ENABLED and config.SPORE_ENABLED)


def _prepare_safe_runtime() -> int:
    """Force safe preferences and recover local state, collecting both errors."""
    global _runtime_ready
    errors: list[str] = []
    try:
        _set_analysis("never")
    except Exception as exc:
        errors.append(str(exc))
    try:
        recover_overlays()
    except Exception as exc:
        errors.append(str(exc))
    resumed = 0
    try:
        resumed = db.reset_interrupted_enrichment(
            max_attempts=config.ENRICHMENT_MAX_ATTEMPTS,
            retry_base_seconds=config.ENRICHMENT_RETRY_BASE_SECONDS,
            release_all=True,
        )
    except Exception as exc:
        errors.append(str(exc))
    try:
        _cleanup_dead_letter_staged_media()
    except Exception as exc:
        errors.append(str(exc))
    if errors:
        _runtime_ready = False
        raise RuntimeError("; ".join(errors))
    return resumed


def initialize() -> bool:
    """Recover interrupted state and force Plex preferences to their safe value."""
    global _runtime_ready
    if not enabled():
        _runtime_ready = False
        return False
    process_lock = None
    try:
        process_lock = open(_PROCESS_LOCK_PATH, "a+", encoding="utf-8")
        try:
            fcntl.flock(
                process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except BlockingIOError:
            _runtime_ready = False
            _set_health(last_run_status="busy")
            log.warning("Enrichment safety initialization deferred to active worker")
            return False
        resumed = _prepare_safe_runtime()
        if resumed:
            log.warning("Enrichment released %d expired claim(s)", resumed)
        _runtime_ready = True
        _set_health(preferences_safe=True, last_run_status="ready")
        return True
    except Exception as exc:
        _runtime_ready = False
        _set_health(preferences_safe=False, last_run_status="unsafe")
        log.critical("Enrichment startup safety check failed: %s", exc)
        return False
    finally:
        if process_lock is not None:
            try:
                fcntl.flock(process_lock.fileno(), fcntl.LOCK_UN)
            finally:
                process_lock.close()


def queue_from_playback(token: str, reason: str = "plex-session") -> int:
    """Queue the bounded progression window for a confirmed Plex session."""
    if not enabled():
        return 0
    items = db.get_series_enrichment_items(
        token,
        season_cap=config.ENRICHMENT_SEASON_CAP,
        next_count=config.ENRICHMENT_NEXT_SEASON_EPISODES,
    )
    changed = db.queue_media_enrichment(items, reason=reason)
    if changed:
        played = db.get_virtual_item(token) or {}
        log.info(
            "Enrichment queued from Plex session: %s season=%s episode=%s "
            "items=%d cap=%d next=%d",
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


def _session_identity(node: ET.Element) -> tuple[str, str, str, str] | None:
    session = next((child for child in node if child.tag == "Session"), None)
    player = next((child for child in node if child.tag == "Player"), None)
    if session is None or player is None:
        return None
    session_id = session.get("id") or node.get("sessionKey") or ""
    rating_key = node.get("ratingKey") or ""
    player_id = player.get("machineIdentifier") or player.get("address") or ""
    state = (player.get("state") or "").lower()
    if not session_id or not rating_key or state not in _PLAY_STATES:
        return None
    event_id = hashlib.sha256(
        f"{session_id}\0{rating_key}".encode("utf-8")
    ).hexdigest()
    return event_id, session_id, rating_key, player_id


def _session_part_path(node: ET.Element) -> str | None:
    paths = [part.get("file") for part in node.iter("Part") if part.get("file")]
    matching = [
        path for path in paths
        if "/series/" in path.replace("\\", "/")
        or "/movies/" in path.replace("\\", "/")
    ]
    return matching[0] if len(matching) == 1 else None


def poll_plex_sessions() -> dict:
    """Queue each real Plex session start once and refresh the playback gate."""
    if not enabled():
        return {"status": "disabled", "sessions": 0, "queued": 0, "errors": 0}
    try:
        response = _plex_request("GET", "/status/sessions")
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except Exception as exc:
        _set_health(last_session_poll_ok=False)
        log.warning("Enrichment Plex session poll failed: %s", exc)
        return {"status": "failed", "sessions": 0, "queued": 0, "errors": 1}

    sessions = 0
    queued = 0
    errors = 0
    for node in root:
        if node.tag not in ("Video", "Track"):
            continue
        identity = _session_identity(node)
        if identity is None:
            continue
        sessions += 1
        try:
            event_id, session_id, rating_key, player_id = identity
            part_path = _session_part_path(node)
            item = db.find_virtual_item_by_plex_path(part_path) if part_path else None
            token = item.get("token") if item else None
            needs_queue = db.record_plex_playback_event(
                event_id, session_id, rating_key, player_id, token
            )
            if not needs_queue or not token:
                continue
            queue_from_playback(token, reason="plex-session")
            db.mark_plex_playback_event_queued(event_id)
            queued += 1
        except Exception as exc:
            errors += 1
            log.error(
                "Enrichment could not record Plex session rating_key=%s: %s",
                identity[2], exc,
            )
    _set_health(last_session_poll_ok=(errors == 0))
    return {
        "status": "ok" if errors == 0 else "partial",
        "sessions": sessions,
        "queued": queued,
        "errors": errors,
    }


def _cache_dir() -> Path:
    path = Path(config.ENRICHMENT_CACHE_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(token: str) -> Path:
    return _cache_dir() / f"{token}.media"


def _cache_metadata_path(token: str) -> Path:
    return _cache_dir() / f"{token}.media.json"


def _staged_media_matches(item: dict, path: Path) -> bool:
    """Bind reusable staged bytes to the exact release claimed by the queue."""
    metadata_path = _cache_metadata_path(str(item.get("token") or ""))
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            return False
        sha256 = str(metadata.get("sha256") or "")
        identity_matches = bool(
            metadata.get("schema_version") == 1
            and metadata.get("token") == item.get("token")
            and metadata.get("info_hash") == item.get("info_hash")
            and int(metadata.get("size") or 0) == path.stat().st_size
            and re.fullmatch(r"[0-9a-f]{64}", sha256)
        )
        if not identity_matches:
            return False
        if item.get("_enrichment_verified_sha256") != sha256:
            if _file_sha256(path) != sha256:
                return False
            item["_enrichment_verified_sha256"] = sha256
        return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _write_staged_metadata(item: dict, path: Path, sha256: str) -> None:
    metadata_path = _cache_metadata_path(item["token"])
    temporary = metadata_path.with_name(metadata_path.name + ".part")
    payload = {
        "schema_version": 1,
        "token": item["token"],
        "info_hash": item["info_hash"],
        "size": path.stat().st_size,
        "sha256": sha256,
    }
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, metadata_path)
        descriptor = os.open(metadata_path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def local_stream_path(token: str) -> Path | None:
    """Return staged media only while Plex is analyzing this claimed item."""
    if db.get_enrichment_state(token) != "analyzing":
        return None
    path = _cache_path(token)
    try:
        return path if path.is_file() and path.stat().st_size >= 1024 * 1024 else None
    except OSError:
        return None


def _stub_path(item: dict) -> Path:
    strm_path = Path(item["strm_path"])
    return strm_generator._spore_stub_dir(strm_path) / (strm_path.stem + ".mkv")


def _backup_path(stub: Path) -> Path:
    return stub.with_name("." + stub.name + ".enrichment-stub")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def recover_overlays() -> int:
    """Restore exact stubs left swapped by a stopped worker."""
    restored = 0
    failures: list[str] = []
    for item in db.get_enrichment_recovery_items():
        try:
            stub = _stub_path(item)
        except (KeyError, TypeError, ValueError):
            continue
        backup = _backup_path(stub)
        if not backup.exists():
            continue
        try:
            if (
                backup.is_symlink()
                or not backup.is_file()
                or backup.stat().st_size > 1024 * 1024
            ):
                raise RuntimeError(f"invalid enrichment stub backup {backup}")
            if stub.is_symlink() or stub.exists():
                stub.unlink()
            os.replace(backup, stub)
            if not stub.is_file() or stub.stat().st_size > 1024 * 1024:
                raise RuntimeError(f"invalid restored enrichment stub {stub}")
            restored += 1
        except Exception as exc:
            log.error("Enrichment could not restore %s: %s", stub, exc)
            failures.append(str(exc))
    if restored:
        log.warning("Enrichment restored %d interrupted stub overlay(s)", restored)
    if failures:
        raise RuntimeError(
            f"failed to restore {len(failures)} enrichment stub overlay(s): "
            + "; ".join(failures[:3])
        )
    return restored


def _local_media_valid(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 1024 * 1024:
            return False
    except OSError:
        return False
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(path),
            ],
            capture_output=True,
            timeout=60,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


def _drop_clean_cache_pages(handle) -> None:
    if not hasattr(os, "posix_fadvise") or not hasattr(os, "POSIX_FADV_DONTNEED"):
        return
    try:
        os.posix_fadvise(handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    except OSError:
        pass


def _renew_claim(lease_id: str) -> None:
    renewed = db.renew_enrichment_claim(
        lease_id, lease_seconds=config.ENRICHMENT_LEASE_SECONDS
    )
    if renewed < 1:
        raise RuntimeError("enrichment lease expired or changed owner")


def _set_claim_state(lease_id: str, tokens: list[str], state: str,
                     error: str | None = None) -> None:
    expected = len(set(tokens))
    changed = db.set_enrichment_claim_state(lease_id, tokens, state, error)
    if changed != expected:
        raise RuntimeError(
            f"enrichment lease lost while entering {state}: {changed}/{expected}"
        )


def _download(item: dict, batch_bytes: int, max_bytes: int,
              lease_id: str) -> int:
    token = item["token"]
    final = _cache_path(token)
    _renew_claim(lease_id)
    if _staged_media_matches(item, final) and _local_media_valid(final):
        size = final.stat().st_size
        if size > max_bytes:
            raise EnrichmentItemTooLarge(
                f"staged item exceeds configured byte cap for {item['title']}"
            )
        if batch_bytes + size > max_bytes:
            raise EnrichmentBatchFull("enrichment batch reached configured byte cap")
        _set_claim_state(lease_id, [token], "staged")
        return size

    if playback_guard.active(force=True):
        raise EnrichmentDeferred("playback became active before download")

    _set_claim_state(lease_id, [token], "downloading")
    url = catbox.materialize(token, allow_readd=True, record_playback=False)
    if not url:
        raise RuntimeError(f"materialize failed for {item['title']}")
    _renew_claim(lease_id)
    if playback_guard.active(force=True):
        raise EnrichmentDeferred("playback became active during materialization")

    part = final.with_suffix(".part")
    reserve_bytes = int(config.ENRICHMENT_MIN_FREE_GB) * 1024 ** 3
    try:
        with requests.get(url, stream=True, timeout=(15, 120)) as response:
            if response.status_code >= 400:
                raise RuntimeError(
                    f"download HTTP {response.status_code} for {item['title']}"
                )
            expected = int(response.headers.get("Content-Length") or 0)
            if expected > max_bytes:
                raise EnrichmentItemTooLarge(
                    f"item exceeds configured byte cap for {item['title']}"
                )
            if expected and batch_bytes + expected > max_bytes:
                raise EnrichmentBatchFull("enrichment batch reached configured byte cap")
            written = 0
            digest = hashlib.sha256()
            check_at = 64 * 1024 * 1024
            renew_at = time.monotonic() + min(
                300, max(30, config.ENRICHMENT_LEASE_SECONDS // 4)
            )
            playback_check_at = time.monotonic() + 15
            with part.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                    if not chunk:
                        continue
                    if batch_bytes + written + len(chunk) > max_bytes:
                        if batch_bytes == 0:
                            raise EnrichmentItemTooLarge(
                                "streamed item exceeds configured byte cap for "
                                f"{item['title']}"
                            )
                        raise EnrichmentBatchFull(
                            "enrichment batch reached configured byte cap"
                        )
                    free = shutil.disk_usage(final.parent).free
                    if free - len(chunk) < reserve_bytes:
                        raise RuntimeError("enrichment cache free-space reserve reached")
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    if time.monotonic() >= renew_at:
                        _renew_claim(lease_id)
                        renew_at = time.monotonic() + min(
                            300, max(30, config.ENRICHMENT_LEASE_SECONDS // 4)
                        )
                    if written >= check_at or time.monotonic() >= playback_check_at:
                        if written >= check_at:
                            check_at += 64 * 1024 * 1024
                        playback_check_at = time.monotonic() + 15
                        if playback_guard.active(force=True):
                            raise EnrichmentDeferred("playback started during download")
                handle.flush()
                os.fsync(handle.fileno())
                _drop_clean_cache_pages(handle)
            if expected and written != expected:
                raise RuntimeError(
                    f"download size mismatch for {item['title']}: {written}/{expected}"
                )
        os.chmod(part, 0o644)
        os.replace(part, final)
        if not _local_media_valid(final):
            raise RuntimeError(f"ffprobe rejected downloaded media for {item['title']}")
        sha256 = digest.hexdigest()
        _write_staged_metadata(item, final, sha256)
        item["_enrichment_verified_sha256"] = sha256
        _renew_claim(lease_id)
        _set_claim_state(lease_id, [token], "staged")
        size = final.stat().st_size
        log.info("Enrichment staged %s (%d bytes)", item["title"], size)
        return size
    except Exception:
        part.unlink(missing_ok=True)
        raise


def _overlay(item: dict) -> dict:
    stub = _stub_path(item)
    backup = _backup_path(stub)
    media = _cache_path(item["token"])
    if not _staged_media_matches(item, media) or not _local_media_valid(media):
        raise RuntimeError(f"staged media missing for {item['title']}")
    if backup.exists():
        if stub.is_symlink() or stub.exists():
            stub.unlink()
        os.replace(backup, stub)
    if not stub.is_file() or stub.stat().st_size > 1024 * 1024:
        raise RuntimeError(f"refusing to replace non-stub path {stub}")
    checksum = _file_sha256(stub)
    os.replace(stub, backup)
    try:
        os.symlink(str(media), stub)
    except Exception:
        os.replace(backup, stub)
        raise
    return {"stub": stub, "backup": backup, "checksum": checksum}


def _restore_overlay(overlay: dict) -> None:
    stub = overlay["stub"]
    backup = overlay["backup"]
    if not backup.exists():
        raise RuntimeError(f"enrichment stub backup missing for {stub}")
    if stub.is_symlink() or stub.exists():
        stub.unlink()
    os.replace(backup, stub)
    if _file_sha256(stub) != overlay["checksum"]:
        raise RuntimeError(f"enrichment stub checksum mismatch for {stub}")


def _plex_config() -> tuple[str, str]:
    url = os.environ.get("PLEX_URL", "").rstrip("/")
    token = os.environ.get("PLEX_TOKEN", "")
    if not (url and token):
        try:
            import settings
            url = url or str(settings.get("PLEX_URL", "") or "").rstrip("/")
            token = token or str(settings.get("PLEX_TOKEN", "") or "")
        except Exception:
            pass
    if not (url and token):
        raise RuntimeError("PLEX_URL and PLEX_TOKEN are required for enrichment")
    return url, token


def _plex_request(method: str, path: str, **kwargs) -> requests.Response:
    url, token = _plex_config()
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["X-Plex-Token"] = token
    timeout = kwargs.pop("timeout", 30)
    return requests.request(method, url + path, headers=headers, timeout=timeout, **kwargs)


def _read_preferences() -> dict[str, str]:
    response = _plex_request("GET", "/:/prefs", timeout=5)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    wanted = set(_ANALYSIS_SETTINGS + _BUTLER_SETTINGS)
    return {
        str(node.get("id")): str(node.get("value"))
        for node in root if node.get("id") in wanted
    }


def _set_analysis(mode: str) -> None:
    global _runtime_ready
    errors: list[str] = []
    for key in _ANALYSIS_SETTINGS:
        try:
            response = _plex_request(
                "PUT", "/:/prefs", params={key: mode}, timeout=5
            )
            if response.status_code >= 400:
                errors.append(f"{key}: HTTP {response.status_code}")
        except Exception as exc:
            errors.append(f"{key}: {exc}")
    for key in _BUTLER_SETTINGS:
        try:
            response = _plex_request(
                "PUT", "/:/prefs", params={key: "0"}, timeout=5
            )
            if response.status_code >= 400:
                errors.append(f"{key}: HTTP {response.status_code}")
        except Exception as exc:
            errors.append(f"{key}: {exc}")
    actual: dict[str, str] = {}
    try:
        actual = _read_preferences()
    except Exception as exc:
        errors.append(f"preference read-back: {exc}")
    expected = {key: mode for key in _ANALYSIS_SETTINGS}
    expected.update({key: "0" for key in _BUTLER_SETTINGS})
    wrong = {
        key: actual.get(key) for key, value in expected.items()
        if actual.get(key) != value
    }
    if wrong:
        errors.append(f"read-back mismatch: {wrong}")
    if errors:
        _runtime_ready = False
        _set_health(preferences_safe=False)
        raise RuntimeError(
            "Plex analysis preference verification failed: " + "; ".join(errors)
        )
    _set_health(preferences_safe=(mode == "never"))


def _show_title(item: dict) -> str:
    return _EPISODE_SUFFIX.sub("", item.get("title") or "").strip()


def _title_and_year(item: dict) -> tuple[str, int | None]:
    title = (
        _show_title(item) if item.get("media_type") == "series"
        else str(item.get("title") or "")
    )
    match = _TRAILING_YEAR.search(title)
    year = int(item["year"]) if item.get("year") else None
    if match:
        year = year or int(match.group(1))
        title = title[:match.start()].strip()
    return title, year


def _normalize_title(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return " ".join(re.findall(r"[a-z0-9]+", ascii_value.casefold()))


def _plex_section(media_type: str) -> str:
    key = "PLEX_SERIES_SECTION" if media_type == "series" else "PLEX_MOVIE_SECTION"
    legacy_key = "PLEX_SECTION_TV" if media_type == "series" else "PLEX_SECTION_MOVIE"
    default = "8" if media_type == "series" else "7"
    value = os.environ.get(key) or os.environ.get(legacy_key)
    if not value:
        try:
            import settings
            value = settings.get(key) or settings.get(legacy_key)
        except Exception:
            value = None
    return str(
        value
        or getattr(config, key, None)
        or getattr(config, legacy_key, default)
        or default
    )


def _candidate_node(rating_key: str) -> ET.Element:
    response = _plex_request(
        "GET", f"/library/metadata/{rating_key}", params={"includeGuids": "1"}
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    node = next(
        (child for child in root if child.tag in ("Directory", "Video")), None
    )
    if node is None:
        raise RuntimeError(f"Plex metadata missing for rating key {rating_key}")
    return node


def _candidate_ids(node: ET.Element) -> tuple[set[str], set[int]]:
    imdb: set[str] = set()
    tmdb: set[int] = set()
    values = [node.get("guid") or ""] + [
        child.get("id") or "" for child in node if child.tag == "Guid"
    ]
    for value in values:
        if value.startswith("imdb://"):
            imdb.add(value.split("//", 1)[1])
        elif value.startswith("tmdb://"):
            try:
                tmdb.add(int(value.split("//", 1)[1]))
            except ValueError:
                pass
    return imdb, tmdb


def _search_terms(title: str) -> list[str]:
    normalized = _normalize_title(title)
    words = [word for word in normalized.split() if len(word) >= 4]
    candidates = [title, normalized]
    candidates.extend(sorted(words, key=lambda word: (-len(word), word))[:3])
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.casefold()
        if candidate and key not in seen:
            result.append(candidate)
            seen.add(key)
    return result


def _lookup_library_item(item: dict) -> ET.Element:
    title, year = _title_and_year(item)
    expected_type = "show" if item.get("media_type") == "series" else "movie"
    expected_section = _plex_section(item.get("media_type") or "movie")
    normalized_title = _normalize_title(title)
    imdb_id = str(item.get("imdb_id") or "")
    tmdb_id = int(item["tmdb_id"]) if item.get("tmdb_id") else None
    candidates: dict[str, ET.Element] = {}
    for query in _search_terms(title):
        response = _plex_request(
            "GET", "/search", params={"query": query, "includeGuids": "1"}
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
        for result in root:
            if result.get("type") != expected_type:
                continue
            if str(result.get("librarySectionID") or "") != expected_section:
                continue
            candidate_title = _TRAILING_YEAR.sub(
                "", result.get("title") or ""
            ).strip()
            candidate_imdb, candidate_tmdb = _candidate_ids(result)
            title_matches = _normalize_title(candidate_title) == normalized_title
            id_matches = (
                bool(imdb_id and imdb_id in candidate_imdb)
                or bool(tmdb_id is not None and tmdb_id in candidate_tmdb)
            )
            if not title_matches and not id_matches:
                continue
            rating_key = result.get("ratingKey")
            if rating_key:
                candidates.setdefault(rating_key, result)
    if not candidates:
        raise RuntimeError(f"Plex {expected_type} not found: {title}")

    detailed: list[ET.Element] = []
    for rating_key in sorted(candidates):
        node = _candidate_node(rating_key)
        if node.get("type") != expected_type:
            continue
        if str(node.get("librarySectionID") or "") != expected_section:
            continue
        detailed.append(node)

    imdb_matches = (
        [node for node in detailed if imdb_id in _candidate_ids(node)[0]]
        if imdb_id else []
    )
    tmdb_matches = (
        [node for node in detailed if tmdb_id in _candidate_ids(node)[1]]
        if tmdb_id is not None else []
    )
    if imdb_id and tmdb_id is not None and imdb_matches and tmdb_matches:
        imdb_keys = {node.get("ratingKey") for node in imdb_matches}
        tmdb_keys = {node.get("ratingKey") for node in tmdb_matches}
        if imdb_keys.isdisjoint(tmdb_keys):
            raise RuntimeError(f"conflicting Plex IMDb/TMDB match for {title}")

    if imdb_id:
        if len(imdb_matches) == 1:
            candidate_tmdb = _candidate_ids(imdb_matches[0])[1]
            if tmdb_id is not None and candidate_tmdb and tmdb_id not in candidate_tmdb:
                raise RuntimeError(f"conflicting Plex IMDb/TMDB match for {title}")
            return imdb_matches[0]
        if len(imdb_matches) > 1:
            raise RuntimeError(f"ambiguous Plex IMDb match for {title}")
    if tmdb_id is not None:
        if len(tmdb_matches) == 1:
            candidate_imdb = _candidate_ids(tmdb_matches[0])[0]
            if imdb_id and candidate_imdb and imdb_id not in candidate_imdb:
                raise RuntimeError(f"conflicting Plex IMDb/TMDB match for {title}")
            return tmdb_matches[0]
        if len(tmdb_matches) > 1:
            raise RuntimeError(f"ambiguous Plex TMDB match for {title}")

    if year is not None:
        matches = [
            node for node in detailed
            if _normalize_title(node.get("title") or "") == normalized_title
            and str(node.get("year") or "") == str(year)
            and (
                not imdb_id
                or not _candidate_ids(node)[0]
                or imdb_id in _candidate_ids(node)[0]
            )
            and (
                tmdb_id is None
                or not _candidate_ids(node)[1]
                or tmdb_id in _candidate_ids(node)[1]
            )
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError(f"ambiguous normalized Plex title/year match for {title}")
    raise RuntimeError(f"no stable Plex identity match for {title}")


def _resolve_batch(batch: list[dict]) -> tuple[dict[str, dict], dict[str, str]]:
    targets: dict[str, dict] = {}
    failures: dict[str, str] = {}
    if not batch:
        return targets, failures
    if batch[0].get("media_type") == "series":
        try:
            show = _lookup_library_item(batch[0])
            show_key = show.get("ratingKey")
            response = _plex_request("GET", f"/library/metadata/{show_key}/allLeaves")
            response.raise_for_status()
            mapping: dict[
                tuple[int, int], set[tuple[str, str | None]]
            ] = {}
            for node in ET.fromstring(response.content):
                try:
                    pair = (
                        int(node.get("parentIndex") or 0),
                        int(node.get("index") or 0),
                    )
                except ValueError:
                    continue
                rating_key = node.get("ratingKey")
                if pair[0] >= 0 and pair[1] > 0 and rating_key:
                    mapping.setdefault(pair, set()).add(
                        (rating_key, node.get("parentRatingKey"))
                    )
            for item in batch:
                pair = (
                    int(item.get("season") or 0),
                    int(item.get("episode") or 0),
                )
                matches = mapping.get(pair, set())
                if len(matches) == 1:
                    target = next(iter(matches))
                    targets[item["token"]] = {
                        "rating_key": target[0], "season_key": target[1]
                    }
                elif len(matches) > 1:
                    failures[item["token"]] = (
                        f"ambiguous Plex episode for {item['title']}"
                    )
                else:
                    failures[item["token"]] = (
                        f"Plex episode not found for {item['title']}"
                    )
        except Exception as exc:
            for item in batch:
                failures[item["token"]] = str(exc)
    else:
        for item in batch:
            try:
                node = _lookup_library_item(item)
                rating_key = node.get("ratingKey")
                if not rating_key:
                    raise RuntimeError(
                        f"Plex movie rating key missing for {item['title']}"
                    )
                targets[item["token"]] = {
                    "rating_key": rating_key, "season_key": None
                }
            except Exception as exc:
                failures[item["token"]] = str(exc)
    return targets, failures


def _analysis_activities() -> list[ET.Element]:
    response = _plex_request("GET", "/activities")
    if response.status_code != 200:
        raise RuntimeError(f"Plex activities failed: HTTP {response.status_code}")
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise RuntimeError("Plex activities returned invalid XML") from exc
    return [
        node for node in root
        if "media.generate" in (node.get("type") or "")
        or "media.analyze" in (node.get("type") or "")
    ]


def _metadata_evidence(rating_key: str) -> dict:
    response = _plex_request(
        "GET", f"/library/metadata/{rating_key}",
        params={"includeMarkers": "1", "includeGuids": "1"},
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    node = next(
        (child for child in root if child.tag in ("Directory", "Video")), None
    )
    if node is None:
        raise RuntimeError(f"Plex evidence metadata missing for {rating_key}")
    marker_count = sum(1 for child in node if child.tag == "Marker")
    parts = list(node.iter("Part"))
    streams = [stream for part in parts for stream in part if stream.tag == "Stream"]
    summary = {
        "rating_key": str(rating_key),
        "updated_at": node.get("updatedAt"),
        "duration_ms": int(node.get("duration") or 0),
        "marker_count": marker_count,
        "part_count": len(parts),
        "stream_count": len(streams),
        "part_sizes": sorted(int(part.get("size") or 0) for part in parts),
    }
    summary["digest"] = hashlib.sha256(response.content).hexdigest()
    return summary


def _analyze_target(rating_key: str, lease_id: str | None = None,
                    require_media_artifact: bool = True) -> dict:
    if lease_id:
        _renew_claim(lease_id)
    if _analysis_activities():
        raise EnrichmentDeferred("Plex analysis was already active")
    before = _metadata_evidence(rating_key)
    response = _plex_request("PUT", f"/library/metadata/{rating_key}/analyze")
    if response.status_code != 200:
        raise RuntimeError(f"Plex analyze failed: HTTP {response.status_code}")

    deadline = time.monotonic() + config.ENRICHMENT_ANALYZE_TIMEOUT_SECONDS
    renew_at = time.monotonic() + min(
        300, max(30, config.ENRICHMENT_LEASE_SECONDS // 4)
    )
    seen_active = False
    stable_digest: str | None = None
    stable_polls = 0
    while time.monotonic() < deadline:
        if playback_guard.active(force=True):
            raise EnrichmentDeferred("playback started during Plex analysis")
        if lease_id and time.monotonic() >= renew_at:
            _renew_claim(lease_id)
            renew_at = time.monotonic() + min(
                300, max(30, config.ENRICHMENT_LEASE_SECONDS // 4)
            )
        activities = _analysis_activities()
        if activities:
            seen_active = True
            stable_digest = None
            stable_polls = 0
        else:
            after = _metadata_evidence(rating_key)
            metadata_changed = after["digest"] != before["digest"]
            has_artifact = not require_media_artifact or bool(
                after["duration_ms"] > 0
                and after["part_count"] > 0
                and after["stream_count"] > 0
            )
            positive_change = metadata_changed or (
                not require_media_artifact and seen_active
            )
            if positive_change and has_artifact:
                if stable_digest == after["digest"]:
                    stable_polls += 1
                else:
                    stable_digest = after["digest"]
                    stable_polls = 1
            else:
                stable_digest = None
                stable_polls = 0
            required_polls = 2 if seen_active else 3
            if stable_polls >= required_polls:
                return {
                    "activity_started": seen_active,
                    "activity_completed": True,
                    "metadata_changed": metadata_changed,
                    "artifact": after,
                }
        time.sleep(2)
    raise RuntimeError(
        f"Plex analysis produced no completion evidence for {rating_key}"
    )


def _analyze_batch(batch: list[dict], targets: dict[str, dict],
                   lease_id: str) -> tuple[dict[str, dict], dict[str, str]]:
    evidence: dict[str, dict] = {}
    failures: dict[str, str] = {}
    season_keys: set[str] = set()
    for item in batch:
        target = targets.get(item["token"])
        if not target:
            continue
        if playback_guard.active(force=True):
            raise EnrichmentDeferred("playback started during analysis batch")
        try:
            item_evidence = _analyze_target(target["rating_key"], lease_id)
            item_evidence["media_type"] = item.get("media_type")
            evidence[item["token"]] = item_evidence
            if target.get("season_key"):
                season_keys.add(str(target["season_key"]))
            log.info("Enrichment analyzed %s", item["title"])
        except EnrichmentDeferred:
            raise
        except Exception as exc:
            failures[item["token"]] = str(exc)
            log.error("Enrichment item analysis failed for %s: %s", item["title"], exc)

    season_evidence: dict[str, dict] = {}
    for season_key in sorted(season_keys):
        if playback_guard.active(force=True):
            raise EnrichmentDeferred("playback started before season marker pass")
        try:
            season_evidence[season_key] = _analyze_target(
                season_key, lease_id, require_media_artifact=False
            )
        except EnrichmentDeferred:
            raise
        except Exception as exc:
            for token in list(evidence):
                if str(targets[token].get("season_key")) == season_key:
                    failures[token] = f"season analysis failed: {exc}"
                    evidence.pop(token, None)
            log.error("Enrichment season analysis failed for %s: %s", season_key, exc)
    for token, item_evidence in evidence.items():
        season_key = targets[token].get("season_key")
        if season_key:
            item_evidence["season_pass"] = season_evidence[str(season_key)]
    return evidence, failures


def _fail_tokens(lease_id: str, tokens: list[str], error: str) -> dict[str, int]:
    return db.fail_enrichment_claim(
        lease_id,
        tokens,
        error,
        max_attempts=config.ENRICHMENT_MAX_ATTEMPTS,
        retry_base_seconds=config.ENRICHMENT_RETRY_BASE_SECONDS,
    )


def _discard_staged_media(token: str) -> None:
    for path in (
        _cache_path(token),
        _cache_metadata_path(token),
        _cache_path(token).with_suffix(".part"),
        _cache_metadata_path(token).with_name(
            _cache_metadata_path(token).name + ".part"
        ),
    ):
        path.unlink(missing_ok=True)


def _cleanup_dead_letter_staged_media() -> int:
    removed = 0
    for token in db.get_enrichment_tokens_by_state("dead_letter"):
        paths = (
            _cache_path(token),
            _cache_metadata_path(token),
            _cache_path(token).with_suffix(".part"),
            _cache_metadata_path(token).with_name(
                _cache_metadata_path(token).name + ".part"
            ),
        )
        existed = any(path.exists() or path.is_symlink() for path in paths)
        _discard_staged_media(token)
        removed += int(existed)
    if removed:
        log.warning("Enrichment removed staged media for %d dead letter(s)", removed)
    return removed


def run_once() -> dict:
    """Claim and process one bounded movie or series-season batch."""
    global _runtime_ready
    if not enabled():
        return {"status": "disabled"}
    if not _runtime_ready and not initialize():
        return {"status": "unsafe-preferences"}
    if not _run_lock.acquire(blocking=False):
        return {"status": "already-running"}

    lease_id = uuid.uuid4().hex
    batch: list[dict] = []
    overlays: list[dict] = []
    claimed: set[str] = set()
    staged: set[str] = set()
    completed: set[str] = set()
    failures = 0
    batch_bytes = 0
    process_lock = None
    owns_process_lock = False
    result: dict = {"status": "failed"}
    try:
        process_lock = open(_PROCESS_LOCK_PATH, "a+", encoding="utf-8")
        try:
            fcntl.flock(process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result = {"status": "already-running"}
            return result
        owns_process_lock = True

        _prepare_safe_runtime()
        if playback_guard.defer("media_enrichment"):
            result = {"status": "deferred-playback"}
            return result

        batch = db.claim_enrichment_batch(
            lease_id,
            lease_seconds=config.ENRICHMENT_LEASE_SECONDS,
            max_attempts=config.ENRICHMENT_MAX_ATTEMPTS,
            retry_base_seconds=config.ENRICHMENT_RETRY_BASE_SECONDS,
            max_items=config.ENRICHMENT_SEASON_CAP,
        )
        if not batch:
            result = {"status": "idle"}
            return result
        claimed = {item["token"] for item in batch}
        max_bytes = int(config.ENRICHMENT_MAX_BATCH_GB) * 1024 ** 3

        for index, item in enumerate(batch):
            token = item["token"]
            try:
                batch_bytes += _download(item, batch_bytes, max_bytes, lease_id)
                staged.add(token)
            except EnrichmentBatchFull as exc:
                remaining = [row["token"] for row in batch[index:]]
                db.defer_enrichment_claim(
                    lease_id, remaining, str(exc), staged_tokens=staged
                )
                claimed.difference_update(remaining)
                break
            except EnrichmentDeferred:
                raise
            except Exception as exc:
                _fail_tokens(lease_id, [token], str(exc))
                claimed.discard(token)
                failures += 1
                log.exception("Enrichment staging failed for %s: %s", item["title"], exc)

        ready = [
            item for item in batch
            if item["token"] in staged and item["token"] in claimed
        ]
        if not ready:
            result = {
                "status": "failed" if failures else "deferred-cap",
                "items": 0,
                "failures": failures,
            }
            return result
        if playback_guard.active(force=True):
            raise EnrichmentDeferred("playback active after staging")

        targets, resolution_failures = _resolve_batch(ready)
        for token, error in resolution_failures.items():
            _fail_tokens(lease_id, [token], error)
            claimed.discard(token)
            staged.discard(token)
            failures += 1
            log.error("Enrichment identity resolution failed token=%s: %s", token, error)
        ready = [item for item in ready if item["token"] in targets]
        if not ready:
            result = {"status": "failed", "items": 0, "failures": failures}
            return result

        overlay_items: list[dict] = []
        for item in ready:
            token = item["token"]
            try:
                overlays.append(_overlay(item))
                overlay_items.append(item)
            except Exception as exc:
                _fail_tokens(lease_id, [token], str(exc))
                claimed.discard(token)
                staged.discard(token)
                failures += 1
                log.exception("Enrichment overlay failed for %s: %s", item["title"], exc)
        ready = overlay_items
        if not ready:
            result = {"status": "failed", "items": 0, "failures": failures}
            return result

        analyzing_tokens = [item["token"] for item in ready]
        _set_claim_state(lease_id, analyzing_tokens, "analyzing")
        analysis_evidence: dict[str, dict] = {}
        analysis_failures: dict[str, str] = {}
        analysis_error: Exception | None = None
        try:
            _set_analysis("scheduled")
            analysis_evidence, analysis_failures = _analyze_batch(
                ready, targets, lease_id
            )
        except Exception as exc:
            analysis_error = exc
        finally:
            restore_errors = []
            unrestored = []
            for overlay in reversed(overlays):
                try:
                    _restore_overlay(overlay)
                except Exception as exc:
                    unrestored.append(overlay)
                    restore_errors.append(str(exc))
                    log.critical("Enrichment stub restore failed: %s", exc)
            overlays[:] = unrestored
            try:
                _set_analysis("never")
                _runtime_ready = True
            except Exception as exc:
                _runtime_ready = False
                restore_errors.append(str(exc))
                log.critical("Enrichment could not restore safe Plex preferences: %s", exc)
            if restore_errors:
                analysis_error = RuntimeError("; ".join(restore_errors))

        if analysis_error is not None:
            raise analysis_error

        for token, error in analysis_failures.items():
            _fail_tokens(lease_id, [token], error)
            claimed.discard(token)
            failures += 1

        for item in ready:
            token = item["token"]
            if token in analysis_failures:
                continue
            evidence = analysis_evidence.get(token)
            if not evidence:
                _fail_tokens(lease_id, [token], "missing positive analysis evidence")
                claimed.discard(token)
                failures += 1
                continue
            evidence["stub_restored"] = True
            evidence["downloaded_bytes"] = _cache_path(token).stat().st_size
            if not db.complete_enrichment_claim(
                lease_id, token, targets[token]["rating_key"], evidence
            ):
                raise RuntimeError(f"enrichment lease lost before completion for {token}")
            claimed.discard(token)
            completed.add(token)
            _cache_path(token).unlink(missing_ok=True)
            _cache_metadata_path(token).unlink(missing_ok=True)

        status = "complete" if completed and not failures else "partial"
        if not completed:
            status = "failed"
        result = {
            "status": status,
            "items": len(completed),
            "failures": failures,
            "bytes": batch_bytes,
        }
        log.info(
            "Enrichment batch result: status=%s items=%d failures=%d bytes=%d",
            status, len(completed), failures, batch_bytes,
        )
        return result
    except EnrichmentDeferred as exc:
        pending = sorted(claimed)
        if pending:
            db.defer_enrichment_claim(
                lease_id, pending, str(exc), staged_tokens=staged
            )
            claimed.clear()
        log.info("Enrichment deferred: %s", exc)
        result = {"status": "deferred-playback", "items": len(completed)}
        return result
    except Exception as exc:
        pending = sorted(claimed)
        if pending:
            outcome = _fail_tokens(lease_id, pending, str(exc))
            failures += outcome["retry"] + outcome["dead_letter"]
            claimed.clear()
        log.exception("Enrichment batch failed: %s", exc)
        result = {
            "status": "failed",
            "error": str(exc)[:200],
            "items": len(completed),
            "failures": failures,
        }
        return result
    finally:
        for overlay in reversed(overlays):
            try:
                _restore_overlay(overlay)
            except Exception as exc:
                _runtime_ready = False
                _set_health(preferences_safe=False)
                log.critical("Enrichment stub restore failed: %s", exc)
        if owns_process_lock:
            try:
                _set_analysis("never")
            except Exception as exc:
                _runtime_ready = False
                _set_health(preferences_safe=False)
                log.critical(
                    "Enrichment final Plex preference safety check failed: %s", exc
                )
            try:
                _cleanup_dead_letter_staged_media()
            except Exception as exc:
                log.error("Enrichment dead-letter cache cleanup failed: %s", exc)
        if process_lock is not None:
            try:
                fcntl.flock(process_lock.fileno(), fcntl.LOCK_UN)
            finally:
                process_lock.close()
        _set_health(last_run_status=result.get("status", "unknown"))
        _run_lock.release()
