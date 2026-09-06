from concurrent.futures import ThreadPoolExecutor
import importlib.util
import os
import sqlite3
import threading

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


def _episode(season, episode, info_hash=None, imdb_id="tt1234567",
             show="Long Show", token_prefix=""):
    token = f"{token_prefix}s{season:02d}e{episode:03d}"
    h = info_hash or (str(season) * 40)
    db.insert_virtual_item(
        token=token,
        info_hash=h,
        magnet=f"magnet:?xt=urn:btih:{h}",
        title=f"{show} S{season:02d}E{episode:02d}",
        media_type="series",
        strm_path=f"/data/media/series/{show}/Season {season:02d}/{token}.strm",
        imdb_id=imdb_id,
        season=season,
        episode=episode,
    )
    return token


def _force_complete(token):
    with db._connect() as conn:
        conn.execute(
            "UPDATE media_enrichment_queue SET state='complete', "
            "completed_at=CURRENT_TIMESTAMP WHERE token=?",
            (token,),
        )


def test_progression_selects_twelve_current_and_two_next_episodes():
    for episode in range(1, 46):
        _episode(1, episode)
    for episode in range(1, 9):
        _episode(2, episode)

    items = db.get_series_enrichment_items("s01e005", season_cap=40, next_count=4)

    current = [item for item in items if item["season"] == 1]
    following = [item for item in items if item["season"] == 2]
    assert len(current) == 12
    assert [item["episode"] for item in current] == list(range(5, 17))
    assert [item["episode"] for item in following] == [1, 2]
    priorities = {item["episode"]: item["enrichment_priority"] for item in current}
    assert priorities[6] == 0
    assert priorities[7] == 1


def test_same_release_stays_complete_but_new_hash_requeues():
    token = _episode(1, 1, info_hash="a" * 40)
    item = db.get_virtual_item(token)
    item["enrichment_priority"] = 0
    db.queue_media_enrichment([item])
    _force_complete(token)

    db.queue_media_enrichment([item])
    assert db.enrichment_counts() == {"complete": 1}

    changed = dict(item)
    changed["info_hash"] = "b" * 40
    db.queue_media_enrichment([changed])
    assert db.enrichment_counts() == {"queued": 1}


def test_tmdb_identity_is_used_only_when_durable_sources_agree():
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO users (id, username, password_hash) "
            "VALUES (1, 'tester', 'not-a-real-hash')"
        )
        conn.execute(
            """
            INSERT INTO requests (title, imdb_id, tmdb_id, media_type)
            VALUES ('Example', 'tt1234567', 101, 'movie')
            """
        )
        conn.execute(
            """
            INSERT INTO watchlist (user_id, tmdb_id, imdb_id, title, media_type)
            VALUES (1, 101, 'tt1234567', 'Example', 'movie')
            """
        )
    assert db.get_tmdb_id_for_imdb("tt1234567") == 101

    with db._connect() as conn:
        conn.execute(
            """
            UPDATE watchlist SET tmdb_id=202 WHERE imdb_id='tt1234567'
            """
        )
    assert db.get_tmdb_id_for_imdb("tt1234567") is None


def test_existing_queue_row_accepts_new_stable_identity_metadata():
    item = {
        "token": "identity-later", "info_hash": "a" * 40,
        "title": "Example (2024)", "media_type": "movie",
        "enrichment_priority": 10,
    }
    db.queue_media_enrichment([item])

    improved = dict(item, imdb_id="tt1234567", tmdb_id=101, year=2024)
    db.queue_media_enrichment([improved])

    with db._connect() as conn:
        row = conn.execute(
            "SELECT imdb_id, tmdb_id, year, state "
            "FROM media_enrichment_queue WHERE token='identity-later'"
        ).fetchone()
    assert dict(row) == {
        "imdb_id": "tt1234567", "tmdb_id": 101,
        "year": 2024, "state": "queued",
    }


def test_no_imdb_progression_does_not_cross_titles_containing_s():
    played = _episode(
        1, 1, imdb_id=None, show="The Simpsons", token_prefix="simpsons-"
    )
    _episode(1, 2, imdb_id=None, show="The Simpsons", token_prefix="simpsons-")
    _episode(1, 1, imdb_id=None, show="The Sopranos", token_prefix="sopranos-")

    items = db.get_series_enrichment_items(played)

    assert {item["title"].rsplit(" S", 1)[0] for item in items} == {
        "The Simpsons"
    }


def test_recovery_candidates_are_limited_to_incomplete_queue_items():
    queued = _episode(1, 1)
    complete = _episode(1, 2)
    for token in (queued, complete):
        item = db.get_virtual_item(token)
        item["enrichment_priority"] = 0
        db.queue_media_enrichment([item])
    _force_complete(complete)

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
    assert db.enrichment_counts() == {"retry": 2}


def test_enrichment_state_lookup():
    token = _episode(1, 1)
    item = db.get_virtual_item(token)
    item["enrichment_priority"] = 0
    db.queue_media_enrichment([item])

    assert db.get_enrichment_state(token) == "queued"
    assert db.get_enrichment_state("missing") is None


def test_claim_is_atomic_and_increments_attempts_at_claim():
    for episode in (1, 2):
        token = _episode(1, episode)
        item = db.get_virtual_item(token)
        item["enrichment_priority"] = episode
        db.queue_media_enrichment([item])

    claimed = db.claim_enrichment_batch("lease-a", max_items=12)

    assert [item["attempts"] for item in claimed] == [1, 1]
    assert db.claim_enrichment_batch("lease-b") == []
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT state, lease_id FROM media_enrichment_queue ORDER BY token"
        ).fetchall()
    assert {(row["state"], row["lease_id"]) for row in rows} == {
        ("claimed", "lease-a")
    }


def test_concurrent_workers_cannot_claim_the_same_batch():
    for episode in (1, 2):
        token = _episode(1, episode)
        item = db.get_virtual_item(token)
        item["enrichment_priority"] = episode
        db.queue_media_enrichment([item])
    barrier = threading.Barrier(2)

    def claim(lease_id):
        barrier.wait()
        return db.claim_enrichment_batch(lease_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("lease-a", "lease-b")))

    assert sorted(len(result) for result in results) == [0, 2]
    claimed_tokens = [
        item["token"] for result in results for item in result
    ]
    assert sorted(claimed_tokens) == ["s01e001", "s01e002"]


def test_no_imdb_claim_keeps_exact_show_batch_together():
    first = _episode(
        1, 1, imdb_id=None, show="The Simpsons", token_prefix="simpsons-"
    )
    second = _episode(
        1, 2, imdb_id=None, show="The Simpsons", token_prefix="simpsons-"
    )
    other = _episode(
        1, 1, imdb_id=None, show="The Sopranos", token_prefix="sopranos-"
    )
    for token in (first, second, other):
        item = db.get_virtual_item(token)
        item["enrichment_priority"] = 0
        db.queue_media_enrichment([item])

    claimed = db.claim_enrichment_batch("lease-a")

    assert {item["token"] for item in claimed} == {first, second}


def test_live_claim_can_renew_but_expired_claim_cannot_complete():
    token = _episode(1, 1)
    item = db.get_virtual_item(token)
    item["enrichment_priority"] = 0
    db.queue_media_enrichment([item])
    db.claim_enrichment_batch("lease-a", lease_seconds=60)

    assert db.renew_enrichment_claim("lease-a", lease_seconds=120) == 1
    with db._connect() as conn:
        conn.execute(
            "UPDATE media_enrichment_queue "
            "SET lease_expires_at=datetime('now','-1 second') WHERE token=?",
            (token,),
        )
    assert db.renew_enrichment_claim("lease-a", lease_seconds=120) == 0
    assert db.complete_enrichment_claim(
        "lease-a", token, "9", {
            "activity_completed": True,
            "metadata_changed": True,
            "stub_restored": True,
            "downloaded_bytes": 1024,
            "artifact": {"part_count": 1, "stream_count": 2},
        },
    ) is False


def test_process_lock_owner_can_release_orphaned_unexpired_claim():
    token = _episode(1, 1)
    item = db.get_virtual_item(token)
    item["enrichment_priority"] = 0
    db.queue_media_enrichment([item])
    db.claim_enrichment_batch("stopped-worker", lease_seconds=3600)

    assert db.reset_interrupted_enrichment() == 0
    assert db.reset_interrupted_enrichment(release_all=True) == 1
    assert db.enrichment_counts() == {"retry": 1}


def test_completion_refuses_weak_evidence_even_with_live_lease():
    token = _episode(1, 1)
    item = db.get_virtual_item(token)
    item["enrichment_priority"] = 0
    db.queue_media_enrichment([item])
    db.claim_enrichment_batch("lease-a", lease_seconds=60)

    with pytest.raises(ValueError, match="positive Plex analysis"):
        db.complete_enrichment_claim(
            "lease-a", token, "9", {"activity_completed": True}
        )
    assert db.enrichment_counts() == {"claimed": 1}


def test_generic_state_setter_cannot_bypass_completion_evidence():
    token = _episode(1, 1)
    item = db.get_virtual_item(token)
    db.queue_media_enrichment([item])

    with pytest.raises(ValueError, match="positive evidence"):
        db.set_enrichment_state([token], "complete")


def test_expired_staged_claim_backs_off_without_blocking_other_title():
    first = _episode(
        1, 1, imdb_id="tt1000001", show="First Show", token_prefix="a"
    )
    second = _episode(
        1, 1, imdb_id="tt1000002", show="Second Show", token_prefix="b"
    )
    for priority, token in enumerate((first, second)):
        item = db.get_virtual_item(token)
        item["enrichment_priority"] = priority
        db.queue_media_enrichment([item])
    db.claim_enrichment_batch("lost-lease")
    assert db.set_enrichment_claim_state(
        "lost-lease", [first], "staged"
    ) == 1
    with db._connect() as conn:
        conn.execute(
            "UPDATE media_enrichment_queue "
            "SET lease_expires_at=datetime('now','-1 second') WHERE token=?",
            (first,),
        )

    assert db.reset_interrupted_enrichment(retry_base_seconds=900) == 1
    claimed = db.claim_enrichment_batch("healthy-lease")
    assert [item["token"] for item in claimed] == [second]


def test_preexisting_exhausted_and_orphaned_rows_are_dead_lettered():
    exhausted = _episode(1, 1)
    item = db.get_virtual_item(exhausted)
    item["enrichment_priority"] = 0
    db.queue_media_enrichment([item])
    with db._connect() as conn:
        conn.execute(
            "UPDATE media_enrichment_queue SET state='failed', attempts=5 "
            "WHERE token=?",
            (exhausted,),
        )
        conn.execute(
            """
            INSERT INTO media_enrichment_queue
                (token, info_hash, title, media_type, state)
            VALUES ('orphan', ?, 'Orphan', 'movie', 'queued')
            """,
            ("f" * 40,),
        )

    assert db.claim_enrichment_batch("lease-a") == []
    assert db.enrichment_counts() == {"dead_letter": 2}


def test_failed_batch_backs_off_and_does_not_block_another_title():
    first = _episode(
        1, 1, imdb_id="tt1000001", show="Poison Show", token_prefix="a"
    )
    second = _episode(
        1, 1, imdb_id="tt1000002", show="Healthy Show", token_prefix="b"
    )
    for priority, token in enumerate((first, second)):
        item = db.get_virtual_item(token)
        item["enrichment_priority"] = priority
        db.queue_media_enrichment([item])

    claimed = db.claim_enrichment_batch("lease-poison")
    assert [item["token"] for item in claimed] == [first]
    assert db.fail_enrichment_claim(
        "lease-poison", [first], "identity mismatch", retry_base_seconds=900
    ) == {"retry": 1, "dead_letter": 0}

    next_claim = db.claim_enrichment_batch("lease-healthy")
    assert [item["token"] for item in next_claim] == [second]


def test_fifth_failed_claim_moves_item_to_dead_letter():
    token = _episode(1, 1)
    item = db.get_virtual_item(token)
    item["enrichment_priority"] = 0
    db.queue_media_enrichment([item])

    for attempt in range(1, 6):
        lease = f"lease-{attempt}"
        assert db.claim_enrichment_batch(
            lease, max_attempts=99
        )[0]["attempts"] == attempt
        result = db.fail_enrichment_claim(
            lease, [token], "still broken", max_attempts=99,
            retry_base_seconds=1,
        )
        if attempt < 5:
            assert result == {"retry": 1, "dead_letter": 0}
            with db._connect() as conn:
                conn.execute(
                    "UPDATE media_enrichment_queue SET next_attempt_at=datetime('now','-1 second')"
                )
        else:
            assert result == {"retry": 0, "dead_letter": 1}
    assert db.enrichment_counts() == {"dead_letter": 1}


def test_quarantine_preserves_completion_and_real_session_rebuilds_item():
    complete = _episode(1, 1)
    pending = _episode(1, 2)
    for token in (complete, pending):
        item = db.get_virtual_item(token)
        item["enrichment_priority"] = 0
        db.queue_media_enrichment([item])
    _force_complete(complete)

    assert db.quarantine_pending_enrichment("batch-1", "untrusted") == 1
    assert db.enrichment_counts() == {"complete": 1, "quarantined": 1}

    pending_item = db.get_virtual_item(pending)
    pending_item["enrichment_priority"] = 0
    db.queue_media_enrichment([pending_item], reason="plex-session")
    assert db.enrichment_counts() == {"complete": 1, "queued": 1}


def test_exact_plex_path_mapping_rejects_unknown_and_ambiguous_paths():
    token = _episode(1, 1)
    path = "/spore-nfs-media/series/Long Show/Season 01/s01e001.mkv"
    assert db.find_virtual_item_by_plex_path(path)["token"] == token
    assert db.find_virtual_item_by_plex_path("/other/library/file.mkv") is None


def test_plex_playback_event_is_durable_and_idempotent():
    token = _episode(1, 1)
    assert db.record_plex_playback_event(
        "event-1", "session-1", "rating-1", "player-1", token
    ) is True
    db.mark_plex_playback_event_queued("event-1")
    assert db.record_plex_playback_event(
        "event-1", "session-1", "rating-1", "player-1", token
    ) is False
    assert db.recent_plex_playback(600) is True


def test_legacy_queue_migration_preserves_103_completed_rows(
        tmp_path, monkeypatch):
    current = getattr(db._tls, "conn", None)
    if current is not None:
        current.close()
        del db._tls.conn
    legacy_path = tmp_path / "legacy-enrichment.db"
    monkeypatch.setattr(db, "DB_PATH", str(legacy_path))
    connection = sqlite3.connect(legacy_path)
    connection.execute(
        """
        CREATE TABLE media_enrichment_queue (
            token TEXT PRIMARY KEY,
            info_hash TEXT NOT NULL,
            imdb_id TEXT,
            title TEXT NOT NULL,
            media_type TEXT NOT NULL,
            season INTEGER,
            episode INTEGER,
            priority INTEGER NOT NULL DEFAULT 100,
            reason TEXT NOT NULL DEFAULT 'playback',
            state TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            queued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO media_enrichment_queue
            (token, info_hash, title, media_type, state, completed_at)
        VALUES (?, ?, ?, 'series', 'complete', CURRENT_TIMESTAMP)
        """,
        [(f"complete-{index}", "a" * 40, f"Episode {index}")
         for index in range(103)],
    )
    connection.execute(
        """
        INSERT INTO media_enrichment_queue
            (token, info_hash, title, media_type, state)
        VALUES ('pending', ?, 'Pending', 'movie', 'queued')
        """,
        ("b" * 40,),
    )
    connection.commit()
    connection.close()

    db.init()

    assert db.enrichment_counts() == {"complete": 103, "queued": 1}
    with db._connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(media_enrichment_queue)"
            )
        }
    assert {"lease_id", "evidence_json", "quarantine_batch"} <= columns
