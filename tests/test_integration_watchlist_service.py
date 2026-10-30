"""Internal membership plus historical results against isolated synthetic SQLite."""
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

import integration_auth as ia
import integration_personalization as personalization
import integration_watchlist_overlay as overlay
import integration_watchlist_service as service
import tmdb_checked as tc

DIGEST = hashlib.sha256(b'{"media":{"type":"movie","tmdb_id":603}}').hexdigest()


@pytest.fixture()
def env(monkeypatch, tmp_path):
    for name in ("db", "config", "settings", "request_intents"):
        sys.modules.pop(name, None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "watchlist.db"))
    import config
    config.TMDB_API_KEY = "synthetic-only-watchlist-tests"
    import db
    db.init()
    uid = db.create_user("synthetic-watchlist-owner", "synthetic")
    other = db.create_user("synthetic-other-owner", "synthetic", role="admin")
    for owner in (uid, other):
        plan = overlay.prepare_owner(owner)
        overlay.apply_prepared(plan, expected_plan_hash=plan.plan_hash)
        overlay.select_prepared(plan, expected_plan_hash=plan.plan_hash, expected_revision=0)
    return db, uid, other


def raw(tid=603, *, kind="movie", imdb="tt0000001", **changes):
    value = {"id": tid, "title" if kind == "movie" else "name": "Server metadata title", "overview": "", "poster_path": "/server.jpg", "external_ids": {"id": tid, "imdb_id": imdb}}
    value.update(changes)
    return value


def key(n):
    return f"synthetic-watchlist-{n:08d}"


def mutate(env, action="POST", tid=603, *, n=1, kind="movie", user=None, fetch=None, digest=DIGEST):
    return service.mutate(env[1] if user is None else user, action,
                          {"media": {"type": kind, "tmdb_id": tid}}, key(n), digest,
                          fetch=fetch or (lambda *args: raw(tid, kind=kind)))


def rows(env, table):
    with env[0]._connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM " + table)]


def no_fetch(*args):
    pytest.fail("unexpected metadata access")


def test_membership_metadata_selection_and_historic_receipt_commit_together(env):
    db, uid, _ = env
    protected = ("users", "favorite_actors", "user_requests", "requests", "request_intents", "request_outbox", "integration_saved_people")
    before = {table: rows(env, table) for table in protected}
    calls = []
    def fetch(path, params):
        with db._connect() as conn:
            assert not conn.in_transaction
        calls.append((path, params))
        return raw()
    result = mutate(env, fetch=fetch)
    assert result == {"media": {"type": "movie", "tmdb_id": 603}, "saved": True}
    assert calls == [("/movie/603", {"append_to_response": "external_ids"})]
    member = rows(env, "watchlist")[0]
    assert member["title"] == "Server metadata title" and member["poster_path"] == "/server.jpg"
    page = overlay.list_selected(uid)
    assert page["items"][0]["tmdb_id"] == 603 and overlay.check_selection(uid)["revision"] == 2
    receipt = rows(env, "integration_personalization_results")[0]
    assert receipt["resource"] == "watchlist" and receipt["raw_body_sha256"] == DIGEST
    assert json.loads(receipt["response_json"]) == result
    assert {table: rows(env, table) for table in protected} == before


def test_historical_replay_never_resurrects_removed_or_deletes_new_membership(env):
    db, uid, _ = env
    added = mutate(env)
    removed = mutate(env, "DELETE", n=2, fetch=no_fetch)
    assert removed["saved"] is False and overlay.list_selected(uid)["items"] == []
    assert mutate(env, fetch=no_fetch, digest=hashlib.sha256(b"different raw spacing").hexdigest()) == added
    assert rows(env, "watchlist") == []
    new = mutate(env, n=3)
    new_id = rows(env, "watchlist")[0]["id"]
    assert mutate(env, "DELETE", n=2, fetch=no_fetch) == removed
    assert rows(env, "watchlist")[0]["id"] == new_id
    import config
    config.TMDB_API_KEY = ""
    assert mutate(env, n=3, fetch=no_fetch) == new
    db._tls.conn.close()
    del db._tls.conn
    db.init()
    assert mutate(env, n=3, fetch=no_fetch) == new


def test_noop_post_and_delete_need_no_provider_and_keep_selection_revision(env):
    _, uid, _ = env
    mutate(env, "DELETE", n=1, fetch=no_fetch)
    assert overlay.check_selection(uid)["revision"] == 1
    mutate(env, n=2)
    selected = overlay.check_selection(uid)
    mutate(env, n=3, fetch=no_fetch)
    assert overlay.check_selection(uid) == selected
    assert len(rows(env, "watchlist")) == 1 and len(rows(env, "integration_personalization_results")) == 3


@pytest.mark.parametrize("change", [{"id": True}, {"id": 604}, {"media_type": "tv"}, {"overview": None},
    {"external_ids": None}, {"external_ids": {"id": 604, "imdb_id": "tt0000001"}},
    {"external_ids": {"imdb_id": "bad"}}, {"imdb_id": "tt0000002"}, {"title": ""}])
def test_new_member_identity_proof_and_metadata_fail_closed(env, change):
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, fetch=lambda *args: raw(**change))
    assert error.value.status == 503
    assert rows(env, "watchlist") == rows(env, "integration_personalization_results") == []
    assert overlay.check_selection(env[1])["revision"] == 1


@pytest.mark.parametrize("failure,retryable", [(tc.TmdbNotFound("not_found"), False),
    (tc.TmdbUnavailable("upstream_unreachable"), True), (RuntimeError("private URL/key text"), False)])
def test_provider_failure_is_safe_and_no_partial_mutation(env, failure, retryable):
    def fetch(*args):
        raise failure
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, fetch=fetch)
    assert error.value.status == 503 and error.value.retryable is retryable
    assert "private" not in error.value.message
    assert rows(env, "watchlist") == []


def test_owner_and_full_source_changes_during_resolution_prevent_writes(env):
    db, uid, _ = env
    def fetch(*args):
        db.add_to_watchlist(uid, "tt0000002", 604, "movie", "Legacy concurrent row")
        return raw()
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, fetch=fetch)
    assert error.value.status == 409
    assert [r["tmdb_id"] for r in rows(env, "watchlist")] == [604]
    assert rows(env, "integration_personalization_results") == []


def test_selection_change_during_resolution_is_not_silently_rebased(env):
    _, uid, _ = env
    mutate(env)
    def fetch(*args):
        plan = overlay.prepare_owner(uid, fetch=lambda *a: raw(title="New selected metadata"))
        overlay.apply_prepared(plan, expected_plan_hash=plan.plan_hash)
        overlay.select_prepared(plan, expected_plan_hash=plan.plan_hash, expected_revision=2)
        return raw(604, imdb="tt0000002")
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, tid=604, n=2, fetch=fetch)
    assert error.value.status == 409 and error.value.code == "watchlist_selection_changed"
    assert len(rows(env, "watchlist")) == 1


def test_disable_during_resolution_and_historical_replay_after_disable(env):
    db, uid, _ = env
    def fetch(*args):
        db.update_user(uid, enabled=0)
        return raw()
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, fetch=fetch)
    assert error.value.status == 403 and rows(env, "watchlist") == []
    db.update_user(uid, enabled=1)
    mutate(env)
    db.update_user(uid, enabled=0)
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, fetch=no_fetch)
    assert error.value.status == 403


def test_atomic_rollback_when_receipt_insert_fails(env):
    db, uid, _ = env
    before = {table: rows(env, table) for table in ("watchlist", "integration_watchlist_preparations", "integration_watchlist_overlay_entries", "integration_watchlist_selection")}
    with db._connect() as conn:
        conn.execute("CREATE TRIGGER synthetic_receipt_failure BEFORE INSERT ON integration_personalization_results BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env)
    assert error.value.status == 503
    assert {table: rows(env, table) for table in before} == before
    assert rows(env, "integration_personalization_results") == []


def test_receipt_namespace_conflicts_and_owned_isolation(env):
    _, uid, other = env
    mutate(env)
    for operation in (lambda: mutate(env, "DELETE", fetch=no_fetch),
        lambda: mutate(env, tid=604, fetch=no_fetch),
        lambda: personalization.replay_lookup(uid, "follows", "POST", {"person_id": 603}, key(1))):
        with pytest.raises(ia.IntegrationError) as error:
            operation()
        assert error.value.status == 409
    assert mutate(env, user=other)["saved"] is True
    assert len(rows(env, "watchlist")) == 2
    mutate(env, "DELETE", n=2, fetch=no_fetch)
    assert [row["user_id"] for row in rows(env, "watchlist")] == [other]


def test_concurrent_same_key_has_one_membership_and_one_result(env):
    barrier = Barrier(2)
    def fetch(*args):
        barrier.wait(timeout=5)
        return raw()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: mutate(env, fetch=fetch), range(2)))
    assert results[0] == results[1]
    assert len(rows(env, "watchlist")) == len(rows(env, "integration_personalization_results")) == 1


def test_retention_rollback_and_return_to_existing_empty_plan_at_capacity(env, monkeypatch):
    _, uid, _ = env
    mutate(env)
    monkeypatch.setattr(overlay, "MAX_OWNER_PLANS", 2)
    with pytest.raises(ia.IntegrationError):
        mutate(env, tid=604, n=2, fetch=no_fetch)
    assert len(rows(env, "watchlist")) == 1 and len(rows(env, "integration_personalization_results")) == 1
    assert mutate(env, "DELETE", n=3, fetch=no_fetch)["saved"] is False
    assert overlay.list_selected(uid)["items"] == []


@pytest.mark.parametrize("payload", [{}, {"media": {"type": "movie", "tmdb_id": True}},
    {"media": {"type": "movie", "tmdb_id": 603}, "title": "Untrusted"},
    {"media": {"type": "movie", "tmdb_id": 603, "imdb_id": "tt0000001"}}])
def test_exact_body_and_identity_required_before_metadata(env, payload):
    with pytest.raises(ia.IntegrationError) as error:
        service.mutate(env[1], "POST", payload, key(1), DIGEST, fetch=no_fetch)
    assert error.value.status == 400
    assert rows(env, "watchlist") == []


def test_movie_and_tv_have_distinct_typed_membership(env):
    mutate(env)
    mutate(env, kind="tv", n=2)
    assert {(r["media_type"], r["tmdb_id"]) for r in rows(env, "watchlist")} == {("movie", 603), ("tv", 603)}
    assert {item["media_type"] for item in overlay.list_selected(env[1])["items"]} == {"movie", "tv"}
    mutate(env, "DELETE", n=3, fetch=no_fetch)
    assert [r["media_type"] for r in rows(env, "watchlist")] == ["tv"]


def test_existing_resolved_null_native_id_is_not_rewritten_by_noop_post(env):
    db, uid, _ = env
    db.add_to_watchlist(uid, "tt0000001", None, "movie", "Opaque legacy display text")
    def fetch(path, params):
        return {"movie_results": [{"id": 603}]} if path.startswith('/find/') else raw()
    plan = overlay.prepare_owner(uid, fetch=fetch)
    overlay.apply_prepared(plan, expected_plan_hash=plan.plan_hash)
    overlay.select_prepared(plan, expected_plan_hash=plan.plan_hash, expected_revision=1)
    before = rows(env, "watchlist")
    assert mutate(env, fetch=no_fetch)["saved"] is True
    assert rows(env, "watchlist") == before and before[0]["tmdb_id"] is None


def test_no_implicit_preparation_or_identity_collision_merge(env):
    db, uid, _ = env
    new_owner = db.create_user("unprepared-owner", "synthetic")
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, user=new_owner, fetch=no_fetch)
    assert error.value.status == 503
    mutate(env)
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, tid=604, n=2)  # Provider IMDb conflicts with existing different typed identity.
    assert error.value.status == 409 and len(rows(env, "watchlist")) == 1


@pytest.mark.parametrize("response", [{"media": {"type": "movie", "tmdb_id": 603}, "saved": False},
    {"media": {"type": "tv", "tmdb_id": 603}, "saved": True},
    {"media": {"type": "movie", "tmdb_id": True}, "saved": True},
    {"media": {"type": "movie", "tmdb_id": 603}, "saved": 1},
    {"media": {"type": "movie", "tmdb_id": 603}, "saved": True, "can_play": True}])
def test_malformed_historical_result_is_not_replayed_or_reapplied(env, response):
    db, uid, _ = env
    mutate(env)
    before = rows(env, "watchlist")
    with db._connect() as conn:
        conn.execute("DROP TRIGGER integration_results_no_update")
        conn.execute("UPDATE integration_personalization_results SET response_json=?", (json.dumps(response),))
        personalization.init_schema(conn)
    with pytest.raises(ia.IntegrationError) as error:
        mutate(env, fetch=no_fetch)
    assert error.value.status == 503 and rows(env, "watchlist") == before
