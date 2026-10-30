"""Receiver-owned values with checked synthetic metadata and isolated native SQLite."""
import copy
from datetime import datetime, timezone, timedelta
import gc
import hashlib
import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import multiprocessing

import pytest

import integration_auth as ia
import request_preparation as preparation
import request_preparation_tokens as tokens
import tmdb_checked as tc

KEY = "synthetic-preparation-0001"


class Clock:
    def __init__(self):
        self.ticks = 100.0
        self.wall = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    def monotonic(self):
        return self.ticks
    def utc(self):
        return self.wall


class Metadata:
    def __init__(self, kind="tv", tid=100):
        self.calls = []
        self.kind, self.tid = kind, tid
        self.data = {f"/{kind}/{tid}": {"id": tid, "name" if kind == "tv" else "title": "Server title",
            "external_ids": {"id": tid, "imdb_id": "tt1234567"}}}
        if kind == "tv":
            self.data[f"/{kind}/{tid}"]["seasons"] = [{"season_number": s, "episode_count": 2} for s in (0, 1, 2)]
            for s in (0, 1, 2):
                self.data[f"/tv/{tid}/season/{s}"] = {"season_number": s, "show_id": tid, "episodes": [
                    {"id": s*10+n+1, "season_number": s, "episode_number": n+1, "show_id": tid,
                     "name": f"Episode {n+1}", "air_date": "2026-01-01" if n == 0 else None, "runtime": 45}
                    for n in range(2)]}
    def __call__(self, path, params):
        self.calls.append((path, params))
        return copy.deepcopy(self.data[path])


@pytest.fixture()
def env(monkeypatch, tmp_path):
    for name in ("db", "config", "settings", "request_intents"):
        sys.modules.pop(name, None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "preparation.db"))
    import config
    config.TMDB_API_KEY = "synthetic-preparation-only"
    import db
    db.init()
    uid = db.create_user("synthetic-owner", "synthetic", quota_monthly=3)
    other = db.create_user("synthetic-other", "synthetic", role="admin")
    return db, uid, other, Clock(), tmp_path


def body(kind="all_seasons", *, media_type="tv", **scope):
    return json.dumps({"media": {"type": media_type, "tmdb_id": 100},
                       "scope": {"kind": kind, **scope}}, separators=(",", ":")).encode()


def prepare(env, raw=None, *, fetch=None, key=KEY, user=None):
    return preparation.prepare(env[1] if user is None else user, raw or body(), key,
                               fetch=fetch or Metadata(), clock=env[3].monotonic, utc_clock=env[3].utc)


def check(env, value, raw=None, *, key=KEY, user=None):
    return preparation.check_prepared(value, user_id=env[1] if user is None else user,
                                       raw_body=raw or body(), idempotency_key=key)


def protected(env):
    with env[0]._connect() as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {table: [tuple(r) for r in conn.execute('SELECT * FROM "' + table + '"')] for table in tables}


def no_fetch(*args):
    pytest.fail("unexpected metadata access")


def test_read_only_issued_value_exact_metadata_paths_and_separate_hashes(env):
    before = protected(env)
    metadata = Metadata()
    def fetch(path, params):
        with env[0]._connect() as conn:
            assert not conn.in_transaction
        return metadata(path, params)
    value = prepare(env, fetch=fetch)
    report = check(env, value)
    assert report["prepared"] is True and report["admission_qualified"] is False
    assert report["raw_body_sha256"] == hashlib.sha256(body()).hexdigest()
    assert len({report[k] for k in ("raw_body_sha256", "input_hash", "predicate_hash", "snapshot_hash", "accepted_hash", "preparation_hash")}) == 6
    assert metadata.calls == [("/tv/100", {"append_to_response": "external_ids"}),
                              ("/tv/100/season/1", {}), ("/tv/100/season/2", {})]
    assert protected(env) == before  # No request, quota, schema, token-ledger or outbox writes.
    assert not any("external_ids" in path or "find" in path for path, _ in metadata.calls)


def test_equivalent_body_keeps_semantic_snapshot_hashes_but_not_raw_binding(env):
    first = prepare(env)
    spaced = b' { "scope": {"kind":"all_seasons"}, "media":{"tmdb_id":100,"type":"tv"} } '
    second = prepare(env, spaced)
    a, b = check(env, first), check(env, second, spaced)
    assert all(a[k] == b[k] for k in ("input_hash", "predicate_hash", "snapshot_hash", "accepted_hash"))
    assert a["raw_body_sha256"] != b["raw_body_sha256"] and a["preparation_hash"] != b["preparation_hash"]
    with pytest.raises(ia.IntegrationError) as error:
        check(env, first, spaced)
    assert error.value.status == 409


@pytest.mark.parametrize("kind,scope,seasons", [("all_seasons", {}, {1, 2}),
    ("future", {}, {1, 2}), ("selected_seasons", {"seasons": [0, 2]}, {0, 2}),
    ("episodes", {"episodes": [{"season": 0, "episode": 2}, {"season": 1, "episode": 1}]}, {0, 1})])
def test_scope_and_explicit_specials_remain_exact(env, kind, scope, seasons):
    raw = body(kind, **scope)
    value = prepare(env, raw)
    document, built = preparation._issued(value)
    assert {e["season"] for e in built["episodes"]} == seasons
    assert document["normalized_input"]["scope"]["kind"] == kind
    if kind == "episodes":
        assert {(e["season"], e["episode"]) for e in built["episodes"]} == {(0, 2), (1, 1)}
    if kind == "future":
        assert built["predicate"]["starts_on"] == "2026-01-01"
        assert {e["state"] for e in built["episodes"]} == {"pending", "waiting_airdate"}
    check(env, value, raw)


def test_movie_and_exact_raw_show_season_have_no_alias_or_global_helper(env):
    raw = body("movie", media_type="movie")
    metadata = Metadata("movie")
    value = prepare(env, raw, fetch=metadata)
    assert preparation._issued(value)[1]["episodes"] == []
    assert metadata.calls == [("/movie/100", {"append_to_response": "external_ids"})]


def test_cutoff_is_receiver_utc_and_cannot_roll_forward_at_check(env):
    env[3].wall = datetime(2026, 1, 1, 20, tzinfo=timezone(timedelta(hours=-5)))
    raw = body("future")
    value = prepare(env, raw)
    assert check(env, value, raw)["cutoff"] == "2026-01-02"
    env[3].wall = datetime(2026, 1, 3, tzinfo=timezone.utc)
    with pytest.raises(ia.IntegrationError) as error:
        check(env, value, raw)
    assert error.value.code == "request_preparation_expired"
    env[3].wall = datetime(2026, 1, 2, tzinfo=timezone.utc)
    with pytest.raises(ia.IntegrationError):
        check(env, value, raw)  # Expiration is a tombstone within this process, never revived.


@pytest.mark.parametrize("delta", [60.001, -1])
def test_monotonic_expiry_and_rollback_retire_the_value(env, delta):
    value = prepare(env)
    env[3].ticks += delta
    with pytest.raises(ia.IntegrationError) as error:
        check(env, value)
    assert error.value.code == "request_preparation_expired"
    env[3].ticks = 100
    with pytest.raises(ia.IntegrationError):
        check(env, value)


def test_cancelled_forged_exported_or_mutated_values_are_not_accepted(env):
    value = prepare(env)
    with pytest.raises(TypeError):
        preparation.PreparedRequest()
    with pytest.raises(AttributeError):
        value._body = b"{}"
    for fake in (value._body, json.loads(value._body), value.preparation_hash,
                 object.__new__(preparation.PreparedRequest)):
        with pytest.raises(ia.IntegrationError):
            check(env, fake)
    other = prepare(env)
    object.__setattr__(other, "_body", value._body)
    with pytest.raises(ia.IntegrationError):
        check(env, other)
    preparation.retire(value)
    preparation.retire(value)
    with pytest.raises(ia.IntegrationError) as error:
        check(env, value)
    assert error.value.code == "request_preparation_retired"


@pytest.mark.parametrize("changes", [{"user": 2}, {"key": "synthetic-other-key-0002"},
                                   {"raw": body("selected_seasons", seasons=[1])}])
def test_owner_key_scope_and_raw_body_cannot_be_substituted(env, changes):
    value = prepare(env)
    with pytest.raises(ia.IntegrationError) as error:
        check(env, value, **changes)
    assert error.value.status == 409


@pytest.mark.parametrize("field,value", [("role", "admin"), ("quota_monthly", 4), ("auto_approve", 1), ("enabled", 0)])
def test_native_policy_drift_is_checked_without_grant_authority(env, field, value):
    prepared = prepare(env)
    env[0].update_user(env[1], **{field: value})
    with pytest.raises(ia.IntegrationError) as error:
        check(env, prepared)
    assert error.value.status == (403 if field == "enabled" else 409)
    assert len(env[0].get_user_requests(env[1])) == 0


def test_policy_change_during_fetch_rejects_preparation(env):
    before = protected(env)
    def fetch(path, params):
        env[0].update_user(env[1], quota_monthly=4)
        return Metadata()(path, params)
    with pytest.raises(ia.IntegrationError) as error:
        prepare(env, fetch=fetch)
    assert error.value.code == "request_policy_changed"
    after = protected(env)
    assert all(before[t] == after[t] for t in before if t != "users")


@pytest.mark.parametrize("clock", [lambda: True, lambda: float("nan"), lambda: -1,
                                  lambda: 10**1000])
def test_bad_monotonic_clock_fails_before_metadata(env, clock):
    with pytest.raises(ia.IntegrationError):
        preparation.prepare(env[1], body(), KEY, fetch=no_fetch, clock=clock, utc_clock=env[3].utc)


def test_elapsed_or_midnight_crossing_during_metadata_is_not_masked_as_outage(env):
    def fetch(path, params):
        env[3].ticks += 61
        return Metadata()(path, params)
    with pytest.raises(ia.IntegrationError) as error:
        prepare(env, fetch=fetch)
    assert error.value.code == "request_preparation_expired" and error.value.status == 409


@pytest.mark.parametrize("raw", [b'{}', b'{"media":{},"media":{},"scope":{}}',
    body("future", starts_on="2020-01-01"), b'[]', b'{"n":NaN}', b'x'*65537])
def test_strict_body_rejection_precedes_any_metadata(env, raw):
    with pytest.raises(ia.IntegrationError) as error:
        prepare(env, raw, fetch=no_fetch)
    assert error.value.status == 400


def test_missing_database_is_not_created_and_nested_transaction_is_untouched(env, monkeypatch):
    db, uid, _, _, tmp = env
    with db._connect() as conn:
        conn.execute("BEGIN")
        try:
            with pytest.raises(ia.IntegrationError):
                prepare(env, fetch=no_fetch)
            assert conn.in_transaction
        finally:
            conn.execute("ROLLBACK")
    missing = tmp / "does-not-exist.db"
    monkeypatch.setattr(db, "DB_PATH", str(missing))
    with pytest.raises(ia.IntegrationError):
        prepare(env, fetch=no_fetch)
    assert not missing.exists()


def test_capacity_and_failure_release_use_existing_bounded_slots(env, monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(preparation, "_SLOTS", slots)
    with pytest.raises(ia.IntegrationError):
        prepare(env, fetch=lambda *args: {})
    value = prepare(env)
    with pytest.raises(ia.IntegrationError) as error:
        prepare(env, fetch=no_fetch)
    assert error.value.retryable is True
    del value
    gc.collect()
    assert check(env, prepare(env))["prepared"] is True


@pytest.mark.parametrize("change", [{"id": True}, {"id": 101}, {"media_type": "movie"},
    {"external_ids": {"id": 101, "imdb_id": "tt1234567"}},
    {"external_ids": {"imdb_id": "tt0000001"}, "imdb_id": "tt1234567"},
    {"external_ids": {"imdb_id": "bad"}}])
def test_root_identity_roundtrip_is_required(env, change):
    metadata = Metadata()
    metadata.data['/tv/100'].update(change)
    with pytest.raises(ia.IntegrationError) as error:
        prepare(env, fetch=metadata)
    assert error.value.status == 503


def test_fetch_bound_and_source_size_do_not_produce_partial_preparation(env, monkeypatch):
    monkeypatch.setattr(preparation, "MAX_FETCHES", 1)
    with pytest.raises(ia.IntegrationError):
        prepare(env)
    monkeypatch.setattr(preparation, "MAX_FETCHES", 101)
    monkeypatch.setattr(preparation, "MAX_RESPONSE_BYTES", 10)
    with pytest.raises(ia.IntegrationError):
        prepare(env)


def test_failed_transport_is_safe_retryable_and_never_falls_back(env):
    def fetch(*args):
        raise tc.TmdbUnavailable("upstream_unreachable")
    with pytest.raises(ia.IntegrationError) as error:
        prepare(env, fetch=fetch)
    assert error.value.status == 503 and error.value.retryable is True
    assert error.value.message == "request preparation unavailable"


def cancel(env, value, *, user=None, raw=None, key=KEY):
    return tokens.cancel(value, user_id=env[1] if user is None else user,
                         raw_body=raw or body(), idempotency_key=key)


def test_durable_cancellation_is_atomic_immutable_and_does_not_touch_native_data(env):
    value = prepare(env)
    before = protected(env)
    report = cancel(env, value)
    assert report == {"retired": True, "durable": True, "preparation_hash": value_hash(value)}
    assert cancel(env, value) == report
    after = protected(env)
    assert all(before[t] == after[t] for t in before if t != "request_preparation_tombstones")
    assert len(after["request_preparation_tombstones"]) == 1
    with env[0]._connect() as connection:
        for sql in ("UPDATE request_preparation_tombstones SET state=state",
                    "DELETE FROM request_preparation_tombstones",
                    "INSERT OR REPLACE INTO request_preparation_tombstones SELECT * FROM request_preparation_tombstones"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)
    # The durable record is checked independently of the volatile retirement bit.
    with preparation._LOCK:
        preparation._ISSUED[value]["status"] = "prepared"
    with pytest.raises(ia.IntegrationError) as error:
        check(env, value)
    assert error.value.code == "request_preparation_retired"


def value_hash(value):
    return preparation._hash("prepared", preparation._original(value))


@pytest.mark.parametrize("context", [{"user": 2}, {"key": "synthetic-wrong-key-0002"},
    {"raw": body("selected_seasons", seasons=[1])}])
def test_wrong_context_cannot_cancel_another_preparation(env, context):
    value = prepare(env)
    with pytest.raises(ia.IntegrationError) as error:
        cancel(env, value, **context)
    assert error.value.status == 409
    assert check(env, value)["prepared"] is True
    assert protected(env)["request_preparation_tombstones"] == []


def test_expired_value_can_be_cancelled_but_not_reactivated(env):
    value = prepare(env)
    env[3].ticks += 61
    with pytest.raises(ia.IntegrationError):
        check(env, value)
    assert cancel(env, value)["durable"] is True
    env[3].ticks = 100
    with pytest.raises(ia.IntegrationError):
        check(env, value)


def test_retirement_during_policy_check_wins_before_readiness_return(env, monkeypatch):
    value = prepare(env)
    entered, release = threading.Event(), threading.Event()
    original = preparation._policy
    def delayed_policy(owner):
        result = original(owner)
        entered.set()
        assert release.wait(timeout=5)
        return result
    monkeypatch.setattr(preparation, "_policy", delayed_policy)
    def attempt():
        try:
            check(env, value)
            return "incorrectly ready"
        except ia.IntegrationError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(attempt)
        assert entered.wait(timeout=5)
        try:
            cancel(env, value)
        finally:
            release.set()
        assert future.result(timeout=5) == "request_preparation_retired"


def test_forked_value_is_not_an_issuance_in_the_child_process(env):
    value = prepare(env)
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe(duplex=False)
    def attempt():
        try:
            check(env, value)
            child.send("incorrectly ready")
        except ia.IntegrationError as error:
            child.send(error.status)
        finally:
            child.close()
    process = context.Process(target=attempt)
    process.start()
    child.close()
    try:
        assert parent.poll(5), "child did not reject the inherited value promptly"
        assert parent.recv() == 503
        process.join(timeout=5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()  # Only the disposable synthetic fixture child, never a job payload.
            process.join(timeout=5)
        parent.close()
    assert check(env, value)["prepared"] is True


def test_elapsed_budget_is_rechecked_after_serializing_the_issued_document(env, monkeypatch):
    original = preparation._canonical
    def slow(value):
        result = original(value)
        if type(value) is dict and "metadata_sources" in value:
            env[3].ticks += 61
        return result
    monkeypatch.setattr(preparation, "_canonical", slow)
    with pytest.raises(ia.IntegrationError) as error:
        prepare(env)
    assert error.value.code == "request_preparation_expired"


def test_retention_never_evicts_and_matching_cancel_replay_works_at_capacity(env, monkeypatch):
    first, second = prepare(env), prepare(env)
    monkeypatch.setattr(tokens, "MAX_OWNER_TOMBSTONES", 1)
    assert cancel(env, first)["durable"] is True
    assert cancel(env, first)["durable"] is True
    with pytest.raises(ia.IntegrationError) as error:
        cancel(env, second)
    assert error.value.status == 503
    assert len(protected(env)["request_preparation_tombstones"]) == 1
    with pytest.raises(ia.IntegrationError):
        check(env, second)  # Failed durable storage still retires its local value.


def test_missing_or_corrupted_tombstone_guards_fail_without_provider_access(env):
    value = prepare(env)
    with env[0]._connect() as connection:
        connection.execute("DROP TRIGGER request_preparation_tombstones_no_update")
    with pytest.raises(ia.IntegrationError):
        check(env, value)
    with pytest.raises(ia.IntegrationError):
        prepare(env, fetch=no_fetch)


def test_native_owner_deletion_does_not_remove_or_block_retained_cancellation(env):
    value = prepare(env)
    cancel(env, value)
    env[0].delete_user(env[1])
    assert len(protected(env)["request_preparation_tombstones"]) == 1


def test_cancel_storage_failure_does_not_report_durable_success(env):
    value = prepare(env)
    with env[0]._connect() as connection:
        connection.execute("CREATE TRIGGER synthetic_cancel_failure BEFORE INSERT ON request_preparation_tombstones BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(ia.IntegrationError) as error:
        cancel(env, value)
    assert error.value.status == 503 and protected(env)["request_preparation_tombstones"] == []
    with pytest.raises(ia.IntegrationError):
        check(env, value)
