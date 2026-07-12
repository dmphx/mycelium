"""Tests for the mislabeled-pack guard (release_sanity).

Proves the concrete production incident: a single-MOVIE request must never latch
onto a 525 GB "Star Trek Complete Series" pack that shares the imdb_id, while a
normal 1-8 GB single-file movie release is still accepted. Also covers the
episode-pack path and the pure name/size heuristic feeding rank_streams.
"""
import os
import sys
import types

import pytest

os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import release_sanity
import torrentio
from torrentio import TorrentioStream

GB = 1024 ** 3


@pytest.fixture(autouse=True)
def _deterministic_settings(monkeypatch):
    """Force the settings overlay to return config.py defaults for the duration of
    each test, then auto-restore. Other test modules swap sys.modules["settings"]
    between a MagicMock and the real module during collection; without pinning it
    here, release_sanity._cfg() / torrentio.rank_streams() would read whatever
    junk that leaves behind (and our monkeypatch would leak into their tests).
    monkeypatch.setitem restores the previous object after the test."""
    stub = types.SimpleNamespace(get=lambda key, default=None: default)
    monkeypatch.setitem(sys.modules, "settings", stub)
    release_sanity._pack_re_cache.clear()
    yield
    release_sanity._pack_re_cache.clear()


# ── the real incident: 525GB "Star Trek Complete Series" pack ─────────────────

def _star_trek_pack_entry(with_files: bool = True) -> dict:
    entry = {
        "name": "Star Trek Complete Series in Stardate Watch Order - 1080p x265",
        "size": 525 * GB,
        "files": [],
    }
    if with_files:
        entry["files"] = [
            {"name": f"Star Trek TOS - S01E{e:02d} - 1080p x265.mkv", "size": 1500 * 1024 * 1024}
            for e in range(1, 30)
        ]
    return entry


def test_movie_rejects_complete_series_pack_with_file_list():
    # 29 episode-tagged files under one movie imdb -> obvious series pack.
    reason = release_sanity.verify_entry(_star_trek_pack_entry(with_files=True), "movie")
    assert reason is not None
    assert "episode-tagged" in reason


def test_movie_rejects_oversized_pack_without_file_list():
    # Single-file shape (no `files`): the 525GB top-level size alone disqualifies it.
    reason = release_sanity.verify_entry(_star_trek_pack_entry(with_files=False), "movie")
    assert reason is not None
    assert "cap" in reason and "525" in reason


# ── a normal single movie must still pass ─────────────────────────────────────

def test_normal_single_file_movie_accepted():
    entry = {"name": "The Wheel 2019 1080p WEB-DL x264.mkv", "size": 4 * GB, "files": []}
    assert release_sanity.verify_entry(entry, "movie") is None


def test_normal_multi_file_movie_accepted_ignoring_sample():
    entry = {
        "name": "The.Wheel.2019.1080p.WEB-DL.x264-GRP",
        "size": 5 * GB,
        "files": [
            {"name": "The.Wheel.2019.1080p.WEB-DL.x264-GRP.mkv", "size": 5 * GB},
            {"name": "sample.mkv", "size": 40 * 1024 * 1024},
            {"name": "rarbg.txt", "size": 1024},
        ],
    }
    assert release_sanity.verify_entry(entry, "movie") is None


def test_small_1gb_movie_accepted():
    entry = {"name": "Indie Film 2021 1080p WEB-DL.mkv", "size": 1 * GB, "files": []}
    assert release_sanity.verify_entry(entry, "movie") is None


# ── pure name/size heuristic (rank-time layer) ────────────────────────────────

def test_name_size_reject_flags_pack_name():
    assert release_sanity.movie_name_size_reject("Star Trek Complete Series 1080p x265", 12.0)


def test_name_size_reject_flags_oversized():
    # Above the 100GB movie cap -> flagged as pack/collection by size.
    r = release_sanity.movie_name_size_reject("Some Movie 2020 2160p REMUX", 150.0)
    assert r and "cap" in r


def test_name_size_spares_large_4k_single_movie():
    # A single 4K movie legitimately in the 25-90GB range must NOT be flagged.
    for name, gb in [
        ("Witness.1985.2160p.WEB-DL.x265.HDR.TrueHD", 25.3),
        ("Sahara.2005.2160p.WEB-DL", 58.7),
        ("The.Man.Who.Shot.Liberty.Valance.1962.2160p.UHD.BluRay.x265.HDR", 28.0),
    ]:
        assert release_sanity.movie_name_size_reject(name, gb) is None, name


def test_name_filter_catches_named_collections_under_size_cap():
    # Collections whose size (here small) wouldn't trip the cap must be caught by
    # their unambiguous multi-title name.
    for name in [
        "Akira Kurosawa Boxset",
        "101 Horror Movies Mega Pack Vol 12",
        "The Fast and The Furious - Complete Collection (2001-2021)",
    ]:
        assert release_sanity.movie_name_size_reject(name, 30.0), name


def test_name_size_reject_passes_normal_release():
    assert release_sanity.movie_name_size_reject("The Wheel 2019 1080p WEB-DL x264", 4.0) is None


def test_name_filter_spares_legitimate_single_films():
    # Pack-ish words but genuine single films -> must NOT be flagged.
    for name in (
        "A Complete Unknown 2024 1080p WEB-DL x264",
        "Season of the Witch 2011 1080p BluRay x264",
        "Seven Samurai 1954 Criterion Collection 1080p BluRay",
        "Night at the Museum 2006 1080p BluRay",
    ):
        assert release_sanity.movie_name_size_reject(name, 6.0) is None, name


# ── integration through torrentio.rank_streams(media_kind="movie") ────────────

def _stream(name, quality, size_gb, hash_hex):
    return TorrentioStream(
        name=name, title=name, info_hash=hash_hex, quality=quality,
        seeders=10, size_gb=size_gb, is_season_pack=False,
    )


def test_rank_streams_movie_drops_pack_keeps_real():
    real = _stream("The.Wheel.2019.1080p.WEB-DL.x264", "1080p", 4.0, "a" * 40)
    pack = _stream("Star Trek Complete Series Stardate 1080p x265", "1080p", 525.0, "b" * 40)
    ranked = torrentio.rank_streams([real, pack], override={"runtime_minutes": 90},
                                    media_kind="movie")
    names = [s.name for s in ranked]
    assert "The.Wheel.2019.1080p.WEB-DL.x264" in names
    assert all("Complete Series" not in n for n in names)


def test_rank_streams_movie_all_packs_returns_empty():
    # No real single-movie release available -> empty (movie stays 'wanted'),
    # never falls back to allowing the pack.
    pack1 = _stream("Show Complete Series 1080p", "1080p", 300.0, "c" * 40)
    pack2 = _stream("Show Seasons 1-5 1080p", "1080p", 120.0, "d" * 40)
    ranked = torrentio.rank_streams([pack1, pack2], override={"runtime_minutes": 90},
                                    media_kind="movie")
    assert ranked == []


# ── episode + season-pack paths ───────────────────────────────────────────────

def test_episode_pack_without_target_file_rejected():
    entry = {
        "name": "Show Complete Series 1080p",
        "size": 200 * GB,
        "files": [{"name": f"Show S01E{e:02d} 1080p.mkv", "size": GB} for e in range(1, 11)],
    }
    # Ask for S02E05, absent from this S01 pack.
    reason = release_sanity.verify_entry(entry, "episode", season=2, episode=5)
    assert reason and "identifiable" in reason


def test_episode_pack_with_target_file_accepted():
    entry = {
        "name": "Show S01 1080p WEB-DL",
        "size": 20 * GB,
        "files": [{"name": f"Show S01E{e:02d} 1080p.mkv", "size": GB} for e in range(1, 11)],
    }
    assert release_sanity.verify_entry(entry, "episode", season=1, episode=5) is None


def test_episode_single_file_wrong_episode_rejected():
    entry = {"name": "Show S03E09 1080p WEB-DL.mkv", "size": 2 * GB, "files": []}
    reason = release_sanity.verify_entry(entry, "episode", season=3, episode=8)
    assert reason and "not S03E08" in reason


def test_season_pack_rejected_only_when_no_video():
    good = {"name": "Show S01 1080p", "size": 20 * GB,
            "files": [{"name": "Show S01E01 1080p.mkv", "size": GB}]}
    assert release_sanity.verify_entry(good, "season_pack", season=1) is None
    empty = {"name": "Show S01 1080p", "size": 20 * GB,
             "files": [{"name": "readme.txt", "size": 1000}]}
    assert release_sanity.verify_entry(empty, "season_pack", season=1)


def test_uncached_or_empty_entry_fails_open():
    # No listing -> nothing to verify -> pass (rank-time name/size is the guard).
    assert release_sanity.verify_entry(None, "movie") is None
    assert release_sanity.verify_entry({}, "movie") is None


# ── files=0 conservative behavior (TorBox returns no per-file listing) ─────────
# checkcached often reports only a top-level name/size (files=0). A single-MOVIE
# request must still catch an oversized/pack-named hash by that alone, but a
# legit cached season pack (whose name is a folder, no extension) must NOT be
# rejected for an episode or season-pack request  -  playback resolves the real
# file via find_by_id. This is the "Human Target Complete TV Series" regression.

def test_episode_from_cached_pack_no_filelist_accepted():
    entry = {
        "name": "HUMAN TARGET (2010-2011) - Complete TV Series, Season 1-2 S01-S02 - 1080p BluRay x264",
        "size": 27 * GB,
        "files": [],
    }
    # files=0 + pack name, asking for a specific episode -> must NOT be rejected.
    assert release_sanity.verify_entry(entry, "episode", season=1, episode=11) is None


def test_season_pack_no_filelist_accepted():
    entry = {
        "name": "HUMAN TARGET Complete Series S01-S02 1080p BluRay x264",
        "size": 27 * GB,
        "files": [],
    }
    assert release_sanity.verify_entry(entry, "season_pack", season=1) is None


def test_movie_still_caught_when_no_filelist():
    # Same shape as above but a MOVIE request: the pack NAME must still get it
    # rejected even though 27GB is under the size cap (this is the 2099 -> pack
    # class; the real Star Trek hash is 525GB so size catches that one too).
    entry = {
        "name": "HUMAN TARGET Complete Series S01-S02 1080p BluRay x264",
        "size": 27 * GB,
        "files": [],
    }
    assert release_sanity.verify_entry(entry, "movie") is not None
