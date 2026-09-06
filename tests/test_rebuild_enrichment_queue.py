import importlib.util
import os
import sys

import pytest


def _load_rebuild_tool():
    root = os.path.join(os.path.dirname(__file__), "..")
    db_spec = importlib.util.spec_from_file_location(
        "rebuild_enrichment_db", os.path.join(root, "db.py")
    )
    real_db = importlib.util.module_from_spec(db_spec)
    db_spec.loader.exec_module(real_db)
    saved_db = sys.modules.get("db")
    sys.modules["db"] = real_db
    path = os.path.join(root, "scripts", "rebuild_enrichment_queue.py")
    spec = importlib.util.spec_from_file_location("rebuild_enrichment_tool", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        if saved_db is None:
            sys.modules.pop("db", None)
        else:
            sys.modules["db"] = saved_db
    return module


tool = _load_rebuild_tool()


@pytest.fixture
def isolated_tool(tmp_path, monkeypatch):
    current = getattr(tool.db._tls, "conn", None)
    if current is not None:
        current.close()
        del tool.db._tls.conn
    database = tmp_path / "requests.db"
    cache = tmp_path / "analysis"
    monkeypatch.setattr(tool.db, "DB_PATH", str(database))
    monkeypatch.setattr(tool.config, "DB_PATH", str(database))
    monkeypatch.setattr(tool.config, "ENRICHMENT_CACHE_DIR", str(cache))
    monkeypatch.setattr(tool, "_PROCESS_LOCK_PATH", str(tmp_path / "worker.lock"))
    monkeypatch.setattr(tool, "_recover_overlays", lambda: 0)
    tool.db.init()
    yield tool, database, cache
    current = getattr(tool.db._tls, "conn", None)
    if current is not None:
        current.close()
        del tool.db._tls.conn


def test_quarantine_preserves_completion_backup_and_staged_media(isolated_tool):
    rebuild, database, cache = isolated_tool
    cache.mkdir()
    complete = {
        "token": "complete", "info_hash": "a" * 40,
        "title": "Complete", "media_type": "movie",
    }
    pending = {
        "token": "pending", "info_hash": "b" * 40,
        "title": "Pending", "media_type": "movie",
    }
    rebuild.db.queue_media_enrichment([complete, pending])
    with rebuild.db._connect() as connection:
        connection.execute(
            "UPDATE media_enrichment_queue SET state='complete', "
            "completed_at=CURRENT_TIMESTAMP WHERE token='complete'"
        )
    (cache / "complete.media").write_bytes(b"completed-cache")
    (cache / "pending.media").write_bytes(b"pending-cache")
    (cache / "pending.media.json").write_text("{}\n", encoding="utf-8")

    result = rebuild.quarantine(rebuild.QUARANTINE_CONFIRMATION)

    assert result["quarantined_rows"] == 1
    assert result["quarantined_staged_files"] == 2
    assert result["restored_overlays"] == 0
    assert rebuild.db.enrichment_counts() == {
        "complete": 1, "quarantined": 1,
    }
    assert (cache / "complete.media").read_bytes() == b"completed-cache"
    assert not (cache / "pending.media").exists()
    assert not (cache / "pending.media.json").exists()
    backup = database.parent / "backups" / (
        f"requests-before-{result['batch_id']}.db"
    )
    assert backup.is_file()

    restored = rebuild.restore(
        result["batch_id"], rebuild.RESTORE_CONFIRMATION
    )

    assert restored["restored_rows"] == 1
    assert restored["restored_staged_files"] == 2
    assert rebuild.db.enrichment_counts() == {"complete": 1, "queued": 1}
    assert (cache / "pending.media").read_bytes() == b"pending-cache"
    assert (cache / "pending.media.json").read_text(encoding="utf-8") == "{}\n"


def test_quarantine_requires_exact_confirmation(isolated_tool):
    rebuild, _, _ = isolated_tool
    with pytest.raises(RuntimeError, match="confirmation must be exactly"):
        rebuild.quarantine("yes")


def test_overlay_recovery_failure_prevents_quarantine_and_file_move(
        isolated_tool, monkeypatch):
    rebuild, _, cache = isolated_tool
    cache.mkdir()
    pending = {
        "token": "pending", "info_hash": "b" * 40,
        "title": "Pending", "media_type": "movie",
    }
    rebuild.db.queue_media_enrichment([pending])
    staged = cache / "pending.media"
    staged.write_bytes(b"pending-cache")
    monkeypatch.setattr(
        rebuild, "_recover_overlays",
        lambda: (_ for _ in ()).throw(RuntimeError("restore failed")),
    )

    with pytest.raises(RuntimeError, match="restore failed"):
        rebuild.quarantine(rebuild.QUARANTINE_CONFIRMATION)

    assert rebuild.db.enrichment_counts() == {"queued": 1}
    assert staged.read_bytes() == b"pending-cache"
