import threading
from pathlib import Path
from types import SimpleNamespace

import phantom_cleanup


class FakeTmdb:
    def find_by_imdb(self, imdb_id, kind="tv"):
        return 42

    def get_show_info(self, tmdb_id):
        return {"seasons": [{"season_number": 1, "episode_count": 8}]}

    def get_season_episodes(self, tmdb_id, season):
        return [{"episode_number": number} for number in range(1, 9)]


def _item(episode, **updates):
    item = {
        "token": f"token-{episode}",
        "imdb_id": "tt1234567",
        "media_type": "series",
        "title": "Short Season",
        "season": 1,
        "episode": episode,
        "last_played": None,
        "strm_path": f"/media/series/Short Season/Season 01/Short Season S01E{episode:02d}.strm",
    }
    item.update(updates)
    return item


def test_discover_only_returns_unplayed_episodes_past_confirmed_boundary():
    result = phantom_cleanup.discover(
        items=[_item(8), _item(9), _item(10, last_played="2026-08-01 12:00:00")],
        tmdb_client=FakeTmdb(),
    )

    assert [row["episode"] for row in result["candidates"]] == [9]
    assert result["candidates"][0]["official_last"] == 8
    assert result["skipped"][0]["reason"] == "episode has playback history"


def test_discover_fails_closed_when_tmdb_payloads_disagree():
    client = FakeTmdb()
    client.get_season_episodes = lambda tmdb_id, season: [
        {"episode_number": number} for number in range(1, 8)
    ]

    result = phantom_cleanup.discover(items=[_item(9)], tmdb_client=client)

    assert result["candidates"] == []
    assert result["skipped"][0]["reason"] == "TMDB season boundary not authoritative"


def test_discover_finds_explicit_orphan_spore_stubs(monkeypatch, tmp_path):
    stub = (tmp_path / "spore" / "series" / "Short Season" / "Season 01" /
            "Short Season S01E09.mkv")
    stub.parent.mkdir(parents=True)
    stub.write_text("stub", encoding="utf-8")
    stub.with_suffix(".minfo").write_text("token=orphan-token\n", encoding="utf-8")

    result = phantom_cleanup.discover(
        items=[],
        tmdb_client=FakeTmdb(),
        orphan_show_ids={"Short Season": "tt1234567"},
        spore_root=tmp_path / "spore",
    )

    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["episode"] == 9
    assert result["candidates"][0]["orphan_stub"] is True
    assert result["candidates"][0]["token"] is None


def test_cleanup_quarantines_exact_files_and_deletes_row(monkeypatch, tmp_path):
    media_root = tmp_path / "media"
    spore_root = tmp_path / "spore"
    strm = media_root / "series" / "Short Season" / "Season 01" / "Short Season S01E09.strm"
    stub = spore_root / strm.relative_to(media_root).with_suffix(".mkv")
    minfo = stub.with_suffix(".minfo")
    for path in (strm, stub, minfo):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test", encoding="utf-8")

    item = _item(9, strm_path=str(strm))
    deleted = []
    marked = []
    fake_db = SimpleNamespace(
        get_all_virtual_items=lambda: [item],
        delete_virtual_item=lambda token: deleted.append(token),
    )
    fake_servers = SimpleNamespace(mark_removed=lambda path: marked.append(path))
    monkeypatch.setattr(phantom_cleanup, "db", fake_db)
    monkeypatch.setattr(phantom_cleanup, "MEDIA_PATH", str(media_root))
    monkeypatch.setattr(phantom_cleanup, "SPORE_MEDIA_PATH", str(spore_root))
    monkeypatch.setattr(phantom_cleanup, "tmdb", FakeTmdb())
    monkeypatch.setattr(phantom_cleanup.playback_guard, "active", lambda force=False: False)
    monkeypatch.setattr(phantom_cleanup.strm_generator, "_maintenance_lock", threading.Lock())
    monkeypatch.setitem(__import__("sys").modules, "media_servers", fake_servers)

    result = phantom_cleanup.cleanup(
        dry_run=False,
        quarantine_root=tmp_path / "quarantine",
    )

    assert result["status"] == "complete"
    assert len(result["quarantined"]) == 1
    assert deleted == ["token-9"]
    assert marked == [strm]
    assert not strm.exists()
    assert not stub.exists()
    assert not minfo.exists()
    quarantine = Path(result["quarantine"])
    assert (quarantine / "media" / strm.relative_to(media_root)).is_file()
    assert (quarantine / "spore" / stub.relative_to(spore_root)).is_file()
    assert (quarantine / "manifest.json").is_file()


def test_cleanup_refuses_to_apply_during_playback(monkeypatch):
    monkeypatch.setattr(phantom_cleanup, "discover", lambda imdb_ids=None, orphan_show_ids=None: {
        "candidates": [_item(9)],
        "skipped": [],
    })
    monkeypatch.setattr(phantom_cleanup.playback_guard, "active", lambda force=False: True)

    result = phantom_cleanup.cleanup(dry_run=False)

    assert result["status"] == "deferred_playback"
