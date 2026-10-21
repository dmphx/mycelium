"""Request intent foundation on real temporary SQLite. Synthetic data only."""
import sqlite3
import sys
import threading

import pytest


@pytest.fixture()
def env(monkeypatch, tmp_path):
    for m in ("db", "settings", "config", "request_intents"):
        sys.modules.pop(m, None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    import config
    config.DB_PATH = str(tmp_path / "t.db")
    import db
    db.init()
    import request_intents as ri
    return db, ri


MD = {"title": "Synthetic Show", "imdb_id": "tt1234567"}


def P(tid=100, kind="all_seasons", typ="tv", **scope):
    return {"media": {"type": typ, "tmdb_id": tid}, "scope": dict(kind=kind, **scope)}


def K(n):
    return f"key-{n:0>14}"


def mk(db, name, role="user", **f):
    uid = db.create_user(name, "scrypt$x", role=role)
    if f:
        db.update_user(uid, **f)
    return uid


def rows(db, table):
    with db._connect() as c:
        return c.execute(f"SELECT * FROM {table}").fetchall()


# ── payload ──────────────────────────────────────────────────────────────────

def test_normalize_canonical_and_hashes(env):
    db, ri = env
    a = ri.normalize_payload(P(kind="selected_seasons", seasons=[3, 1, 2]))
    assert a["scope"]["seasons"] == [1, 2, 3]
    b = ri.normalize_payload({"scope": {"seasons": [2, 3, 1], "kind": "selected_seasons"},
                              "media": {"tmdb_id": 100, "type": "tv"}})
    assert ri.semantic_hash(a) == ri.semantic_hash(b)
    import hashlib
    assert ri.raw_signature_hash(b'{"a": 1}') == hashlib.sha256(b'{"a": 1}').hexdigest()
    assert ri.raw_signature_hash(b'{"a":1}') != ri.raw_signature_hash(b'{"a": 1}')
    for notbytes in ({"a": 1}, "text", None, 5):
        with pytest.raises(ri.IntentError):
            ri.raw_signature_hash(notbytes)
    e = ri.normalize_payload(P(kind="episodes", episodes=[{"season": 2, "episode": 1},
                                                          {"season": 1, "episode": 9}]))
    assert e["scope"]["episodes"][0] == {"season": 1, "episode": 9}
    assert ri.normalize_payload(P(typ="movie", kind="movie", tid=5))["scope"] == {"kind": "movie"}


@pytest.mark.parametrize("bad", [
    None, [], {}, {"media": {}, "scope": {}},
    {**P(), "extra": 1},
    {"media": {"type": "tv", "tmdb_id": 1, "x": 1}, "scope": {"kind": "all_seasons"}},
    P(tid=0), P(tid=-1), P(tid=2**31), P(tid=True), P(tid=1.0), P(tid="5"),
    P(typ="music"), P(kind="bogus"), P(typ="movie", kind="all_seasons"), P(typ="tv", kind="movie"),
    P(kind="all_seasons", seasons=[1]), P(kind="future", episodes=[]),
    P(kind="selected_seasons"), P(kind="selected_seasons", seasons=[]),
    P(kind="selected_seasons", seasons=[1, 1]), P(kind="selected_seasons", seasons=[1000]),
    P(kind="selected_seasons", seasons=[-1]), P(kind="selected_seasons", seasons=[True]),
    P(kind="selected_seasons", seasons=[1.0]), P(kind="selected_seasons", seasons=list(range(101))),
    P(kind="selected_seasons", seasons=[1], episodes=[]),
    P(kind="episodes", episodes=[]), P(kind="episodes", episodes=[{"season": 1, "episode": 0}]),
    P(kind="episodes", episodes=[{"season": 1, "episode": 10000}]),
    P(kind="episodes", episodes=[{"season": 1, "episode": 1}] * 2),
    P(kind="episodes", episodes=[{"season": 1, "episode": 1, "x": 1}]),
    P(kind="episodes", episodes=[{"season": 1.5, "episode": 1}]),
    P(kind="episodes", episodes=[{"season": 0, "episode": n} for n in range(1, 502)]),
])
def test_normalize_rejects(env, bad):
    _, ri = env
    with pytest.raises(ri.IntentError) as e:
        ri.normalize_payload(bad)
    assert (e.value.code, e.value.status) == ("invalid_payload", 400)


def test_100_seasons_and_500_episodes_accepted(env):
    _, ri = env
    ri.normalize_payload(P(kind="selected_seasons", seasons=list(range(100))))
    ri.normalize_payload(P(kind="episodes", episodes=[{"season": 0, "episode": n} for n in range(1, 501)]))


@pytest.mark.parametrize("key", ["short", "x" * 129, "k" * 16 + "\n", "\n" + "k" * 16, "k" * 15 + "\u0661", "key with spaces!!", "kéy-" + "a" * 20, None, 5, ""])
def test_bad_idempotency_keys(env, key):
    db, ri = env
    uid = mk(db, "u")
    with pytest.raises(ri.IntentError) as e:
        ri.admit(uid, P(), key, MD)
    assert e.value.code == "invalid_idempotency_key"


# ── admission ────────────────────────────────────────────────────────────────

def test_pending_creates_no_outbox_and_receipt_shape(env):
    db, ri = env
    uid = mk(db, "u")
    r = ri.admit(uid, P(), K(1), MD)
    assert set(r) == {"request_id", "media", "scope", "request_status", "processing",
                      "availability", "outcome"}
    assert r["request_status"] == "pending" and r["processing"] == {"stage": "awaiting_approval"}
    assert r["outcome"] == "created"
    assert r["availability"] == {"state": "unknown", "entitlement": "unknown",
                                 "lookup": "unknown", "playability": "unknown"}
    assert rows(db, "request_outbox") == []
    legacy = db.get_user_request(r["request_id"])
    assert legacy["status"] == "pending" and legacy["user_id"] == uid


def test_auto_approve_and_admin_create_ready_job_atomically(env):
    db, ri = env
    a = mk(db, "auto", auto_approve=1)
    boss = mk(db, "boss", role="admin")
    r1 = ri.admit(a, P(1), K(1), MD)
    r2 = ri.admit(boss, P(2), K(2), MD)
    for r in (r1, r2):
        assert r["request_status"] == "approved" and r["processing"]["stage"] == "queued"
    assert len(rows(db, "request_outbox")) == 2


def test_native_enabled_user_required(env):
    db, ri = env
    off = mk(db, "off", enabled=0)
    for uid in (999, off, None, True):
        with pytest.raises(ri.IntentError) as e:
            ri.admit(uid, P(), K(1), MD)
        assert e.value.status == 403
    with pytest.raises(ri.IntentError):
        ri.replay_lookup(off, P(), K(1))
    assert rows(db, "user_requests") == []


def test_idempotent_replay_conflict_and_no_metadata_needed(env):
    db, ri = env
    uid = mk(db, "u")
    first = ri.admit(uid, P(), K(1), MD)
    assert ri.replay_lookup(uid, P(), K(1)) == first
    assert ri.admit(uid, P(), K(1), None) == first
    assert ri.replay_lookup(uid, P(), K(2)) is None
    with pytest.raises(ri.IntentError) as e:
        ri.admit(uid, P(tid=101), K(1), MD)
    assert (e.value.code, e.value.status) == ("idempotency_conflict", 409)
    with pytest.raises(ri.IntentError):
        ri.replay_lookup(uid, P(tid=101), K(1))
    assert len(rows(db, "user_requests")) == 1
    # same key, other user: independent namespace
    other = mk(db, "o")
    assert ri.admit(other, P(), K(1), MD)["request_id"] != first["request_id"]
    # equivalent but differently ordered body replays
    s1 = ri.admit(uid, P(kind="selected_seasons", seasons=[2, 1]), K(3), MD)
    assert ri.admit(uid, P(kind="selected_seasons", seasons=[1, 2]), K(3), None) == s1


def test_coalescing_same_owner_only_and_exact_scope(env):
    db, ri = env
    u, o = mk(db, "u"), mk(db, "o")
    a = ri.admit(u, P(), K(1), MD)
    b = ri.admit(u, P(), K(2), None)
    assert b["request_id"] == a["request_id"] and b["outcome"] == "existing_request"
    assert ri.admit(u, P(), K(2), None) == b  # second key replays independently
    c = ri.admit(o, P(), K(3), MD)
    assert c["request_id"] != a["request_id"] and c["outcome"] == "created"
    d = ri.admit(u, P(kind="selected_seasons", seasons=[1]), K(4), MD)
    assert d["request_id"] != a["request_id"]
    e = ri.admit(u, P(tid=101), K(5), MD)
    assert e["request_id"] not in (a["request_id"], d["request_id"])
    assert len(rows(db, "request_idempotency")) == 5


def test_denied_request_does_not_coalesce_and_cannot_revive(env):
    db, ri = env
    u = mk(db, "u")
    boss = mk(db, "boss", role="admin")
    a = ri.admit(u, P(), K(1), MD)
    ri.review(a["request_id"], boss, False, "no")
    with pytest.raises(ri.IntentError) as e:
        ri.review(a["request_id"], boss, True)
    assert e.value.code == "already_reviewed"
    again = ri.admit(u, P(), K(2), MD)
    assert again["request_id"] != a["request_id"]
    assert ri.get(u, a["request_id"])["request_status"] == "denied"
    assert rows(db, "request_outbox") == []


def test_missing_or_bad_metadata_creates_nothing(env):
    db, ri = env
    u = mk(db, "u")
    for md in (None, {}, {"title": "x", "imdb_id": "bad"}, {"title": "", "imdb_id": "tt1234567"}):
        with pytest.raises(ri.IntentError):
            ri.admit(u, P(), K(1), md)
    assert rows(db, "user_requests") == [] and rows(db, "request_idempotency") == []


def test_failure_mid_admission_rolls_everything_back(env):
    db, ri = env
    u = mk(db, "u", auto_approve=1)
    with db._connect() as c:
        c.execute("CREATE TRIGGER boom BEFORE INSERT ON request_outbox "
                  "BEGIN SELECT RAISE(ABORT,'boom'); END")
    with pytest.raises(sqlite3.DatabaseError):
        ri.admit(u, P(), K(1), MD)
    for t in ("user_requests", "request_intents", "request_idempotency", "request_outbox"):
        assert rows(db, t) == [], t
    with db._connect() as c:
        c.execute("DROP TRIGGER boom")
    assert ri.admit(u, P(), K(1), MD)["request_status"] == "approved"


# ── quota ────────────────────────────────────────────────────────────────────

def test_quota_rules(env):
    db, ri = env
    u = mk(db, "u", quota_monthly=2)
    unlimited = mk(db, "free", quota_monthly=0)
    boss = mk(db, "boss", role="admin", quota_monthly=1)
    ri.admit(u, P(1), K(1), MD)
    # replay and coalesce are free
    ri.admit(u, P(1), K(1), MD)
    ri.admit(u, P(1), K(9), MD)
    ri.admit(u, P(2), K(2), MD)
    with pytest.raises(ri.IntentError) as e:
        ri.admit(u, P(3), K(3), MD)
    assert (e.value.code, e.value.status) == ("quota_exceeded", 429)
    for i in range(5):
        ri.admit(unlimited, P(10 + i), K(20 + i), MD)
        ri.admit(boss, P(30 + i), K(40 + i), MD)
    # denial frees quota
    other_admin = boss
    first = ri.list_for_user(u)[-1]["request_id"]
    ri.review(first, other_admin, False)
    assert ri.admit(u, P(3), K(3), MD)["outcome"] == "created"


def test_legacy_rows_count_and_failed_still_charged(env):
    db, ri = env
    u = mk(db, "u", quota_monthly=2, auto_approve=1)
    db.create_user_request(u, "tt7654321", 9, "movie", "Legacy")
    r = ri.admit(u, P(1), K(1), MD)
    with pytest.raises(ri.IntentError):
        ri.admit(u, P(2), K(2), MD)
    j = ri.claim_next(now=1000.0)
    assert ri.fail(j["outbox_id"], j["lease_token"], "x", max_attempts=1, now=1001.0) == "failed"
    assert ri.get(u, r["request_id"])["request_status"] == "failed"
    with pytest.raises(ri.IntentError):
        ri.admit(u, P(2), K(2), MD)  # failed is still charged
    # failed request no longer coalesces
    assert db.get_user_request(r["request_id"])["status"] == "failed"


def test_quota_concurrency(env):
    db, ri = env
    u = mk(db, "u", quota_monthly=3)
    out = []

    def go(i):
        try:
            out.append(ri.admit(u, P(500 + i), K(i), MD)["outcome"])
        except ri.IntentError as e:
            out.append(e.code)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out.count("created") == 3 and out.count("quota_exceeded") == 7
    assert len(rows(db, "user_requests")) == 3


def test_concurrent_same_key_and_same_scope(env):
    db, ri = env
    u = mk(db, "u")
    res = []
    ts = [threading.Thread(target=lambda: res.append(ri.admit(u, P(), K(1), MD))) for _ in range(8)]
    ts += [threading.Thread(target=lambda i=i: res.append(ri.admit(u, P(), K(10 + i), MD)))
           for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len({r["request_id"] for r in res}) == 1 and len(res) == 16
    assert len(rows(db, "user_requests")) == 1 and len(rows(db, "request_intents")) == 1


# ── review ───────────────────────────────────────────────────────────────────

def test_review_requires_enabled_native_admin_and_known_intent(env):
    db, ri = env
    u = mk(db, "u")
    boss = mk(db, "boss", role="admin")
    off = mk(db, "off", role="admin", enabled=0)
    legacy = db.create_user_request(u, "tt7654321", 9, "movie", "Legacy")
    r = ri.admit(u, P(), K(1), MD)
    for who in (u, off, 999, None):
        with pytest.raises(ri.IntentError) as e:
            ri.review(r["request_id"], who, True)
        assert e.value.status == 403
    with pytest.raises(ri.IntentError) as e:
        ri.review(legacy, boss, True)
    assert e.value.status == 404
    with pytest.raises(ri.IntentError):
        ri.review(r["request_id"], boss, "yes")
    assert ri.get(u, r["request_id"])["request_status"] == "pending"
    out = ri.review(r["request_id"], boss, True)
    assert out["request_status"] == "approved" and len(rows(db, "request_outbox")) == 1
    assert db.get_user_request(r["request_id"])["reviewed_by"] == boss


def test_double_approval_race_yields_one_job(env):
    db, ri = env
    u = mk(db, "u")
    a1, a2 = mk(db, "a1", role="admin"), mk(db, "a2", role="admin")
    r = ri.admit(u, P(), K(1), MD)
    out = []

    def go(adm, approve):
        try:
            out.append(ri.review(r["request_id"], adm, approve)["request_status"])
        except ri.IntentError as e:
            out.append(e.code)

    ts = [threading.Thread(target=go, args=(a, ok)) for a, ok in
          [(a1, True), (a2, True), (a1, False), (a2, True)]]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out.count("already_reviewed") == 3
    assert len(rows(db, "request_outbox")) == (1 if "approved" in out else 0)


# ── ownership ────────────────────────────────────────────────────────────────

def test_owner_only_get_and_list(env):
    db, ri = env
    a, b = mk(db, "a"), mk(db, "b")
    ra = ri.admit(a, P(1), K(1), MD)
    ri.admit(a, P(2), K(2), MD)
    rb = ri.admit(b, P(3), K(3), MD)
    assert ri.get(a, ra["request_id"]) == ra
    assert ri.get(b, ra["request_id"]) is None
    assert ri.get(a, 99999) is None
    assert ri.get(a, rb["request_id"]) is None
    assert [r["media"]["tmdb_id"] for r in ri.list_for_user(a)] == [2, 1]
    assert [r["request_id"] for r in ri.list_for_user(b)] == [rb["request_id"]]
    assert len(ri.list_for_user(a, limit=1)) == 1
    older = ri.list_for_user(a, before_id=ri.list_for_user(a)[0]["request_id"])
    assert [r["media"]["tmdb_id"] for r in older] == [1]


# ── scope immutability and migration ─────────────────────────────────────────

def test_scope_is_immutable_and_future_cutoff_recorded(env):
    db, ri = env
    u = mk(db, "u")
    r = ri.admit(u, P(kind="selected_seasons", seasons=[2, 1]), K(1), MD)
    rid = r["request_id"]
    for col, val in (("scope_json", "{}"), ("scope_hash", "x"), ("scope_kind", "all_seasons"),
                     ("tmdb_id", 5), ("media_type", "movie"), ("title", "z"), ("imdb_id", "tt1"),
                     ("future_cutoff", "2000-01-01"), ("user_id", 999)):
        with db._connect() as c:
            with pytest.raises(sqlite3.DatabaseError):
                c.execute(f"UPDATE request_intents SET {col}=? WHERE request_id=?", (val, rid))
    assert ri.get(u, rid)["scope"] == {"kind": "selected_seasons", "seasons": [1, 2]}
    cut = rows(db, "request_intents")[0]["future_cutoff"]
    assert len(cut) == 10 and cut[4] == "-"
    assert db.get_user_request(rid)["seasons"] == "1,2"


def test_future_receipt_exposes_admission_cutoff_without_accepting_client_override(env):
    db, ri = env
    u = mk(db, "future-viewer")
    receipt = ri.admit(u, P(kind="future"), K(1), MD)
    cutoff = rows(db, "request_intents")[0]["future_cutoff"]
    assert receipt["scope"] == {"kind": "future", "starts_on": cutoff}
    assert receipt["media"]["title"] == MD["title"]
    assert ri.replay_lookup(u, P(kind="future"), K(1))["scope"] == receipt["scope"]
    bad = P(kind="future")
    bad["scope"]["starts_on"] = "2000-01-01"
    with pytest.raises(ri.IntentError):
        ri.admit(u, bad, K(2), MD)


def test_init_rerun_preserves_data(env):
    db, ri = env
    u = mk(db, "u", auto_approve=1)
    r = ri.admit(u, P(), K(1), MD)
    db.init()
    db.init()
    assert ri.get(u, r["request_id"]) == r
    assert ri.replay_lookup(u, P(), K(1)) == r
    assert len(rows(db, "request_outbox")) == 1


# ── outbox ───────────────────────────────────────────────────────────────────

def test_claim_lease_expiry_reclaim_and_stale_finish(env):
    db, ri = env
    u = mk(db, "u", auto_approve=1)
    r = ri.admit(u, P(kind="selected_seasons", seasons=[2]), K(1), MD)
    j1 = ri.claim_next(lease_seconds=30, now=1000.0)
    assert j1["request_id"] == r["request_id"] and j1["attempt"] == 1
    assert j1["scope"] == {"kind": "selected_seasons", "seasons": [2]}
    assert j1["imdb_id"] == "tt1234567" and j1["media"] == {"type": "tv", "tmdb_id": 100}
    assert ri.get(u, r["request_id"])["processing"]["stage"] == "dispatching"
    assert ri.claim_next(now=1010.0) is None  # still leased
    assert ri.renew(j1["outbox_id"], j1["lease_token"], 30, now=1020.0)
    assert ri.claim_next(now=1040.0) is None  # renewed to 1050
    assert not ri.renew(j1["outbox_id"], "wrong", 30, now=1041.0)
    j2 = ri.claim_next(now=1051.0)  # expired: another worker (or restart) reclaims
    assert j2["outbox_id"] == j1["outbox_id"] and j2["attempt"] == 2
    assert j2["lease_token"] != j1["lease_token"]
    assert not ri.renew(j1["outbox_id"], j1["lease_token"], 30, now=1052.0)
    assert ri.finish(j1["outbox_id"], j1["lease_token"], now=1052.0) is False
    assert ri.fail(j1["outbox_id"], j1["lease_token"], now=1052.0) is None
    assert ri.get(u, r["request_id"])["processing"]["stage"] == "dispatching"
    assert ri.finish(j2["outbox_id"], j2["lease_token"], now=1053.0) is True
    assert ri.finish(j2["outbox_id"], j2["lease_token"], now=1054.0) is False
    assert ri.get(u, r["request_id"])["processing"]["stage"] == "dispatched"
    assert ri.claim_next(now=9999.0) is None


def test_expired_renew_refused_and_fail_backoff_then_give_up(env):
    db, ri = env
    u = mk(db, "u", auto_approve=1)
    r = ri.admit(u, P(), K(1), MD)
    j = ri.claim_next(lease_seconds=10, now=100.0)
    assert not ri.renew(j["outbox_id"], j["lease_token"], 10, now=111.0)
    assert ri.fail(j["outbox_id"], j["lease_token"], "e" * 500, retry_after=60, max_attempts=2,
                   now=105.0) == "retry"
    assert ri.claim_next(now=150.0) is None
    j2 = ri.claim_next(now=173.0)
    assert j2["attempt"] == 2
    assert ri.fail(j2["outbox_id"], j2["lease_token"], "again", max_attempts=2, now=180.0) == "failed"
    assert len(rows(db, "request_outbox")[0]["last_error"]) <= 200
    assert ri.get(u, r["request_id"])["request_status"] == "failed"
    assert ri.claim_next(now=10**6) is None


def test_disabled_owner_jobs_do_not_dispatch_until_enabled(env):
    db, ri = env
    u = mk(db, "u", auto_approve=1)
    ri.admit(u, P(), K(1), MD)
    db.update_user(u, enabled=0)
    assert ri.claim_next(now=1.0) is None
    db.update_user(u, enabled=1)
    assert ri.claim_next(now=2.0) is not None


def test_claim_order_and_concurrent_claims_are_exclusive(env):
    db, ri = env
    u = mk(db, "u", auto_approve=1)
    ids = [ri.admit(u, P(100 + i), K(i), MD)["request_id"] for i in range(6)]
    got = []
    ts = [threading.Thread(target=lambda: got.append(ri.claim_next(lease_seconds=60, now=5.0)))
          for _ in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    claimed = [g["request_id"] for g in got if g]
    assert sorted(claimed) == ids and len(set(claimed)) == 6


# ── follow-up fixes ──────────────────────────────────────────────────────────

def test_raw_hash_is_exact_bytes_or_null(env):
    import hashlib
    db, ri = env
    u = mk(db, "u")
    body = b'{"media":{"type":"tv","tmdb_id":100}, "scope":{"kind":"all_seasons"}}'
    r1 = ri.admit(u, P(), K(1), MD, raw_body=body)
    r2 = ri.admit(u, P(tid=2), K(2), MD)
    got = {r["idem_key"]: r["raw_hash"] for r in rows(db, "request_idempotency")}
    assert got[K(1)] == hashlib.sha256(body).hexdigest() and got[K(2)] is None
    # replay with other bytes but same meaning returns the original, hash untouched
    assert ri.admit(u, P(), K(1), None, raw_body=b"{ }") == r1
    assert {r["idem_key"]: r["raw_hash"] for r in rows(db, "request_idempotency")}[K(1)] \
        == hashlib.sha256(body).hexdigest()
    with pytest.raises(ri.IntentError):
        ri.admit(u, P(tid=3), K(3), MD, raw_body="text")
    assert r2["request_id"]


@pytest.mark.parametrize("imdb", ["tt1234567\n", "tt12345", "tt12345678901", "TT1234567", "tt١٢٣٤٥٦٧"])
def test_imdb_fullmatch_rejects(env, imdb):
    db, ri = env
    u = mk(db, "u")
    with pytest.raises(ri.IntentError):
        ri.admit(u, P(), K(1), {"title": "t", "imdb_id": imdb})
    for ok in ("tt123456", "tt1234567890"):
        ri.admit(u, P(int(ok[2:7])), K(int(ok[2:7])), {"title": "t", "imdb_id": ok})


def test_owner_reads_require_enabled_native_user(env):
    db, ri = env
    u = mk(db, "u")
    r = ri.admit(u, P(), K(1), MD)
    db.update_user(u, enabled=0)
    for call in (lambda: ri.get(u, r["request_id"]), lambda: ri.list_for_user(u),
                 lambda: ri.get(999, 1)):
        with pytest.raises(ri.IntentError) as e:
            call()
        assert e.value.status == 403
    db.update_user(u, enabled=1)
    assert ri.get(u, r["request_id"]) == r


@pytest.mark.parametrize("rid", [True, False, 0, -1, 1.0, "1", None])
def test_request_ids_strict_positive_int(env, rid):
    db, ri = env
    u = mk(db, "u")
    boss = mk(db, "boss", role="admin")
    ri.admit(u, P(), K(1), MD)  # request id 1 exists
    with pytest.raises(ri.IntentError) as e:
        ri.get(u, rid)
    assert e.value.status == 400
    with pytest.raises(ri.IntentError) as e:
        ri.review(rid, boss, True)
    assert e.value.status == 400
    if rid is not None:  # None means "first page"
        with pytest.raises(ri.IntentError):
            ri.list_for_user(u, before_id=rid)
    assert ri.get(u, 1) is not None


def claimed(db, ri, **f):
    u = mk(db, "u", auto_approve=1, **f)
    r = ri.admit(u, P(), K(1), MD)
    return u, r, ri.claim_next(lease_seconds=10, now=100.0)


@pytest.mark.parametrize("op", ["finish", "fail", "defer", "renew"])
def test_expired_but_unreclaimed_lease_refused_without_side_effects(env, op):
    db, ri = env
    u, r, j = claimed(db, ri)
    args = (j["outbox_id"], j["lease_token"])
    before = [dict(x) for x in rows(db, "request_outbox")]
    now = 110.0  # lease expires exactly at 110
    res = {"finish": lambda: ri.finish(*args, now=now), "fail": lambda: ri.fail(*args, now=now),
           "defer": lambda: ri.defer(*args, 5, "rate_limited", now=now),
           "renew": lambda: ri.renew(*args, 10, now=now)}[op]()
    assert res in (False, None)
    assert [dict(x) for x in rows(db, "request_outbox")] == before
    assert ri.get(u, r["request_id"])["processing"]["stage"] == "dispatching"
    # the same token still works while unexpired
    assert ri.finish(*args, now=109.0) is True


@pytest.mark.parametrize("op", ["finish", "fail", "defer", "renew"])
@pytest.mark.parametrize("change", ["disable", "deny", "fail_status"])
def test_owner_disabled_or_status_changed_blocks_lease_ops(env, op, change):
    db, ri = env
    u, r, j = claimed(db, ri)
    if change == "disable":
        db.update_user(u, enabled=0)
    else:
        db.update_user_request_status(r["request_id"], "denied" if change == "deny" else "failed")
    args = (j["outbox_id"], j["lease_token"])
    before = [dict(x) for x in rows(db, "request_outbox")]
    intent_before = [dict(x) for x in rows(db, "request_intents")]
    res = {"finish": lambda: ri.finish(*args, now=101.0), "fail": lambda: ri.fail(*args, now=101.0),
           "defer": lambda: ri.defer(*args, 5, "waiting_release", now=101.0),
           "renew": lambda: ri.renew(*args, 10, now=101.0)}[op]()
    assert res in (False, None)
    assert [dict(x) for x in rows(db, "request_outbox")] == before
    assert [dict(x) for x in rows(db, "request_intents")] == intent_before
    if change == "disable":
        db.update_user(u, enabled=1)
        j2 = ri.claim_next(now=111.0)  # lease expired, owner back: recovers
        assert j2 and j2["attempt"] == 2


def test_finish_stage_allowlist_and_dispatch_not_media_completed(env):
    db, ri = env
    u, r, j = claimed(db, ri)
    for bad in ("completed", "available", "", None, "failed"):
        with pytest.raises(ri.IntentError):
            ri.finish(j["outbox_id"], j["lease_token"], bad, now=101.0)
    assert ri.finish(j["outbox_id"], j["lease_token"], "registered", now=101.0)
    out = ri.get(u, r["request_id"])
    assert out["processing"]["stage"] == "registered"
    assert out["availability"]["state"] == "unknown"


def test_default_finish_stage_is_dispatched_and_pending_override(env):
    db, ri = env
    u, r, j = claimed(db, ri)
    assert ri.finish(j["outbox_id"], j["lease_token"], now=101.0)
    assert ri.get(u, r["request_id"])["processing"]["stage"] == "dispatched"
    # authoritative status overrides a stale persisted stage
    p = ri.admit(u, P(tid=7), K(7), MD)
    db.update_user_request_status(p["request_id"], "pending")
    with db._connect() as c:
        c.execute("UPDATE request_intents SET processing_state='dispatched' WHERE request_id=?",
                  (p["request_id"],))
    assert ri.get(u, p["request_id"])["processing"]["stage"] == "awaiting_approval"


def test_defer_does_not_consume_attempts_and_persists_stage(env):
    db, ri = env
    u, r, j = claimed(db, ri)
    assert j["attempt"] == 1
    assert ri.defer(j["outbox_id"], j["lease_token"], 30, "rate_limited", now=101.0)
    assert ri.defer(j["outbox_id"], j["lease_token"], 30, "rate_limited", now=101.0) is False
    assert rows(db, "request_outbox")[0]["attempts"] == 0
    assert ri.get(u, r["request_id"])["processing"]["stage"] == "rate_limited"
    assert ri.claim_next(now=120.0) is None
    for n, t in enumerate((131.0, 200.0, 300.0), 1):
        j2 = ri.claim_next(lease_seconds=10, now=t)
        assert j2["attempt"] == 1  # budget never grows from deferrals
        assert ri.defer(j2["outbox_id"], j2["lease_token"], 5, "waiting_release", now=t + 1)
    assert ri.get(u, r["request_id"])["processing"]["stage"] == "waiting_release"
    j3 = ri.claim_next(lease_seconds=10, now=400.0)
    assert ri.fail(j3["outbox_id"], j3["lease_token"], "x", max_attempts=1, now=401.0) == "failed"
    with pytest.raises(ri.IntentError):
        ri.defer(1, "tok", 5, "completed", now=1.0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, 0, 3601, True, "5", None])
def test_lease_seconds_bounds(env, bad):
    db, ri = env
    claimed_u = mk(db, "u", auto_approve=1)
    ri.admit(claimed_u, P(), K(1), MD)
    with pytest.raises(ri.IntentError):
        ri.claim_next(lease_seconds=bad, now=1.0)
    j = ri.claim_next(lease_seconds=10, now=1.0)
    with pytest.raises(ri.IntentError):
        ri.renew(j["outbox_id"], j["lease_token"], bad, now=2.0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, 604801, True, "5"])
def test_retry_after_bounds_and_max_attempts(env, bad):
    db, ri = env
    u, r, j = claimed(db, ri)
    with pytest.raises(ri.IntentError):
        ri.fail(j["outbox_id"], j["lease_token"], retry_after=bad, now=101.0)
    with pytest.raises(ri.IntentError):
        ri.defer(j["outbox_id"], j["lease_token"], bad, "rate_limited", now=101.0)
    for m in (0, 101, True, 2.0):
        with pytest.raises(ri.IntentError):
            ri.fail(j["outbox_id"], j["lease_token"], max_attempts=m, now=101.0)
    with pytest.raises(ri.IntentError):
        ri.finish(True, j["lease_token"], now=101.0)
    with pytest.raises(ri.IntentError):
        ri.claim_next(now=float("nan"))
