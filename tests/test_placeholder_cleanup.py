from pathlib import Path

import placeholder_cleanup as pc


class FakeDb:
    def __init__(self, ids, monitored=None):
        self.ids = ids
        self.monitored = monitored or []

    def get_virtual_item_imdb_ids_under_path(self, path):
        return self.ids

    def get_all_monitored_series(self):
        return self.monitored


def _series(root: Path, name: str, imdb: str, token: str = "abc") -> Path:
    folder = root / "series" / name
    season = folder / "Season 01"
    season.mkdir(parents=True)
    (folder / "tvshow.nfo").write_text(
        f'<tvshow><title>{name}</title><uniqueid type="imdb">{imdb}</uniqueid></tvshow>'
    )
    (season / f"{name} S01E01.strm").write_text(f"http://example/stream/{token}")
    return folder


def test_discover_accepts_confirmed_placeholder(monkeypatch, tmp_path):
    media = tmp_path / "media"
    spore = tmp_path / "spore"
    _series(media, "tmdb1434", "tt0182576")
    monkeypatch.setattr(pc, "MEDIA_PATH", str(media))
    monkeypatch.setattr(pc, "SPORE_MEDIA_PATH", str(spore))
    monkeypatch.setattr(pc, "db", FakeDb({"tt0182576"}))
    monkeypatch.setattr(pc.strm_generator, "_canonical_series_folder", lambda imdb: "Family Guy")

    plans = pc.discover()

    assert len(plans) == 1
    assert plans[0]["target"].name == "Family Guy"
    assert plans[0]["conflicts"] == []


def test_discover_holds_different_episode_content(monkeypatch, tmp_path):
    media = tmp_path / "media"
    spore = tmp_path / "spore"
    _series(media, "tmdb255752", "tt32499579", token="old")
    _series(media, "Haunted Hotel", "tt32499579", token="new")
    monkeypatch.setattr(pc, "MEDIA_PATH", str(media))
    monkeypatch.setattr(pc, "SPORE_MEDIA_PATH", str(spore))
    monkeypatch.setattr(pc, "db", FakeDb({"tt32499579"}))
    monkeypatch.setattr(pc.strm_generator, "_canonical_series_folder", lambda imdb: "Haunted Hotel")

    plans = pc.discover()

    assert len(plans) == 1
    assert any("episode conflict" in reason for reason in plans[0]["conflicts"])


def test_dry_run_never_mutates(monkeypatch, tmp_path):
    media = tmp_path / "media"
    spore = tmp_path / "spore"
    source = _series(media, "tmdb1434", "tt0182576")
    monkeypatch.setattr(pc, "MEDIA_PATH", str(media))
    monkeypatch.setattr(pc, "SPORE_MEDIA_PATH", str(spore))
    monkeypatch.setattr(pc, "db", FakeDb({"tt0182576"}))
    monkeypatch.setattr(pc.strm_generator, "_canonical_series_folder", lambda imdb: "Family Guy")

    result = pc.cleanup(dry_run=True)

    assert result["eligible"] == 1
    assert result["migrated"] == 0
    assert source.exists()
    assert not (media / "series" / "Family Guy").exists()


def test_discover_repairs_placeholder_database_title(monkeypatch, tmp_path):
    media = tmp_path / "media"
    spore = tmp_path / "spore"
    folder = _series(media, "Family Guy", "tt0182576")
    fake_db = FakeDb(
        {"tt0182576"},
        [{"title": "tmdb:1434", "imdb_id": "tt0182576"}],
    )
    monkeypatch.setattr(pc, "MEDIA_PATH", str(media))
    monkeypatch.setattr(pc, "SPORE_MEDIA_PATH", str(spore))
    monkeypatch.setattr(pc, "db", fake_db)
    monkeypatch.setattr(pc.strm_generator, "_canonical_series_folder", lambda imdb: "Family Guy")

    plans = pc.discover()

    assert len(plans) == 1
    assert plans[0]["title_only"] is True
    assert plans[0]["source"] == folder
    assert plans[0]["conflicts"] == []


def test_discover_holds_title_only_repair_without_canonical_folder(monkeypatch, tmp_path):
    media = tmp_path / "media"
    spore = tmp_path / "spore"
    (media / "series").mkdir(parents=True)
    fake_db = FakeDb(
        set(),
        [{"title": "tmdb:1434", "imdb_id": "tt0182576"}],
    )
    monkeypatch.setattr(pc, "MEDIA_PATH", str(media))
    monkeypatch.setattr(pc, "SPORE_MEDIA_PATH", str(spore))
    monkeypatch.setattr(pc, "db", fake_db)
    monkeypatch.setattr(pc.strm_generator, "_canonical_series_folder", lambda imdb: "Family Guy")

    plans = pc.discover()

    assert len(plans) == 1
    assert plans[0]["title_only"] is True
    assert plans[0]["conflicts"] == ["canonical folder unavailable"]
