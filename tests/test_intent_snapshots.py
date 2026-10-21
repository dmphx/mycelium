"""Admission snapshots on real temporary SQLite. Synthetic data only."""
import copy
import sys
import threading

import pytest


@pytest.fixture()
def env(monkeypatch, tmp_path):
    for m in ("db", "settings", "config", "request_intents", "intent_snapshots"):
        sys.modules.pop(m, None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    import config
    config.DB_PATH = str(tmp_path / "t.db")
    import db
    db.init()
    import request_intents as ri
    import intent_snapshots as sn
    return db, ri, sn


CUT = "2020-06-15"
MD = {"title": "Show", "imdb_id": "tt1234567"}


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


def epi(eid, s, n, air="2019-01-01", name=None, rt=40):
    state = "unknown" if air is None else ("aired" if air <= CUT else "unaired")
    return {"tmdb_episode_id": eid, "season": s, "episode": n, "name": name or f"Ep {n}",
            "air_date": air, "runtime_min": rt, "air_state": state}


def TV(kind="all_seasons", tid=100, **scope):
    return {"media": {"type": "tv", "tmdb_id": tid}, "scope": dict(kind=kind, **scope)}


def RES(payload, episodes, cutoff=CUT, **over):
    norm = {"media": payload["media"], "scope": payload["scope"]}
    if norm["scope"].get("kind") == "selected_seasons":
        norm["scope"] = {"kind": "selected_seasons", "seasons": sorted(norm["scope"]["seasons"])}
    out = {"media": norm["media"], "scope": norm["scope"], "title": "Show", "imdb_id": "tt1234567",
           "year": 2019, "cutoff": cutoff, "episodes": episodes}
    out.update(over)
    return out


def standard():
    p = TV()
    return p, RES(p, [epi(2, 1, 2), epi(1, 1, 1), epi(3, 2, 1, air=None), epi(4, 2, 2, air="2030-01-01")])


# ── pure build ───────────────────────────────────────────────────────────────

def test_build_predicates_hashes_and_initial_states(env):
    db, ri, sn = env
    p, res = standard()
    norm = ri.normalize_payload(p)
    ih = ri.semantic_hash(norm)
    b = sn.build(res, norm, ih)
    assert [(e["season"], e["episode"], e["state"]) for e in b["episodes"]] == [
        (1, 1, "pending"), (1, 2, "pending"), (2, 1, "waiting_airdate"), (2, 2, "unaired")]
    assert b["predicate"] == {"input_scope": {"kind": "all_seasons"}, "numbering": "tmdb",
                              "monitoring": "all_regular_seasons", "specials": "explicit_only"}
    assert b["input_hash"] == ih and len({b["snapshot_hash"], b["predicate_hash"], b["accepted_hash"]}) == 3
    # same input again is stable; changing one observed field changes snapshot and accepted only
    again = sn.build(copy.deepcopy(res), norm, ih)
    assert again["accepted_hash"] == b["accepted_hash"]
    res2 = copy.deepcopy(res)
    res2["episodes"][0]["name"] = "Other"
    c = sn.build(res2, norm, ih)
    assert c["predicate_hash"] == b["predicate_hash"] and c["accepted_hash"] != b["accepted_hash"]


def test_predicate_per_scope_kind(env):
    db, ri, sn = env
    cases = [
        (TV("episodes", episodes=[{"season": 1, "episode": 1}]), "none", False),
        (TV("selected_seasons", seasons=[2, 1]), "selected_seasons", False),
        (TV("future"), "all_regular_seasons", True),
    ]
    for p, mon, starts in cases:
        norm = ri.normalize_payload(p)
        res = RES(p, [epi(1, 1, 1, air=CUT)] if p["scope"]["kind"] != "future" else [epi(1, 1, 1, air=CUT)])
        pred = sn.predicate_for(norm, CUT)
        assert pred["monitoring"] == mon and pred["numbering"] == "tmdb" and pred["specials"] == "explicit_only"
        assert ("starts_on" in pred) == starts and (not starts or pred["starts_on"] == CUT)
    mv = {"media": {"type": "movie", "tmdb_id": 5}, "scope": {"kind": "movie"}}
    assert sn.predicate_for(ri.normalize_payload(mv), CUT) == {"kind": "movie"}
    out = sn.build(RES(mv, []), ri.normalize_payload(mv), "h")
    assert out["episodes"] == [] and out["predicate"] == {"kind": "movie"}


def bad_cases():
    p, res = standard()
    def m(fn):
        r = copy.deepcopy(res)
        fn(r)
        return (p, r)
    ep0 = lambda r: r["episodes"][0]
    yield "wrong media", m(lambda r: r["media"].update(tmdb_id=101))
    yield "wrong scope", m(lambda r: r.update(scope={"kind": "future"}))
    yield "extra key", m(lambda r: r.update(extra=1))
    yield "missing key", m(lambda r: r.pop("year"))
    yield "bad title", m(lambda r: r.update(title="a\x00b"))
    yield "blank title", m(lambda r: r.update(title=" "))
    yield "bad imdb", m(lambda r: r.update(imdb_id="tt12"))
    yield "imdb newline", m(lambda r: r.update(imdb_id="tt1234567\n"))
    yield "bad cutoff", m(lambda r: r.update(cutoff="2020-02-30"))
    yield "year bool", m(lambda r: r.update(year=True))
    yield "dup pair", m(lambda r: r["episodes"].append(epi(99, 1, 1)))
    yield "dup id", m(lambda r: r["episodes"].append(epi(1, 3, 3)))
    yield "episode extra key", m(lambda r: ep0(r).update(x=1))
    yield "id bool", m(lambda r: ep0(r).update(tmdb_episode_id=True))
    yield "season str", m(lambda r: ep0(r).update(season="1"))
    yield "air_state lie", m(lambda r: ep0(r).update(air_state="unaired"))
    yield "bad air_date", m(lambda r: ep0(r).update(air_date="2020-13-01"))
    yield "runtime float", m(lambda r: ep0(r).update(runtime_min=4.5))
    yield "name control", m(lambda r: ep0(r).update(name="a\nb"))
    yield "special in all", m(lambda r: r["episodes"].append(epi(77, 0, 1)))
    yield "episodes not list", m(lambda r: r.update(episodes={}))
    yield "not a dict", (p, "x")
    sel = TV("selected_seasons", seasons=[1])
    yield "outside selected", (sel, RES(sel, [epi(1, 2, 1)]))
    eps = TV("episodes", episodes=[{"season": 1, "episode": 1}, {"season": 1, "episode": 2}])
    yield "episodes subset", (eps, RES(eps, [epi(1, 1, 1)]))
    yield "episodes extra", (eps, RES(eps, [epi(1, 1, 1), epi(2, 1, 2), epi(3, 1, 3)]))
    fut = TV("future")
    yield "future backcatalog", (fut, RES(fut, [epi(1, 1, 1, air="2020-06-14")]))
    mv = {"media": {"type": "movie", "tmdb_id": 5}, "scope": {"kind": "movie"}}
    yield "movie episodes", (mv, RES(mv, [epi(1, 1, 1)]))


@pytest.mark.parametrize("name,case", list(bad_cases()), ids=[n for n, _ in bad_cases()])
def test_build_rejects(env, name, case):
    db, ri, sn = env
    p, res = case
    with pytest.raises(sn.SnapshotError) as e:
        sn.build(res, ri.normalize_payload(p), "h")
    assert (e.value.code, e.value.status) == ("invalid_metadata", 400)


def test_oversize_is_503_and_5000_ok(env):
    db, ri, sn = env
    p = TV()
    big = [epi(i, 1 + i // 4000, 1 + i % 4000) for i in range(1, 5001)]
    assert len(sn.build(RES(p, big), ri.normalize_payload(p), "h")["episodes"]) == 5000
    with pytest.raises(sn.SnapshotError) as e:
        sn.build(RES(p, big + [epi(9999, 9, 9)]), ri.normalize_payload(p), "h")
    assert (e.value.code, e.value.status) == ("catalog_too_large", 503)


def test_future_same_day_included_and_unknown_waits(env):
    db, ri, sn = env
    p = TV("future")
    b = sn.build(RES(p, [epi(1, 1, 1, air=CUT), epi(2, 1, 2, air=None), epi(3, 1, 3, air="2021-01-01")]),
                 ri.normalize_payload(p), "h")
    assert [e["state"] for e in b["episodes"]] == ["pending", "waiting_airdate", "unaired"]


# ── admission ────────────────────────────────────────────────────────────────

def test_admit_resolved_stores_everything_and_receipt_shape_unchanged(env):
    db, ri, sn = env
    u = mk(db, "u", auto_approve=1)
    p, res = standard()
    r = ri.admit(u, p, K(1), None, resolved=res)
    legacy = ri.admit(u, TV(tid=7), K(2), MD)
    assert set(r) == set(legacy) == {"request_id", "media", "scope", "request_status", "processing",
                                     "availability", "outcome"}
    assert r["request_status"] == "approved" and len(rows(db, "request_outbox")) == 2
    row = rows(db, "request_intents")[0]
    assert row["future_cutoff"] == CUT and row["title"] == "Show" and row["imdb_id"] == "tt1234567"
    s = ri.get_snapshot(u, r["request_id"])
    assert s["cutoff"] == CUT and s["catalog_count"] == 4
    assert set(s["hashes"]) == {"snapshot", "predicate", "accepted", "input"}
    assert s["hashes"]["input"] == ri.semantic_hash(ri.normalize_payload(p))
    assert [(e["season"], e["episode"], e["state"]) for e in s["episodes"]][2] == (2, 1, "waiting_airdate")
    assert len(rows(db, "request_episode_observation")) == 4
    assert ri.get_snapshot(u, legacy["request_id"]) is None  # legacy admission has none
    assert len(rows(db, "request_metadata_snapshot")) == 1


def test_pending_creates_snapshot_but_no_outbox(env):
    db, ri, sn = env
    u = mk(db, "u")
    p, res = standard()
    r = ri.admit(u, p, K(1), None, resolved=res)
    assert r["request_status"] == "pending" and rows(db, "request_outbox") == []
    assert len(rows(db, "request_episode_state")) == 4


def test_metadata_and_resolved_must_agree(env):
    db, ri, sn = env
    u = mk(db, "u")
    p, res = standard()
    for md in ({"title": "Other", "imdb_id": "tt1234567"}, {"title": "Show", "imdb_id": "tt7654321"}, {}):
        with pytest.raises(ri.IntentError) as e:
            ri.admit(u, p, K(1), md, resolved=res)
        assert e.value.status == 400
    assert rows(db, "user_requests") == []
    assert ri.admit(u, p, K(1), dict(MD), resolved=res)["outcome"] == "created"


def test_invalid_or_oversize_resolved_leaves_no_data_and_no_quota_use(env):
    db, ri, sn = env
    u = mk(db, "u", quota_monthly=1)
    p, res = standard()
    bad = copy.deepcopy(res)
    bad["media"]["tmdb_id"] = 5
    with pytest.raises(ri.IntentError) as e:
        ri.admit(u, p, K(1), None, resolved=bad)
    assert (e.value.code, e.value.status) == ("invalid_metadata", 400)
    huge = RES(p, [epi(i, 1 + i // 4000, 1 + i % 4000) for i in range(1, 5002)])
    with pytest.raises(ri.IntentError) as e:
        ri.admit(u, p, K(2), None, resolved=huge)
    assert (e.value.code, e.value.status) == ("catalog_too_large", 503)
    for t in ("user_requests", "request_intents", "request_idempotency", "request_outbox",
              "request_metadata_snapshot", "request_episode_state", "request_episode_observation"):
        assert rows(db, t) == [], t
    assert ri.admit(u, p, K(3), None, resolved=res)["outcome"] == "created"  # quota intact


@pytest.mark.parametrize("table", ["request_metadata_snapshot", "request_episode_state",
                                   "request_episode_observation", "request_outbox",
                                   "request_idempotency"])
def test_injected_failure_rolls_back_everything(env, table):
    db, ri, sn = env
    u = mk(db, "u", auto_approve=1, quota_monthly=1)
    p, res = standard()
    with db._connect() as c:
        c.execute(f"CREATE TRIGGER boom BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'boom'); END")
    with pytest.raises(Exception):
        ri.admit(u, p, K(1), None, resolved=res)
    for t in ("user_requests", "request_intents", "request_idempotency", "request_outbox",
              "request_metadata_snapshot", "request_episode_state", "request_episode_observation"):
        assert rows(db, t) == [], t
    with db._connect() as c:
        c.execute("DROP TRIGGER boom")
    assert ri.admit(u, p, K(1), None, resolved=res)["outcome"] == "created"  # key and quota free


def test_replay_and_coalesce_need_no_resolved_and_ignore_a_bad_one(env):
    db, ri, sn = env
    u = mk(db, "u")
    p, res = standard()
    first = ri.admit(u, p, K(1), None, resolved=res)
    assert ri.admit(u, p, K(1), None) == first
    assert ri.admit(u, p, K(1), None, resolved={"junk": 1}) == first
    co = ri.admit(u, p, K(2), None)
    assert co["request_id"] == first["request_id"] and co["outcome"] == "existing_request"
    assert len(rows(db, "request_metadata_snapshot")) == 1


def test_admit_resolved_requires_resolved_and_find_active(env):
    db, ri, sn = env
    u, o = mk(db, "u"), mk(db, "o")
    p, res = standard()
    with pytest.raises(ri.IntentError):
        ri.admit_resolved(u, p, K(1), None)
    with pytest.raises(ri.IntentError):
        ri.admit_resolved(u, p, K(1), "x")
    assert ri.find_active(u, p) is None
    r = ri.admit_resolved(u, p, K(1), res)
    f = ri.find_active(u, p)
    assert f["request_id"] == r["request_id"] and f["outcome"] == "existing_request"
    assert ri.find_active(o, p) is None and ri.find_active(u, TV(tid=9)) is None
    assert len(rows(db, "user_requests")) == 1
    db.update_user(u, enabled=0)
    with pytest.raises(ri.IntentError) as e:
        ri.find_active(u, p)
    assert e.value.status == 403


def test_concurrent_admissions_store_one_snapshot(env):
    db, ri, sn = env
    u = mk(db, "u")
    p, res = standard()
    out = []
    ts = [threading.Thread(target=lambda i=i: out.append(ri.admit(u, p, K(i), None, resolved=res)))
          for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len({r["request_id"] for r in out}) == 1 and len(out) == 12
    assert len(rows(db, "request_metadata_snapshot")) == 1
    assert len(rows(db, "request_episode_state")) == 4 and len(rows(db, "user_requests")) == 1


# ── immutability, cutoff, reads ──────────────────────────────────────────────

def test_snapshot_rows_are_immutable_but_progress_state_moves(env):
    db, ri, sn = env
    u = mk(db, "u")
    p, res = standard()
    rid = ri.admit(u, p, K(1), None, resolved=res)["request_id"]
    import sqlite3
    with db._connect() as c:
        for sql in ("UPDATE request_metadata_snapshot SET snapshot_json='{}'",
                    "UPDATE request_metadata_snapshot SET cutoff='2000-01-01'",
                    "DELETE FROM request_metadata_snapshot",
                    "UPDATE request_episode_state SET tmdb_episode_id=999 WHERE season=1 AND episode=1",
                    "UPDATE request_episode_state SET name='x'", "UPDATE request_episode_state SET air_date='2020-01-01'",
                    "UPDATE request_episode_state SET runtime_min=1", "UPDATE request_episode_state SET season=9",
                    "DELETE FROM request_episode_state",
                    "UPDATE request_episode_observation SET name='x'", "DELETE FROM request_episode_observation",
                    "UPDATE request_intents SET future_cutoff='2000-01-01'"):
            with pytest.raises(sqlite3.DatabaseError):
                c.execute(sql)
        c.execute("UPDATE request_episode_state SET state='registered' WHERE season=1 AND episode=1")
    s = ri.get_snapshot(u, rid)
    assert s["episodes"][0]["state"] == "registered" and s["cutoff"] == CUT
    assert s["snapshot"]["episodes"][0]["name"] == "Ep 1"


def test_user_delete_cascades_through_guarded_children(env):
    db, ri, sn = env
    u, other = mk(db, "u"), mk(db, "o")
    p, res = standard()
    ri.admit(u, p, K(1), None, resolved=res)
    keep = ri.admit(other, p, K(2), None, resolved=res)["request_id"]
    db.delete_user(u)
    for t in ("request_metadata_snapshot",):
        assert [r["request_id"] for r in rows(db, t)] == [keep]
    assert len(rows(db, "request_episode_state")) == 4 and len(rows(db, "request_episode_observation")) == 4
    assert ri.get_snapshot(other, keep)["catalog_count"] == 4


def test_cutoff_stays_the_resolved_day_not_today(env):
    db, ri, sn = env
    u = mk(db, "u")
    p = TV("future")
    res = RES(p, [epi(1, 1, 1, air="2020-06-15"), epi(2, 1, 2, air=None)], cutoff="2020-06-15")
    rid = ri.admit(u, p, K(1), None, resolved=res)["request_id"]
    assert rows(db, "request_intents")[0]["future_cutoff"] == "2020-06-15"
    ri_snap = ri.get_snapshot(u, rid)
    assert ri_snap["predicate"]["starts_on"] == "2020-06-15"
    assert [e["state"] for e in ri_snap["episodes"]] == ["pending", "waiting_airdate"]
    assert rows(db, "request_outbox") == []  # pending owner: no side effect of any kind


def test_snapshot_reads_are_owner_and_enabled_only(env):
    db, ri, sn = env
    u, o = mk(db, "u"), mk(db, "o")
    p, res = standard()
    rid = ri.admit(u, p, K(1), None, resolved=res)["request_id"]
    assert ri.get_snapshot(o, rid) is None and ri.get_snapshot(u, 9999) is None
    for bad in (True, 0, -1, "1", None):
        with pytest.raises(ri.IntentError):
            ri.get_snapshot(u, bad)
    db.update_user(u, enabled=0)
    with pytest.raises(ri.IntentError) as e:
        ri.get_snapshot(u, rid)
    assert e.value.status == 403
    with pytest.raises(ri.IntentError):
        ri.get_snapshot(999, rid)


def test_init_rerun_keeps_snapshots_and_triggers(env):
    db, ri, sn = env
    u = mk(db, "u")
    p, res = standard()
    rid = ri.admit(u, p, K(1), None, resolved=res)["request_id"]
    before = ri.get_snapshot(u, rid)
    db.init()
    db.init()
    assert ri.get_snapshot(u, rid) == before


def test_real_resolver_output_is_accepted(env):
    db, ri, sn = env
    import intent_metadata as im
    data = {"/tv/100": {"id": 100, "name": "Show", "first_air_date": "2019-01-01",
                        "seasons": [{"season_number": 1, "episode_count": 2},
                                    {"season_number": 0, "episode_count": 1}]},
            "/tv/100/external_ids": {"id": 100, "imdb_id": "tt1234567"},
            "/tv/100/season/1": {"id": 5, "season_number": 1, "episodes": [
                {"id": 1, "season_number": 1, "episode_number": 1, "name": "A", "air_date": "2019-01-01", "runtime": 40},
                {"id": 2, "season_number": 1, "episode_number": 2, "name": "B", "air_date": None, "runtime": None}]}}
    res = im.resolve(TV(), today="2024-01-01", fetch=data.get)
    u = mk(db, "u")
    rid = ri.admit_resolved(u, TV(), K(1), res)["request_id"]
    assert ri.get_snapshot(u, rid)["cutoff"] == "2024-01-01"


@pytest.mark.parametrize("bad_id", [True, 1.0, "1"])
def test_resolved_media_identity_cannot_use_python_equality_aliases(env, bad_id):
    db, ri, sn = env
    payload = TV(tid=1)
    resolved = RES(payload, [epi(1, 1, 1)])
    resolved["media"] = {"type": "tv", "tmdb_id": bad_id}
    with pytest.raises(sn.SnapshotError):
        sn.build(resolved, ri.normalize_payload(payload), "h")


@pytest.mark.parametrize("bad_season", [True, 1.0, "1"])
def test_resolved_scope_identity_cannot_use_python_equality_aliases(env, bad_season):
    db, ri, sn = env
    payload = TV("selected_seasons", seasons=[1])
    resolved = RES(payload, [epi(1, 1, 1)])
    resolved["scope"] = {"kind": "selected_seasons", "seasons": [bad_season]}
    with pytest.raises(sn.SnapshotError):
        sn.build(resolved, ri.normalize_payload(payload), "h")


def test_episode_id_bounds_match_the_raw_resolver(env):
    db, ri, sn = env
    payload = TV()
    with pytest.raises(sn.SnapshotError):
        sn.build(RES(payload, [epi(2**31, 1, 1)]), ri.normalize_payload(payload), "h")
