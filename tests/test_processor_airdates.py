import sys
from types import SimpleNamespace

import processor


def test_partial_season_returns_only_released_episode_numbers(monkeypatch):
    fake_tmdb = SimpleNamespace(
        find_by_imdb=lambda *_args, **_kwargs: 615,
        get_season_episodes=lambda *_args, **_kwargs: [
            {"episode_number": 1, "air_date": "2026-08-03"},
            {"episode_number": 2, "air_date": "2026-08-10"},
            {"episode_number": 3, "air_date": "2999-09-07"},
        ],
    )
    monkeypatch.setitem(sys.modules, "tmdb", fake_tmdb)

    released, complete = processor._season_release_state("tt0149460", 11)

    assert released == [1, 2]
    assert complete is False


def test_complete_season_allows_every_episode(monkeypatch):
    fake_tmdb = SimpleNamespace(
        find_by_imdb=lambda *_args, **_kwargs: 615,
        get_season_episodes=lambda *_args, **_kwargs: [
            {"episode_number": 1, "air_date": "2023-07-24"},
            {"episode_number": 2, "air_date": "2023-07-31"},
        ],
    )
    monkeypatch.setitem(sys.modules, "tmdb", fake_tmdb)

    released, complete = processor._season_release_state("tt0149460", 8)

    assert released == [1, 2]
    assert complete is True


def test_undated_episode_keeps_season_pack_blocked(monkeypatch):
    fake_tmdb = SimpleNamespace(
        find_by_imdb=lambda *_args, **_kwargs: 615,
        get_season_episodes=lambda *_args, **_kwargs: [
            {"episode_number": 1, "air_date": "2026-08-03"},
            {"episode_number": 2, "air_date": None},
        ],
    )
    monkeypatch.setitem(sys.modules, "tmdb", fake_tmdb)

    released, complete = processor._season_release_state("tt0149460", 11)

    assert released == [1]
    assert complete is False
