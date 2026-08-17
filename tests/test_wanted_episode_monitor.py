import monitor


def test_episode_exists_in_zero_padded_season_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "MEDIA_PATH", str(tmp_path))
    monitor.db.get_virtual_item_by_episode.return_value = None
    episode = tmp_path / "series" / "Example Show" / "Season 01" / "Example Show S01E05.strm"
    episode.parent.mkdir(parents=True)
    episode.write_text("https://example.invalid/stream", encoding="utf-8")

    assert monitor.strm_exists_episode("Example Show", 1, 5, imdb_id="tt1234567")


def test_virtual_episode_is_authoritative_without_matching_folder(monkeypatch):
    monitor.db.get_virtual_item_by_episode.return_value = {"token": "already-registered"}

    assert monitor.strm_exists_episode("Renamed Show", 1, 2, imdb_id="tt7654321")
