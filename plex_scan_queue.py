"""Versioned atomic spool for targeted Plex scanner requests."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

SCHEMA_VERSION = 1
MAX_ATTEMPTS = 5
VALID_MODES = {"scan", "remove", "analyze"}
VALID_ROOT = "/mnt/library/"
SUBDIRS = ("ready", "working", "done", "dead", "temp")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("spool timestamps must include a timezone")
    return parsed


def _paths(root: str | Path) -> dict[str, Path]:
    base = Path(root)
    result = {name: base / name for name in SUBDIRS}
    for path in result.values():
        path.mkdir(parents=True, exist_ok=True)
    return result


@contextmanager
def _locked_paths(root: str | Path):
    """Serialize state transitions across producers and Python consumers."""
    paths = _paths(root)
    lock_path = Path(root) / ".queue.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o640)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield paths
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validate(mode: str, section: str, path: str) -> tuple[str, str, str]:
    mode = str(mode or "").lower()
    section = str(section or "")
    path = str(path or "")
    if mode not in VALID_MODES:
        raise ValueError(f"invalid Plex scan mode: {mode}")
    if not section.isdigit():
        raise ValueError("Plex section must be numeric")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError("Plex scan path is outside the library root")
    raw_parts = path.split("/")
    candidate = PurePosixPath(path.rstrip("/"))
    if (
        path == VALID_ROOT.rstrip("/")
        or candidate.parts[:3] != ("/", "mnt", "library")
        or len(candidate.parts) <= 3
        or any(part in (".", "..") for part in raw_parts)
    ):
        raise ValueError("Plex scan path is outside the library root")
    return mode, section, str(candidate)


def _request_id(mode: str, section: str, path: str) -> str:
    return hashlib.sha256(
        f"{mode}\0{section}\0{path}".encode("utf-8")
    ).hexdigest()


def _write_atomic(payload: dict, target: Path, paths: dict[str, Path]) -> None:
    temporary = paths["temp"] / f"{target.name}.{uuid.uuid4().hex}.tmp"
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o640)
    os.replace(temporary, target)
    _fsync_directory(target.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_sync(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _quarantine_invalid(path: Path, paths: dict[str, Path]) -> None:
    """Preserve an unreadable active request instead of retrying it forever."""
    if not path.exists():
        return
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    target = paths["dead"] / f"invalid.{stamp}.{uuid.uuid4().hex[:8]}.json"
    os.replace(path, target)
    _fsync_directory(path.parent)
    _fsync_directory(target.parent)


def _load(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid Plex scan request: {path.name}")
    required = {
        "schema_version", "request_id", "mode", "section", "path",
        "created_at", "attempts", "next_attempt_at", "lease_id",
        "lease_expires_at", "last_error",
    }
    if not required.issubset(payload):
        raise ValueError(f"invalid Plex scan request: {path.name}")
    if int(payload["schema_version"]) != SCHEMA_VERSION:
        raise ValueError(f"unsupported Plex scan schema: {payload['schema_version']}")
    mode, section, scan_path = _validate(
        payload["mode"], payload["section"], payload["path"]
    )
    expected_id = _request_id(mode, section, scan_path)
    if str(payload["request_id"]) != expected_id:
        raise ValueError(f"Plex scan request identity mismatch: {path.name}")
    if path.parent.name in ("ready", "working") and path.name != f"{expected_id}.json":
        raise ValueError(f"Plex scan request filename mismatch: {path.name}")
    try:
        attempts = int(payload["attempts"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid Plex scan attempts: {path.name}") from exc
    if attempts < 0:
        raise ValueError(f"invalid Plex scan attempts: {path.name}")
    if _parse(payload.get("created_at")) is None:
        raise ValueError(f"invalid Plex scan creation time: {path.name}")
    _parse(payload.get("next_attempt_at"))
    _parse(payload.get("lease_expires_at"))
    payload.update({
        "mode": mode,
        "section": section,
        "path": scan_path,
        "request_id": expected_id,
        "attempts": attempts,
    })
    return payload


def enqueue(root: str | Path, mode: str, section: str, path: str) -> str:
    """Idempotently enqueue one request while an equivalent request is pending."""
    mode, section, path = _validate(mode, section, path)
    request_id = _request_id(mode, section, path)
    filename = f"{request_id}.json"
    with _locked_paths(root) as paths:
        if ((paths["ready"] / filename).exists()
                or (paths["working"] / filename).exists()):
            return request_id
        payload = {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "mode": mode,
            "section": section,
            "path": path,
            "created_at": _iso(_now()),
            "attempts": 0,
            "next_attempt_at": None,
            "lease_id": None,
            "lease_expires_at": None,
            "last_error": None,
        }
        _write_atomic(payload, paths["ready"] / filename, paths)
    return request_id


def _archive(payload: dict, destination: Path, paths: dict[str, Path]) -> None:
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    target = destination / f"{payload['request_id']}.{stamp}.{uuid.uuid4().hex[:8]}.json"
    _write_atomic(payload, target, paths)


def _release_failed(path: Path, payload: dict, paths: dict[str, Path],
                    error: str, max_attempts: int,
                    retry_base_seconds: int) -> str:
    payload["last_error"] = str(error)[:500]
    payload["lease_id"] = None
    payload["lease_expires_at"] = None
    if int(payload["attempts"]) >= max_attempts:
        payload["dead_lettered_at"] = _iso(_now())
        _archive(payload, paths["dead"], paths)
        _unlink_sync(path)
        return "dead"
    delay = min(
        86400,
        max(1, retry_base_seconds) * (2 ** max(0, int(payload["attempts"]) - 1)),
    )
    payload["next_attempt_at"] = _iso(_now() + timedelta(seconds=delay))
    ready = paths["ready"] / f"{payload['request_id']}.json"
    _write_atomic(payload, ready, paths)
    if path != ready:
        _unlink_sync(path)
    return "retry"


def _recover_expired(paths: dict[str, Path], max_attempts: int,
                     retry_base_seconds: int) -> int:
    recovered = 0
    now = _now()
    for path in sorted(paths["working"].glob("*.json")):
        try:
            payload = _load(path)
            expires = _parse(payload.get("lease_expires_at"))
            if expires is not None and expires > now:
                continue
            _release_failed(
                path, payload, paths, "worker lease expired",
                max_attempts, retry_base_seconds,
            )
            recovered += 1
        except OSError:
            continue
        except (ValueError, TypeError, AttributeError, UnicodeError):
            _quarantine_invalid(path, paths)
    return recovered


def recover_expired(root: str | Path, max_attempts: int = 5,
                    retry_base_seconds: int = 60) -> int:
    with _locked_paths(root) as paths:
        return _recover_expired(
            paths, min(MAX_ATTEMPTS, max(1, int(max_attempts))),
            max(1, int(retry_base_seconds)),
        )


def claim(root: str | Path, worker_id: str, lease_seconds: int = 900,
          max_attempts: int = 5,
          retry_base_seconds: int = 60) -> dict | None:
    """Atomically claim the next due request and increment its attempt."""
    if not worker_id:
        raise ValueError("worker_id is required")
    max_attempts = min(MAX_ATTEMPTS, max(1, int(max_attempts)))
    retry_base_seconds = max(1, int(retry_base_seconds))
    with _locked_paths(root) as paths:
        _recover_expired(paths, max_attempts, retry_base_seconds)
        now = _now()
        for ready in sorted(paths["ready"].glob("*.json")):
            try:
                payload = _load(ready)
                lease_expires = _parse(payload.get("lease_expires_at"))
                if payload.get("lease_id"):
                    if lease_expires is not None and lease_expires > now:
                        continue
                    _release_failed(
                        ready, payload, paths, "claim interrupted before delivery",
                        max_attempts, retry_base_seconds,
                    )
                    continue
                if int(payload["attempts"]) >= max_attempts:
                    _release_failed(
                        ready, payload, paths, "maximum attempts already reached",
                        max_attempts, retry_base_seconds,
                    )
                    continue
                due = _parse(payload.get("next_attempt_at"))
                if due is not None and due > now:
                    continue
                working = paths["working"] / ready.name
                if working.exists():
                    continue
                payload["attempts"] = int(payload["attempts"]) + 1
                payload["lease_id"] = worker_id
                payload["lease_expires_at"] = _iso(
                    now + timedelta(seconds=max(30, int(lease_seconds)))
                )
                payload["next_attempt_at"] = None
                # Persist the claim payload before the atomic ready-to-working
                # rename. A crash can leave a leased request in ready, but its
                # consumed attempt remains durable and is recovered with backoff.
                _write_atomic(payload, ready, paths)
                os.replace(ready, working)
                _fsync_directory(ready.parent)
                _fsync_directory(working.parent)
                return payload
            except OSError:
                continue
            except (ValueError, TypeError, AttributeError, UnicodeError):
                _quarantine_invalid(ready, paths)
                continue
    return None


def renew(root: str | Path, request_id: str, worker_id: str,
          lease_seconds: int = 900) -> bool:
    """Extend a live spool lease only for its current owner."""
    with _locked_paths(root) as paths:
        working = paths["working"] / f"{request_id}.json"
        if not working.exists():
            return False
        payload = _load(working)
        expires = _parse(payload.get("lease_expires_at"))
        if (
            payload.get("lease_id") != worker_id
            or expires is None
            or expires <= _now()
        ):
            return False
        payload["lease_expires_at"] = _iso(
            _now() + timedelta(seconds=max(30, int(lease_seconds)))
        )
        _write_atomic(payload, working, paths)
        return True


def ack(root: str | Path, request_id: str, worker_id: str) -> bool:
    with _locked_paths(root) as paths:
        working = paths["working"] / f"{request_id}.json"
        if not working.exists():
            return False
        payload = _load(working)
        expires = _parse(payload.get("lease_expires_at"))
        if (
            payload.get("lease_id") != worker_id
            or expires is None
            or expires <= _now()
        ):
            return False
        payload["completed_at"] = _iso(_now())
        payload["lease_id"] = None
        payload["lease_expires_at"] = None
        _archive(payload, paths["done"], paths)
        _unlink_sync(working)
        return True


def nack(root: str | Path, request_id: str, worker_id: str, error: str,
         max_attempts: int = 5,
         retry_base_seconds: int = 60) -> str | None:
    with _locked_paths(root) as paths:
        working = paths["working"] / f"{request_id}.json"
        if not working.exists():
            return None
        payload = _load(working)
        expires = _parse(payload.get("lease_expires_at"))
        if (
            payload.get("lease_id") != worker_id
            or expires is None
            or expires <= _now()
        ):
            return None
        return _release_failed(
            working, payload, paths, error,
            min(MAX_ATTEMPTS, max(1, int(max_attempts))),
            max(1, int(retry_base_seconds)),
        )


def counts(root: str | Path) -> dict[str, int]:
    paths = _paths(root)
    return {
        name: sum(1 for _ in paths[name].glob("*.json"))
        for name in ("ready", "working", "done", "dead")
    }
