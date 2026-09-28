"""Regression tests for repair_expired_strms pass 1 and same-token duplicates.

On 2026-09-27 the WS26 quarantine moved the .strm out of the leftover folder
"Example Film (2025)", whose item is bound to the renamed sibling
"Example Film (2025) {imdb-tt0000042}". The NFO and artwork stayed behind.
Six hours later pass 1 saw a folder with an NFO but no .strm and wrote a new
.strm there, recreating the duplicate. The name-based sibling check did not
match because the renamed folder carries an "{imdb-...}" suffix.
"""
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import strm_generator as sg

IMDB = "tt0000042"
TOKEN = "0123456789abcdef"


class FakeDb:
    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]

    def get_virtual_items_by_imdb(self, imdb_id, media_type=None):
        return [dict(r) for r in self.rows if r["imdb_id"] == imdb_id
                and (media_type is None or r["media_type"] == media_type)]

    def get_virtual_item(self, token):
        return next((dict(r) for r in self.rows if r["token"] == token), None)


@pytest.fixture
def media(tmp_path, monkeypatch):
    monkeypatch.setattr(sg, "MEDIA_PATH", str(tmp_path))
    monkeypatch.setitem(
        sys.modules, "catbox",
        SimpleNamespace(
            catbox_host=lambda: "http://mycelium",
            proxy_url=lambda token: f"http://mycelium/stream/{token}",
        ),
    )
    monkeypatch.setattr(sg, "_maintenance_lock", threading.Lock())
    (tmp_path / "movies").mkdir()
    return tmp_path


def _movie(root: Path, folder: str, with_strm: bool) -> Path:
    d = root / "movies" / folder
    d.mkdir()
    (d / f"{folder}.nfo").write_text(f"<movie><imdbid>{IMDB}</imdbid></movie>", encoding="utf-8")
    (d / "poster.jpg").write_bytes(b"jpg")
    if with_strm:
        (d / f"{folder}.strm").write_text(f"http://mycelium/stream/{TOKEN}", encoding="utf-8")
    return d


def test_leftover_duplicate_of_bound_folder_is_not_recreated(media, monkeypatch, caplog):
    bound = _movie(media, f"Example Film (2025) {{imdb-{IMDB}}}", with_strm=True)
    leftover = _movie(media, "Example Film (2025)", with_strm=False)
    monkeypatch.setattr(sg, "db", FakeDb([{
        "token": TOKEN, "imdb_id": IMDB, "media_type": "movie",
        "strm_path": str(bound / f"{bound.name}.strm"),
    }]))
    caplog.set_level("INFO")
    result = sg.repair_expired_strms("movie")
    assert not list(leftover.glob("*.strm")), "duplicate folder got a new .strm"
    assert result["relinked"] == 0 and result["requeued"] == 0
    assert "not recreating Example Film (2025)" in caplog.text
    # The bound folder is untouched and still counted as healthy by pass 2.
    assert (bound / f"{bound.name}.strm").read_text(encoding="utf-8").endswith(TOKEN)
    assert result["ok"] == 1


def test_folder_bound_to_itself_is_still_relinked(media, monkeypatch):
    own = _movie(media, "Solo Film (2024)", with_strm=False)
    monkeypatch.setattr(sg, "db", FakeDb([{
        "token": TOKEN, "imdb_id": IMDB, "media_type": "movie",
        "strm_path": str(own / f"{own.name}.strm"),
    }]))
    result = sg.repair_expired_strms("movie")
    assert result["relinked"] == 1
    assert (own / f"{own.name}.strm").read_text(encoding="utf-8").strip() == f"http://mycelium/stream/{TOKEN}"


def test_bound_path_missing_elsewhere_still_relinks_here(media, monkeypatch):
    # If the bound folder's .strm is gone too, this is not a duplicate case:
    # keep the original behaviour and relink so the movie stays playable.
    here = _movie(media, "Lost Film (2020)", with_strm=False)
    gone = media / "movies" / "Lost Film (2020) {imdb-tt0000001}" / "Lost Film.strm"
    monkeypatch.setattr(sg, "db", FakeDb([{
        "token": TOKEN, "imdb_id": IMDB, "media_type": "movie", "strm_path": str(gone),
    }]))
    result = sg.repair_expired_strms("movie")
    assert result["relinked"] == 1
    assert list(here.glob("*.strm"))
