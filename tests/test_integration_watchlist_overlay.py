"""Internal overlays against temporary SQLite and synthetic checked metadata only."""
import copy
import json
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

import integration_auth as ia
import integration_watchlist_overlay as overlay
import tmdb_checked as tc


@pytest.fixture()
def env(monkeypatch, tmp_path):
    for name in ("db", "config", "settings", "request_intents"):
        sys.modules.pop(name, None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "overlay.db"))
    import config
    config.TMDB_API_KEY = "synthetic-overlay-key"
    import db
    db.init()
    uid = db.create_user("overlay-owner", "synthetic", quota_monthly=2)
    other = db.create_user("other-owner", "synthetic", role="admin")
    return db, uid, other


def add(env, n=1, *, tid=603, kind="movie", user=None):
    db, uid, _ = env
    db.add_to_watchlist(uid if user is None else user, f"tt{n:07d}", tid, kind,
                        "UNTRUSTED caller title", "/UNTRUSTED.jpg")


def raw(tid=603, imdb="tt0000001", kind="movie", **changes):
    data = {"id": tid, "title" if kind == "movie" else "name": "Server title", "overview": "Server overview",
            "poster_path": "/server.jpg", "backdrop_path": "/backdrop.jpg", "vote_average": 8,
            "release_date" if kind == "movie" else "first_air_date": "2020-01-02",
            "external_ids": {"id": tid, "imdb_id": imdb}}
    data.update(changes)
    return data


def prepare(env, *, fetch=None, user=None, **kwargs):
    return overlay.prepare_owner(env[1] if user is None else user,
        fetch=fetch or (lambda *a: raw()), **kwargs)


def apply(prepared):
    return overlay.apply_prepared(prepared, expected_plan_hash=prepared.plan_hash)


def rows(env, table):
    with env[0]._connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM " + table)]


def no_fetch(*args):
    pytest.fail("unexpected metadata fetch")


def test_prepare_check_are_read_only_apply_only_overlay_and_title_only(env, monkeypatch):
    db, uid, _ = env
    add(env)
    db.add_favorite_actor(uid, 12, "Legacy automatic favorite")
    protected = ("watchlist", "favorite_actors", "users", "requests", "user_requests", "request_intents",
                 "request_outbox", "integration_saved_people", "integration_personalization_results")
    before = {table: rows(env, table) for table in protected}
    calls = []
    def fetch(path, params):
        with db._connect() as conn:
            assert not conn.in_transaction
        calls.append((path, params))
        return raw()  # No credits/videos/providers/region blocks needed.
    with db._connect() as conn:
        changes = conn.total_changes
    plan = prepare(env, fetch=fetch)
    assert overlay.check_prepared(plan)["row_count"] == 1
    assert overlay.check_overlay(uid, plan.plan_hash) == {"ready": False, "reason": "not_prepared"}
    with db._connect() as conn:
        assert conn.total_changes == changes
    assert calls == [("/movie/603", {"append_to_response": "external_ids"})]
    assert rows(env, "integration_watchlist_preparations") == []
    assert apply(plan) == overlay.check_overlay(uid, plan.plan_hash)
    entry = rows(env, "integration_watchlist_overlay_entries")[0]
    dto = json.loads(entry["title_json"])
    assert dto["title"] == "Server title" and dto["overview"] == "Server overview"
    assert dto["poster_url"].endswith("/server.jpg")
    assert "UNTRUSTED" not in json.dumps(entry)
    assert not {"availability", "library_status", "entitlement", "can_play", "imdb_id"} & dto.keys()
    assert {table: rows(env, table) for table in protected} == before
    assert apply(plan) == overlay.check_prepared(plan)
    assert len(rows(env, "integration_watchlist_preparations")) == 1


@pytest.mark.parametrize("kind", ["movie", "tv"])
def test_null_tmdb_find_exact_matching_kind_and_roundtrip(env, kind):
    db, uid, _ = env
    add(env, tid=None, kind=kind)
    calls = []
    def fetch(path, params):
        with db._connect() as conn:
            assert not conn.in_transaction
        calls.append((path, params))
        if path.startswith("/find/"):
            return {"movie_results": [{"id": 603}] if kind == "movie" else [],
                    "tv_results": [{"id": 603}] if kind == "tv" else [],
                    "tv_episode_results": [{"id": 999, "show_id": 111}]}
        return raw(kind=kind)
    plan = prepare(env, fetch=fetch)
    apply(plan)
    assert calls == [("/find/tt0000001", {"external_source": "imdb_id"}),
                     (f"/{kind}/603", {"append_to_response": "external_ids"})]
    entry = rows(env, "integration_watchlist_overlay_entries")[0]
    proof = json.loads(entry["identity_proof_json"])
    assert proof["find_candidate_id"] == 603 and len(proof["find_hash"]) == 64
    assert rows(env, "watchlist")[0]["tmdb_id"] is None  # No silent repair.


@pytest.mark.parametrize("found", [{}, {"movie_results": []}, {"movie_results": [{"id": 1}, {"id": 2}]},
    {"movie_results": [{"id": True}]}, {"movie_results": [{"id": 1.0}]},
    {"movie_results": [{"id": 603, "media_type": "tv"}]}, {"movie_results": {}},
    {"tv_results": [{"id": 603}], "tv_episode_results": [{"id": 9, "show_id": 603}]}])
def test_find_zero_ambiguous_bad_and_other_kind_never_guess(env, found):
    add(env, tid=None)
    calls = []
    def fetch(path, params):
        calls.append(path)
        return found
    with pytest.raises(ia.IntegrationError) as error:
        prepare(env, fetch=fetch)
    assert error.value.status == 503 and calls == ["/find/tt0000001"]
    assert rows(env, "integration_watchlist_preparations") == []


@pytest.mark.parametrize("change", [
    {"id": True}, {"id": 603.0}, {"id": 604}, {"media_type": "tv"},
    {"overview": None}, {"overview": 1}, {"external_ids": None},
    {"external_ids": {"id": 603, "imdb_id": "tt0000002"}},
    {"external_ids": {"id": True, "imdb_id": "tt0000001"}},
    {"external_ids": {"id": 604, "imdb_id": "tt0000001"}}, {"imdb_id": "tt0000002"},
    {"title": ""}, {"overview": "bad\x00"}, {"release_date": "2020-02-30"}, {"vote_average": True},
])
def test_raw_identity_and_metadata_fail_closed(env, change):
    add(env)
    with pytest.raises(ia.IntegrationError) as error:
        prepare(env, fetch=lambda *a: raw(**change))
    assert error.value.status == 503 and not error.value.retryable
    assert rows(env, "integration_watchlist_preparations") == []


def test_missing_overview_rejected_no_legacy_fallback(env):
    add(env)
    data = raw()
    del data["overview"]
    with pytest.raises(ia.IntegrationError):
        prepare(env, fetch=lambda *a: data)


@pytest.mark.parametrize("failure,retryable", [(tc.TmdbNotFound("not_found"), False),
    (tc.TmdbUnavailable("upstream_unreachable"), True), (tc.TmdbUnavailable("bad_json"), False),
    (RuntimeError("private upstream URL or path"), False)])
def test_metadata_errors_are_safe_no_partial_plan(env, failure, retryable):
    add(env)
    def fetch(*args):
        raise failure
    with pytest.raises(ia.IntegrationError) as error:
        prepare(env, fetch=fetch)
    assert error.value.status == 503 and error.value.retryable is retryable
    assert "private" not in error.value.message
    assert rows(env, "integration_watchlist_overlay_entries") == []


@pytest.mark.parametrize("column,value", [("id", 88), ("user_id", 2), ("imdb_id", "tt0000002"),
    ("tmdb_id", 604), ("media_type", "tv"), ("title", "Different caller title"),
    ("poster_path", "/different.jpg"), ("added_at", "2020-01-01 00:00:00")])
def test_every_native_column_drift_prevents_apply(env, column, value):
    db, uid, _ = env
    add(env)
    plan = prepare(env)
    with db._connect() as conn:
        conn.execute(f"UPDATE watchlist SET {column}=? WHERE user_id=?", (value, uid))
    with pytest.raises(ia.IntegrationError) as error:
        apply(plan)
    assert error.value.status == 409
    assert rows(env, "integration_watchlist_preparations") == []


def test_add_delete_readd_and_drift_during_resolution(env):
    db, uid, _ = env
    add(env)
    plan = prepare(env)
    add(env, n=2, tid=604)
    with pytest.raises(ia.IntegrationError):
        overlay.check_prepared(plan)
    db.remove_from_watchlist(uid, "tt0000002", "movie")
    # Current-state equality is sufficient; no event-history/ABA promise.
    apply(plan)
    db.remove_from_watchlist(uid, "tt0000001", "movie")  # Overlay cannot block legacy deletion.
    add(env)
    with pytest.raises(ia.IntegrationError):
        overlay.check_overlay(uid, plan.plan_hash)
    def drift(path, params):
        add(env, n=3, tid=605)
        return raw()
    with pytest.raises(ia.IntegrationError):
        prepare(env, fetch=drift)
    assert len(rows(env, "integration_watchlist_preparations")) == 1


def test_owner_enabled_separation_and_disable_during_resolution(env):
    db, uid, other = env
    add(env)
    plan = prepare(env)
    assert overlay.check_overlay(other, plan.plan_hash) == {"ready": False, "reason": "not_prepared"}
    for enabled in (0,):
        db.update_user(uid, enabled=enabled)
        for call in (lambda: prepare(env, fetch=no_fetch), lambda: overlay.check_prepared(plan), lambda: apply(plan),
                     lambda: overlay.check_overlay(uid, plan.plan_hash)):
            with pytest.raises(ia.IntegrationError) as error:
                call()
            assert error.value.status == 403
    db.update_user(uid, enabled=1)
    def disable(path, params):
        db.update_user(uid, enabled=0)
        return raw()
    with pytest.raises(ia.IntegrationError) as error:
        prepare(env, fetch=disable)
    assert error.value.status == 403


def test_immutable_internal_object_no_loader_forged_copy_or_mutable_bytes(env):
    add(env)
    plan = prepare(env)
    with pytest.raises(TypeError):
        overlay.PreparedWatchlist()
    with pytest.raises(AttributeError):
        plan._body = b"{}"
    for fake in (json.loads(plan._body), plan._body, copy.deepcopy(json.loads(plan._body))):
        with pytest.raises(ia.IntegrationError):
            overlay.apply_prepared(fake, expected_plan_hash=plan.plan_hash)
    forged = object.__new__(overlay.PreparedWatchlist)
    object.__setattr__(forged, "_body", plan._body)
    with pytest.raises(ia.IntegrationError):
        overlay.check_prepared(forged)
    object.__setattr__(plan, "_body", b"{}")
    with pytest.raises(ia.IntegrationError):
        overlay.check_prepared(plan)


def test_wrong_review_hash_rejected_and_identical_prepare_deterministic(env):
    add(env)
    first, second = prepare(env), prepare(env)
    assert first.plan_hash == second.plan_hash
    with pytest.raises(ia.IntegrationError) as error:
        overlay.apply_prepared(first, expected_plan_hash="0" * 64)
    assert error.value.status == 409


def test_empty_owner_real_empty_and_no_schema_initialization(env):
    db, uid, _ = env
    with db._connect() as conn:
        conn.execute("DROP TABLE integration_watchlist_overlay_entries")
        conn.execute("DROP TABLE integration_watchlist_preparations")
        before = conn.total_changes
    plan = prepare(env, fetch=no_fetch)
    assert overlay.check_prepared(plan)["row_count"] == 0
    with db._connect() as conn:
        assert conn.total_changes == before
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='integration_watchlist_preparations'").fetchall()
    with pytest.raises(ia.IntegrationError) as error:
        overlay.check_overlay(uid, plan.plan_hash)
    assert error.value.status == 503


def test_fixed_bounds_predict_calls_before_fetch_and_source_size(env, monkeypatch):
    db, uid, _ = env
    assert (overlay.MAX_NATIVE_ROWS, overlay.MAX_FETCHES, overlay.MAX_SECONDS, overlay.MAX_PREPARATION_BYTES,
            overlay.MAX_OWNER_PLANS, overlay.MAX_GLOBAL_PLANS, overlay.MAX_GLOBAL_ENTRIES) == (
        100, 32, 60, 1024 * 1024, 16, 10000, 1000000)
    for n in range(1, 18):
        add(env, n=n, tid=None)
    with pytest.raises(ia.IntegrationError):
        prepare(env, fetch=no_fetch)  # 34 predictable calls.
    with db._connect() as conn:
        conn.execute("DELETE FROM watchlist")
        conn.executemany("INSERT INTO watchlist(user_id,imdb_id,tmdb_id,media_type,title) VALUES (?,?,?,'movie','x')",
                         [(uid, f"tt{n:07d}", n) for n in range(1, 102)])
    with pytest.raises(ia.IntegrationError):
        prepare(env, fetch=no_fetch)
    with db._connect() as conn:
        conn.execute("DELETE FROM watchlist")
    add(env)
    with db._connect() as conn:
        conn.execute("UPDATE watchlist SET title=?", ("x" * (overlay.MAX_PREPARATION_BYTES + 1),))
    with pytest.raises(ia.IntegrationError):
        prepare(env, fetch=no_fetch)


def test_elapsed_and_serialized_byte_budgets_no_applicable_plan(env):
    add(env)
    for readings in ([0, 61], [0, 0, 61], [1, 0], [float("nan")]):
        clock = iter(readings)
        with pytest.raises(ia.IntegrationError):
            prepare(env, clock=lambda: next(clock))
    with pytest.raises(ia.IntegrationError):
        prepare(env, fetch=lambda *a: raw(overview="😀" * 100000))
    assert rows(env, "integration_watchlist_preparations") == []


def test_duplicate_typed_identities_rejected_without_dedup(env):
    add(env)
    add(env, n=2)
    with pytest.raises(ia.IntegrationError):
        prepare(env, fetch=no_fetch)
    assert len(rows(env, "watchlist")) == 2


@pytest.mark.parametrize("column,value", [("media_type", "series"), ("imdb_id", "TT1234567"),
    ("tmdb_id", 0), ("tmdb_id", 1.5), ("added_at", "2020-02-30 00:00:00")])
def test_invalid_native_identity_no_alias_guess(env, column, value):
    db, _, _ = env
    add(env)
    with db._connect() as conn:
        conn.execute(f"UPDATE watchlist SET {column}=?", (value,))
    with pytest.raises(ia.IntegrationError):
        prepare(env, fetch=no_fetch)


def test_atomic_apply_rolls_back_all_entries_on_insert_failure(env):
    db, _, _ = env
    add(env)
    add(env, n=2, tid=604)
    plan = prepare(env, fetch=lambda path, params: raw(604, "tt0000002") if path.endswith("604") else raw())
    with db._connect() as conn:
        conn.execute("CREATE TRIGGER synthetic_overlay_failure BEFORE INSERT ON integration_watchlist_overlay_entries "
                     "WHEN NEW.tmdb_id=604 BEGIN SELECT RAISE(ABORT,'private database path'); END")
    with pytest.raises(ia.IntegrationError) as error:
        apply(plan)
    assert error.value.status == 503 and "private" not in error.value.message
    assert rows(env, "integration_watchlist_preparations") == rows(env, "integration_watchlist_overlay_entries") == []
    assert len(rows(env, "watchlist")) == 2


def test_same_plan_concurrent_apply_one_retained_copy(env):
    add(env)
    plan = prepare(env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: apply(plan), range(2)))
    assert results[0] == results[1]
    assert len(rows(env, "integration_watchlist_preparations")) == len(rows(env, "integration_watchlist_overlay_entries")) == 1


@pytest.mark.parametrize("limit", ["MAX_OWNER_PLANS", "MAX_GLOBAL_PLANS", "MAX_GLOBAL_ENTRIES"])
def test_retention_capacity_no_eviction_and_identical_replay_allowed(env, monkeypatch, limit):
    add(env)
    first = prepare(env)
    apply(first)
    second = prepare(env, fetch=lambda *a: raw(title="New server title"))
    monkeypatch.setattr(overlay, limit, 1)
    assert apply(first)["ready"]
    with pytest.raises(ia.IntegrationError) as error:
        apply(second)
    assert error.value.status == 503 and not error.value.retryable
    assert len(rows(env, "integration_watchlist_preparations")) == 1


def test_retention_concurrent_distinct_plans_respects_owner_cap(env, monkeypatch):
    add(env)
    plans = [prepare(env, fetch=lambda *a, n=n: raw(title=f"Server {n}")) for n in range(2)]
    monkeypatch.setattr(overlay, "MAX_OWNER_PLANS", 1)
    def attempt(plan):
        try:
            return apply(plan)["ready"]
        except ia.IntegrationError as error:
            return error.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, plans))
    assert sorted(outcomes, key=str) == [503, True]
    assert len(rows(env, "integration_watchlist_preparations")) == 1


def test_immutable_tables_native_removal_and_owner_cascade(env):
    db, uid, _ = env
    add(env)
    plan = prepare(env)
    apply(plan)
    with db._connect() as conn:
        for table in overlay._TABLES:
            for sql in (f"DELETE FROM {table}", f"UPDATE {table} SET plan_hash=plan_hash",
                        f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"):
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(sql)
    db.remove_from_watchlist(uid, "tt0000001", "movie")
    assert len(rows(env, "integration_watchlist_overlay_entries")) == 1
    db.delete_user(uid)
    assert rows(env, "integration_watchlist_preparations") == rows(env, "integration_watchlist_overlay_entries") == []


@pytest.mark.parametrize("column,value", [("title_json", '{}'), ("title_json", '{"availability":"present"}'),
    ("title_hash", "0" * 64), ("identity_proof_json", '{}'), ("row_source_hash", "0" * 64),
    ("native_identity_json", '{}'), ("tmdb_id", 604)])
def test_persisted_corruption_rejected_on_check_and_identical_apply(env, column, value):
    db, uid, _ = env
    add(env)
    plan = prepare(env)
    apply(plan)
    with db._connect() as conn:
        conn.execute("DROP TRIGGER integration_watchlist_overlay_entries_no_update")
        conn.execute(f"UPDATE integration_watchlist_overlay_entries SET {column}=?", (value,))
        overlay.init_schema(conn)
    for call in (lambda: overlay.check_overlay(uid, plan.plan_hash), lambda: apply(plan)):
        with pytest.raises(ia.IntegrationError) as error:
            call()
        assert error.value.status == 503


def test_database_failure_and_no_nested_transaction(env, monkeypatch):
    db, uid, _ = env
    add(env)
    plan = prepare(env)
    with db._connect() as conn:
        conn.execute("BEGIN")
        try:
            with pytest.raises(ia.IntegrationError):
                prepare(env, fetch=no_fetch)
            assert conn.in_transaction
        finally:
            conn.execute("ROLLBACK")
    def failure():
        raise sqlite3.OperationalError("private database filename")
    monkeypatch.setattr(db, "_connect", failure)
    for call in (lambda: prepare(env, fetch=no_fetch), lambda: overlay.check_prepared(plan),
                 lambda: overlay.check_overlay(uid, plan.plan_hash), lambda: apply(plan)):
        with pytest.raises(ia.IntegrationError) as error:
            call()
        assert error.value.status == 503 and "private" not in error.value.message


def test_applied_named_check_after_process_object_registry_is_lost(env):
    add(env)
    plan = prepare(env)
    plan_hash = plan.plan_hash
    apply(plan)
    with overlay._ISSUED_LOCK:
        overlay._ISSUED.clear()
    assert overlay.check_overlay(env[1], plan_hash)["ready"] is True
    with pytest.raises(ia.IntegrationError):
        overlay.apply_prepared(plan, expected_plan_hash=plan_hash)


def test_missing_storage_guards_no_new_installation(env):
    db, _, _ = env
    add(env)
    plan = prepare(env)
    with db._connect() as conn:
        conn.execute("DROP TRIGGER integration_watchlist_preparations_no_update")
    with pytest.raises(ia.IntegrationError):
        apply(plan)
    assert rows(env, "integration_watchlist_preparations") == []


def test_overlay_uses_only_metadata_source_no_automatic_module_paths(env, monkeypatch):
    import builtins
    add(env)
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert name not in {"processor", "auto_approve", "library_status", "request_intents"}
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    apply(prepare(env))


def select(plan, revision=0):
    return overlay.select_prepared(plan, expected_plan_hash=plan.plan_hash, expected_revision=revision)


def test_selection_is_explicit_applied_owned_and_read_only_check(env):
    db, uid, other = env
    add(env)
    plan = prepare(env)
    assert overlay.check_selection(uid) == {"ready": False, "reason": "not_selected", "revision": 0}
    with pytest.raises(ia.IntegrationError):
        select(plan)  # An unapplied issued preparation cannot become current.
    apply(plan)
    assert overlay.check_selection(uid)["ready"] is False
    protected = ("watchlist", "users", "requests", "user_requests", "request_intents", "request_outbox")
    before = {table: rows(env, table) for table in protected}
    selected = select(plan)
    assert selected == {"ready": True, "plan_hash": plan.plan_hash, "row_count": 1, "revision": 1}
    with db._connect() as conn:
        changes = conn.total_changes
        assert overlay.check_selection(uid) == selected
        assert conn.total_changes == changes
    assert overlay.check_selection(other)["ready"] is False
    assert {table: rows(env, table) for table in protected} == before
    assert select(plan, 1) == selected  # No gratuitous new revision for exact current selection.


def test_selection_compare_and_swap_prevents_stale_revision_and_aba(env):
    add(env)
    first = prepare(env)
    second = prepare(env, fetch=lambda *a: raw(title="New metadata"))
    for plan in (first, second):
        apply(plan)
    assert select(first)["revision"] == 1
    with pytest.raises(ia.IntegrationError) as error:
        select(second, 0)
    assert error.value.code == "watchlist_selection_changed" and error.value.status == 409
    assert select(second, 1)["revision"] == 2
    assert select(first, 2)["revision"] == 3
    with pytest.raises(ia.IntegrationError):
        select(second, 1)
    assert overlay.check_selection(env[1])["revision"] == 3
    assert len(rows(env, "integration_watchlist_preparations")) == 2


def test_concurrent_selection_of_distinct_plans_has_one_cas_winner(env):
    add(env)
    plans = [prepare(env, fetch=lambda *a, n=n: raw(title=f"Server {n}")) for n in range(2)]
    for plan in plans:
        apply(plan)
    def attempt(plan):
        try:
            return select(plan)["revision"]
        except ia.IntegrationError as error:
            return error.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, plans)) == [1, 409]
    assert len(rows(env, "integration_watchlist_selection")) == 1


@pytest.mark.parametrize("column,value", [("title", "Edited caller text"), ("tmdb_id", 604),
                                         ("added_at", "2020-01-01 00:00:00")])
def test_selection_rechecks_entire_native_source_not_age(env, column, value):
    db, uid, _ = env
    add(env)
    plan = prepare(env)
    apply(plan)
    select(plan)
    with db._connect() as conn:
        conn.execute(f"UPDATE watchlist SET {column}=? WHERE user_id=?", (value, uid))
    for call in (lambda: overlay.check_selection(uid), lambda: select(plan, 1)):
        with pytest.raises(ia.IntegrationError) as error:
            call()
        assert error.value.status == 409
    assert rows(env, "integration_watchlist_selection")[0]["revision"] == 1


def test_selection_legacy_removal_does_not_block_or_silently_drop_other_rows(env):
    db, uid, _ = env
    add(env)
    plan = prepare(env)
    apply(plan)
    select(plan)
    db.remove_from_watchlist(uid, "tt0000001", "movie")
    assert rows(env, "watchlist") == []
    assert len(rows(env, "integration_watchlist_overlay_entries")) == 1
    with pytest.raises(ia.IntegrationError):
        overlay.check_selection(uid)
    empty = prepare(env, fetch=no_fetch)
    apply(empty)
    assert select(empty, 1)["row_count"] == 0
    assert overlay.check_selection(uid)["revision"] == 2


def test_selection_enabled_owner_and_whole_owner_cascade(env):
    db, uid, _ = env
    add(env)
    plan = prepare(env)
    apply(plan)
    select(plan)
    db.update_user(uid, enabled=0)
    for call in (lambda: overlay.check_selection(uid), lambda: select(plan, 1)):
        with pytest.raises(ia.IntegrationError) as error:
            call()
        assert error.value.status == 403
    db.delete_user(uid)
    assert rows(env, "integration_watchlist_selection") == []
    assert rows(env, "integration_watchlist_preparations") == []


def test_selection_requires_issued_object_exact_hash_and_valid_revision(env):
    add(env)
    plan = prepare(env)
    apply(plan)
    for value in (True, -1, 1.0, "0", 9223372036854775808):
        with pytest.raises(ia.IntegrationError):
            select(plan, value)
    with pytest.raises(ia.IntegrationError) as error:
        overlay.select_prepared(plan, expected_plan_hash="0" * 64, expected_revision=0)
    assert error.value.status == 409
    for value in (plan.plan_hash, plan._body, json.loads(plan._body)):
        with pytest.raises(ia.IntegrationError):
            overlay.select_prepared(value, expected_plan_hash=plan.plan_hash, expected_revision=0)
    assert rows(env, "integration_watchlist_selection") == []


def test_selection_replacement_deletion_and_invalid_revisions_are_guarded(env):
    db, uid, _ = env
    add(env)
    plan = prepare(env)
    apply(plan)
    select(plan)
    with db._connect() as conn:
        for sql in ("DELETE FROM integration_watchlist_selection",
                    "INSERT OR REPLACE INTO integration_watchlist_selection SELECT * FROM integration_watchlist_selection",
                    "UPDATE integration_watchlist_selection SET revision=revision+1"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
        conn.execute("DROP TRIGGER integration_watchlist_selection_revision")
    for call in (lambda: overlay.check_selection(uid), lambda: select(plan, 1)):
        with pytest.raises(ia.IntegrationError) as error:
            call()
        assert error.value.status == 503


def test_selection_survives_registry_loss_but_hash_does_not_grant_new_selection(env):
    add(env)
    plan = prepare(env)
    apply(plan)
    select(plan)
    with overlay._ISSUED_LOCK:
        overlay._ISSUED.clear()
    assert overlay.check_selection(env[1])["ready"] is True
    with pytest.raises(ia.IntegrationError):
        select(plan, 1)


def test_corrupted_selection_cannot_read_another_owners_applied_plan(env):
    db, uid, other = env
    add(env)
    first = prepare(env)
    apply(first)
    select(first)
    add(env, user=other)
    other_plan = prepare(env, user=other)
    apply(other_plan)
    with db._connect() as conn:
        # Simulate an otherwise structurally valid cross-owner pointer in storage.
        conn.execute("UPDATE integration_watchlist_selection SET plan_hash=?,revision=revision+1 WHERE user_id=?",
                     (other_plan.plan_hash, uid))
    with pytest.raises(ia.IntegrationError) as error:
        overlay.check_selection(uid)
    assert error.value.status == 503
    assert overlay.check_selection(other)["ready"] is False


def test_duplicate_legacy_typed_rows_remain_visible_native_and_unselected(env):
    add(env)
    add(env, n=2)  # Different legacy IMDb row, same typed TMDB identity.
    before = rows(env, "watchlist")
    with pytest.raises(ia.IntegrationError):
        prepare(env, fetch=no_fetch)
    assert rows(env, "watchlist") == before and len(before) == 2
    assert overlay.check_selection(env[1])["ready"] is False


def test_selected_pagination_is_owned_descending_and_metadata_only(env):
    db, uid, other = env
    for n in range(1, 6):
        add(env, n=n, tid=600+n)
    def fetch(path, params):
        tid = int(path.rsplit("/", 1)[1])
        return raw(tid=tid, imdb=f"tt{tid-600:07d}")
    plan = prepare(env, fetch=fetch)
    apply(plan)
    select(plan)
    add(env, n=6, tid=606, user=other)
    before = rows(env, "watchlist")
    first = overlay.list_selected(uid, limit=2)
    second = overlay.list_selected(uid, limit=2, before_id=first["next_before_id"])
    last = overlay.list_selected(uid, limit=2, before_id=second["next_before_id"])
    all_items = first["items"] + second["items"] + last["items"]
    assert [item["watchlist_id"] for item in all_items] == [5, 4, 3, 2, 1]
    assert [first["next_before_id"], second["next_before_id"], last["next_before_id"]] == [4, 2, None]
    for item in all_items:
        assert item["title"] == "Server title"
        assert re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", item["added_at"])
        assert not {"user_id", "imdb_id", "entitlement", "library_status", "can_play", "identity_proof"} & item.keys()
    with pytest.raises(ia.IntegrationError):
        overlay.list_selected(other, before_id=first["next_before_id"])
    assert rows(env, "watchlist") == before


@pytest.mark.parametrize("kwargs", [{"limit": True}, {"limit": 0}, {"limit": 101}, {"limit": 1.0},
    {"before_id": True}, {"before_id": 0}, {"before_id": -1}, {"before_id": "2"},
    {"before_id": 9223372036854775808}])
def test_selected_pagination_rejects_invalid_limits_and_cursors(env, kwargs):
    with pytest.raises(ia.IntegrationError) as error:
        overlay.list_selected(env[1], **kwargs)
    assert error.value.status == 400


def test_selected_empty_requires_explicit_valid_selection_not_missing_source(env):
    _, uid, _ = env
    with pytest.raises(ia.IntegrationError) as error:
        overlay.list_selected(uid)
    assert error.value.status == 503
    plan = prepare(env, fetch=no_fetch)
    apply(plan)
    select(plan)
    assert overlay.list_selected(uid) == {"items": [], "next_before_id": None}


def test_selected_page_checks_rows_outside_requested_page_and_owner_state(env):
    db, uid, _ = env
    for n in range(1, 3):
        add(env, n=n, tid=600+n)
    def fetch(path, params):
        tid = int(path.rsplit("/", 1)[1])
        return raw(tid=tid, imdb=f"tt{tid-600:07d}")
    plan = prepare(env, fetch=fetch)
    apply(plan)
    select(plan)
    page = overlay.list_selected(uid, limit=1)
    assert page["items"][0]["watchlist_id"] == 2
    with db._connect() as conn:
        conn.execute("UPDATE watchlist SET title='Changed unseen row' WHERE id=1")
    with pytest.raises(ia.IntegrationError) as error:
        overlay.list_selected(uid, limit=1)
    assert error.value.status == 409
    db.update_user(uid, enabled=0)
    with pytest.raises(ia.IntegrationError) as error:
        overlay.list_selected(uid, before_id=page["next_before_id"])
    assert error.value.status == 403
