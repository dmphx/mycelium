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
