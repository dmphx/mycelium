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


def test_partial_season_pack_rejected_before_registering_missing_episodes(monkeypatch):
    monkeypatch.setitem(sys.modules, "numbering",
                        types.SimpleNamespace(to_absolute=lambda *a, **k: None))
    entry = {
        "name": "Dr. Stone S04 1080p",
        "size": 12 * GB,
        "files": [
            {"name": f"Dr. Stone S4 - {ep:02d}.mkv", "size": GB}
            for ep in range(1, 13)
        ],
    }
    reason = release_sanity.verify_entry(
        entry, "season_pack", season=4, episodes=list(range(1, 38)),
        imdb_id="tt9679542",
    )
    assert reason and "E13" in reason and "12 video files" in reason


def test_complete_loose_named_season_pack_is_accepted(monkeypatch):
    monkeypatch.setitem(sys.modules, "numbering",
                        types.SimpleNamespace(to_absolute=lambda *a, **k: None))
    entry = {
        "name": "Show S04 1080p",
        "size": 3 * GB,
        "files": [
            {"name": f"Show S4 - {ep:02d}.mkv", "size": GB}
            for ep in range(1, 4)
        ],
    }
    assert release_sanity.verify_entry(
        entry, "season_pack", season=4, episodes=[1, 2, 3], imdb_id="tt1234567"
    ) is None


def test_absolute_ep_named_partial_pack_is_rejected(monkeypatch):
    monkeypatch.setitem(sys.modules, "numbering",
                        types.SimpleNamespace(to_absolute=lambda *a, **k: None))
    entry = {
        "name": "Frieren EP01-12",
        "size": 12 * GB,
        "files": [
            {"name": f"Frieren EP{ep:02d}.mkv", "size": GB}
            for ep in range(1, 13)
        ],
    }
    reason = release_sanity.verify_entry(
        entry, "season_pack", season=1, episodes=list(range(1, 39)),
        imdb_id="tt22248376",
    )
    assert reason and "E13" in reason


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


# ── partial packs that name their own episode span ────────────────────────────
# The live miss this class was added for: American Dad! S22E10/E11 (both aired
# the same night) each latched onto one Russian multi-dub torrent holding only
# episodes 1-7. files=0, and the Cyrillic name defeats _file_episode, so nothing
# rejected it; Plex then dropped both because the NFS read could resolve no file.

_RU_PARTIAL = ("Американский папаша!  American Dad!  Сезон 22  Серии 1-7 из 22 "
               "[2026, WEB-DL 1080p] MVO (TVShows) + MVO (LE-Production) + Original + Sub (Eng)")


def test_partial_pack_outside_declared_span_rejected():
    entry = {"name": _RU_PARTIAL, "size": 5 * GB, "files": []}
    reason = release_sanity.verify_entry(entry, "episode", season=22, episode=10,
                                         imdb_id="tt0397306")
    assert reason and "declares episodes 1-7" in reason


def test_partial_pack_inside_declared_span_accepted():
    entry = {"name": _RU_PARTIAL, "size": 5 * GB, "files": []}
    assert release_sanity.verify_entry(entry, "episode", season=22, episode=3,
                                       imdb_id="tt0397306") is None


def test_latin_episode_span_forms():
    for name, wanted, blocked in (
        ("Show Season 4 Episodes 1-7 1080p WEB-DL", 3, 10),
        ("Show S04 Eps 1 to 7 1080p", 3, 10),
        # A declared span outranks the first SxxExx token: this one holds E03
        # even though _file_episode reads the name as "S04E01".
        ("Show S04E01-E07 1080p WEB-DL", 3, 10),
    ):
        entry = {"name": name, "size": 5 * GB, "files": []}
        assert release_sanity.verify_entry(entry, "episode", season=4,
                                           episode=wanted) is None, name
        assert release_sanity.verify_entry(entry, "episode", season=4,
                                           episode=blocked), name


def test_full_pack_naming_one_episode_still_accepted():
    # "Complete Series" + a lone "Episode 1" is a full pack advertising where it
    # starts, not a one-episode release  -  too ambiguous to reject on.
    entry = {"name": "Show Complete Series Episode 1 onwards 1080p", "size": 40 * GB,
             "files": []}
    assert release_sanity.verify_entry(entry, "episode", season=2, episode=9) is None


def test_span_spelled_with_absolute_numbers_accepted(monkeypatch):
    # Some packs number across the whole series. S22E10 of American Dad! is
    # absolute 401, so a "395-405" pack does hold it even though 10 is outside.
    # numbering is stubbed so this does not depend on the live numbering cache.
    monkeypatch.setitem(sys.modules, "numbering",
                        types.SimpleNamespace(to_absolute=lambda *a, **k: 401))
    entry = {"name": "American Dad! Episodes 395-405 1080p WEB-DL", "size": 8 * GB,
             "files": []}
    assert release_sanity.verify_entry(entry, "episode", season=22, episode=10,
                                       imdb_id="tt0397306") is None


# The three false positives a sweep of 20k real TorBox names turned up. Each
# name below is real; all three would have caused NEW wrong rejections/accepts.

def test_absolute_number_field_is_not_a_span():
    # "S07E13 - 246 - Marry Me": the 246 is an absolute-episode field, not a
    # range end. Reading it as 13-246 would wave through every wrong episode.
    entry = {"name": "Gunsmoke - S07E13 - 246 - Marry Me.avi", "size": GB, "files": []}
    assert release_sanity.verify_entry(entry, "episode", season=7, episode=13) is None
    reason = release_sanity.verify_entry(entry, "episode", season=7, episode=20)
    assert reason and "not S07E20" in reason


def test_lone_episode_number_does_not_outrank_tag():
    # "S03E04 ... Episode 28": 28 is the absolute number. The SxxExx tag wins,
    # so the correct file is still accepted for S03E04.
    entry = {"name": "One-Punch.Man.S03E04.Episode.28.1080p.AMZN.WEB-DL.mkv",
             "size": GB, "files": []}
    assert release_sanity.verify_entry(entry, "episode", season=3, episode=4) is None


def test_discontinuous_run_keeps_its_upper_bound():
    # "Серии 1-31, 33-77 из 78" really does hold episode 50; reading only the
    # first run (1-31) would reject it.
    entry = {"name": "Шоу Тома и Джерри  The Tom and Jerry Show  Сезон 3  "
                     "Серии 1-31, 33-77 из 78 [WEB-DL 1080p]", "size": 20 * GB, "files": []}
    assert release_sanity.verify_entry(entry, "episode", season=3, episode=50) is None
    assert release_sanity.verify_entry(entry, "episode", season=3, episode=90)


def test_span_reject_survives_unavailable_numbering(monkeypatch):
    # No absolute number available (offline / uncached): the season-relative
    # episode alone still decides, so the partial pack is still rejected.
    monkeypatch.setitem(sys.modules, "numbering",
                        types.SimpleNamespace(to_absolute=lambda *a, **k: None))
    entry = {"name": _RU_PARTIAL, "size": 5 * GB, "files": []}
    assert release_sanity.verify_entry(entry, "episode", season=22, episode=10,
                                       imdb_id="tt0397306")


def test_broad_absolute_span_does_not_accept_tmdb_within_season_collision(monkeypatch):
    monkeypatch.setitem(sys.modules, "numbering",
                        types.SimpleNamespace(to_absolute=lambda *a, **k: 412))

    reason = release_sanity._span_reject(
        (1, 167),
        "[ENTE] Bleach (2004) S01-S08 (E001-E167)",
        season=2,
        episode=46,
        imdb_id="tt0434665",
    )

    assert reason is not None
    assert "episodes 1-167" in reason


def test_cached_name_span_rejected_even_when_torbox_listing_is_absent(monkeypatch):
    import torbox

    monkeypatch.setattr(release_sanity, "enabled", lambda: True)
    monkeypatch.setattr(torbox, "check_cached_files", lambda hashes: {})
    monkeypatch.setitem(sys.modules, "numbering",
                        types.SimpleNamespace(to_absolute=lambda *a, **k: 412))
    candidate = _stream(
        "[ENTE] Bleach (2004) S01-S08 (E001-E167)",
        "1080p",
        100.0,
        "e" * 40,
    )

    kept = release_sanity.filter_cached(
        [candidate],
        "episode",
        season=2,
        episode=46,
        imdb_id="tt0434665",
    )

    assert kept == []
