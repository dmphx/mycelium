"""Regression tests for the catbox missing-.strm rebuild (_run_once_catbox).

On 2026-09-25 the hourly rebuild and the daily cleanup started on the same
scheduler tick. The rebuild took its virtual_items snapshot first, then spent
about 7 minutes checking paths. Meanwhile cleanup renamed "Hell's Kitchen (US)"
to "Hell's Kitchen" and rebound the 61 rows. When the rebuild reached those rows
it saw the old paths missing and wrote them again, creating a second show folder
full of same-token orphan .strm files (62 files, including one Megazone rename).
"""
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import strm_generator as sg


class FakeDb:
    def __init__(self, rows, snapshot=None, on_snapshot=None):
        self.rows = {row["token"]: dict(row) for row in rows}
        self.snapshot = snapshot
        self.on_snapshot = on_snapshot

    def get_all_virtual_items(self):
        items = [dict(r) for r in (self.snapshot or self.rows.values())]
        if self.on_snapshot:
            self.on_snapshot(self)
        return items

    def get_virtual_item(self, token):
        row = self.rows.get(token)
        return dict(row) if row else None

    def rename_virtual_item_paths(self, old_dir, new_dir):
        # os.sep so the fake also works on Windows; production paths use "/".
        old_prefix = old_dir.rstrip("/\\") + os.sep
        new_prefix = new_dir.rstrip("/\\") + os.sep
        updated = 0
        for row in self.rows.values():
            path = row.get("strm_path") or ""
            if path.startswith(old_prefix):
                row["strm_path"] = new_prefix + path[len(old_prefix):]
                updated += 1
        return updated


@pytest.fixture
def media(tmp_path, monkeypatch):
    monkeypatch.setattr(sg, "MEDIA_PATH", str(tmp_path))
    monkeypatch.setitem(
        sys.modules, "catbox",
        SimpleNamespace(proxy_url=lambda token: f"http://mycelium/stream/{token}"),
    )
    monkeypatch.setitem(sys.modules, "media_servers", SimpleNamespace(mark=lambda path: None))
    # A fresh lock per test so a failure cannot leak a held lock into others.
    monkeypatch.setattr(sg, "_maintenance_lock", threading.Lock())
    return tmp_path


def _episode(root: Path, folder: str, stem: str, season: int = 1, episode: int = 1) -> str:
    return str(root / "series" / folder / f"Season {season:02d}" / f"{stem} S{season:02d}E{episode:02d}.strm")


def _series_dirs(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "series").iterdir() if p.is_dir())


def test_folder_renamed_after_snapshot_is_not_recreated(media, monkeypatch):
    old_path = _episode(media, "Hell's Kitchen (US)", "Hell's Kitchen (US)")
    Path(old_path).parent.mkdir(parents=True)
    Path(old_path).write_text("http://mycelium/stream/tok1", encoding="utf-8")
    old_folder = media / "series" / "Hell's Kitchen (US)"
    new_folder = media / "series" / "Hell's Kitchen"

    def cleanup_renames_folder(db):
        # What cleanup.rename_messy_series_folders does, landing after the
        # rebuild has taken its snapshot.
        old_folder.rename(new_folder)
        db.rename_virtual_item_paths(str(old_folder), str(new_folder))

    fake = FakeDb(
        [{"token": "tok1", "strm_path": old_path, "imdb_id": "tt0437005"}],
        on_snapshot=cleanup_renames_folder,
    )
    monkeypatch.setattr(sg, "db", fake)

    assert sg._run_once_catbox() == 0

    assert _series_dirs(media) == ["Hell's Kitchen"]
    assert not Path(old_path).exists()
    moved = new_folder / "Season 01" / "Hell's Kitchen (US) S01E01.strm"
    assert fake.rows["tok1"]["strm_path"] == str(moved)
    assert moved.read_text(encoding="utf-8") == "http://mycelium/stream/tok1"


def test_row_rebound_elsewhere_is_not_recreated_at_snapshot_path(media, monkeypatch):
    old_path = _episode(media, "Megazone 23 Part III", "Megazone 23 Part III")
    new_path = _episode(
        media, "Megazone 23 III - Part 1 - The Awakening of Eve (1989)", "Megazone 23 Part III",
    )
    Path(new_path).parent.mkdir(parents=True)
    Path(new_path).write_text("http://mycelium/stream/tok2", encoding="utf-8")
    fake = FakeDb(
        [{"token": "tok2", "strm_path": new_path, "imdb_id": "tt0160523"}],
        snapshot=[{"token": "tok2", "strm_path": old_path, "imdb_id": "tt0160523"}],
    )
    monkeypatch.setattr(sg, "db", fake)

    assert sg._run_once_catbox() == 0
    assert not Path(old_path).parent.parent.exists()


def test_deleted_row_is_not_recreated(media, monkeypatch):
    path = _episode(media, "Gone", "Gone")
    fake = FakeDb([], snapshot=[{"token": "tok3", "strm_path": path, "imdb_id": None}])
    monkeypatch.setattr(sg, "db", fake)

    assert sg._run_once_catbox() == 0
    assert not (media / "series" / "Gone").exists()


def test_genuinely_missing_file_is_still_recreated(media, monkeypatch):
    path = _episode(media, "Severance", "Severance", season=2, episode=3)
    fake = FakeDb([{"token": "tok4", "strm_path": path, "imdb_id": "tt11280740"}])
    monkeypatch.setattr(sg, "db", fake)

    assert sg._run_once_catbox() == 1
    assert Path(path).read_text(encoding="utf-8") == "http://mycelium/stream/tok4"
    assert not sg._maintenance_lock.locked()


def test_rebuild_defers_while_maintenance_holds_the_lock(media, monkeypatch):
    path = _episode(media, "Severance", "Severance")
    fake = FakeDb([{"token": "tok5", "strm_path": path, "imdb_id": "tt11280740"}])
    monkeypatch.setattr(sg, "db", fake)

    assert sg._maintenance_lock.acquire(blocking=False)
    try:
        assert sg._run_once_catbox() == 0
        assert not Path(path).exists()
        # The rebuild must not release a lock it does not own.
        assert sg._maintenance_lock.locked()
    finally:
        sg._maintenance_lock.release()

    # The next run, after maintenance finished, restores the file.
    assert sg._run_once_catbox() == 1
    assert Path(path).exists()


def test_caller_holding_the_lock_can_rebuild(media, monkeypatch):
    path = _episode(media, "Severance", "Severance")
    fake = FakeDb([{"token": "tok6", "strm_path": path, "imdb_id": "tt11280740"}])
    monkeypatch.setattr(sg, "db", fake)

    assert sg._maintenance_lock.acquire(blocking=False)
    try:
        assert sg._run_once_catbox(maintenance_held=True) == 1
        assert sg._maintenance_lock.locked()
    finally:
        sg._maintenance_lock.release()
    assert Path(path).exists()


def test_run_and_refresh_passes_maintenance_flag(monkeypatch):
    seen = []
    monkeypatch.setattr(sg.settings, "get", lambda key, default=None: key == "CATBOX_MODE")
    monkeypatch.setattr(sg, "_run_once_catbox", lambda maintenance_held=False: seen.append(maintenance_held) or 0)
    monkeypatch.setattr(sg, "_self_heal_sample", lambda: None)
    monkeypatch.setitem(sys.modules, "nfo_generator", SimpleNamespace(generate_all=lambda **kw: None))

    sg.run_and_refresh()
    sg.run_and_refresh(maintenance_held=True)

    assert seen == [False, True]
