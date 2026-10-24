"""Multi-movie collections must never serve one film for another.

The incident behind these tests: a 267 GB "Collection ... 1937 a 2022 ... French
VFF" torrent backed dozens of single-movie items. Its largest file was one film
(Pete's Dragon, dubbed in French), so every title that resolved through it,
Bolt, Dumbo, Snow White and others, played that file. The grab-time size cap
caught the pack later, but the play-time check passed it (its name is not a
season-pack name), a pinned file id skipped the check entirely, and the
"largest file" rule picked another film. All fixtures here are synthetic.
"""
import os
import sys
import types

import pytest

os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import release_sanity  # noqa: E402
import torrentio  # noqa: E402
from torrentio import TorrentioStream  # noqa: E402

GB = 1024 ** 3
MB = 1024 ** 2


@pytest.fixture(autouse=True)
def _deterministic_settings(monkeypatch):
    stub = types.SimpleNamespace(get=lambda key, default=None: default)
    monkeypatch.setitem(sys.modules, "settings", stub)
    release_sanity._pack_re_cache.clear()
    yield
    release_sanity._pack_re_cache.clear()


def _collection(names_sizes, name="Synthetic Animation Collection"):
    files = [{"id": idx, "name": f"{name}/{n}", "size": s}
             for idx, (n, s) in enumerate(names_sizes)]
    return {"id": 7, "name": name, "size": sum(s for _, s in names_sizes), "files": files}


def _french_pack():
    return _collection([
        ("[001] Blanche Neige (1937).mkv", int(1.4 * GB)),
        ("[027] Peter et Elliott le dragon (1977).mkv", int(2.6 * GB)),
        ("[049] Volt star malgre lui (2008).mkv", int(1.9 * GB)),
        ("[050] Raiponce (2010).mkv", int(2.0 * GB)),
    ], name="Collection Studio 1937 a 2022 - 1080p H265 French VFF")


def _english_pack():
    return _collection([
        ("Bolt (2008) 1080p.mkv", int(2.1 * GB)),
        ("Tangled (2010) 1080p.mkv", int(2.3 * GB)),
        ("Wreck-It Ralph (2012) 1080p.mkv", int(2.4 * GB)),
        ("Frozen (2013) 1080p.mkv", int(2.6 * GB)),
        ("Bolt (2008) Featurette.mkv", 90 * MB),
    ], name="Synthetic Studio Animation 2008-2013 1080p")


# ── file selection ───────────────────────────────────────────────────────────

def test_collection_serves_the_requested_film_not_the_largest():
    main, reason = release_sanity.pick_movie_file(
        _english_pack()["files"], "Bolt", 2008, "Synthetic Studio Animation")
    assert reason is None
    assert main["name"].endswith("Bolt (2008) 1080p.mkv")


def test_collection_without_the_title_is_rejected_instead_of_largest_file():
    pack = _french_pack()
    main, reason = release_sanity.pick_movie_file(pack["files"], "Bolt", 2008, pack["name"])
    assert main is None
    assert "collection" in reason


def test_wrong_stored_year_from_pack_name_does_not_match_another_film():
    # Older grabs stored the pack's 1937 as every title's year. The year alone
    # must not let "Blanche Neige (1937)" stand in for Dumbo.
    pack = _french_pack()
    main, reason = release_sanity.pick_movie_file(pack["files"], "Dumbo", 1937, pack["name"])
    assert main is None and reason


def test_sequels_are_told_apart_by_number_and_year():
    files = _collection([
        ("Doctor Dolittle 1998 1080p WEB-DL.mkv", int(1.3 * GB)),
        ("Doctor Dolittle 2 2001 1080p WEB-DL.mkv", int(1.4 * GB)),
        ("Doctor Dolittle 3 2006 1080p WEB-DL.mkv", int(1.5 * GB)),
    ])["files"]
    first, _ = release_sanity.pick_movie_file(files, "Doctor Dolittle", 1998)
    second, _ = release_sanity.pick_movie_file(files, "Doctor Dolittle 2", 2001)
    third, _ = release_sanity.pick_movie_file(files, "Doctor Dolittle 3", None)
    assert "Dolittle 1998" in first["name"]
    assert "Dolittle 2 2001" in second["name"]
    assert "Dolittle 3 2006" in third["name"]


def test_single_film_with_extras_keeps_the_largest_file():
    files = _collection([
        ("Heat.1995.1080p.BluRay.x264.mkv", int(9 * GB)),
        ("Extras/Making.Of.mkv", 700 * MB),
        ("Sample/heat-sample.mkv", 40 * MB),
    ], name="Heat.1995.1080p.BluRay.x264")["files"]
    main, reason = release_sanity.pick_movie_file(files, "Heat", 1995)
    assert reason is None and main["name"].endswith("Heat.1995.1080p.BluRay.x264.mkv")


def test_single_film_released_under_a_translated_title_is_kept():
    files = [{"id": 0, "name": "Poder.Policial.2024.1080p.WEB-DL.mkv", "size": 3 * GB}]
    main, reason = release_sanity.pick_movie_file(files, "Power", 2024)
    assert reason is None and main["id"] == 0


def test_single_file_of_another_film_and_year_is_rejected():
    files = [{"id": 0, "name": "Saturn.3.1980.1080p.BluRay.mkv", "size": 6 * GB}]
    main, reason = release_sanity.pick_movie_file(
        files, "The Broken Hearts Gallery", 2020, "Saturn.3.1980.1080p.BluRay")
    assert main is None and reason


def test_without_a_title_the_largest_file_rule_is_unchanged():
    files = _english_pack()["files"]
    main, reason = release_sanity.pick_movie_file(files)
    assert reason is None and main["name"].endswith("Frozen (2013) 1080p.mkv")


# ── verification entry points ────────────────────────────────────────────────

def test_live_check_rejects_a_collection_for_a_title_it_does_not_hold():
    assert release_sanity.verify_live_torrent(
        _french_pack(), "movie", title="Bolt", year=2008)


def test_live_check_accepts_a_collection_that_holds_the_title():
    pack = _english_pack()
    pack["name"] = "Synthetic Studio Animation 1080p"  # no year range in the name
    assert release_sanity.verify_live_torrent(pack, "movie", title="Bolt", year=2008) is None


def test_collection_names_are_recognized():
    reason = release_sanity.movie_collection_name_reason
    assert reason("Collection Studio 1937 a 2022 - 1080p H265 French VFF", "Bolt")
    assert reason("Pirates.of.the.Caribbean-Five.Movies.2003-2017.1080p", "Pirates")
    assert reason("Studio Ghibli Complete Collection (1984-2023) (1080p BluRay x265)")
    assert reason("DC Comics Animated Movies Collection", "Batman Year One")


def test_single_films_named_collection_or_saga_are_not_collections():
    reason = release_sanity.movie_collection_name_reason
    assert reason("Seven Samurai 1954 Criterion Collection 1080p BluRay",
                  "Seven Samurai") is None
    assert reason("The.Collection.2012.1080p.BluRay.x264", "The Collection") is None
    assert reason("The.Twilight.Saga.New.Moon.2009.1080p", "The Twilight Saga New Moon") is None
    assert reason("Blade.Runner.2049.2017.2160p.WEB-DL", "Blade Runner 2049") is None


def test_rank_streams_drops_collection_named_release_for_a_movie():
    pack = TorrentioStream(
        name="Torrentio", title="Synthetic Studio Collection 1937 a 2022 1080p",
        info_hash="a" * 40, quality="1080p", seeders=50, size_gb=40,
        is_season_pack=False)
    single = TorrentioStream(
        name="Torrentio", title="Bolt.2008.1080p.BluRay.x264", info_hash="b" * 40,
        quality="1080p", seeders=5, size_gb=2, is_season_pack=False)
    ranked = torrentio.rank_streams([pack, single], media_kind="movie",
                                    override={"movie_title": "Bolt"})
    assert [s.info_hash for s in ranked] == ["b" * 40]


# ── English-first ranking ────────────────────────────────────────────────────

def _release(title, quality="1080p", seeders=10, size_gb=4.0, h="c"):
    return TorrentioStream(
        name="Torrentio", title=title, info_hash=h * 40, quality=quality,
        seeders=seeders, size_gb=size_gb, is_season_pack=False,
        languages=torrentio._detect_languages(title))


def test_foreign_dub_ranks_after_an_english_release():
    dub = _release("Bolt.2008.TRUEFRENCH.1080p.BluRay.x264", seeders=500, h="d")
    eng = _release("Bolt.2008.1080p.BluRay.x264", seeders=5, h="e")
    ita = _release("Bolt.2008.iTA.AC3.1080p.BluRay", seeders=300, h="f")
    ranked = torrentio.rank_streams([dub, ita, eng], override={"original_language": "en"})
    assert ranked[0].info_hash == "e" * 40


def test_multi_audio_and_english_tagged_releases_are_not_dubs():
    multi = _release("Bolt.2008.MULTi.VFF.1080p.BluRay", seeders=500, h="d")
    dual = _release("Bolt.2008.ITA.ENG.1080p.BluRay", seeders=400, h="f")
    plain = _release("Bolt.2008.1080p.BluRay", seeders=5, h="e")
    ranked = torrentio.rank_streams([plain, multi, dual], override={"original_language": "en"})
    assert ranked[0].info_hash == "d" * 40  # seeders decide among non-dubs


def test_original_language_release_is_not_penalized():
    french = _release("Amelie.2001.FRENCH.1080p.BluRay", seeders=500, h="d")
    other = _release("Amelie.2001.iTA.1080p.BluRay", seeders=600, h="e")
    ranked = torrentio.rank_streams([other, french], override={"original_language": "fr"})
    assert ranked[0].info_hash == "d" * 40


def test_dub_ranking_can_be_turned_off(monkeypatch):
    stub = types.SimpleNamespace(
        get=lambda key, default=None: False if key == "DEPRIORITIZE_FOREIGN_DUBS" else default)
    monkeypatch.setitem(sys.modules, "settings", stub)
    dub = _release("Bolt.2008.TRUEFRENCH.1080p.BluRay", seeders=500, h="d")
    eng = _release("Bolt.2008.1080p.BluRay", seeders=5, h="e")
    ranked = torrentio.rank_streams([eng, dub], override={"original_language": "en"})
    assert ranked[0].info_hash == "d" * 40


def test_vostfr_is_original_audio_not_a_french_dub():
    assert "fr" not in torrentio._detect_languages("Bolt.2008.VOSTFR.1080p.WEB")
    assert "fr" in torrentio._detect_languages("Bolt.2008.VFF.1080p.WEB")


# ── catbox: a pinned file is re-checked ──────────────────────────────────────

@pytest.fixture()
def catbox_harness(monkeypatch):
    import catbox
    import strm_generator

    state = {"file_id": 1, "item": None, "listing": None, "fails": []}

    class FakeDb:
        def get_virtual_item(self, token):
            return dict(state["item"], file_id=state["file_id"])

        def update_virtual_file_id(self, token, file_id):
            state["file_id"] = file_id

        def __getattr__(self, name):
            return lambda *a, **k: None

    fake_torbox = types.SimpleNamespace(
        find_by_id=lambda tid, **k: state["listing"],
        _is_ready=lambda item: True,
        RateLimited=RuntimeError,
    )
    monkeypatch.setattr(catbox, "db", FakeDb())
    monkeypatch.setattr(catbox, "torbox", fake_torbox)
    monkeypatch.setattr(catbox, "_content_key", lambda item: None)
    monkeypatch.setattr(catbox, "_record_playback_touch", lambda token: None)
    monkeypatch.setattr(catbox, "_metrics_inc", lambda result: None)
    monkeypatch.setattr(catbox, "_fail_put", lambda token, *a: state["fails"].append(token))
    monkeypatch.setattr(strm_generator, "_get_stream_url",
                        lambda torbox_id, file_id: f"https://cdn.test/{torbox_id}/{file_id}")
    monkeypatch.setitem(sys.modules, "tmdb", types.SimpleNamespace(
        title_identity=lambda imdb_id, kind="movie": {"year": 2008}))
    return catbox, state


def _movie_item(title="Bolt (1937)"):
    return {"token": "tok", "title": title, "media_type": "movie", "imdb_id": "tt0000001",
            "info_hash": "a" * 40, "magnet": "magnet:?xt=urn:btih:" + "a" * 40,
            "torbox_id": 7, "year": 1937, "protocol": "torrent",
            "debrid_provider": "torbox"}


def test_pinned_file_of_another_film_is_rejected(catbox_harness):
    catbox, state = catbox_harness
    state["item"] = _movie_item()
    state["listing"] = _french_pack()
    state["file_id"] = 1  # the pack's Pete's Dragon file
    assert catbox._materialize_locked("tok") is None
    assert state["file_id"] is None
    assert state["fails"] == ["tok"]


def test_pinned_wrong_file_in_a_good_collection_is_repinned(catbox_harness):
    catbox, state = catbox_harness
    state["item"] = _movie_item()
    pack = _english_pack()
    pack["name"] = "Synthetic Studio Animation 1080p"
    state["listing"] = pack
    state["file_id"] = 3  # Frozen, the largest
    url = catbox._materialize_locked("tok")
    assert url == "https://cdn.test/7/0"
    assert state["file_id"] == 0


def test_pinned_single_file_movie_is_served_unchanged(catbox_harness):
    catbox, state = catbox_harness
    state["item"] = _movie_item("Heat (1995)")
    state["listing"] = {"id": 7, "name": "Heat.1995.1080p", "size": 9 * GB,
                        "files": [{"id": 0, "name": "Heat.1995.1080p.mkv", "size": 9 * GB}]}
    state["file_id"] = 0
    sys.modules["tmdb"].title_identity = lambda imdb_id, kind="movie": {"year": 1995}
    assert catbox._materialize_locked("tok") == "https://cdn.test/7/0"
