import importlib.util
import os

import pytest


def _load_real_db():
    spec = importlib.util.spec_from_file_location(
        "wanted_queue_db", os.path.join(os.path.dirname(__file__), "..", "db.py"))
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
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "wanted.db"))
    db.init()
    yield
    conn = getattr(db._tls, "conn", None)
    if conn is not None:
        conn.close()
        del db._tls.conn


def _insert_episode(index, recent=True, attempts=0, attempted_ago_hours=None):
    air_modifier = "-1 day" if recent else "-180 days"
    with db._connect() as conn:
        conn.execute(
            """INSERT INTO wanted_episodes
               (imdb_id, title, season, episode, air_date, status,
                attempt_count, last_attempted)
               VALUES (?, ?, 1, ?, date('now', ?), 'wanted', ?,
                       CASE WHEN ? IS NULL THEN NULL ELSE datetime('now', ?) END)""",
            (f"tt{index:07d}", f"Show {index}", index, air_modifier,
             attempts, attempted_ago_hours,
             f"-{attempted_ago_hours} hours" if attempted_ago_hours is not None else None),
        )


def test_bounded_batch_reserves_capacity_for_recent_and_old_episodes():
    for index in range(60):
        _insert_episode(index, recent=True)
    for index in range(60, 80):
        _insert_episode(index, recent=False)

    rows = db.get_wanted_episodes(max_attempts=10_000, limit=50)

    assert len(rows) == 50
    assert sum(int(row["imdb_id"][2:]) < 60 for row in rows) == 40
    assert sum(int(row["imdb_id"][2:]) >= 60 for row in rows) == 10


def test_retry_cadence_only_returns_due_attempts():
    _insert_episode(1, attempts=1, attempted_ago_hours=3)
    _insert_episode(2, attempts=1, attempted_ago_hours=1)
    _insert_episode(3, attempts=2, attempted_ago_hours=7)
    _insert_episode(4, attempts=2, attempted_ago_hours=5)
    _insert_episode(5, attempts=4, attempted_ago_hours=25)
    _insert_episode(6, attempts=4, attempted_ago_hours=23)

    rows = db.get_wanted_episodes(max_attempts=10_000, limit=20)

    assert {row["imdb_id"] for row in rows} == {
        "tt0000001", "tt0000003", "tt0000005",
    }


def test_reconcile_uses_virtual_episode_identity():
    _insert_episode(1)
    db.insert_virtual_item(
        token="episode-token", info_hash="a" * 40,
        magnet="magnet:?xt=urn:btih:" + "a" * 40,
        title="Show 1 S01E01", media_type="series",
        imdb_id="tt0000001", season=1, episode=1,
    )

    assert db.reconcile_wanted_episodes() == 1
    assert db.get_all_wanted_episodes()[0]["status"] == "found"


def test_reconcile_preserves_deterministic_no_file_retry():
    _insert_episode(1)
    db.insert_virtual_item(
        token="bad-episode-token", info_hash="a" * 40,
        magnet="magnet:?xt=urn:btih:" + "a" * 40,
        title="Show 1 S01E01", media_type="series",
        imdb_id="tt0000001", season=1, episode=1,
    )
    db.update_playability_fail("tt0000001:S01E01", "NO_FILE")

    assert db.reconcile_wanted_episodes() == 0
    assert db.get_all_wanted_episodes()[0]["status"] == "wanted"


def test_unplayable_found_episode_is_requeued_immediately():
    _insert_episode(1, attempts=4, attempted_ago_hours=1)
    db.mark_episode_status("tt0000001", 1, 1, "found")

    assert db.requeue_wanted_episode("tt0000001", 1, 1) is True

    row = db.get_wanted_episode("tt0000001", 1, 1)
    assert row["status"] == "wanted"
    assert row["attempt_count"] == 0
    assert row["last_attempted"] is None
    assert db.get_wanted_episodes(max_attempts=10, limit=10)[0]["id"] == row["id"]
    assert db.requeue_wanted_episode("tt0000001", 1, 1) is False


def test_fresh_lane_retries_new_releases_without_touching_old_backlog():
    _insert_episode(1, recent=True, attempts=2, attempted_ago_hours=1)
    _insert_episode(2, recent=True, attempts=2, attempted_ago_hours=0.1)
    _insert_episode(3, recent=False, attempts=0)

    rows = db.get_fresh_wanted_episodes(limit=10, window_days=3)

    assert {row["imdb_id"] for row in rows} == {"tt0000001"}


def test_release_candidate_rejection_is_scoped_to_content_key():
    key_one = "tt1234567:S01E01"
    key_two = "tt1234567:S01E02"
    candidate = {
        "info_hash": "a" * 40,
        "protocol": "torrent",
        "magnet": "magnet:?xt=urn:btih:" + "a" * 40,
        "source": "test",
        "title": "Show.S01E01",
        "rank_order": 1,
    }
    db.upsert_release_candidates(key_one, [candidate])
    db.upsert_release_candidates(key_two, [candidate])
    db.reject_release_candidate(key_one, "a" * 40, "NO_FILE")

    assert db.get_rejected_candidate_hashes(key_one) == {"a" * 40}
    assert db.get_rejected_candidate_hashes(key_two) == set()
    assert db.get_alternate_candidates(key_one) == []
    assert len(db.get_alternate_candidates(key_two)) == 1


def test_provider_cache_status_keeps_positive_and_negative_results():
    hashes = ["a" * 40, "b" * 40]
    db.set_provider_cache_status("torbox", {hashes[0]}, hashes)

    cached, uncached = db.get_provider_cache_status("torbox", hashes)

    assert cached == {hashes[0]}
    assert uncached == {hashes[1]}


def test_identity_repair_cursor_rotates_unresolved_items():
    for index in range(2):
        db.insert_virtual_item(
            token=f"identity-{index}", info_hash=str(index) * 40,
            magnet=f"magnet:?xt=urn:btih:{str(index) * 40}",
            title=f"Unknown {index}", media_type="series",
        )

    first = db.get_virtual_items_missing_identity(limit=1)[0]
    db.touch_virtual_identity_check(first["token"])
    second = db.get_virtual_items_missing_identity(limit=1)[0]

    assert second["token"] != first["token"]
