"""catbox._prepare_next_episode must not register fakes of unaired episodes.

Regression for 2026-09-20: watching South Park S29E01 registered
"South Park S29E02 1080p WEB H264-MeGusta.exe" ten days before E02 aired and
marked the episode found, so the real release would never have been searched.
"""
import sys
import types

import catbox
import monitor  # noqa: F401  (imported for real before the stubs below go in)
from torrentio import TorrentioStream


def _stream(title, info_hash):
    return TorrentioStream(name=title, title=title, info_hash=info_hash,
                           quality="1080p", seeders=1, size_gb=1.0,
                           is_season_pack=False, source="test")


def _wire(monkeypatch, wanted, streams, episode_title):
    calls = {"search": 0, "written": []}

    def search(*_args, **_kwargs):
        calls["search"] += 1
        return streams

    monkeypatch.setattr(catbox, "db", types.SimpleNamespace(
        get_wanted_episode=lambda _imdb, season, episode: (
            wanted if (season, episode) == (29, 2) else None),
        get_virtual_item_by_episode=lambda *_args: None,
        mark_candidate_cached=lambda *_args: None,
        mark_episode_status=lambda *_args: None,
    ))
    monkeypatch.setitem(sys.modules, "search_engine", types.SimpleNamespace(
        content_key=lambda imdb, season, episode: f"{imdb}:S{season:02d}E{episode:02d}",
        search_candidates=search,
        cache_map=lambda _key, items: {"torbox": {item.info_hash for item in items}},
        mark_selected=lambda *_args: None,
        reject=lambda *_args: None,
    ))
    monkeypatch.setitem(sys.modules, "strm_generator", types.SimpleNamespace(
        _base_series_title=lambda title: title,
        _canonical_series_folder=lambda *_args, **_kwargs: None,
        create_lazy_episode_strm=lambda info_hash, *_args, **_kwargs: (
            calls["written"].append(info_hash) or True),
    ))
    monkeypatch.setitem(sys.modules, "release_sanity", types.SimpleNamespace(
        filter_cached=lambda items, **_kwargs: items,
        check_hash=lambda *_args, **_kwargs: (True, None),
    ))
    monkeypatch.setitem(sys.modules, "processor", types.SimpleNamespace(
        _episode_search_override=lambda *_args: {"episode_title": episode_title},
    ))
    return calls


def test_unaired_next_episode_is_not_searched(monkeypatch):
    fake = _stream("South Park S29E02 1080p WEB H264-MeGusta.exe", "e" * 40)
    calls = _wire(monkeypatch, {"status": "not_aired", "air_date": "2999-01-01"},
                  [fake], "Episode 2")

    catbox._prepare_next_episode("tt0121955", "South Park", 29, 1)

    assert calls["search"] == 0
    assert calls["written"] == []


def test_aired_next_episode_needs_its_title_when_no_release_carries_it(monkeypatch):
    titleless = _stream("WEB avc 1000.92 MB Knaben", "b" * 40)
    calls = _wire(monkeypatch, {"status": "wanted", "air_date": "2000-01-01"},
                  [titleless], "Pajama Party")

    catbox._prepare_next_episode("tt0121955", "South Park", 29, 1)

    assert calls["search"] == 1
    assert calls["written"] == []


def test_aired_next_episode_with_its_title_is_prepared(monkeypatch):
    real = _stream("South Park S29E02 Pajama Party 1080p AMZN WEB-DL H 264-NTb", "a" * 40)
    calls = _wire(monkeypatch, {"status": "wanted", "air_date": "2000-01-01"},
                  [real], "Pajama Party")

    catbox._prepare_next_episode("tt0121955", "South Park", 29, 1)

    assert calls["written"] == ["a" * 40]
