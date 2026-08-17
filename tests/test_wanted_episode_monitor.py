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


def test_add_series_canonicalizes_placeholder_title_before_wanted_sync(monkeypatch):
    import strm_generator

    monitor.db.upsert_monitored_series.reset_mock()
    monkeypatch.setattr(monitor.tmdb, "find_by_imdb", lambda *_args, **_kwargs: 71795)
    monkeypatch.setattr(
        strm_generator,
        "_canonical_series_folder",
        lambda _imdb_id: "Criminal Minds (2017)",
    )
    sync_calls = []
    monkeypatch.setattr(
        monitor,
        "_sync_wanted",
        lambda imdb_id, tmdb_id, title, seasons: sync_calls.append(
            (imdb_id, tmdb_id, title, seasons)
        ),
    )

    monitor.add_series("tt6568694", "tt6568694", [1])

    monitor.db.upsert_monitored_series.assert_called_once_with(
        "tt6568694", 71795, "Criminal Minds (2017)", [1]
    )
    assert sync_calls == [("tt6568694", 71795, "Criminal Minds (2017)", [1])]
