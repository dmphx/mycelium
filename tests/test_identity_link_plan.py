"""Race-safe link helper and the offline linking tool, on real temporary SQLite files."""
import json
import os
import sys
import threading

import pytest

ISS = "https://idp.example/realm"
JSRV = "a" * 32
JU = ["b" * 32, "c" * 32]


@pytest.fixture()
def env(monkeypatch, tmp_path):
    for m in ("db", "settings", "config"):
        sys.modules.pop(m, None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    import config
    config.DB_PATH = str(tmp_path / "t.db")
    import db
    db.init()
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    sys.modules.pop("link_oidc_identity", None)
    import link_oidc_identity as tool
    users = []
    for i in range(2):
        uid = db.create_user(f"u{i}", "scrypt$x", role="user")
        db.update_user(uid, quota_monthly=i + 1, auto_approve=i)
        db.link_external_identity(uid, "jellyfin", JSRV, JU[i])
        users.append(uid)
    return db, tool, users, str(tmp_path / "t.db"), tmp_path


def manifest(users, subs=("s0", "s1")):
    links = []
    for i, uid in enumerate(users):
        links.append({
            "native_user_id": uid,
            "jellyfin": {"server_guid": JSRV.upper(), "user_guid": JU[i]},
            "oidc": {"issuer": ISS, "subject": subs[i]},
            "expected": {"role": "user", "enabled": 1, "quota_monthly": i + 1, "auto_approve": i},
            "existing_links": [{"provider": "jellyfin", "server_id": JSRV, "subject": JU[i]}],
        })
    return {"version": 1, "links": links}


def oidc_rows(path):
    import sqlite3
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT user_id, subject FROM external_identities WHERE provider='oidc' ORDER BY user_id").fetchall()
    finally:
        c.close()


def test_link_helper_idempotent_and_refuses_conflicts(env):
    db, _, users, _, _ = env
    assert db.link_external_identity(users[0], "oidc", ISS, "s0")
    assert db.link_external_identity(users[0], "oidc", ISS, "s0")
    with pytest.raises(ValueError):
        db.link_external_identity(users[1], "oidc", ISS, "s0")
    with pytest.raises(ValueError):
        db.link_external_identity(users[0], "oidc", ISS, "other")
    with pytest.raises(ValueError):
        db.link_external_identity(9999, "oidc", ISS, "z")


def test_concurrent_relink_and_concurrent_conflict(env):
    db, _, users, _, _ = env
    results = []

    def go(uid, sub):
        try:
            results.append(db.link_external_identity(uid, "oidc", ISS, sub))
        except ValueError as exc:
            results.append(str(exc))

    ts = [threading.Thread(target=go, args=(users[0], "same")) for _ in range(6)]
    ts += [threading.Thread(target=go, args=(users[1], "same")) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(results) == 12
    assert db.get_user_by_external_identity("oidc", ISS, "same")["id"] in users
    assert results.count(True) >= 1 and all(r is True or "already linked" in r for r in results)


def test_check_is_read_only_then_apply_with_fingerprint(env):
    db, tool, users, path, _ = env
    m = tool.normalize_manifest(manifest(users))
    res = tool.run(path, m)
    assert res["to_add"] == 2 and not res["applied"] and oidc_rows(path) == []
    out = tool.run(path, m, apply=True, fingerprint=res["fingerprint"])
    assert out["added"] == 2 and len(oidc_rows(path)) == 2
    again = tool.run(path, m)
    assert again["to_add"] == 0
    assert tool.run(path, m, apply=True, fingerprint=again["fingerprint"])["added"] == 0
    assert db.get_user(users[0])["quota_monthly"] == 1 and db.user_count() == 2


def test_apply_needs_matching_fingerprint_and_detects_drift(env):
    db, tool, users, path, _ = env
    m = tool.normalize_manifest(manifest(users))
    fp = tool.run(path, m)["fingerprint"]
    with pytest.raises(tool.PlanError):
        tool.run(path, m, apply=True)
    with pytest.raises(tool.PlanError):
        tool.run(path, m, apply=True, fingerprint="0" * 64)
    db.update_user(users[1], role="admin")
    with pytest.raises(tool.PlanError):
        tool.run(path, m, apply=True, fingerprint=fp)
    assert oidc_rows(path) == []


def test_whole_plan_rolls_back_on_conflict(env):
    db, tool, users, path, _ = env
    extra = db.create_user("third", "scrypt$x")
    db.link_external_identity(extra, "oidc", ISS, "s1")  # second user's subject is taken
    m = tool.normalize_manifest(manifest(users))
    with pytest.raises(tool.PlanError):
        tool.run(path, m)
    # a plan that passes the check but fails at insert time still leaves nothing behind
    m2 = tool.normalize_manifest(manifest(users, subs=("s0", "s9")))
    fp = tool.run(path, m2)["fingerprint"]
    import sqlite3
    c = sqlite3.connect(path)
    c.execute("CREATE TRIGGER boom BEFORE INSERT ON external_identities WHEN NEW.subject='s9' "
              "BEGIN SELECT RAISE(ABORT,'boom'); END")
    c.commit(); c.close()
    with pytest.raises(tool.PlanError):
        tool.run(path, m2, apply=True, fingerprint=fp)
    assert [r for r in oidc_rows(path) if r[1] == "s0"] == []


def test_missing_jellyfin_anchor_or_wrong_preimage_refused(env):
    db, tool, users, path, _ = env
    bad = manifest(users)
    bad["links"][0]["jellyfin"]["user_guid"] = JU[1]
    with pytest.raises(tool.PlanError):
        tool.run(path, tool.normalize_manifest(bad))
    bad = manifest(users)
    bad["links"][0]["existing_links"] = []
    with pytest.raises(tool.PlanError):
        tool.run(path, tool.normalize_manifest(bad))
    bad = manifest(users)
    bad["links"][0]["expected"]["enabled"] = 0
    with pytest.raises(tool.PlanError):
        tool.run(path, tool.normalize_manifest(bad))


def test_manifest_validation_and_cli(env):
    _, tool, users, path, tmp = env
    for broken in ({}, {"version": 2, "links": []}, {"version": 1, "links": [{}]}):
        with pytest.raises(tool.PlanError):
            tool.normalize_manifest(broken)
    dup = manifest(users)
    dup["links"].append(dup["links"][0])
    with pytest.raises(tool.PlanError):
        tool.run(path, tool.normalize_manifest(dup))
    mp = tmp / "m.json"
    mp.write_text(json.dumps(manifest(users)))
    assert tool.main(["--db", path, "--manifest", str(mp)]) == 0
    assert tool.main(["--db", path, "--manifest", str(mp), "--apply"]) == 1
    assert oidc_rows(path) == []


def _bad(users, mutate):
    m = manifest(users)
    mutate(m["links"][0])
    return m


@pytest.mark.parametrize("mutate", [
    lambda l: l["expected"].update(role="root"),
    lambda l: l["expected"].update(enabled=True),
    lambda l: l["expected"].update(enabled=2),
    lambda l: l["expected"].update(auto_approve=1.0),
    lambda l: l["expected"].update(quota_monthly=True),
    lambda l: l["expected"].update(quota_monthly=-1),
    lambda l: l["expected"].update(quota_monthly="1"),
    lambda l: l["existing_links"][0].update(subject=5),
    lambda l: l["existing_links"][0].update(server_id=None),
    lambda l: l["existing_links"][0].update(provider=""),
    lambda l: l["jellyfin"].update(user_guid=12345),
    lambda l: l["jellyfin"].update(server_guid=None),
    lambda l: l["jellyfin"].update(user_guid="zz" * 16),
    lambda l: l.update(existing_links="nope"),
    lambda l: l["oidc"].update(subject=7),
])
def test_strict_manifest_types_become_plan_errors(env, mutate):
    _, tool, users, _, _ = env
    with pytest.raises(tool.PlanError):
        tool.normalize_manifest(_bad(users, mutate))


def test_duplicate_keys_and_nonfinite_numbers_rejected(env):
    _, tool, users, _, tmp = env
    good = json.dumps(manifest(users))
    p = tmp / "dup.json"
    p.write_text(good.replace('"version": 1', '"version": 1, "version": 1', 1))
    with pytest.raises(tool.PlanError):
        tool.load_manifest(str(p))
    p.write_text(good.replace('"quota_monthly": 1', '"quota_monthly": NaN', 1))
    with pytest.raises(tool.PlanError):
        tool.load_manifest(str(p))
    p.write_text("{not json")
    with pytest.raises(tool.PlanError):
        tool.load_manifest(str(p))


def test_database_path_with_uri_characters_and_one_snapshot(env):
    db, tool, users, path, tmp = env
    odd = tmp / "we ird?dir#x"
    odd.mkdir()
    dst = str(odd / "a b?c#d.db")
    import sqlite3
    src, out = sqlite3.connect(path), sqlite3.connect(dst)
    src.backup(out)
    src.close(); out.close()
    m = tool.normalize_manifest(manifest(users))
    res = tool.run(dst, m)
    assert res["to_add"] == 2
    assert tool.run(dst, m, apply=True, fingerprint=res["fingerprint"])["added"] == 2
    assert len(oidc_rows(dst)) == 2 and oidc_rows(path) == []
    with pytest.raises(tool.PlanError):
        tool.run(str(tmp / "missing.db"), m)


def test_check_leaves_database_untouched_and_foreign_keys_on(env):
    import sqlite3
    db, tool, users, path, _ = env
    m = tool.normalize_manifest(manifest(users))
    before = open(path, "rb").read()
    tool.run(path, m)
    assert open(path, "rb").read() == before
    # an orphan insert would be refused with foreign keys on: unknown user in manifest
    bad = manifest(users)
    bad["links"][0]["native_user_id"] = 9999
    with pytest.raises(tool.PlanError):
        tool.run(path, tool.normalize_manifest(bad))


def test_read_check_never_falls_back_to_writable_connection(env, monkeypatch):
    import sqlite3
    _, tool, users, path, _ = env
    modes = []

    class UnreadableWal:
        def execute(self, statement):
            if statement.startswith("PRAGMA"):
                return self
            raise sqlite3.OperationalError("unavailable WAL snapshot")

        def close(self):
            pass

    def open_readonly(base, mode):
        modes.append(mode)
        return UnreadableWal()

    monkeypatch.setattr(tool, "_open", open_readonly)
    with pytest.raises(tool.PlanError):
        tool.run(path, tool.normalize_manifest(manifest(users)))
    assert modes == ["ro"]


def test_boolean_manifest_version_rejected(env):
    _, tool, users, _, _ = env
    data = manifest(users)
    data["version"] = True
    with pytest.raises(tool.PlanError):
        tool.normalize_manifest(data)
