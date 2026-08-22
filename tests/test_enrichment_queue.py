import importlib.util
import os

import pytest


def _load_real_db():
    spec = importlib.util.spec_from_file_location(
        "enrichment_queue_db", os.path.join(os.path.dirname(__file__), "..", "db.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


db = _load_real_db()


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    old_conn = getattr(db._tls, "conn", None)
    if old_conn is not None:
        old_conn.close()
        del db._tls.conn
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "enrichment.db"))
    db.init()
    yield
    conn = getattr(db._tls, "conn", None)
    if conn is not None:
        conn.close()
        del db._tls.conn


def _episode(season, episode, info_hash=None):
    token = f"s{season:02d}e{episode:03d}"
    h = info_hash or (str(season) * 40)
    db.insert_virtual_item(
        token=token,
        info_hash=h,
        magnet=f"magnet:?xt=urn:btih:{h}",
        title=f"Long Show S{season:02d}E{episode:02d}",
        media_type="series",
        strm_path=f"/data/media/series/Long Show/Season {season:02d}/{token}.strm",
        imdb_id="tt1234567",
        season=season,
        episode=episode,
    )
    return token


def test_progression_caps_current_season_and_adds_four_next_episodes():
    for episode in range(1, 46):
        _episode(1, episode)
    for episode in range(1, 9):
        _episode(2, episode)

    items = db.get_series_enrichment_items("s01e005", season_cap=40, next_count=4)

    current = [item for item in items if item["season"] == 1]
    following = [item for item in items if item["season"] == 2]
    assert len(current) == 40
    assert [item["episode"] for item in following] == [1, 2, 3, 4]
    priorities = {item["episode"]: item["enrichment_priority"] for item in current}
    assert priorities[6] == 0
    assert priorities[7] == 1


def test_same_release_stays_complete_but_new_hash_requeues():
    token = _episode(1, 1, info_hash="a" * 40)
    item = db.get_virtual_item(token)
    item["enrichment_priority"] = 0
    db.queue_media_enrichment([item])
    db.set_enrichment_state([token], "complete")

    db.queue_media_enrichment([item])
    assert db.enrichment_counts() == {"complete": 1}

    changed = dict(item)
    changed["info_hash"] = "b" * 40
    db.queue_media_enrichment([changed])
    assert db.enrichment_counts() == {"queued": 1}


def test_recovery_candidates_are_limited_to_incomplete_queue_items():
    queued = _episode(1, 1)
    complete = _episode(1, 2)
    for token in (queued, complete):
        item = db.get_virtual_item(token)
        item["enrichment_priority"] = 0
        db.queue_media_enrichment([item])
    db.set_enrichment_state([complete], "complete")

    candidates = db.get_enrichment_recovery_items()

    assert [(item["token"], item["state"]) for item in candidates] == [
        (queued, "queued")
    ]


def test_interrupted_states_are_requeued():
    downloading = _episode(1, 1)
    analyzing = _episode(1, 2)
    for token, state in ((downloading, "downloading"), (analyzing, "analyzing")):
        item = db.get_virtual_item(token)
        item["enrichment_priority"] = 0
        db.queue_media_enrichment([item])
        db.set_enrichment_state([token], state)

    assert db.reset_interrupted_enrichment() == 2
    assert db.enrichment_counts() == {"queued": 2}
