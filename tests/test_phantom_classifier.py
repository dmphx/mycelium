"""Classifier tests for phantom_cleanup, built from the 2026-11-13 hygiene audit.

The fixtures mirror the audited metadata: TMDB's self-consistent boundary,
TVmaze's regular episode numbers, each item's own .nfo sidecar, its payload
size and pack hash, and the live canonical slots in the same show.
"""
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import phantom_cleanup


class FakeTmdb:
    def __init__(self, boundaries):
        self.boundaries = boundaries

    def find_by_imdb(self, imdb_id, kind="tv"):
        return imdb_id

    def get_show_info(self, tmdb_id):
        return {"seasons": [{"season_number": season, "episode_count": last}
                            for (imdb, season), last in self.boundaries.items()
                            if imdb == tmdb_id]}

    def get_season_episodes(self, tmdb_id, season):
        last = self.boundaries.get((tmdb_id, season), 0)
        return [{"episode_number": number} for number in range(1, last + 1)]


class FakeTvmaze:
    def __init__(self, numbers):
        self.numbers = numbers

    def regular_numbers(self, imdb_id, season):
        return self.numbers.get((imdb_id, season))


def _nfo(title=None, aired=None):
    lines = ["<episodedetails>"]
    if title is not None:
        lines.append(f"  <title>{title}</title>")
    if aired is not None:
        lines.append(f"  <aired>{aired}</aired>")
    lines.append("</episodedetails>")
    return "\n".join(lines)


class Library:
    """Writes a fake media and Spore tree and returns matching virtual items."""

    def __init__(self, root: Path):
        self.media = root / "media"
        self.spore = root / "spore"
        self.items = []
        self.tmdb = {}
        self.tvmaze = {}

    def season(self, imdb, show, season, tmdb_last, tvmaze, rows, pack="hash-pack",
               size=10.0, source="zilean"):
        self.tmdb[(imdb, season)] = tmdb_last
        if tvmaze is not None:
            self.tvmaze[(imdb, season)] = set(tvmaze)
        for row in rows:
            row = dict(row) if isinstance(row, dict) else {"episode": row}
            episode = row["episode"]
            strm = (self.media / "series" / show / f"Season {season:02d}" /
                    f"{show} S{season:02d}E{episode:02d}.strm")
            stub = (self.spore / strm.relative_to(self.media)).with_suffix(".mkv")
            for path in (strm, stub):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
            if "title" in row or "aired" in row:
                strm.with_suffix(".nfo").write_text(
                    _nfo(row.get("title"), row.get("aired")), encoding="utf-8")
            self.items.append({
                "token": f"{imdb}-{season}-{episode}",
                "imdb_id": imdb,
                "media_type": "series",
                "title": f"{show} S{season:02d}E{episode:02d}",
                "season": season,
                "episode": episode,
                "last_played": row.get("last_played"),
                "play_count": 0,
                "info_hash": row.get("hash", pack),
                "size_gb": row.get("size", size),
                "source": row.get("source", source),
                "file_id": None,
                "strm_path": str(strm),
            })
        return self

    def discover(self):
        return phantom_cleanup.discover(
            items=self.items,
            tmdb_client=FakeTmdb(self.tmdb),
            tvmaze_client=FakeTvmaze(self.tvmaze),
            media_root=self.media,
            spore_root=self.spore,
        )

    def stub(self, show, season, episode):
        return (self.spore / "series" / show / f"Season {season:02d}" /
                f"{show} S{season:02d}E{episode:02d}.mkv")


def _key(row):
    return (row["imdb_id"], row["season"], row["episode"])


def _classes(result):
    return {_key(row): row["class"] for row in result["candidates"] + result["review"]}


def _audit_library(tmp_path):
    """All 14 audited folders with the evidence each verdict rested on."""
    lib = Library(tmp_path)
    # Ancient Aliens S21: 17 everywhere, pack rows E18-20 with placeholder titles.
    lib.season("tt1643266", "Ancient Aliens", 21, 17, range(1, 18),
               list(range(1, 18)) + [{"episode": n, "title": f"Episode {n}"} for n in (18, 19, 20)],
               size=8.02)
    # Desperate Housewives S8: TMDB merges the finale, TVmaze lists E23.
    lib.season("tt0410975", "Desperate Housewives", 8, 22, range(1, 24),
               list(range(1, 22)) + [{"episode": 22, "title": "Give Me the Blame"},
                                     {"episode": 23, "title": "Finishing the Hat", "aired": "2012-05-13"},
                                     {"episode": 24, "size": None, "hash": "hash-dh24", "source": None}],
               size=15.44)
    # Amazing Spaces S7: 8 everywhere, specials unnumbered; E10-16 have no payload.
    lib.season("tt3321546", "George Clarke's Amazing Spaces", 7, 8, range(1, 9),
               list(range(1, 9)) + [{"episode": n, "size": None, "hash": f"as-{n}", "source": None}
                                    for n in range(10, 17)])
    # Happy Valley S3: 6 everywhere, E07 without payload.
    lib.season("tt3428912", "Happy Valley", 3, 6, range(1, 7),
               list(range(1, 7)) + [{"episode": 7, "size": None, "hash": "hv-7", "source": None}])
    # Murder Most Puzzling S1: 3 two-hour episodes; E04-06 ride the same 12.4 GB pack.
    lib.season("tt13185754", "Murder Most Puzzling", 1, 3, range(1, 4), range(1, 7), size=12.4)
    # My Cat From Hell: S5/S6 slots above the boundary duplicate live S05E10, S06E04-07, S07E08-10.
    lib.season("tt1933073", "My Cat From Hell", 5, 10, range(1, 11),
               list(range(1, 10)) + [
                   {"episode": 10, "title": "Demon Cat"},
                   {"episode": 11, "title": "Demon Cat", "aired": "2014-07-05"},
                   {"episode": 12, "title": "Episode 12", "aired": "2014-07-12"},
                   {"episode": 13, "title": "Monty &amp; Tigger", "aired": "2014-07-15"},
                   {"episode": 14, "title": "Reese the Ripper", "aired": "2014-11-01"},
                   {"episode": 15, "title": "When the Fat Lady Sings", "aired": "2014-11-08"},
                   {"episode": 16, "title": "Appetite for Destruction", "aired": "2014-11-15"},
                   {"episode": 17, "title": "Chloe the Bully", "aired": "2014-11-22"},
                   {"episode": 18, "title": "Episode 18", "aired": "2014-11-29"},
               ], size=17.03)
    lib.season("tt1933073", "My Cat From Hell", 6, 7, [1, 2, 3, 4, 5, 7, 64],
               [1, 2, 3, {"episode": 4, "title": "Reese the Ripper"},
                {"episode": 5, "title": "When the Fat Lady Sings"},
                {"episode": 6, "title": "Appetite for Destruction"},
                {"episode": 7, "title": "Chloe the Bully"},
                {"episode": 8, "title": "Loves Bites!", "aired": "2015-05-30"},
                {"episode": 9, "title": "Woody the Killer Kitty", "aired": "2015-06-06"},
                {"episode": 10, "title": "Paranormal Cat-tivity", "aired": "2015-06-13"}],
               pack="hash-mcfh6", size=119.21)
    lib.season("tt1933073", "My Cat From Hell", 7, 10, range(1, 11),
               list(range(1, 8)) + [{"episode": 8, "title": "Loves Bites!"},
                                    {"episode": 9, "title": "Woody The Killer Kitty"},
                                    {"episode": 10, "title": "Paranormal Cat-Tivity"}],
               pack="hash-mcfh7")
    # Mysteries From Above S1: the legacy blind 1..24 registration on one pack.
    lib.season("tt19226058", "Mysteries From Above", 1, 10, range(1, 11), range(1, 25), size=10.9)
    # Phineas and Ferb S5: 38 everywhere; E39 on the pack, E40 its own small payload.
    lib.season("tt0852863", "Phineas and Ferb", 5, 38, range(1, 39),
               list(range(1, 40)) + [{"episode": 40, "size": 0.237, "hash": "hash-pf40",
                                      "source": "torrentio"}], size=44.22)
    # Great British Sewing Bee S1: 4 everywhere, E05-08 with zero payload.
    lib.season("tt3010856", "The Great British Sewing Bee", 1, 4, range(1, 5),
               list(range(1, 5)) + [{"episode": n, "size": 0.0, "hash": f"gbsb-{n}",
                                     "source": "mediafusion"} for n in range(5, 9)])
    # RHOA S6: Kandi's Wedding (TVDB specials) on the pack, E31 with no payload.
    lib.season("tt1252370", "The Real Housewives of Atlanta", 6, 27, range(1, 28),
               list(range(1, 28)) + [
                   {"episode": 28, "title": "Kandi's Wedding: Say Yes to the Distress", "aired": "2014-06-01"},
                   {"episode": 29, "title": "Kandi's Wedding: Blessings and Dressings", "aired": "2014-06-08"},
                   30,
                   {"episode": 31, "size": None, "hash": "rhoa-31", "source": None}],
               pack="hash-rhoa6", size=67.87)
    # RHOA S7 and S11: TVmaze lists the spin-off arcs as regular episodes.
    lib.season("tt1252370", "The Real Housewives of Atlanta", 7, 25, range(1, 29),
               list(range(1, 26)) + [
                   {"episode": 26, "title": "Kandi's Ski Trip Part 1", "aired": "2015-05-17"},
                   {"episode": 27, "title": "Kandi's Ski Trip Part 2", "aired": "2015-05-24"},
                   {"episode": 28, "title": "Kandi's Ski Trip: A Family Affair", "aired": "2015-06-07"}],
               pack="hash-rhoa7", size=64.74)
    lib.season("tt1252370", "The Real Housewives of Atlanta", 11, 23, range(1, 27),
               list(range(1, 24)) + [
                   {"episode": 24, "title": "Porsha's Having A Baby Part 1", "aired": "2019-04-28"},
                   {"episode": 25, "title": "Porsha's Having A Baby: Grandmother Dearest", "aired": "2019-05-05"},
                   {"episode": 26, "title": "Porsha's Having A Baby Part 3", "aired": "2019-05-12"}],
               pack="hash-rhoa11", size=54.87)
    # TWD Webisodes S1: TMDB restructured the show; E07-08 are real web series.
    lib.season("tt2022190", "The Walking Dead Webisodes", 1, 6, range(1, 7),
               [{"episode": n, "title": t} for n, t in enumerate(
                   ["Torn Apart - TWD", "Cold Storage - TWD", "The Oath - TWD",
                    "Flight 462 - FTWD", "Passage - FTWD", "Red Machete - TWD"], start=1)] + [
                   {"episode": 7, "title": "The Althea Tapes - FTWD", "aired": "2019-07-27"},
                   {"episode": 8, "title": "Dead in the Water - FTWD", "aired": "2022-04-10"}],
               size=0.92)
    return lib


AUDIT_RESTORE_17 = {
    ("tt0410975", 8, 23),
    ("tt13185754", 1, 4), ("tt13185754", 1, 5), ("tt13185754", 1, 6),
    ("tt1933073", 5, 13),
    ("tt0852863", 5, 40),
    ("tt1252370", 6, 28), ("tt1252370", 6, 29), ("tt1252370", 6, 30),
    ("tt1252370", 7, 26), ("tt1252370", 7, 27), ("tt1252370", 7, 28),
    ("tt1252370", 11, 24), ("tt1252370", 11, 25), ("tt1252370", 11, 26),
    ("tt2022190", 1, 7), ("tt2022190", 1, 8),
}


def test_audit_none_of_the_17_restored_entries_is_flagged(tmp_path):
    result = _audit_library(tmp_path).discover()

    flagged = {_key(row) for row in result["candidates"]}
    reviewed = {_key(row) for row in result["review"]}
    assert flagged & AUDIT_RESTORE_17 == set()
    assert AUDIT_RESTORE_17 <= reviewed
    # Everything the classifier still flags is an audited phantom (42 of 59 total).
    assert len(flagged) == 36
    assert all(row["class"] in {"phantom", "duplicate"} for row in result["candidates"])


def test_t1_legacy_blind_registration_is_phantom(tmp_path):
    classes = _classes(_audit_library(tmp_path).discover())

    assert {classes[("tt19226058", 1, n)] for n in range(11, 25)} == {"phantom"}


def test_t2_merged_two_parter_is_review_but_empty_slot_is_phantom(tmp_path):
    result = _audit_library(tmp_path).discover()
    rows = {_key(row): row for row in result["candidates"] + result["review"]}

    assert rows[("tt0410975", 8, 23)]["class"] == "review"
    assert rows[("tt0410975", 8, 23)]["reason"] == "second provider lists episode"
    assert rows[("tt0410975", 8, 24)]["class"] == "phantom"
    assert rows[("tt0410975", 8, 24)]["reason"] == "no payload"


def test_t3_spin_off_arc_listed_by_second_provider_is_review(tmp_path):
    classes = _classes(_audit_library(tmp_path).discover())

    for season, episodes in ((7, (26, 27, 28)), (11, (24, 25, 26))):
        assert {classes[("tt1252370", season, n)] for n in episodes} == {"review"}


def test_t4_moved_to_specials_is_review_by_provenance(tmp_path):
    result = _audit_library(tmp_path).discover()
    rows = {_key(row): row for row in result["candidates"] + result["review"]}

    assert rows[("tt1252370", 6, 28)]["reason"] == "sidecar carries a provider title"
    assert rows[("tt1252370", 6, 29)]["class"] == "review"
    assert rows[("tt1252370", 6, 30)]["reason"] == "not legacy blind-registration shape"
    assert rows[("tt1252370", 6, 31)]["class"] == "phantom"


def test_t5_duplicate_of_live_canonical_slot_records_target(tmp_path):
    result = _audit_library(tmp_path).discover()
    rows = {_key(row): row for row in result["candidates"]}

    for episode, canonical in ((8, 8), (9, 9), (10, 10)):
        row = rows[("tt1933073", 6, episode)]
        assert row["class"] == "duplicate"
        assert (row["canonical"]["season"], row["canonical"]["episode"]) == (7, canonical)
        assert row["canonical"]["token"] == f"tt1933073-7-{canonical}"
    assert rows[("tt1933073", 5, 14)]["canonical"]["episode"] == 4


def test_t6_duplicate_without_canonical_stub_is_review(tmp_path):
    lib = _audit_library(tmp_path)
    lib.stub("My Cat From Hell", 7, 8).unlink()

    result = lib.discover()
    rows = {_key(row): row for row in result["review"]}

    assert rows[("tt1933073", 6, 8)]["reason"] == "canonical slot missing"


def test_t6_duplicate_rechecked_at_apply_time(monkeypatch, tmp_path):
    lib = _audit_library(tmp_path)
    report = lib.discover()
    duplicate = next(row for row in report["candidates"]
                     if _key(row) == ("tt1933073", 6, 8))
    lib.stub("My Cat From Hell", 7, 8).unlink()
    _patch_apply(monkeypatch, lib, {"candidates": [duplicate], "review": [], "skipped": []})

    result = phantom_cleanup.cleanup(dry_run=False, quarantine_root=tmp_path / "q")

    assert result["quarantined"] == []
    assert result["review"][0]["reason"] == "canonical slot missing"
    assert Path(duplicate["strm_path"]).is_file()


def test_t5_generic_title_on_several_canonical_slots_is_review(tmp_path):
    lib = Library(tmp_path).season("tt1", "Show", 1, 3, range(1, 4), [
        {"episode": 1, "title": "Reunion"}, 2, {"episode": 3, "title": "Reunion"}])
    lib.season("tt1", "Show", 2, 2, range(1, 3), [
        1, 2, {"episode": 3, "title": "Reunion"}])

    rows = {_key(row): row for row in lib.discover()["review"]}

    assert rows[("tt1", 2, 3)]["reason"] == "title matches more than one canonical slot"


def test_t5_same_title_with_different_air_date_is_review(tmp_path):
    lib = Library(tmp_path).season("tt1", "Show", 1, 2, range(1, 3), [
        {"episode": 1, "title": "Pilot", "aired": "2020-01-01"}, 2,
        {"episode": 3, "title": "Pilot", "aired": "2021-05-05"}])

    rows = {_key(row): row for row in lib.discover()["review"]}

    assert rows[("tt1", 1, 3)]["reason"] == "air date differs from canonical slot"


def test_t7_restructured_show_is_review(tmp_path):
    classes = _classes(_audit_library(tmp_path).discover())

    assert classes[("tt2022190", 1, 7)] == classes[("tt2022190", 1, 8)] == "review"


def test_t8_distinct_single_payload_is_not_legacy_shape(tmp_path):
    result = _audit_library(tmp_path).discover()
    rows = {_key(row): row for row in result["review"]}

    assert rows[("tt0852863", 5, 40)]["reason"] == "not legacy blind-registration shape"
    assert rows[("tt13185754", 1, 4)]["reason"] == "not legacy blind-registration shape"


def test_t9_second_provider_unavailable_skips_season(tmp_path):
    lib = Library(tmp_path).season("tt1", "Show", 1, 8, None,
                                   list(range(1, 9)) + [{"episode": 9, "size": None}])

    result = lib.discover()

    assert result["candidates"] == [] and result["review"] == []
    assert result["skipped"][0]["reason"] == "second provider could not confirm season"


def test_t10_unnumbered_specials_do_not_veto(tmp_path):
    classes = _classes(_audit_library(tmp_path).discover())

    assert {classes[("tt3321546", 7, n)] for n in range(10, 17)} == {"phantom"}


def test_t11_placeholder_title_with_air_date_is_review(tmp_path):
    result = _audit_library(tmp_path).discover()
    rows = {_key(row): row for row in result["review"]}

    assert rows[("tt1933073", 5, 12)]["reason"] == "sidecar carries an air date"
    assert rows[("tt1933073", 5, 18)]["reason"] == "sidecar carries an air date"


def _patch_apply(monkeypatch, lib, report, marked=None, deleted=None):
    marked = [] if marked is None else marked
    deleted = [] if deleted is None else deleted
    monkeypatch.setattr(phantom_cleanup, "discover",
                        lambda imdb_ids=None, orphan_show_ids=None: report)
    monkeypatch.setattr(phantom_cleanup, "MEDIA_PATH", str(lib.media))
    monkeypatch.setattr(phantom_cleanup, "SPORE_MEDIA_PATH", str(lib.spore))
    monkeypatch.setattr(phantom_cleanup, "db", SimpleNamespace(
        delete_virtual_item=lambda token: deleted.append(token)))
    monkeypatch.setattr(phantom_cleanup.playback_guard, "active", lambda force=False: False)
    monkeypatch.setattr(phantom_cleanup.strm_generator, "_maintenance_lock", threading.Lock())
    monkeypatch.setitem(__import__("sys").modules, "media_servers",
                        SimpleNamespace(mark_removed=lambda path: marked.append(path)))
    return marked, deleted


def _empty_slots(tmp_path, shows, per_show):
    lib = Library(tmp_path)
    for index in range(shows):
        lib.season(f"tt{index:07d}", f"Show {index}", 1, 4, range(1, 5),
                   list(range(1, 5)) + [{"episode": n, "size": None, "hash": f"h{index}-{n}"}
                                        for n in range(5, 5 + per_show)])
    return lib


def test_t12_run_cap_writes_proposal_and_moves_nothing(monkeypatch, tmp_path):
    lib = _empty_slots(tmp_path, shows=4, per_show=3)
    report = lib.discover()
    assert len(report["candidates"]) == 12
    marked, deleted = _patch_apply(monkeypatch, lib, report)
    monkeypatch.setattr(phantom_cleanup, "MAX_QUARANTINE", 10)
    monkeypatch.setattr(phantom_cleanup, "MAX_PER_SHOW", 5)

    result = phantom_cleanup.cleanup(dry_run=False, quarantine_root=tmp_path / "q")

    assert result["status"] == "over_cap"
    assert "per-run cap" in result["reason"]
    assert marked == [] and deleted == []
    assert all(Path(row["strm_path"]).is_file() for row in report["candidates"])
    proposal = json.loads(Path(result["proposal"]).read_text())
    assert len(proposal["candidates"]) == 12
    assert not list((tmp_path / "q").rglob("manifest.json"))


def test_t13_per_show_cap_writes_proposal(monkeypatch, tmp_path):
    lib = _empty_slots(tmp_path, shows=1, per_show=6)
    report = lib.discover()
    marked, deleted = _patch_apply(monkeypatch, lib, report)
    monkeypatch.setattr(phantom_cleanup, "MAX_QUARANTINE", 10)
    monkeypatch.setattr(phantom_cleanup, "MAX_PER_SHOW", 5)

    result = phantom_cleanup.cleanup(dry_run=False, quarantine_root=tmp_path / "q")

    assert result["status"] == "over_cap"
    assert "per-show cap" in result["reason"]
    assert marked == [] and deleted == []


def test_audit_run_as_a_whole_is_over_cap(monkeypatch, tmp_path):
    lib = _audit_library(tmp_path)
    report = lib.discover()
    marked, deleted = _patch_apply(monkeypatch, lib, report)
    monkeypatch.setattr(phantom_cleanup, "MAX_QUARANTINE", 10)
    monkeypatch.setattr(phantom_cleanup, "MAX_PER_SHOW", 5)

    result = phantom_cleanup.cleanup(dry_run=False, quarantine_root=tmp_path / "q")

    assert result["status"] == "over_cap"
    assert marked == [] and deleted == []


def test_t14_played_episode_is_skipped(tmp_path):
    lib = Library(tmp_path).season(
        "tt1", "Show", 1, 8, range(1, 9),
        list(range(1, 9)) + [{"episode": 9, "size": None, "last_played": "2026-09-14 21:01:22"}])

    result = lib.discover()

    assert result["candidates"] == []
    assert result["skipped"][0]["reason"] == "episode has playback history"


def test_t15_manifest_keeps_restorable_row(monkeypatch, tmp_path):
    lib = Library(tmp_path).season("tt1", "Show", 1, 8, range(1, 9),
                                   list(range(1, 9)) + [{"episode": 9, "size": None,
                                                         "hash": "hash-9"}])
    original = dict(next(item for item in lib.items if item["episode"] == 9))
    report = lib.discover()
    marked, deleted = _patch_apply(monkeypatch, lib, report)

    result = phantom_cleanup.cleanup(dry_run=False, quarantine_root=tmp_path / "q")

    assert deleted == ["tt1-1-9"]
    manifest = json.loads((Path(result["quarantine"]) / "manifest.json").read_text())
    row = manifest["quarantined"][0]["row"]
    assert {key: value for key, value in original.items() if value is not None} == \
        {key: value for key, value in row.items() if value is not None}
    assert row == original


def test_tvmaze_client_reads_regular_numbers_only(monkeypatch):
    calls = []

    class Response:
        def __init__(self, status, payload=None, headers=None):
            self.status_code = status
            self._payload = payload
            self.headers = headers or {}

        def json(self):
            return self._payload

    replies = [
        Response(429, headers={"Retry-After": "1"}),
        Response(200, {"id": 7}),
        Response(200, [
            {"season": 7, "number": 8, "type": "regular"},
            {"season": 7, "number": None, "type": "insignificant_special"},
            {"season": 7, "number": 9, "type": "significant_special"},
            {"season": 6, "number": 1, "type": "regular"},
        ]),
    ]
    session = SimpleNamespace(get=lambda url, timeout: (calls.append(url), replies.pop(0))[1])
    monkeypatch.setattr(phantom_cleanup.time, "sleep", lambda seconds: None)
    client = phantom_cleanup.TvmazeClient(session=session, min_interval=0)

    assert client.regular_numbers("tt3321546", 7) == {8}
    assert client.regular_numbers("tt3321546", 6) == {1}
    assert calls == [
        "https://api.tvmaze.com/lookup/shows?imdb=tt3321546",
        "https://api.tvmaze.com/lookup/shows?imdb=tt3321546",
        "https://api.tvmaze.com/shows/7/episodes?specials=1",
    ]


@pytest.mark.parametrize("failure", ["status", "raise", "bad_json"])
def test_tvmaze_client_fails_closed(monkeypatch, failure):
    def get(url, timeout):
        if failure == "raise":
            raise OSError("offline")
        if failure == "status":
            return SimpleNamespace(status_code=404, headers={}, json=lambda: None)

        def bad():
            raise ValueError("not json")
        return SimpleNamespace(status_code=200, headers={}, json=bad)

    client = phantom_cleanup.TvmazeClient(session=SimpleNamespace(get=get), min_interval=0)

    assert client.regular_numbers("tt1", 1) is None
    assert phantom_cleanup._second_provider("tt1", 1, client) is None
