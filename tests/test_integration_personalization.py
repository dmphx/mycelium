"""Saved people against real isolated SQLite and synthetic checked metadata."""
import hashlib
import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import integration_auth as ia
import integration_personalization as service
import tmdb_checked as tc

DIGEST = hashlib.sha256(b"{}").hexdigest()


@pytest.fixture()
def env(monkeypatch, tmp_path):
    for module in ("db", "config", "settings", "request_intents"):
        sys.modules.pop(module, None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "personalization.db"))
    import config
    config.TMDB_API_KEY = "synthetic-person-key"
    import db
    db.init()
    uid = db.create_user("saved-person-owner", "synthetic", quota_monthly=2)
    other = db.create_user("other-owner", "synthetic", role="admin")
    return db, uid, other


def key(n):
    return f"saved-person-{n:08d}"


def raw_person(pid=9, **changes):
    raw = {"id": pid, "name": "Synthetic Person", "profile_path": "/person.jpg",
           "combined_credits": {"cast": [], "crew": []}}
    raw.update(changes)
    return raw


def mutate(env, method="POST", pid=9, n=1, *, user=None, fetch=None, payload=None, digest=DIGEST):
    return service.mutate_saved_person(env[1] if user is None else user, method, pid,
        {} if payload is None else payload, key(n), digest,
        fetch=fetch or (lambda *a: raw_person(pid)))


def rows(env, table):
    with env[0]._connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM " + table)]


def no_fetch(*args):
    pytest.fail("unexpected provider fetch")


def test_additive_source_and_exact_metadata_call(env, monkeypatch):
    db, uid, _ = env
    db.add_favorite_actor(uid, 12, "Automatic favorite", "/legacy.jpg")
    db.add_to_watchlist(uid, "tt1234567", 44, "movie", "Legacy watchlist")
    before = {t: rows(env, t) for t in ("favorite_actors", "watchlist", "user_requests",
                                       "request_intents", "request_outbox", "users")}
    import builtins
    real_import = builtins.__import__
    def forbid_automatic(name, *args, **kwargs):
        assert name not in {"processor", "auto_approve", "library_status", "request_intents"}
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", forbid_automatic)
    calls = []
    def fetch(path, params):
        with db._connect() as conn:
            assert not conn.in_transaction
        calls.append((path, params))
        return raw_person()
    assert mutate(env, fetch=fetch) == {"person_id": 9, "following": True, "automatic_requests": False}
    assert calls == [("/person/9", {"append_to_response": "combined_credits"})]
    assert service.list_saved_people(uid) == {"items": [
        {"person_id": 9, "name": "Synthetic Person", "profile_url": "https://image.tmdb.org/t/p/w185/person.jpg"}],
        "complete": True, "automatic_requests": False}
    assert {t: rows(env, t) for t in before} == before
    result = rows(env, "integration_personalization_results")[0]
    assert result["raw_body_sha256"] == DIGEST and result["resource"] == "follows"
    assert set(json.loads(result["response_json"])) == {"person_id", "following", "automatic_requests"}
    assert not {"token", "jwt", "authorization", "private_key"} & result.keys()


@pytest.mark.parametrize("pid", [0, -1, True, 1.0, "9", None, 2147483648])
def test_strict_ids(env, pid):
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, pid=pid, fetch=no_fetch)
    assert error.value.status == 400 and rows(env, "integration_personalization_results") == []


@pytest.mark.parametrize("payload", [[], "", False, {"name": "Caller"}, {"owner": 1},
    {"person_id": 9}, {"profile_url": "https://example.test/a"}, {"automatic_requests": False}])
def test_exact_empty_body(env, payload):
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, payload=payload, fetch=no_fetch)
    assert error.value.status == 400


@pytest.mark.parametrize("value", [None, "short", "a" * 129, "x" * 15, "x" * 16 + " ", "é" * 16])
def test_key_syntax(env, value):
    with pytest.raises(ia.IntegrationError) as error:
        service.mutate_saved_person(env[1], "POST", 9, {}, value, DIGEST, fetch=no_fetch)
    assert error.value.status == 400


@pytest.mark.parametrize("digest", [None, True, "a" * 63, "A" * 64, "g" * 64, "0" * 64 + "\n"])
def test_raw_digest_syntax(env, digest):
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, digest=digest, fetch=no_fetch)
    assert error.value.status == 400


def test_owner_enabled_on_reads_writes_replays_and_after_resolution(env):
    db, uid, other = env
    mutate(env)
    assert service.list_saved_people(other)["items"] == []
    # The same key is independent for another native owner, including an admin.
    mutate(env, pid=10, user=other)
    assert [r["person_id"] for r in service.list_saved_people(uid)["items"]] == [9]
    db.update_user(uid, enabled=0)
    for operation in (lambda: service.list_saved_people(uid),
                      lambda: mutate(env, fetch=no_fetch),
                      lambda: service.replay_lookup(uid, "follows", "POST", {"person_id": 9}, key(1))):
        with pytest.raises(ia.IntegrationError) as error:
            operation()
        assert error.value.status == 403
    db.update_user(uid, enabled=1)
    def disable(path, params):
        db.update_user(uid, enabled=0)
        return raw_person(11)
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, pid=11, n=2, fetch=disable)
    assert error.value.status == 403
    assert len(rows(env, "integration_personalization_results")) == 2
    assert {r["person_id"] for r in rows(env, "integration_saved_people")} == {9, 10}


def test_historical_replay_conflicts_no_metadata_and_restart(env):
    db, uid, _ = env
    original = mutate(env)
    assert mutate(env, fetch=no_fetch, digest=hashlib.sha256(b" { } ").hexdigest()) == original
    for operation in (lambda: mutate(env, method="DELETE", fetch=no_fetch),
                      lambda: mutate(env, pid=10, fetch=no_fetch),
                      lambda: service.replay_lookup(uid, "watchlist", "POST",
                          {"media": {"type": "movie", "tmdb_id": 9}}, key(1))):
        with pytest.raises(ia.IntegrationError) as error:
            operation()
        assert error.value.status == 409
    assert mutate(env, method="DELETE", n=2, fetch=no_fetch)["following"] is False
    import config
    config.TMDB_API_KEY = ""
    assert mutate(env, fetch=no_fetch) == original
    assert service.list_saved_people(uid)["items"] == []
    db._tls.conn.close()
    del db._tls.conn
    db.init()
    assert mutate(env, fetch=no_fetch) == original
    assert service.list_saved_people(uid)["items"] == []
    assert len(rows(env, "integration_personalization_results")) == 2
    assert rows(env, "integration_personalization_results")[0]["raw_body_sha256"] == DIGEST


def test_existing_member_and_delete_do_not_need_provider_or_config(env):
    mutate(env)
    import config
    config.TMDB_API_KEY = ""
    assert mutate(env, n=2, fetch=no_fetch)["following"] is True
    assert mutate(env, method="DELETE", n=3, fetch=no_fetch)["following"] is False
    assert mutate(env, method="DELETE", pid=99, n=4, fetch=no_fetch)["following"] is False
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, pid=99, n=5, fetch=no_fetch)
    assert error.value.status == 503
    assert len(rows(env, "integration_personalization_results")) == 4


def test_same_key_concurrency_one_membership_and_receipt(env):
    barrier = threading.Barrier(2)
    def fetch(path, params):
        barrier.wait(timeout=5)
        return raw_person()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(mutate, env, fetch=fetch) for _ in range(2)]
        outcomes = [f.result(timeout=10) for f in futures]
    assert outcomes[0] == outcomes[1]
    assert len(rows(env, "integration_saved_people")) == len(rows(env, "integration_personalization_results")) == 1


def test_different_keys_opposing_writes_serialize_at_commit(env):
    started, release = threading.Event(), threading.Event()
    def fetch(path, params):
        started.set()
        assert release.wait(timeout=5)
        return raw_person()
    with ThreadPoolExecutor(max_workers=2) as pool:
        post = pool.submit(mutate, env, fetch=fetch)
        assert started.wait(timeout=5)
        # DELETE can commit while POST resolves outside the write transaction.
        assert mutate(env, method="DELETE", n=2, fetch=no_fetch)["following"] is False
        release.set()
        assert post.result(timeout=10)["following"] is True
    assert len(rows(env, "integration_saved_people")) == 1
    assert len(rows(env, "integration_personalization_results")) == 2


def test_result_insert_failure_rolls_back_membership_and_delete(env):
    db, uid, _ = env
    mutate(env)
    with db._connect() as conn:
        conn.execute("CREATE TRIGGER synthetic_result_failure BEFORE INSERT ON "
                     "integration_personalization_results BEGIN SELECT RAISE(ABORT,'private database path'); END")
    for method, pid, n in (("POST", 10, 2), ("DELETE", 9, 3)):
        with pytest.raises(ia.IntegrationError) as error:
            mutate(env, method=method, pid=pid, n=n)
        assert error.value.status == 503 and "private" not in error.value.message
    assert [r["person_id"] for r in service.list_saved_people(uid)["items"]] == [9]
    assert len(rows(env, "integration_personalization_results")) == 1


def test_results_immutable_retained_and_owner_cascade_allowed(env):
    db, uid, _ = env
    mutate(env)
    mutate(env, method="DELETE", n=2, fetch=no_fetch)
    with db._connect() as conn:
        for sql in (
            "UPDATE integration_personalization_results SET created_at='old' WHERE user_id=?",
            "DELETE FROM integration_personalization_results WHERE user_id=?",
            "INSERT OR REPLACE INTO integration_personalization_results "
            "SELECT * FROM integration_personalization_results WHERE user_id=?",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, (uid,))
    assert len(rows(env, "integration_personalization_results")) == 2
    mutate(env, n=3)
    db.delete_user(uid)
    assert rows(env, "integration_saved_people") == rows(env, "integration_personalization_results") == []


def test_membership_cap_existing_free_and_lookahead_completeness(env, monkeypatch):
    db, uid, _ = env
    monkeypatch.setattr(service, "MAX_SAVED_PEOPLE", 2)
    monkeypatch.setattr(service, "LIST_LIMIT", 1)
    assert service.list_saved_people(uid) == {"items": [], "complete": True, "automatic_requests": False}
    mutate(env)
    assert service.list_saved_people(uid)["complete"] is True
    mutate(env, pid=10, n=2)
    assert len(service.list_saved_people(uid)["items"]) == 1
    assert service.list_saved_people(uid)["complete"] is False
    assert mutate(env, n=3, fetch=no_fetch)["following"] is True
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, pid=11, n=4, fetch=no_fetch)
    assert error.value.status == 503 and not error.value.retryable
    mutate(env, method="DELETE", pid=10, n=5, fetch=no_fetch)
    assert service.list_saved_people(uid)["complete"] is True
    mutate(env, pid=11, n=6)
    assert len(rows(env, "integration_saved_people")) == 2


@pytest.mark.parametrize("scope", ["owner", "global"])
def test_result_capacity_preserves_atomicity_and_replays(env, monkeypatch, scope):
    db, uid, other = env
    monkeypatch.setattr(service, "MAX_OWNER_RESULTS" if scope == "owner" else "MAX_GLOBAL_RESULTS", 1)
    original = mutate(env)
    assert mutate(env, fetch=no_fetch) == original
    for method, pid, user in (("POST", 10, uid), ("DELETE", 9, uid)) + (
        (("DELETE", 99, other),) if scope == "global" else ()):
        with pytest.raises(ia.IntegrationError) as error:
            mutate(env, method=method, pid=pid, n=2, user=user, fetch=no_fetch)
        assert error.value.status == 503 and not error.value.retryable
    assert [r["person_id"] for r in service.list_saved_people(uid)["items"]] == [9]
    assert len(rows(env, "integration_personalization_results")) == 1


@pytest.mark.parametrize("raw,status,retryable", [
    (tc.TmdbNotFound("not_found"), 404, False),
    (tc.TmdbUnavailable("upstream_unreachable"), 503, True),
    (tc.TmdbUnavailable("bad_json"), 503, False),
    ({}, 503, False), (raw_person(10), 503, False),
    (raw_person(name=""), 503, False), (raw_person(combined_credits={}), 503, False),
])
def test_metadata_failure_no_membership_or_result(env, raw, status, retryable):
    def fetch(path, params):
        if isinstance(raw, Exception):
            raise raw
        return raw
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, fetch=fetch)
    assert error.value.status == status and error.value.retryable is retryable
    assert rows(env, "integration_saved_people") == rows(env, "integration_personalization_results") == []


def test_source_corruption_and_readiness_fail_closed_without_legacy_fallback(env):
    db, uid, _ = env
    mutate(env)
    with db._connect() as conn:
        conn.execute("UPDATE integration_saved_people SET profile_url='https://example.test/not-tmdb'")
    with pytest.raises(ia.IntegrationError) as error:
        service.list_saved_people(uid)
    assert error.value.status == 503
    assert service.operations(uid, metadata_ready=True) == set()
    with db._connect() as conn:
        conn.execute("UPDATE integration_saved_people SET profile_url=NULL")
        conn.execute("DROP TRIGGER integration_results_no_update")
        conn.execute("UPDATE integration_personalization_results SET response_json='{}'")
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, fetch=no_fetch)
    assert error.value.status == 503
    assert service.operations(uid, metadata_ready=True) == {"follows.read"}


def test_database_failure_and_no_nested_transaction(env, monkeypatch):
    db, uid, _ = env
    assert service.operations(uid) == {"follows.read"}
    assert service.operations(uid, metadata_ready=True) == {"follows.read", "follows.write"}
    with db._connect() as conn:
        conn.execute("BEGIN")
        try:
            with pytest.raises(ia.IntegrationError) as error:
                service.list_saved_people(uid)
            assert error.value.status == 503 and conn.in_transaction
        finally:
            conn.execute("ROLLBACK")
    def failure():
        raise sqlite3.OperationalError("private database filename")
    monkeypatch.setattr(db, "_connect", failure)
    assert service.operations(uid, metadata_ready=True) == set()
    with pytest.raises(ia.IntegrationError) as error:
        service.list_saved_people(uid)
    assert error.value.status == 503 and "private" not in error.value.message


def test_exact_configured_bounds_and_actual_lookahead(env):
    db, uid, _ = env
    assert (service.MAX_SAVED_PEOPLE, service.LIST_LIMIT, service.MAX_OWNER_RESULTS,
            service.MAX_GLOBAL_RESULTS) == (1000, 200, 100000, 1000000)
    with db._connect() as conn:
        conn.executemany("INSERT INTO integration_saved_people(user_id,person_id,name) VALUES (?,?,?)",
                         [(uid, n, "Synthetic") for n in range(1, 201)])
    at_limit = service.list_saved_people(uid)
    assert len(at_limit["items"]) == 200 and at_limit["complete"] is True
    with db._connect() as conn:
        conn.executemany("INSERT INTO integration_saved_people(user_id,person_id,name) VALUES (?,?,?)",
                         [(uid, n, "Synthetic") for n in range(201, 1001)])
    truncated = service.list_saved_people(uid)
    assert len(truncated["items"]) == 200 and truncated["complete"] is False
    assert [r["person_id"] for r in truncated["items"]] == list(range(1, 201))
    assert mutate(env, fetch=no_fetch)["following"] is True
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, pid=1001, n=2, fetch=no_fetch)
    assert error.value.status == 503 and not error.value.retryable
    assert len(rows(env, "integration_saved_people")) == 1000


@pytest.mark.parametrize("method,payload", [("PUT", {}), ("post", {}), (None, {}), ("POST", None)])
def test_direct_call_action_and_body_are_strict(env, method, payload):
    with pytest.raises(ia.IntegrationError) as error:
        service.mutate_saved_person(env[1], method, 9, payload, key(1), DIGEST, fetch=no_fetch)
    assert error.value.status == 400


def test_missing_guard_blocks_new_results_but_historical_reads_remain(env):
    db, uid, _ = env
    original = mutate(env)
    with db._connect() as conn:
        conn.execute("DROP TRIGGER integration_results_no_delete")
    assert mutate(env, fetch=no_fetch) == original
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, pid=10, n=2, fetch=no_fetch)
    assert error.value.status == 503
    assert service.operations(uid, metadata_ready=True) == {"follows.read"}
    assert len(rows(env, "integration_personalization_results")) == 1
