"""Offline tests for numbering._index (TheXEM map parsing).

The live TheXEM client and the matcher integration are validated end-to-end
against real shows (Naruto Shippuden S13E15 -> absolute 275 -> the '- 275'
file in the grabbed pack); those need network so are not unit-tested here.
"""
import os
import sys

os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numbering  # noqa: E402  (import after sys.path/env setup)


def test_index_keeps_tvdb_absolute_only():
    entries = [
        {"tvdb": {"season": 13, "episode": 15, "absolute": 275},
         "scene": {"season": 1, "episode": 1, "absolute": 1}},
        {"tvdb": {"season": 20, "episode": 15, "absolute": 446}},
        {"scene": {"season": 1, "episode": 1, "absolute": 9}},   # no tvdb -> ignored
    ]
    assert numbering._index(entries) == {(13, 15): 275, (20, 15): 446}


def test_index_handles_empty():
    assert numbering._index(None) == {}
    assert numbering._index([]) == {}


def test_tmdb_absolute_uses_tmdb_season_counts(monkeypatch):
    numbering._tmdb_absolute.cache_clear()

    class FakeTmdb:
        @staticmethod
        def find_by_imdb(_imdb_id, kind="tv"):
            assert kind == "tv"
            return 30984

        @staticmethod
        def get_season_episodes(_tmdb_id, season):
            count = {1: 366, 2: 53}[season]
            return [{"episode_number": number} for number in range(1, count + 1)]

    monkeypatch.setitem(sys.modules, "tmdb", FakeTmdb)

    assert numbering._tmdb_absolute("tt0434665", 2, 46) == 412


def test_tmdb_absolute_rejects_episode_missing_from_current_metadata(monkeypatch):
    numbering._tmdb_absolute.cache_clear()

    class FakeTmdb:
        @staticmethod
        def find_by_imdb(_imdb_id, kind="tv"):
            return 1

        @staticmethod
        def get_season_episodes(_tmdb_id, _season):
            return [{"episode_number": 1}, {"episode_number": 2}]

    monkeypatch.setitem(sys.modules, "tmdb", FakeTmdb)

    assert numbering._tmdb_absolute("tt0000001", 1, 3) is None
