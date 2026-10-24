"""Per-season and absolute episode numbers must not coexist in one season.

TMDB numbers some long anime by a running count inside each season (season 2
holds episodes 33-53). Items registered with per-season numbers (S02E01..E21)
then duplicated the absolute set in the same folder, and a season that only
had the per-season copy would be lost by a blind cleanup. Synthetic data only.
"""
import os
import sys
import types
from pathlib import Path

import pytest

os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import episode_scheme  # noqa: E402
import strm_generator  # noqa: E402

SHOW = "Synthetic Saga"


class FakeTmdb:
    """Season 1 = 1-3 (per-season and absolute agree), season 2 = 4-6."""
    seasons = {1: [1, 2, 3], 2: [4, 5, 6], 3: [2, 5, 9]}

    def find_by_imdb(self, imdb_id, kind="tv"):
        return 99

    def get_season_episodes(self, tmdb_id, season):
        return [{"episode_number": n} for n in self.seasons.get(season, [])]


@pytest.fixture(autouse=True)
def _clear_cache():
    episode_scheme._cache.clear()
    yield
    episode_scheme._cache.clear()


def test_offset_needs_a_contiguous_range_above_one():
    assert episode_scheme.absolute_offset((33, 34, 35)) == 33
    assert episode_scheme.absolute_offset((1, 2, 3)) is None
    assert episode_scheme.absolute_offset((2, 5, 9)) is None
    assert episode_scheme.absolute_offset(()) is None


def test_per_season_numbers_map_onto_the_absolute_range():
    tm = FakeTmdb()
    assert episode_scheme.tmdb_episode_number("tt1", 2, 1, tmdb_client=tm) == 4
    assert episode_scheme.tmdb_episode_number("tt1", 2, 3, tmdb_client=tm) == 6
    # Already absolute, beyond the count, or a regular season: unchanged.
    assert episode_scheme.tmdb_episode_number("tt1", 2, 5, tmdb_client=tm) == 5
    assert episode_scheme.tmdb_episode_number("tt1", 1, 2, tmdb_client=tm) == 2
    assert episode_scheme.tmdb_episode_number("tt1", 3, 1, tmdb_client=tm) == 1


def _item(token, season, episode, strm_path, play_count=0):
    return {"token": token, "imdb_id": "tt1", "media_type": "series",
            "title": f"{SHOW} S{season:02d}E{episode:02d}", "season": season,
            "episode": episode, "strm_path": str(strm_path), "play_count": play_count}


def test_discover_splits_duplicates_from_episodes_to_renumber():
    items = [
        _item("a1", 2, 1, "/m/a1.strm"),   # twin S02E04 exists -> duplicate
        _item("a4", 2, 4, "/m/a4.strm"),
        _item("a2", 2, 2, "/m/a2.strm"),   # no S02E05 -> renumber, keep it
        _item("s1", 1, 1, "/m/s1.strm"),   # regular season, untouched
    ]
    plan = episode_scheme.discover(items=items, tmdb_client=FakeTmdb())
    assert [(d["token"], d["absolute"], d["twin_token"]) for d in plan["duplicates"]] == [
        ("a1", 4, "a4")]
    assert [(r["token"], r["absolute"]) for r in plan["renumber"]] == [("a2", 5)]


@pytest.fixture()
def library(tmp_path, monkeypatch):
    media = tmp_path / "media"
    spore = tmp_path / "plex-media"
    rows = {}

    class FakeDb:
        def get_all_virtual_items(self):
            return [dict(r) for r in rows.values()]

        def get_virtual_item(self, token):
            return dict(rows[token]) if token in rows else None

        def delete_virtual_item(self, token):
            rows.pop(token, None)

        def restore_virtual_item(self, row):
            rows[row["token"]] = dict(row)

        def renumber_virtual_episode(self, token, episode, title, strm_path):
            rows[token].update(episode=episode, title=title, strm_path=strm_path,
                               file_id=None)

        def mark_episode_status(self, *a):
            pass

    monkeypatch.setattr(episode_scheme, "db", FakeDb())
    monkeypatch.setattr(episode_scheme, "MEDIA_PATH", str(media))
    monkeypatch.setattr(episode_scheme, "SPORE_MEDIA_PATH", str(spore))
    monkeypatch.setattr(strm_generator, "MEDIA_PATH", str(media))
    monkeypatch.setattr(strm_generator, "SPORE_MEDIA_PATH", str(spore))
    monkeypatch.setattr(episode_scheme, "playback_guard",
                        types.SimpleNamespace(active=lambda force=False: False))
    monkeypatch.setitem(sys.modules, "media_servers", types.SimpleNamespace(
        mark=lambda p: None, mark_removed=lambda p: None))

    def add(token, season, episode, play_count=0):
        season_dir = f"Season {season:02d}"
        strm = media / "series" / SHOW / season_dir / f"{SHOW} S{season:02d}E{episode:02d}.strm"
        stub = spore / "series" / SHOW / season_dir
        strm.parent.mkdir(parents=True, exist_ok=True)
        stub.mkdir(parents=True, exist_ok=True)
        strm.write_text(f"http://mycelium/stream/{token}")
        strm.with_suffix(".nfo").write_text("<episodedetails/>")
        (stub / f"{strm.stem}.mkv").write_bytes(b"stub")
        (stub / f"{strm.stem}.minfo").write_text(f"token={token}\n")
        rows[token] = _item(token, season, episode, strm, play_count)
        return strm

    return types.SimpleNamespace(media=media, spore=spore, rows=rows, add=add,
                                 quarantine=tmp_path / "quarantine")


def test_reconcile_quarantines_duplicates_and_renames_the_only_copy(library):
    dup = library.add("a1", 2, 1)
    library.add("a4", 2, 4)
    only = library.add("a2", 2, 2, play_count=3)

    result = episode_scheme.reconcile(dry_run=False, tmdb_client=FakeTmdb(),
                                      quarantine_root=library.quarantine)

    assert result["status"] == "complete"
    assert result["duplicates_removed"] == 1 and result["renumbered"] == 1
    assert "a1" not in library.rows and not dup.exists()
    # The episode with no absolute copy is kept under its TMDB number.
    renamed = only.with_name(f"{SHOW} S02E05.strm")
    assert renamed.read_text() == "http://mycelium/stream/a2"
    assert not only.exists()
    stub_dir = library.spore / "series" / SHOW / "Season 02"
    assert (stub_dir / f"{SHOW} S02E05.minfo").read_text() == "token=a2\n"
    assert library.rows["a2"]["episode"] == 5
    assert library.rows["a2"]["strm_path"] == str(renamed)
    # Every season still has all of its episodes.
    assert sorted(r["episode"] for r in library.rows.values()) == [4, 5]
    manifest = Path(result["manifest"])
    assert manifest.is_file() and (manifest.parent / "media").is_dir()


def test_reconcile_respects_its_limit_and_dry_run(library):
    library.add("a1", 2, 1)
    library.add("a4", 2, 4)
    library.add("a2", 2, 2)
    assert episode_scheme.reconcile(dry_run=True, tmdb_client=FakeTmdb())["status"] == "dry_run"
    assert set(library.rows) == {"a1", "a4", "a2"}
    result = episode_scheme.reconcile(dry_run=False, limit=1, tmdb_client=FakeTmdb(),
                                      quarantine_root=library.quarantine)
    assert result["duplicates_removed"] + result["renumbered"] == 1


def test_reconcile_waits_for_playback(library, monkeypatch):
    library.add("a1", 2, 1)
    library.add("a4", 2, 4)
    monkeypatch.setattr(episode_scheme, "playback_guard",
                        types.SimpleNamespace(active=lambda force=False: True))
    result = episode_scheme.reconcile(dry_run=False, tmdb_client=FakeTmdb())
    assert result["status"] == "deferred_playback"
    assert set(library.rows) == {"a1", "a4"}


# ── show folders for ambiguous titles ────────────────────────────────────────

@pytest.fixture()
def series_root(tmp_path, monkeypatch):
    monkeypatch.setattr(strm_generator, "MEDIA_PATH", str(tmp_path))
    (tmp_path / "series").mkdir()
    return tmp_path


def _fake_tmdb(monkeypatch, hit, search_results):
    def _get(path, params=None, timeout=10):
        if path.startswith("/find/"):
            return {"tv_results": [hit]}
        if path == "/search/tv":
            return {"results": search_results}
        return {}
    monkeypatch.setitem(sys.modules, "tmdb", types.SimpleNamespace(_get=_get))


def test_ambiguous_show_name_gets_its_year(series_root, monkeypatch):
    hit = {"id": 1, "name": "The Office", "first_air_date": "2005-03-24"}
    _fake_tmdb(monkeypatch, hit, [hit, {"id": 2, "name": "The Office",
                                        "first_air_date": "2001-07-09"}])
    assert strm_generator._canonical_series_folder("tt0386676") == "The Office (2005)"


def test_same_name_same_year_adds_the_imdb_hint(series_root, monkeypatch):
    hit = {"id": 1, "name": "The Tribe", "first_air_date": "1999-04-24"}
    _fake_tmdb(monkeypatch, hit, [hit, {"id": 3, "name": "The Tribe",
                                        "first_air_date": "1999-01-01"}])
    assert (strm_generator._canonical_series_folder("tt0205700")
            == "The Tribe (1999) {imdb-tt0205700}")


def test_unique_show_name_stays_plain(series_root, monkeypatch):
    hit = {"id": 1, "name": "Synthetic Saga", "first_air_date": "2010-01-01"}
    _fake_tmdb(monkeypatch, hit, [hit])
    assert strm_generator._canonical_series_folder("tt0000009") == "Synthetic Saga"


def test_existing_show_folder_is_never_renamed(series_root, monkeypatch):
    folder = series_root / "series" / "The Office"
    folder.mkdir()
    (folder / "tvshow.nfo").write_text(
        '<tvshow><uniqueid type="imdb">tt0386676</uniqueid></tvshow>')
    hit = {"id": 1, "name": "The Office", "first_air_date": "2005-03-24"}
    _fake_tmdb(monkeypatch, hit, [hit, {"id": 2, "name": "The Office",
                                        "first_air_date": "2001-07-09"}])
    assert strm_generator._canonical_series_folder("tt0386676") == "The Office"
