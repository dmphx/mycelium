"""scripts/repoint_series_identity.py: which replacement is safe, and the
compare-and-swap repoint in db."""
import importlib.util
import os
import sys
import types

import pytest

os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import release_sanity
from torrentio import TorrentioStream

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _load(name, path, extra=None):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    saved = {k: sys.modules.get(k) for k in (extra or {})}
    sys.modules.update(extra or {})
    try:
        spec.loader.exec_module(module)
    finally:
        for key, previous in saved.items():
            if previous is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = previous
    return module


tool = _load("repoint_series_identity_tool",
             os.path.join(_ROOT, "scripts", "repoint_series_identity.py"))

TMNT_1987 = release_sanity.ShowIdentity(
    imdb_id="tt0131613", name="Teenage Mutant Ninja Turtles", regions=frozenset({"US"}),
    year=1987, language="en",
    title_tokens=frozenset({"teenage", "mutant", "ninja", "turtles"}),
    namesakes=frozenset({(2003, frozenset({"US"})), (2012, frozenset({"US"}))}),
)


@pytest.fixture(autouse=True)
def _deterministic_settings(monkeypatch):
    stub = types.SimpleNamespace(get=lambda key, default=None: default)
    monkeypatch.setitem(sys.modules, "settings", stub)


def _stream(name, info_hash):
    return TorrentioStream(name=name, title=name, info_hash=info_hash, quality="1080p",
                           seeders=5, size_gb=1.0, is_season_pack=False, source="zilean")


@pytest.mark.parametrize("title,expected", [
    ("Turtle Tracks", True), ("An Unearthly Child", True), ("Leap of Faith", True),
    ("Episode 3", False), ("Part 2", False), ("The Final", False), ("Reunion", False),
    ("Series Finale", False), ("Heat 4", False), ("Pilot", False), ("Rose", False),
    (None, False),
])
def test_distinctive_title(title, expected):
    assert tool.distinctive_title(title) is expected


def test_choice_needs_a_qualifier_that_names_the_show():
    unqualified = _stream("Teenage Mutant Ninja Turtles S01E01 1080p", "a" * 40)
    named = _stream("Teenage Mutant Ninja Turtles (1987) S01E01 1080p", "b" * 40)
    choice, why = tool.choose_replacement([unqualified, named], TMNT_1987, "Episode 1", "c" * 40)
    assert choice is named and why == "qualifier"


def test_choice_accepts_a_distinctive_episode_title():
    titled = _stream("Teenage Mutant Ninja Turtles S01E01 Turtle Tracks 1080p", "a" * 40)
    choice, why = tool.choose_replacement([titled], TMNT_1987, "Turtle Tracks", "c" * 40)
    assert choice is titled and why == "episode title"


def test_choice_skips_a_foreign_dub_of_an_english_show():
    dub = _stream("Черепашки ниндзя / Teenage Mutant Ninja Turtles S01E01 Turtle Tracks",
                  "a" * 40)
    dub.languages = ("ru",)
    english = _stream("Teenage Mutant Ninja Turtles 1987 S01E01 1080p", "b" * 40)
    choice, _ = tool.choose_replacement([dub, english], TMNT_1987, "Turtle Tracks", "c" * 40)
    assert choice is english


def test_choice_skips_fansub_rips_of_an_english_show():
    fansub = _stream("TEENAGE MUTANT NINJA TURTLES 1987 - Seasons 1 to 10 - French FanSub TVRip",
                     "a" * 40)
    clean = _stream("Teenage Mutant Ninja Turtles 1987 S01E01 1080p", "b" * 40)
    choice, _ = tool.choose_replacement([fansub, clean], TMNT_1987, "Turtle Tracks", "c" * 40)
    assert choice is clean


def test_choice_refuses_ambiguous_or_current_releases():
    unqualified = _stream("Teenage Mutant Ninja Turtles S01E01 1080p", "a" * 40)
    current = _stream("Teenage Mutant Ninja Turtles 1987 S01E01 1080p", "c" * 40)
    assert tool.choose_replacement([unqualified, current], TMNT_1987, "Episode 1",
                                   "c" * 40) == (None, "")


def test_only_mixed_shows_are_repointed():
    findings = ([{"imdb_id": "tt_mixed"}] * 2) + ([{"imdb_id": "tt_whole"}] * 9)
    mixed, share = tool.mixed_show_ids(findings, {"tt_mixed": 10, "tt_whole": 10}, 0.8)
    assert mixed == {"tt_mixed"}
    assert share == {"tt_mixed": 0.2, "tt_whole": 0.9}


def test_whole_show_suspect_offers_only_duplicates_the_right_show_holds():
    present = {"tt_uk": {(5, 1)}}
    mixed = {"tt_mixed"}
    assert tool.eligible({"imdb_id": "tt_mixed", "season": 1, "episode": 1}, mixed, present)
    assert tool.eligible({"imdb_id": "tt_uk", "season": 5, "episode": 1}, mixed, present)
    # The US version lacks this episode: the mislabeled slot is its only copy.
    assert not tool.eligible({"imdb_id": "tt_uk", "season": 5, "episode": 2}, mixed, present)
    assert not tool.eligible({"imdb_id": "tt_other", "season": 1, "episode": 1}, mixed, present)


def test_repoint_virtual_item_swaps_only_the_expected_hash(tmp_path, monkeypatch):
    real_db = _load("repoint_real_db", os.path.join(_ROOT, "db.py"))
    monkeypatch.setattr(real_db, "DB_PATH", str(tmp_path / "test.db"))
    real_db.init()
    with real_db._connect() as conn:
        conn.execute(
            """INSERT INTO virtual_items (token, info_hash, magnet, title, media_type,
                   strm_path, imdb_id, season, episode, quality, source, torbox_id,
                   file_id, spore_tracks, size_gb)
               VALUES ('tok', 'aaaa', 'magnet:?xt=urn:btih:aaaa', 'Show S01E01', 'series',
                       '/data/media/series/Show/Season 01/Show S01E01.strm', 'tt1', 1, 1,
                       '720p', 'zilean', 42, 3, '{"audio": []}', 1.5)""")
        conn.commit()

    assert real_db.repoint_virtual_item("tok", "ffff", "bbbb", "m", "1080p", "x", 2.0) is False
    assert real_db.repoint_virtual_item("tok", "AAAA", "BBBB", "magnet:?xt=urn:btih:bbbb",
                                        "1080p", "identity/torrentio", 2.0) is True
    row = real_db.get_virtual_item("tok")
    assert row["info_hash"] == "bbbb"
    assert (row["quality"], row["source"], row["size_gb"]) == ("1080p", "identity/torrentio", 2.0)
    assert row["torbox_id"] is None and row["file_id"] is None and row["spore_tracks"] is None
    assert (row["protocol"], row["debrid_provider"]) == ("torrent", "torbox")
