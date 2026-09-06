#!/usr/bin/env python3
"""Safely quarantine or restore untrusted Plex enrichment work."""
from __future__ import annotations

import fcntl
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import db

QUARANTINE_CONFIRMATION = "QUARANTINE_PENDING"
RESTORE_CONFIRMATION = "RESTORE_QUARANTINE"
_PROCESS_LOCK_PATH = "/data/media-enrichment.lock"


def _batch_id() -> str:
    return datetime.now(timezone.utc).strftime("enrichment-%Y%m%dT%H%M%S%fZ")


@contextmanager
def _exclusive_worker_lock():
    lock_path = Path(_PROCESS_LOCK_PATH)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "media enrichment worker is active; retry after it is idle"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _backup(batch_id: str) -> Path:
    source_path = Path(config.DB_PATH)
    backup_dir = source_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"requests-before-{batch_id}.db"
    source = sqlite3.connect(source_path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    target.chmod(0o600)
    return target


def _stage_quarantine_dir(batch_id: str) -> Path:
    cache = Path(config.ENRICHMENT_CACHE_DIR)
    return cache.parent / "analysis-quarantine" / batch_id


def _recover_overlays() -> int:
    import enrichment
    return enrichment.recover_overlays()


def _move_staged(batch_id: str, restore: bool = False) -> int:
    cache = Path(config.ENRICHMENT_CACHE_DIR)
    quarantine = _stage_quarantine_dir(batch_id)
    moved = 0
    if restore:
        if not quarantine.is_dir():
            return 0
        cache.mkdir(parents=True, exist_ok=True)
        for source in sorted(quarantine.iterdir()):
            if source.is_symlink() or not source.is_file():
                raise RuntimeError(f"invalid quarantined staged artifact {source}")
            target = cache / source.name
            if target.exists():
                raise RuntimeError(f"refusing to overwrite staged file {target}")
            source.replace(target)
            moved += 1
        return moved

    tokens = set(db.get_quarantined_enrichment_tokens(batch_id))
    candidates = [
        cache / name
        for token in tokens
        for name in (
            f"{token}.media",
            f"{token}.media.json",
            f"{token}.media.json.part",
            f"{token}.part",
        )
    ]
    invalid = [path for path in candidates if path.is_symlink()]
    if invalid:
        raise RuntimeError(f"invalid staged artifact {invalid[0]}")
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return 0
    quarantine.mkdir(parents=True, exist_ok=True)
    for source in existing:
        source.replace(quarantine / source.name)
        moved += 1
    return moved


def status() -> dict:
    health = db.enrichment_metrics()
    cache = Path(config.ENRICHMENT_CACHE_DIR)
    staged = list(cache.glob("*.media")) if cache.is_dir() else []
    return {
        "database": str(config.DB_PATH),
        "states": health["states"],
        "completed": health["states"].get("complete", 0),
        "staged_files": len(staged),
        "staged_bytes": sum(path.stat().st_size for path in staged),
    }


def quarantine(confirmation: str) -> dict:
    if confirmation != QUARANTINE_CONFIRMATION:
        raise RuntimeError(
            f"confirmation must be exactly {QUARANTINE_CONFIRMATION}"
        )
    with _exclusive_worker_lock():
        batch_id = _batch_id()
        restored = _recover_overlays()
        backup = _backup(batch_id)
        changed = db.quarantine_pending_enrichment(
            batch_id,
            "quarantined before authoritative Plex session rebuild",
        )
        moved = _move_staged(batch_id)
    return {
        "batch_id": batch_id,
        "quarantined_rows": changed,
        "quarantined_staged_files": moved,
        "restored_overlays": restored,
        "backup": str(backup),
    }


def restore(batch_id: str, confirmation: str) -> dict:
    if confirmation != RESTORE_CONFIRMATION:
        raise RuntimeError(f"confirmation must be exactly {RESTORE_CONFIRMATION}")
    with _exclusive_worker_lock():
        moved = _move_staged(batch_id, restore=True)
        changed = db.restore_quarantined_enrichment(batch_id)
    return {
        "batch_id": batch_id,
        "restored_rows": changed,
        "restored_staged_files": moved,
    }


def main(argv: list[str]) -> int:
    db.init()
    command = argv[1] if len(argv) > 1 else "status"
    if command == "status":
        output = status()
    elif command == "quarantine" and len(argv) == 3:
        output = quarantine(argv[2])
    elif command == "restore" and len(argv) == 4:
        output = restore(argv[2], argv[3])
    else:
        print(
            "usage: rebuild_enrichment_queue.py "
            "status | quarantine CONFIRMATION | restore BATCH_ID CONFIRMATION",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
