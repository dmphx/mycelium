import monitor
from torrentio import TorrentioStream


def _stream(title, *, usenet=True):
    return TorrentioStream(
        name=title,
        title=title,
        info_hash=("a" if usenet else "b") * 40,
        quality="1080p",
        seeders=1,
        size_gb=1.0,
        is_season_pack=False,
        source="test",
        protocol="usenet" if usenet else "torrent",
        nzb_url="https://example.invalid/item.nzb" if usenet else None,
    )


def test_ambiguous_nzb_requires_current_episode_title_after_sanity_failure():
    classic = _stream("Bleach S02E01 1080p BluRay")
    current = _stream("Bleach S02E01 The Blood Warfare 1080p WEB-DL")

    assert monitor._safe_episode_nzbs(
        [classic, current], "The Blood Warfare", True, "Bleach S02E01"
    ) == [current]


def test_nzb_numbering_remains_available_without_a_sanity_conflict():
    numbered = _stream("Example Show S03E04 1080p WEB-DL")

    assert monitor._safe_episode_nzbs(
        [numbered], "An Episode Title", False, "Example Show S03E04"
    ) == [numbered]


def test_number_collision_requires_title_when_no_candidate_has_current_title():
    classic = _stream("Bleach S02E01 1080p BluRay")

    assert monitor._episode_requires_title_verification(
        [classic], "The Blood Warfare", False
    )


def test_matching_current_title_resolves_number_collision_without_sanity_failure():
    current = _stream("Bleach S02E01 The Blood Warfare 1080p WEB-DL")

    assert not monitor._episode_requires_title_verification(
        [current], "The Blood Warfare", False
    )


def test_cached_fallback_without_current_title_is_removed_after_sanity_failure():
    generic = _stream("Bleach S02E20 1080p WEB-DL", usenet=False)
    current = _stream("Bleach S02E20 I AM THE EDGE 1080p WEB-DL", usenet=False)

    assert monitor._episode_title_verified_candidates(
        [generic, current], "I AM THE EDGE", True
    ) == [current]


def test_cached_fallbacks_remain_available_without_identity_ambiguity():
    generic = _stream("Example Show S03E04 1080p WEB-DL", usenet=False)

    assert monitor._episode_title_verified_candidates(
        [generic], "An Episode Title", False
    ) == [generic]


def test_episode_exists_in_zero_padded_season_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "MEDIA_PATH", str(tmp_path))
    monitor.db.get_virtual_item_by_episode.return_value = None
    episode = tmp_path / "series" / "Example Show" / "Season 01" / "Example Show S01E05.strm"
    episode.parent.mkdir(parents=True)
    episode.write_text("https://example.invalid/stream", encoding="utf-8")

    assert monitor.strm_exists_episode("Example Show", 1, 5, imdb_id="tt1234567")


import pytest


@pytest.mark.parametrize("present,asked,expected", [
    ("Bleach S01E100.strm", 10, False),   # used to match by substring
    ("Bleach S01E104.strm", 10, False),
    ("Bleach S01E1001.strm", 100, False),
    ("Bleach S01E10.strm", 10, True),
    ("Bleach S01E104.strm", 104, True),
    ("Bleach S01E1001.strm", 1001, True),
])
def test_episode_file_match_is_exact_not_a_prefix(tmp_path, monkeypatch, present, asked, expected):
    monkeypatch.setattr(monitor, "MEDIA_PATH", str(tmp_path))
    monitor.db.get_virtual_item_by_episode.return_value = None
    episode = tmp_path / "series" / "Bleach" / "Season 01" / present
    episode.parent.mkdir(parents=True)
    episode.write_text("https://example.invalid/stream", encoding="utf-8")

    assert monitor.strm_exists_episode("Bleach", 1, asked, imdb_id="tt0434665") is expected


def test_sync_puts_a_lost_episode_back_to_wanted(tmp_path, monkeypatch):
    """The collapse left E10 'found' beside a surviving E104; the next metadata
    sync must re-derive it as wanted, while the episode that exists stays found."""
    monkeypatch.setattr(monitor, "MEDIA_PATH", str(tmp_path))
    monitor.db.get_virtual_item_by_episode.return_value = None
    season = tmp_path / "series" / "Bleach" / "Season 01"
    season.mkdir(parents=True)
    (season / "Bleach S01E104.strm").write_text("https://example.invalid/stream", encoding="utf-8")
    monkeypatch.setattr(monitor.tmdb, "get_season_episodes", lambda *a: [
        {"episode_number": 10, "air_date": "2005-01-01"},
        {"episode_number": 104, "air_date": "2007-01-01"}])
    monitor.db.mark_episode_status.reset_mock()

    monitor._sync_wanted("tt0434665", 30984, "Bleach", [1])

    marks = {c.args[2]: c.args[3] for c in monitor.db.mark_episode_status.call_args_list}
    assert marks == {10: "wanted", 104: "found"}


def test_renumbered_episode_with_item_at_its_absolute_number_stays_found(monkeypatch):
    """episode_scheme's renumber moves the item to the absolute episode and marks
    that row found; the exact-key item lookup keeps it found."""
    monitor.db.get_virtual_item_by_episode.side_effect = (
        lambda imdb_id, season, episode: {"token": "renumbered"} if (season, episode) == (1, 1001) else None)
    try:
        assert monitor.strm_exists_episode("No Folder Show", 1, 1001, imdb_id="tt0388629") is True
        assert monitor.strm_exists_episode("No Folder Show", 1, 100, imdb_id="tt0388629") is False
    finally:
        monitor.db.get_virtual_item_by_episode.side_effect = None


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


def test_wanted_search_promotes_aired_rows_before_reconciling(monkeypatch):
    monitor.db.reset_mock()
    monitor.db.promote_aired_episodes.return_value = 3
    monitor.db.reconcile_wanted_episodes.return_value = 0
    monitor.db.get_fresh_wanted_episodes.return_value = []
    monkeypatch.setattr(monitor, "_TODAY", lambda: "2026-09-30")

    monitor._search_wanted_episodes_locked(fresh_only=True)

    names = [call[0] for call in monitor.db.method_calls]
    assert names.index("promote_aired_episodes") < names.index("reconcile_wanted_episodes")
    monitor.db.promote_aired_episodes.assert_called_once_with("2026-09-30")


def test_sync_marks_episode_rows_removed_from_current_metadata(monkeypatch):
    monitor.db.reset_mock()
    monkeypatch.setattr(
        monitor.tmdb,
        "get_season_episodes",
        lambda *_args, **_kwargs: [
            {"episode_number": 1, "air_date": "2024-01-01"},
            {"episode_number": 2, "air_date": "2024-01-08"},
        ],
    )
    monkeypatch.setattr(monitor, "strm_exists_episode", lambda *_args, **_kwargs: False)
    monitor.db.mark_metadata_removed_episodes.return_value = 2

    monitor._sync_wanted("tt15599734", 291212, "Murder Drones", [1])

    monitor.db.mark_metadata_removed_episodes.assert_called_once_with(
        "tt15599734", 1, [1, 2]
    )
