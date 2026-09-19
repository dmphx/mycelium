"""Series identity: national versions of one format must not cross over.

Reproduces the 2026-09-18 Traitors incident. The Traitors (US, tt15557874,
2023) had S03 on "The.Traitors.UK.S03..." and S04 on "The Traitors UK S04Exx";
The Traitors (UK, tt23743442, 2022) had S04E01 on "The.Traitors.2023.S04E01",
S04E09/E11 on "The Traitors US S04Exx" and S01E02/E03 on the Hindi-only India
release. Every catalog (zilean, torrentio, mediafusion, comet, prowlarr) can
return any of them for either IMDb id, so the check sits in the shared ranking
and cached-selection paths. Pack consolidation also mapped the US folder "The
Traitors" to the UK imdb because both shows are monitored under that title.
"""
import os
import sys
import time
import types

import pytest

os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import release_sanity
import torrentio
from torrentio import TorrentioStream

US_IMDB = "tt15557874"
UK_IMDB = "tt23743442"

US = release_sanity.ShowIdentity(
    imdb_id=US_IMDB, name="The Traitors", regions=frozenset({"US"}), year=2023,
    language="en", title_tokens=frozenset({"the", "traitors"}),
    namesakes=frozenset({(2022, frozenset({"GB"})), (2022, frozenset({"AU"})),
                         (2025, frozenset({"IE"}))}),
)
UK = release_sanity.ShowIdentity(
    imdb_id=UK_IMDB, name="The Traitors", regions=frozenset({"GB"}), year=2022,
    language="en", title_tokens=frozenset({"the", "traitors"}),
    namesakes=frozenset({(2023, frozenset({"US"})), (2023, frozenset({"CA"})),
                         (2022, frozenset({"AU"})), (2025, frozenset({"IE"}))}),
)
BSG = release_sanity.ShowIdentity(
    imdb_id="tt0407362", name="Battlestar Galactica",
    regions=frozenset({"CA", "US", "GB"}), year=2004, language="en",
    title_tokens=frozenset({"battlestar", "galactica"}),
    namesakes=frozenset({(2003, frozenset({"CA"})), (1978, frozenset({"US"}))}),
)


@pytest.fixture(autouse=True)
def _deterministic_settings(monkeypatch):
    """Settings return config defaults (see test_release_sanity), and the
    identity cache starts empty for every test."""
    stub = types.SimpleNamespace(get=lambda key, default=None: default)
    monkeypatch.setitem(sys.modules, "settings", stub)
    release_sanity._pack_re_cache.clear()
    release_sanity._identity_cache.clear()
    yield
    release_sanity._identity_cache.clear()


def _verdict(text, identity):
    return release_sanity.classify_release(text, identity)[0]


def _stream(name, title, info_hash, source, quality="1080p", size_gb=2.0, seeders=10):
    return TorrentioStream(
        name=name, title=title, info_hash=info_hash, quality=quality,
        seeders=seeders, size_gb=size_gb, is_season_pack=False, source=source,
    )


# ── classify_release: the incident's release names ───────────────────────────

@pytest.mark.parametrize("text", [
    "The.Traitors.UK.S03.1080p.PCOK.WEB-DL.AAC2.0.H.264-WADU",
    "The Traitors UK S04E05 1080p HEVC x265-MeGusta",
    "The.Traitors.2022.S01E02.1080p.WEB.h264-GRP",
    "The.Traitors.Australia.S02E01.1080p.WEB",
])
def test_other_versions_rejected_for_us_show(text):
    assert _verdict(text, US) == release_sanity.IDENTITY_REJECT


@pytest.mark.parametrize("text", [
    "The.Traitors.2023.S04E01.1080p.WEB.h264-EDITH",
    "The Traitors US S04E09 1080p AMZN WEB-DL DDP5 1 H 264-Kitsune",
    "The Traitors S01E02 Episode 2 1080p CBR AMZN WEB-DL Hindi DDP5.1",
    "The.Traitors.NZ.S01E01.720p.WEB",
    "The.Traitors.New.Zealand.S01E01.720p.WEB",
])
def test_other_versions_rejected_for_uk_show(text):
    assert _verdict(text, UK) == release_sanity.IDENTITY_REJECT


def test_own_qualifiers_match():
    assert _verdict("The Traitors US S04E01 1080p AMZN WEB-DL DDP5 1 H 264-NTb",
                    US) == release_sanity.IDENTITY_MATCH
    assert _verdict("The Traitors 2023 S04E05 1080p WEB h264-EDITH",
                    US) == release_sanity.IDENTITY_MATCH
    assert _verdict("The Traitors (2022) - S04E03 - Episode 3 [WEBDL-1080p]",
                    UK) == release_sanity.IDENTITY_MATCH


def test_unqualified_release_stays_neutral():
    name = "The.Traitors.S04E03.1080p.iP.WEB-DL.AAC2.0.H.264-RNG"
    assert _verdict(name, UK) == release_sanity.IDENTITY_NEUTRAL
    assert _verdict(name, US) == release_sanity.IDENTITY_NEUTRAL


def test_hindi_with_english_audio_is_not_rejected():
    name = "The Traitors S01E02 1080p WEB-DL Hindi English DDP5.1"
    assert _verdict(name, UK) == release_sanity.IDENTITY_NEUTRAL


def test_addon_decoration_is_not_read_as_a_qualifier():
    # Comet/MediaFusion/Torrentio descriptions: the size line says "GB" and the
    # tracker tag sits in brackets after the name; neither is a qualifier.
    comet = ("📄 The.Traitors.UK.S01.1080p.WEBRip.AAC2.0.x264-BTN[eztv.re]\n"
             "📹 avc | 🔊 AAC • 2.0 | 💾 29.6 GB 🔎 DMM")
    assert _verdict(comet, US) == release_sanity.IDENTITY_REJECT
    torrentio_title = ("The.Traitors.S04E05.1080p.iP.WEB-DL.AAC2.0.H.264-RNG\n"
                       "👤 34 💾 2.4 GB ⚙️ ThePirateBay")
    assert _verdict(torrentio_title, US) == release_sanity.IDENTITY_NEUTRAL
    assert _verdict("💾 2.5 GB 👤 10 📂 The.Traitors.S04E05.1080p", US) == \
        release_sanity.IDENTITY_NEUTRAL


def test_site_tag_tld_is_not_a_country():
    assert _verdict("www.torrent.ca - The.Traitors.S04E03.1080p", UK) == \
        release_sanity.IDENTITY_NEUTRAL


def test_file_path_lines_are_read_per_tag():
    assert _verdict("The Traitors/Season 04/The.Traitors.US.S04E01.mkv", UK) == \
        release_sanity.IDENTITY_REJECT


def test_country_word_in_the_show_title_is_not_a_qualifier():
    usa = release_sanity.ShowIdentity(
        imdb_id="tt8269136", name="Love Island USA", regions=frozenset({"US"}),
        year=2019, language="en", title_tokens=frozenset({"love", "island", "usa"}))
    uk = release_sanity.ShowIdentity(
        imdb_id="tt4770018", name="Love Island", regions=frozenset({"GB"}),
        year=2015, language="en", title_tokens=frozenset({"love", "island"}))
    assert _verdict("Love.Island.USA.S06E01.1080p.WEB", usa) == release_sanity.IDENTITY_MATCH
    assert _verdict("Love.Island.USA.S06E01.1080p.WEB", uk) == release_sanity.IDENTITY_REJECT
    # A GB show whose own name holds another country is not a conflict.
    ni = release_sanity.ShowIdentity(
        imdb_id="tt0000001", name="Northern Ireland: Troubles", regions=frozenset({"GB"}),
        year=2020, language="en",
        title_tokens=frozenset({"northern", "ireland", "troubles"}))
    assert _verdict("Northern.Ireland.Troubles.S01E01.1080p", ni) == \
        release_sanity.IDENTITY_NEUTRAL


def test_year_in_the_show_title_is_not_a_qualifier():
    show = release_sanity.ShowIdentity(
        imdb_id="tt19628814", name="1923", regions=frozenset({"US"}), year=2022,
        language="en", title_tokens=frozenset({"1923"}))
    assert _verdict("1923.S01E01.1080p.WEB.h264", show) == release_sanity.IDENTITY_NEUTRAL
    assert _verdict("1923.2022.S01E01.1080p.WEB", show) == release_sanity.IDENTITY_MATCH


def test_one_year_drift_of_the_same_production_is_soft():
    # TMDB dates the 2004 series; scene names it 2003, and TMDB also lists the
    # Canadian 2003 miniseries. Shared country, one year apart: soft only.
    assert _verdict("Battlestar.Galactica.2003.S02E01.720p.BluRay", BSG) == \
        release_sanity.IDENTITY_SOFT
    assert _verdict("Battlestar.Galactica.1978.S01E01.720p", BSG) == \
        release_sanity.IDENTITY_REJECT
    assert _verdict("Battlestar.Galactica.S02E01.720p.BluRay", BSG) == \
        release_sanity.IDENTITY_NEUTRAL


def test_year_of_no_known_version_is_neutral():
    # Season-year tags and TMDB/scene drift without a namesake never reject.
    assert _verdict("The.Traitors.2026.S04E01.1080p", UK) == release_sanity.IDENTITY_NEUTRAL


def test_no_identity_or_no_text_is_neutral():
    assert _verdict("The.Traitors.UK.S03.1080p", None) == release_sanity.IDENTITY_NEUTRAL
    assert _verdict("", US) == release_sanity.IDENTITY_NEUTRAL


# ── series_identity: TMDB lookup, namesakes, caching ─────────────────────────

def _fake_tmdb(calls, search_ok=True):
    shows = {
        215943: {"name": "The Traitors", "original_name": "The Traitors",
                 "origin_country": ["US"], "first_air_date": "2023-01-12",
                 "original_language": "en"},
    }
    search = {"results": [
        {"id": 215943, "name": "The Traitors", "first_air_date": "2023-01-12",
         "origin_country": ["US"], "original_language": "en"},
        {"id": 215307, "name": "The Traitors", "first_air_date": "2022-11-29",
         "origin_country": ["GB"], "original_language": "en"},
        {"id": 212457, "name": "The Traitors", "first_air_date": "2022-03-01",
         "origin_country": ["AU"], "original_language": "en"},
        {"id": 234613, "name": "The Traitors Canada", "first_air_date": "2023-10-06",
         "origin_country": ["CA"], "original_language": "en"},
        {"id": 271489, "name": "The Traitors", "original_name": "x",
         "first_air_date": "2025-06-12", "origin_country": ["IN"],
         "original_language": "hi"},
        {"id": 242476, "name": "The Traitors: Uncloaked", "first_air_date": "2024-01-03",
         "origin_country": ["GB"], "original_language": "en"},
    ]}

    def find_by_imdb(imdb_id, kind="tv"):
        calls.append(("find", imdb_id))
        return 215943 if imdb_id == US_IMDB else None

    def get_show_info(tmdb_id):
        calls.append(("show", tmdb_id))
        return shows.get(tmdb_id)

    def _get(path, params=None):
        calls.append(("get", path, (params or {}).get("query")))
        return search if search_ok else None

    return types.SimpleNamespace(find_by_imdb=find_by_imdb,
                                 get_show_info=get_show_info, _get=_get)


def test_series_identity_from_tmdb(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "tmdb", _fake_tmdb(calls))
    identity = release_sanity.series_identity(US_IMDB, "The Traitors")
    assert identity.regions == frozenset({"US"})
    assert identity.year == 2023
    assert identity.language == "en"
    assert {"the", "traitors"} <= identity.title_tokens
    # Same base name and language only; itself, the Hindi version and the
    # "Uncloaked" spin-off are not namesakes. "The Traitors Canada" is.
    assert identity.namesakes == frozenset({
        (2022, frozenset({"GB"})), (2022, frozenset({"AU"})), (2023, frozenset({"CA"})),
    })
    assert ("get", "/search/tv", "the traitors") in calls


def test_series_identity_is_cached(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "tmdb", _fake_tmdb(calls))
    first = release_sanity.series_identity(US_IMDB)
    count = len(calls)
    assert release_sanity.series_identity(US_IMDB) is first
    assert len(calls) == count


def test_failed_namesake_search_is_cached_briefly(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "tmdb", _fake_tmdb(calls, search_ok=False))
    identity = release_sanity.series_identity(US_IMDB)
    assert identity.regions == frozenset({"US"})
    assert identity.namesakes == frozenset()
    expires = release_sanity._identity_cache[US_IMDB][0]
    assert expires - time.monotonic() <= release_sanity._IDENTITY_RETRY_SEC + 1


def test_unknown_show_uses_title_qualifier_or_fails_open(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "tmdb", _fake_tmdb(calls))
    hinted = release_sanity.series_identity("tt0000009", "The Office (US)")
    assert hinted.regions == frozenset({"US"})
    assert _verdict("The.Office.UK.S01E01.720p", hinted) == release_sanity.IDENTITY_REJECT
    assert release_sanity.series_identity("tt0000010", "The Office") is None


def test_identity_check_can_be_switched_off(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "tmdb", _fake_tmdb(calls))
    monkeypatch.setitem(sys.modules, "settings", types.SimpleNamespace(
        get=lambda key, default=None: False if key == "SERIES_IDENTITY_CHECK" else default))
    assert release_sanity.series_identity(US_IMDB) is None
    assert calls == []


# ── torrentio.rank_streams: every catalog's result format ────────────────────

def _us_s04e05_pool():
    return {
        "zilean_uk": _stream("The Traitors UK S04E05 1080p WEB-DL", "The Traitors UK S04E05 1080p",
                             "1" * 40, "zilean"),
        "torrentio_uk": _stream(
            "Torrentio\n1080p torrentio 1080p WEB-DL h264",
            "The.Traitors.UK.S04E05.1080p.iP.WEB-DL.AAC2.0.H.264-RNG\n👤 34 💾 2.4 GB ⚙️ TPB",
            "2" * 40, "torrentio"),
        "mediafusion_2022": _stream(
            "MediaFusion 1080p", "📂 The.Traitors.2022.S04E05.1080p.WEB.h264\n💾 2.1 GB",
            "3" * 40, "mediafusion"),
        "prowlarr_hindi": _stream(
            "The Traitors S04E05 1080p AMZN WEB-DL Hindi DDP5.1 [IndexerX]",
            "The Traitors S04E05 1080p AMZN WEB-DL Hindi DDP5.1", "4" * 40, "prowlarr/indexerx"),
        "comet_us": _stream(
            "[TB+] Comet 1080p", "📄 The Traitors US S04E05 1080p AMZN WEB-DL DDP5 1 H 264-NTb\n"
            "💾 2.3 GB 🔎 DMM", "5" * 40, "stremio/comet"),
        "torrentio_plain": _stream(
            "Torrentio\n1080p", "The.Traitors.S04E05.1080p.WEB.H264-GRP\n👤 90 💾 2.2 GB",
            "6" * 40, "torrentio", seeders=90),
        "zilean_2023": _stream("The Traitors 2023 S04E05 1080p WEB h264-EDITH WEB-DL",
                               "The Traitors 2023 S04E05 1080p WEB h264-EDITH",
                               "7" * 40, "zilean"),
    }


def test_rank_streams_drops_other_versions_from_every_source():
    pool = _us_s04e05_pool()
    ranked = torrentio.rank_streams(list(pool.values()), override={"show_identity": US})
    kept = {name for name, s in pool.items() if s in ranked}
    assert kept == {"comet_us", "torrentio_plain", "zilean_2023"}


def test_rank_streams_ranks_own_qualifier_ahead_of_unqualified():
    pool = _us_s04e05_pool()
    ranked = torrentio.rank_streams(list(pool.values()), override={"show_identity": US})
    # Same quality: the releases that name the US show come first, even
    # though the unqualified one has more seeders and would otherwise win.
    assert ranked[-1] is pool["torrentio_plain"]
    assert {s.info_hash for s in ranked[:2]} == {
        pool["comet_us"].info_hash, pool["zilean_2023"].info_hash}


def test_rank_streams_never_falls_back_to_another_version():
    pool = _us_s04e05_pool()
    only_uk = [pool["zilean_uk"], pool["torrentio_uk"], pool["mediafusion_2022"]]
    assert torrentio.rank_streams(only_uk, override={"show_identity": US}) == []


def test_rank_streams_soft_candidates_kept_only_when_alone():
    drift = _stream("Battlestar.Galactica.2003.S02E01.720p.BluRay", "", "8" * 40,
                    "zilean", quality="720p")
    plain = _stream("Battlestar.Galactica.S02E01.720p.BluRay", "", "9" * 40,
                    "zilean", quality="720p")
    assert torrentio.rank_streams([drift], override={"show_identity": BSG}) == [drift]
    assert torrentio.rank_streams([drift, plain], override={"show_identity": BSG}) == [plain]


def test_rank_streams_without_identity_is_unchanged():
    pool = _us_s04e05_pool()
    assert len(torrentio.rank_streams(list(pool.values()))) == len(pool)


# ── filter_cached: TorBox names, and with grab-time verification off ─────────

def test_filter_cached_reads_torbox_name_of_unnamed_candidate(monkeypatch):
    import torbox
    unnamed = _stream("", "", "a" * 40, "zilean", quality="unknown", size_gb=8.42)
    good = _stream("The Traitors US S02 1080p", "", "b" * 40, "zilean")
    entries = {
        "a" * 40: {"name": "The.Traitors.UK.S02.1080p.iP.WEB-DL.AAC2.0.H.264-RNG",
                   "size": 9 * 1024 ** 3,
                   "files": [{"name": f"The.Traitors.UK.S02E{e:02d}.1080p.mkv",
                              "size": 700 * 1024 ** 2} for e in range(1, 13)]},
        "b" * 40: {"name": "The.Traitors.US.S02.1080p", "size": 9 * 1024 ** 3,
                   "files": [{"name": f"The.Traitors.US.S02E{e:02d}.1080p.mkv",
                              "size": 700 * 1024 ** 2} for e in range(1, 12)]},
    }
    monkeypatch.setattr(release_sanity, "enabled", lambda: True)
    monkeypatch.setattr(release_sanity, "series_identity", lambda imdb_id, *a: US)
    monkeypatch.setattr(torbox, "check_cached_files", lambda hashes: entries)
    kept = release_sanity.filter_cached([unnamed, good], "season_pack", season=2,
                                        imdb_id=US_IMDB)
    assert kept == [good]


def test_filter_cached_name_check_runs_with_verification_off(monkeypatch):
    uk = _stream("The.Traitors.UK.S03E01.1080p", "", "c" * 40, "zilean")
    us = _stream("The.Traitors.US.S03E01.1080p", "", "d" * 40, "zilean")
    monkeypatch.setattr(release_sanity, "enabled", lambda: False)
    monkeypatch.setattr(release_sanity, "series_identity", lambda imdb_id, *a: US)
    kept = release_sanity.filter_cached([uk, us], "episode", season=3, episode=1,
                                        imdb_id=US_IMDB)
    assert kept == [us]


def test_filter_cached_skips_identity_for_movies(monkeypatch):
    looked_up = []
    monkeypatch.setattr(release_sanity, "enabled", lambda: False)
    monkeypatch.setattr(release_sanity, "series_identity",
                        lambda imdb_id, *a: looked_up.append(imdb_id) or US)
    movie = _stream("The.Traitors.UK.2019.1080p", "", "e" * 40, "zilean")
    assert release_sanity.filter_cached([movie], "movie", imdb_id="tt1") == [movie]
    assert looked_up == []


# ── search_engine: the identity reaches every series search ──────────────────

def test_search_candidates_filters_all_sources(monkeypatch):
    import search_engine
    pool = _us_s04e05_pool()
    by_source = {}
    for stream in pool.values():
        by_source.setdefault(stream.source, []).append(stream)

    def source_jobs(_media_type, _imdb_id, _title, _season, _episode, _include_prowlarr):
        return [(name, lambda rows=rows: rows) for name, rows in by_source.items()]

    stored = []
    monkeypatch.setattr(search_engine, "_source_jobs", source_jobs)
    monkeypatch.setattr(release_sanity, "series_identity", lambda imdb_id, *a: US)
    monkeypatch.setattr(search_engine.blacklist, "filter_candidates", lambda rows: rows)
    monkeypatch.setattr(search_engine.db, "start_search_run", lambda *args: 1)
    monkeypatch.setattr(search_engine.db, "record_source_query", lambda *args: None)
    monkeypatch.setattr(search_engine.db, "get_rejected_candidate_hashes", lambda _key: set())
    monkeypatch.setattr(search_engine.db, "upsert_release_candidates",
                        lambda _key, rows: stored.extend(rows))
    monkeypatch.setattr(search_engine.db, "finish_search_run", lambda *args, **kwargs: None)

    ranked = search_engine.search_candidates(
        "series", US_IMDB, "The Traitors", season=4, episode=5, trigger="test")

    assert {s.info_hash for s in ranked} == {
        pool["comet_us"].info_hash, pool["torrentio_plain"].info_hash,
        pool["zilean_2023"].info_hash}
    # Rejected releases never reach durable candidate history either.
    assert {row["info_hash"] for row in stored} == {s.info_hash for s in ranked}


def test_with_show_identity_keeps_an_explicit_identity(monkeypatch):
    import search_engine
    monkeypatch.setattr(release_sanity, "series_identity",
                        lambda *a: pytest.fail("identity must not be looked up again"))
    override = {"show_identity": UK, "episode_title": "Episode 3"}
    assert search_engine._with_show_identity(override, UK_IMDB, "The Traitors") is override


# ── upgrader.run_pack_consolidation: folder -> imdb, not title -> imdb ───────

def _traitors_library(root, *, nfo=True):
    folders = {"The Traitors": US_IMDB, "The Traitors (2022)": UK_IMDB}
    for folder, imdb_id in folders.items():
        season_dir = root / "series" / folder / "Season 03"
        season_dir.mkdir(parents=True)
        label = "The Traitors (US)" if imdb_id == US_IMDB else folder
        for ep in range(1, 4):
            (season_dir / f"{label} S03E{ep:02d}.strm").write_text("x", encoding="utf-8")
        if nfo:
            (root / "series" / folder / "tvshow.nfo").write_text(
                '<tvshow><title>The Traitors</title>'
                f'<uniqueid type="imdb" default="true">{imdb_id}</uniqueid></tvshow>',
                encoding="utf-8")
    return folders


def _run_consolidation(monkeypatch, root, strm_pairs):
    import upgrader
    searched = []
    monkeypatch.setattr(upgrader, "MEDIA_PATH", str(root))
    monkeypatch.setattr(upgrader.playback_guard, "defer", lambda *_a, **_k: False)
    monkeypatch.setattr(upgrader, "_settings",
                        types.SimpleNamespace(get=lambda key, default=None: default))
    monkeypatch.setattr(upgrader.db, "get_all_monitored_series", lambda: [
        {"title": "The Traitors", "imdb_id": US_IMDB},
        {"title": "The Traitors", "imdb_id": UK_IMDB},
    ])
    monkeypatch.setattr(upgrader.db, "get_series_strm_paths", lambda: strm_pairs)
    monkeypatch.setattr(upgrader, "_fetch_season_candidates",
                        lambda imdb_id, season: searched.append((imdb_id, season)) or [])
    assert upgrader.run_pack_consolidation() == 0
    return searched


def test_consolidation_maps_folders_by_nfo(monkeypatch, tmp_path):
    _traitors_library(tmp_path)
    searched = _run_consolidation(monkeypatch, tmp_path, [])
    assert sorted(searched) == sorted([(US_IMDB, 3), (UK_IMDB, 3)])


def test_consolidation_maps_folders_by_virtual_items(monkeypatch, tmp_path):
    _traitors_library(tmp_path, nfo=False)
    series = tmp_path / "series"
    pairs = [(US_IMDB, str(series / "The Traitors" / "Season 03" / f"The Traitors (US) S03E0{e}.strm"))
             for e in range(1, 4)]
    pairs += [(UK_IMDB, str(series / "The Traitors (2022)" / "Season 03" / f"x S03E0{e}.strm"))
              for e in range(1, 4)]
    searched = _run_consolidation(monkeypatch, tmp_path, pairs)
    assert sorted(searched) == sorted([(US_IMDB, 3), (UK_IMDB, 3)])


def test_consolidation_skips_a_title_shared_by_two_shows(monkeypatch, tmp_path):
    # No nfo and no items: "The Traitors" names two monitored shows, and "The
    # Traitors (2022)" names none, so neither folder is consolidated.
    _traitors_library(tmp_path, nfo=False)
    assert _run_consolidation(monkeypatch, tmp_path, []) == []


def test_consolidation_skips_when_nfo_and_items_disagree(monkeypatch, tmp_path):
    _traitors_library(tmp_path)
    series = tmp_path / "series"
    pairs = [(UK_IMDB, str(series / "The Traitors" / "Season 03" / f"y S03E0{e}.strm"))
             for e in range(1, 4)]
    searched = _run_consolidation(monkeypatch, tmp_path, pairs)
    assert searched == [(UK_IMDB, 3)]


def test_consolidation_is_skipped_in_catbox_mode(monkeypatch, tmp_path):
    import upgrader
    _traitors_library(tmp_path)
    monkeypatch.setattr(upgrader, "MEDIA_PATH", str(tmp_path))
    monkeypatch.setattr(upgrader.playback_guard, "defer", lambda *_a, **_k: False)
    monkeypatch.setattr(upgrader, "_settings", types.SimpleNamespace(
        get=lambda key, default=None: True if key == "CATBOX_MODE" else default))
    monkeypatch.setattr(upgrader, "_fetch_season_candidates",
                        lambda *a: pytest.fail("catbox mode must not search for packs"))
    monkeypatch.setattr(upgrader.torbox, "add_magnet",
                        lambda *a, **k: pytest.fail("catbox mode must not add packs"))
    assert upgrader.run_pack_consolidation() == 0


def test_upgrader_season_candidates_carry_the_identity(monkeypatch):
    import upgrader
    seen = {}
    monkeypatch.setattr(release_sanity, "series_identity", lambda imdb_id, *a: UK)
    monkeypatch.setattr(upgrader, "_episode_runtime_override", lambda *a: {})
    monkeypatch.setattr(upgrader, "_settings",
                        types.SimpleNamespace(get=lambda key, default=None: False))
    monkeypatch.setattr(upgrader.torrentio, "fetch_streams", lambda *a, **k: [])
    monkeypatch.setattr(upgrader.torrentio, "rank_streams",
                        lambda streams, **kwargs: seen.update(kwargs) or [])
    upgrader._fetch_season_candidates(UK_IMDB, 4)
    assert seen["override"]["show_identity"] is UK


# ── monitor: an imdb-scoped existence check stays in its own folder ─────────

def test_series_dir_skips_another_shows_folder(monkeypatch, tmp_path):
    import monitor
    series = tmp_path / "series"
    for folder, imdb_id in (("The Traitors", US_IMDB), ("The Traitors (2022)", UK_IMDB),
                            ("The Traitors Uncloaked", "tt30490874")):
        (series / folder).mkdir(parents=True)
        (series / folder / "tvshow.nfo").write_text(
            f'<tvshow><uniqueid type="imdb" default="true">{imdb_id}</uniqueid></tvshow>',
            encoding="utf-8")
    us_s04 = series / "The Traitors" / "Season 04"
    us_s04.mkdir()
    (us_s04 / "The Traitors (US) S04E03.strm").write_text("x", encoding="utf-8")
    (series / "The Traitors (2022)" / "Season 04").mkdir()
    monkeypatch.setattr(monitor, "MEDIA_PATH", str(tmp_path))
    monkeypatch.setattr(monitor.db, "get_virtual_item_by_episode", lambda *a: None)

    assert monitor._series_dir("The Traitors", UK_IMDB) == str(series / "The Traitors (2022)")
    assert monitor._series_dir("The Traitors", US_IMDB) == str(series / "The Traitors")
    # The US folder's S04E03 must not mark the UK S04E03 as found.
    assert monitor.strm_exists_episode("The Traitors", 4, 3, imdb_id=UK_IMDB) is False
    assert monitor.strm_exists_episode("The Traitors", 4, 3, imdb_id=US_IMDB) is True


def test_series_dir_uses_a_folder_without_nfo_as_fallback(monkeypatch, tmp_path):
    import monitor
    series = tmp_path / "series"
    (series / "Some Show").mkdir(parents=True)
    monkeypatch.setattr(monitor, "MEDIA_PATH", str(tmp_path))
    assert monitor._series_dir("Some Show", "tt0000001") == str(series / "Some Show")
