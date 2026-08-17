from pathlib import Path
from types import SimpleNamespace

import cleanup


def _write_nfo(folder: Path, title: str, year: int, imdb_id: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{folder.name}.nfo").write_text(
        f"<movie><title>{title}</title><year>{year}</year>"
        f"<uniqueid type=\"imdb\">{imdb_id}</uniqueid></movie>",
        encoding="utf-8",
    )


def test_movie_rename_skips_folder_with_mixed_identity(monkeypatch, tmp_path):
    movies = tmp_path / "movies"
    folder = movies / "Breakdown Wrong Folder"
    _write_nfo(folder, "Breakdown", 1997, "tt0118771")
    (folder / "Breakdown 1975 (2025).strm").write_text("stream", encoding="utf-8")
    fake_db = SimpleNamespace(
        get_virtual_item_imdb_ids_under_path=lambda path: {"tt38985973"},
    )
    monkeypatch.setattr(cleanup, "db", fake_db)
    monkeypatch.setattr(cleanup, "MEDIA_PATH", str(tmp_path))

    renamed = cleanup.rename_messy_movie_folders()

    assert renamed == 0
    assert folder.is_dir()
    assert not (movies / "Breakdown (1997)").exists()


def test_movie_merge_excludes_folder_with_mixed_identity(monkeypatch, tmp_path):
    movies = tmp_path / "movies"
    first = movies / "Breakdown (1997)"
    mixed = movies / "Breakdown Alternate (1997)"
    _write_nfo(first, "Breakdown", 1997, "tt0118771")
    _write_nfo(mixed, "Breakdown", 1997, "tt0118771")
    (first / "Breakdown (1997).strm").write_text("first", encoding="utf-8")
    (mixed / "Breakdown 1975 (2025).strm").write_text("mixed", encoding="utf-8")

    def identities(path):
        return {"tt38985973"} if path == str(mixed) else {"tt0118771"}

    fake_db = SimpleNamespace(
        get_virtual_item_imdb_ids_under_path=identities,
        get_media_items=lambda media_type=None: [],
    )
    monkeypatch.setattr(cleanup, "db", fake_db)
    monkeypatch.setattr(cleanup, "MEDIA_PATH", str(tmp_path))

    removed = cleanup.merge_movie_duplicates()

    assert removed == 0
    assert first.is_dir()
    assert mixed.is_dir()
