"""scripts/reidentify_series.py: which plans are accepted, what happens to one
item, where its files go, and that a rollback puts a deleted item back."""
import importlib.util
import json
import os
import sys

import pytest

os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load("reidentify_series_tool",
             os.path.join(_ROOT, "scripts", "reidentify_series.py"))

UK_SLOT = {"season": 1, "episode": 1}


# ── plan validation ──────────────────────────────────────────────────────────

def test_plan_keeps_the_fields_the_run_needs():
    entries = tool.load_plan([
        {"wrong": "tt0412137", "right": "tt0437005", "action": "reidentify"},
        {"wrong": "tt0377260", "right": "tt1586680", "action": "empty", "note": "UK"},
        {"wrong": "tt0283799", "action": "skip"},
    ])
    assert [e["wrong"] for e in entries] == ["tt0412137", "tt0377260", "tt0283799"]
    assert entries[0]["action"] == "reidentify"
    assert entries[1]["note"] == "UK"
    assert entries[2]["right"] is None


def test_plan_accepts_a_shows_wrapper():
    assert tool.load_plan({"shows": [{"wrong": "tt0412137", "right": "tt0437005"}]})[0][
        "action"] == "reidentify"


@pytest.mark.parametrize("payload,message", [
    ([{"right": "tt0437005"}], "no valid 'wrong'"),
    ([{"wrong": "0412137", "right": "tt0437005"}], "no valid 'wrong'"),
    ([{"wrong": "tt0412137"}], "needs a 'right'"),
    ([{"wrong": "tt0412137", "right": "nope"}], "not an imdb id"),
    ([{"wrong": "tt0412137", "right": "tt0412137"}], "the same show"),
    ([{"wrong": "tt0412137", "right": "tt0437005", "action": "delete"}], "action must be"),
    ([{"wrong": "tt1", "right": "tt2"}], "no valid 'wrong'"),
    ("nope", "must be a list"),
])
def test_plan_refuses_input_it_cannot_act_on(payload, message):
    with pytest.raises(ValueError, match=message):
        tool.load_plan(payload)


def test_plan_refuses_the_same_show_twice():
    with pytest.raises(ValueError, match="twice"):
        tool.load_plan([{"wrong": "tt0412137", "right": "tt0437005"},
                        {"wrong": "tt0412137", "right": "tt1586680"}])


# ── per-item disposition ─────────────────────────────────────────────────────

def test_an_episode_the_right_show_lacks_moves():
    assert tool.classify(2, 1, "reidentify", set(), {(2, 1)}, "drop") == (
        tool.MOVE, "the right show lacks this episode")


def test_an_episode_the_right_show_holds_is_a_duplicate():
    assert tool.classify(1, 1, "reidentify", {(1, 1)}, {(1, 1)}, "drop")[0] == tool.DUPLICATE
    assert tool.classify(1, 1, "reidentify", {(1, 1)}, {(1, 1)}, "keep")[0] == tool.KEEP


def test_an_episode_the_right_show_does_not_have_is_a_ghost():
    """UK Shameless S05E13 has no US counterpart: the slot belongs to the UK
    show and was filled from a US pack, so it is emptied, never moved."""
    assert tool.classify(5, 13, "reidentify", set(), {(5, e) for e in range(1, 13)},
                         "drop") == (tool.GHOST, "the right show has no such episode")


def test_unknown_right_show_metadata_never_deletes():
    assert tool.classify(1, 1, "reidentify", set(), None, "drop") == (
        tool.KEEP, "no episode metadata for the right show")


def test_empty_treats_every_item_as_a_ghost():
    verdict, _ = tool.classify(1, 1, "empty", {(1, 1)}, {(1, 1)}, "drop")
    assert verdict == tool.GHOST


# ── paths ────────────────────────────────────────────────────────────────────

def test_episode_path_follows_the_layout_mycelium_writes():
    path = tool.episode_strm("/data/media", "Hell's Kitchen (US)", 2, 7)
    assert path.as_posix() == ("/data/media/series/Hell's Kitchen (US)/Season 02/"
                               "Hell's Kitchen (US) S02E07.strm")


def test_a_move_takes_the_strm_the_nfo_and_the_spore_stub_pair():
    from pathlib import Path
    moves = tool.planned_moves(
        Path("/data/media/series/Powers/Season 01/Powers S01E01.strm"),
        Path("/data/media/series/Powers (2015)/Season 01/Powers (2015) S01E01.strm"),
        Path("/data/plex-media/series/Powers/Season 01"),
        Path("/data/plex-media/series/Powers (2015)/Season 01"))
    assert [Path(dst).name for _, dst in moves] == [
        "Powers (2015) S01E01.strm", "Powers (2015) S01E01.nfo",
        "Powers (2015) S01E01.mkv", "Powers (2015) S01E01.minfo"]
    # The .minfo carries the catbox token, so it travels with the stub and
    # playback keeps resolving after the move.
    assert moves[-1][0].name == "Powers S01E01.minfo"


def test_a_show_that_already_shares_its_folder_moves_nothing():
    from pathlib import Path
    same = Path("/data/media/series/Antiques Roadshow/Season 27/Antiques Roadshow S27E02.strm")
    assert tool.planned_moves(same, same, Path("/a"), Path("/a")) == []


# ── smaller helpers ──────────────────────────────────────────────────────────

def test_episode_nfo_follows_the_episode_without_losing_streamdetails():
    nfo = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           "<episodedetails>\n"
           '  <uniqueid type="imdb" default="true">tt0412137</uniqueid>\n'
           '  <uniqueid type="tmdb">813</uniqueid>\n'
           "  <fileinfo><streamdetails><video><codec>hevc</codec></video>"
           "</streamdetails></fileinfo>\n</episodedetails>\n")
    out = tool.retag_episode_nfo(nfo, "tt0437005", 2370)
    assert '<uniqueid type="imdb" default="true">tt0437005</uniqueid>' in out
    assert '<uniqueid type="tmdb">2370</uniqueid>' in out
    assert "<codec>hevc</codec>" in out


def test_episode_nfo_drops_a_tmdb_id_there_is_no_replacement_for():
    nfo = ('<episodedetails>\n  <uniqueid type="imdb">tt0412137</uniqueid>\n'
           '  <uniqueid type="tmdb">813</uniqueid>\n</episodedetails>\n')
    out = tool.retag_episode_nfo(nfo, "tt0437005", None)
    assert "tt0437005" in out and "813" not in out and "tmdb" not in out


def test_seasons_union_keeps_what_the_right_show_already_monitors():
    assert tool.seasons_union("1,2,3", [3, 4]) == [1, 2, 3, 4]
    assert tool.seasons_union(None, [2]) == [2]
    assert tool.seasons_union("1, ,bad,2", []) == [1, 2]


def test_only_an_item_that_still_holds_the_flagged_release_is_touched():
    finding = {"info_hash": "AABB"}
    assert tool.still_wrong({"info_hash": "aabb"}, finding) is True
    assert tool.still_wrong({"info_hash": "cccc"}, finding) is False
    assert tool.still_wrong({"info_hash": "aabb"}, None) is False


def test_episode_stem_matches_the_folder_name():
    assert tool.episode_stem("Shameless (US)", 5, 13) == "Shameless (US) S05E13"


# ── rollback of a removed item ───────────────────────────────────────────────

def test_rollback_reinserts_a_removed_item_and_its_files(tmp_path, monkeypatch):
    """A ghost or duplicate is deleted, so rollback has to put the row and the
    files back, not just flip a column."""
    real_db = _load("reidentify_real_db", os.path.join(_ROOT, "db.py"))
    monkeypatch.setattr(real_db, "DB_PATH", str(tmp_path / "test.db"))
    real_db.init()
    strm = tmp_path / "media" / "series" / "Shameless" / "Season 05" / "Shameless S05E13.strm"
    strm.parent.mkdir(parents=True)
    strm.write_text("/stream/abc123", encoding="utf-8")
    with real_db._connect() as conn:
        conn.execute(
            """INSERT INTO virtual_items (token, info_hash, magnet, title, media_type,
                   strm_path, imdb_id, season, episode, quality, play_count)
               VALUES ('abc123', 'aaaa', 'magnet:?xt=urn:btih:aaaa', 'Shameless S05E13',
                       'series', ?, 'tt0377260', 5, 13, '1080p', 3)""", (str(strm),))
        conn.execute(
            """INSERT INTO wanted_episodes (imdb_id, title, season, episode, status,
                   attempt_count)
               VALUES ('tt0377260', 'Shameless', 5, 13, 'found', 2)""")
        conn.commit()

    backup = tmp_path / "backup"
    (backup / "files").mkdir(parents=True)
    row = real_db.get_virtual_item("abc123")
    saved = tool._backup_file(strm, backup)
    before = tool._requeue_slot(real_db, "tt0377260", 5, 13)
    strm.unlink()
    real_db.delete_virtual_item("abc123")
    tool._record(backup, {"kind": tool.GHOST, "row": row, "files": [saved],
                          "wrong_wanted": before})

    assert real_db.get_virtual_item("abc123") is None
    assert real_db.get_wanted_episode("tt0377260", 5, 13)["status"] == "wanted"

    monkeypatch.setitem(sys.modules, "db", real_db)
    monkeypatch.setitem(sys.modules, "media_servers",
                        _fake_media_servers())
    tool.rollback(_args(rollback=str(backup)))

    restored = real_db.get_virtual_item("abc123")
    assert restored["imdb_id"] == "tt0377260" and restored["play_count"] == 3
    assert restored["strm_path"] == str(strm)
    assert strm.read_text(encoding="utf-8") == "/stream/abc123"
    wanted = real_db.get_wanted_episode("tt0377260", 5, 13)
    assert (wanted["status"], wanted["attempt_count"]) == ("found", 2)


def test_rollback_moves_a_moved_item_back(tmp_path, monkeypatch):
    """Undoing a move puts the files, the row, the episode NFO and both
    wanted rows back as they were."""
    from pathlib import Path
    real_db = _load("reidentify_real_db3", os.path.join(_ROOT, "db.py"))
    monkeypatch.setattr(real_db, "DB_PATH", str(tmp_path / "test3.db"))
    real_db.init()
    media, spore = tmp_path / "media", tmp_path / "plex-media"
    old = media / "series" / "Powers" / "Season 01" / "Powers S01E01.strm"
    new = media / "series" / "Powers (2015)" / "Season 01" / "Powers (2015) S01E01.strm"
    old_stub = spore / "series" / "Powers" / "Season 01"
    new_stub = spore / "series" / "Powers (2015)" / "Season 01"
    old.parent.mkdir(parents=True)
    old_stub.mkdir(parents=True)
    old.write_text("/stream/tok", encoding="utf-8")
    old.with_suffix(".nfo").write_text(
        '<episodedetails><uniqueid type="imdb">tt0398546</uniqueid></episodedetails>',
        encoding="utf-8")
    (old_stub / "Powers S01E01.mkv").write_bytes(b"stub")
    (old_stub / "Powers S01E01.minfo").write_text("token=tok\nsize=1\n", encoding="utf-8")
    with real_db._connect() as conn:
        conn.execute(
            """INSERT INTO virtual_items (token, info_hash, magnet, title, media_type,
                   strm_path, imdb_id, season, episode, play_count)
               VALUES ('tok', 'aaaa', 'm', 'Powers S01E01', 'series', ?, 'tt0398546',
                       1, 1, 4)""", (str(old),))
        conn.execute("""INSERT INTO wanted_episodes (imdb_id, title, season, episode,
                            status, attempt_count)
                        VALUES ('tt0398546', 'Powers', 1, 1, 'found', 1)""")
        conn.commit()
    row = real_db.get_virtual_item("tok")

    # Apply the move the way apply_show does.
    moved = tool._move_files([(str(s), str(d)) for s, d in tool.planned_moves(
        old, new, old_stub, new_stub)])
    with real_db._connect() as conn:
        conn.execute("UPDATE virtual_items SET imdb_id='tt1851040', "
                     "title='Powers (2015) S01E01', strm_path=? WHERE token='tok'",
                     (str(new),))
        conn.commit()
    nfo_before = tool._retag_nfo(new.with_suffix(".nfo"), "tt1851040", None)
    wrong_before = tool._requeue_slot(real_db, "tt0398546", 1, 1)
    right_before = tool._mark_found(real_db, "tt1851040", 42531, "Powers", 1, 1)
    backup = tmp_path / "backup3"
    backup.mkdir()
    tool._record(backup, {"kind": tool.MOVE, "row": row, "right": "tt1851040",
                          "new_strm": str(new), "new_title": "Powers (2015) S01E01",
                          "moved": moved, "nfo_before": nfo_before,
                          "wrong_wanted": wrong_before, "right_wanted": right_before})
    assert new.exists() and not old.exists()
    assert (new_stub / "Powers (2015) S01E01.minfo").exists()
    assert "tt1851040" in new.with_suffix(".nfo").read_text(encoding="utf-8")

    monkeypatch.setitem(sys.modules, "db", real_db)
    monkeypatch.setitem(sys.modules, "media_servers", _fake_media_servers())
    tool.rollback(_args(rollback=str(backup)))

    back = real_db.get_virtual_item("tok")
    assert (back["imdb_id"], back["strm_path"], back["title"]) == (
        "tt0398546", str(old), "Powers S01E01")
    assert back["play_count"] == 4
    assert old.exists() and not new.exists()
    assert (old_stub / "Powers S01E01.minfo").read_text(encoding="utf-8").startswith(
        "token=tok")
    assert "tt0398546" in old.with_suffix(".nfo").read_text(encoding="utf-8")
    assert real_db.get_wanted_episode("tt0398546", 1, 1)["status"] == "found"
    assert real_db.get_wanted_episode("tt1851040", 1, 1) is None


def test_rollback_leaves_an_item_that_changed_since_the_run(tmp_path, monkeypatch):
    real_db = _load("reidentify_real_db2", os.path.join(_ROOT, "db.py"))
    monkeypatch.setattr(real_db, "DB_PATH", str(tmp_path / "test2.db"))
    real_db.init()
    with real_db._connect() as conn:
        conn.execute(
            """INSERT INTO virtual_items (token, info_hash, magnet, title, media_type,
                   strm_path, imdb_id, season, episode)
               VALUES ('tok', 'bbbb', 'm', 'Show S01E01', 'series', '/x.strm',
                       'tt1', 1, 1)""")
        conn.commit()
    backup = tmp_path / "backup2"
    backup.mkdir()
    tool._record(backup, {"kind": tool.GHOST,
                          "row": {"token": "tok", "imdb_id": "tt1", "season": 1,
                                  "episode": 1, "strm_path": "/x.strm"},
                          "files": [], "wrong_wanted": None})
    monkeypatch.setitem(sys.modules, "db", real_db)
    monkeypatch.setitem(sys.modules, "media_servers", _fake_media_servers())
    tool.rollback(_args(rollback=str(backup)))
    # The row is still there, so the rollback must not insert a second one.
    with real_db._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM virtual_items WHERE token='tok'").fetchone()[0] == 1


def test_folder_nfo_is_repointed_only_once_the_folder_is_one_show(tmp_path, monkeypatch):
    """Antiques Roadshow, Bullseye and friends never had two folders: one
    folder, one tvshow.nfo, two imdb ids in the DB. The nfo may only follow the
    move when nothing under the folder still belongs to another show."""
    import types
    folder = tmp_path / "media" / "series" / "Bullseye"
    folder.mkdir(parents=True)
    nfo = folder / "tvshow.nfo"
    nfo.write_text('<tvshow><title>Bullseye</title>'
                   '<uniqueid type="imdb" default="true">tt0200329</uniqueid></tvshow>',
                   encoding="utf-8")
    backup = tmp_path / "backup"
    backup.mkdir()
    written = {}
    fake_gen = types.SimpleNamespace(
        MEDIA_PATH=str(tmp_path / "media"),
        _write_nfo=lambda *a, **kw: written.update(
            {"imdb": a[1], "path": kw["nfo_path"]}))
    plan = {"entry": {"right": "tt35800978"}, "right_folder": "Bullseye",
            "right_tmdb_id": 305225}

    mixed = types.SimpleNamespace(
        get_virtual_item_imdb_ids_under_path=lambda p: {"tt35800978", "tt0200329"})
    assert tool._retitle_folder_nfo(mixed, fake_gen, plan, backup) is False
    assert nfo.exists() and not written

    clean = types.SimpleNamespace(
        get_virtual_item_imdb_ids_under_path=lambda p: {"tt35800978"})
    assert tool._retitle_folder_nfo(clean, fake_gen, plan, backup) is True
    assert written["imdb"] == "tt35800978"
    assert not nfo.exists()  # removed so _write_nfo, which never overwrites, can write
    record = json.loads((backup / "rows.jsonl").read_text(encoding="utf-8"))
    assert record["kind"] == "nfo" and record["was"] == "tt0200329"

    monkeypatch.setitem(sys.modules, "db", types.SimpleNamespace(
        get_virtual_item=lambda token: None))
    monkeypatch.setitem(sys.modules, "media_servers", _fake_media_servers())
    tool.rollback(_args(rollback=str(backup)))
    assert "tt0200329" in nfo.read_text(encoding="utf-8")


def test_folder_nfo_that_already_names_the_right_show_is_left_alone(tmp_path):
    import types
    folder = tmp_path / "media" / "series" / "Antiques Roadshow"
    folder.mkdir(parents=True)
    (folder / "tvshow.nfo").write_text(
        '<tvshow><uniqueid type="imdb">tt0159847</uniqueid></tvshow>', encoding="utf-8")
    plan = {"entry": {"right": "tt0159847"}, "right_folder": "Antiques Roadshow"}
    fake_gen = types.SimpleNamespace(MEDIA_PATH=str(tmp_path / "media"))
    assert tool._retitle_folder_nfo(None, fake_gen, plan, tmp_path / "backup") is False


def _fake_media_servers():
    import types
    return types.SimpleNamespace(mark=lambda *a: None, mark_removed=lambda *a: None,
                                 _flush=lambda: None)


def _args(**fields):
    import types
    return types.SimpleNamespace(**fields)
