"""OIDC admission and role policy on a real temporary SQLite database. Synthetic data only."""
import sys

import pytest

ISS = "https://idp.example/realm"
CID = "client-a"


@pytest.fixture()
def env(monkeypatch, tmp_path):
    for m in ("db", "settings", "config", "oidc"):
        sys.modules.pop(m, None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    import config
    config.DB_PATH = str(tmp_path / "t.db")
    import db
    db.init()
    import oidc
    return db, oidc


def claims(sub="sub-1", iss=ISS, aud=CID, groups=None, name="alice"):
    c = {"iss": iss, "aud": aud, "sub": sub, "preferred_username": name}
    if groups is not None:
        c["groups"] = groups
    return c


def login(oidc, c, **kw):
    kw.setdefault("admission_mode", "linked_only")
    kw.setdefault("role_policy", "preserve")
    kw.setdefault("admin_group", "admins")
    return oidc.resolve_login(c, issuer=ISS, client_id=CID, **kw)


def mk(db, name="alice", role="user", **f):
    uid = db.create_user(name, "scrypt$x", role=role)
    if f:
        db.update_user(uid, **f)
    return uid


def test_config_defaults_and_invalid_values_fail_closed(monkeypatch):
    sys.modules.pop("config", None)
    import config
    assert (config.OIDC_ADMISSION_MODE, config.OIDC_ROLE_POLICY) == ("linked_only", "preserve")
    for name in ("OIDC_ADMISSION_MODE", "OIDC_ROLE_POLICY"):
        monkeypatch.setenv(name, "bogus")
        sys.modules.pop("config", None)
        with pytest.raises(RuntimeError):
            import config as _c  # noqa: F401
        monkeypatch.delenv(name)
    sys.modules.pop("config", None)


def test_unknown_subject_with_same_name_is_rejected_and_writes_nothing(env):
    db, oidc = env
    uid = mk(db, "alice")
    before = db.get_user(uid)
    with pytest.raises(oidc.LoginRejected) as e:
        login(oidc, claims(sub="stranger", name="alice"))
    assert e.value.code == "oidc_unlinked"
    assert db.get_user(uid) == before
    assert db.user_count() == 1
    assert not db.has_external_identity(uid)


def test_linked_login_preserves_role_and_policy_even_with_admin_group(env):
    db, oidc = env
    uid = mk(db, role="user", quota_monthly=5, auto_approve=0)
    db.link_external_identity(uid, "oidc", ISS, "sub-1")
    u = login(oidc, claims(groups=["admins"]))
    assert u["id"] == uid and u["role"] == "user"
    assert (u["quota_monthly"], u["auto_approve"]) == (5, 0)
    # and an admin is not demoted by a missing groups claim
    aid = mk(db, "boss", role="admin")
    db.link_external_identity(aid, "oidc", ISS, "sub-2")
    assert login(oidc, claims(sub="sub-2", groups=[], name="x"))["role"] == "admin"


def test_groups_policy_syncs_exactly_including_revocation(env):
    db, oidc = env
    uid = mk(db, role="user")
    db.link_external_identity(uid, "oidc", ISS, "sub-1")
    assert login(oidc, claims(groups=["admins"]), role_policy="groups")["role"] == "admin"
    assert login(oidc, claims(groups=["other"]), role_policy="groups")["role"] == "user"
    assert login(oidc, claims(groups=["admins"]), role_policy="groups")["role"] == "admin"
    assert login(oidc, claims(), role_policy="groups")["role"] == "user"  # claim absent = revoked
    assert login(oidc, claims(groups="admins, x"), role_policy="groups")["role"] == "admin"


def test_disabled_identity_rejected_before_any_timestamp_changes(env):
    db, oidc = env
    uid = mk(db, enabled=0)
    db.link_external_identity(uid, "oidc", ISS, "sub-1")
    with db._connect() as c:
        pre = (dict(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()),
               dict(c.execute("SELECT * FROM external_identities WHERE user_id=?", (uid,)).fetchone()))
    with pytest.raises(oidc.LoginRejected) as e:
        login(oidc, claims(groups=["admins"]), role_policy="groups")
    assert e.value.code == "oidc_unlinked"
    with db._connect() as c:
        post = (dict(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()),
                dict(c.execute("SELECT * FROM external_identities WHERE user_id=?", (uid,)).fetchone()))
    assert pre == post and post[0]["role"] == "user"


def test_accepted_login_sets_timestamps(env):
    db, oidc = env
    uid = mk(db)
    db.link_external_identity(uid, "oidc", ISS, "sub-1")
    assert db.get_user(uid)["last_login"] is None
    login(oidc, claims())
    assert db.get_user(uid)["last_login"]


def test_provision_mode_creates_ordinary_user_and_never_escalates_on_preserve(env):
    db, oidc = env
    u = login(oidc, claims(sub="new", name="newbie", groups=["admins"]), admission_mode="provision")
    assert (u["role"], u["auto_approve"], u["quota_monthly"], u["enabled"]) == ("user", 0, 0, 1)
    again = login(oidc, claims(sub="new", name="renamed", groups=["admins"]), admission_mode="provision")
    assert again["id"] == u["id"] and again["role"] == "user"
    g = login(oidc, claims(sub="new2", name="n2", groups=["admins"]),
              admission_mode="provision", role_policy="groups")
    assert g["role"] == "admin"


def test_provision_does_not_merge_with_existing_same_name(env):
    db, oidc = env
    uid = mk(db, "alice")
    u = login(oidc, claims(sub="other", name="alice"), admission_mode="provision")
    assert u["id"] != uid and u["username"] != "alice"


@pytest.mark.parametrize("bad", [
    claims(iss=ISS + "/"), claims(iss="https://other.example"), claims(aud="other-client"),
    claims(aud=[CID, "x"]), claims(sub=""), {}, claims(aud=""), claims(aud=[]),
    claims(aud=[CID, 5]), claims(aud=None), claims(aud=7), claims(sub=5), claims(iss=None),
    None, "not-a-mapping", dict(claims(), azp="other-client"),
])
def test_wrong_issuer_audience_or_subject_rejected(env, bad):
    db, oidc = env
    uid = mk(db)
    db.link_external_identity(uid, "oidc", ISS, "sub-1")
    with pytest.raises(oidc.LoginRejected) as e:
        login(oidc, bad)
    assert e.value.code == "oidc"


def test_multi_audience_with_matching_azp_is_accepted(env):
    db, oidc = env
    uid = mk(db)
    db.link_external_identity(uid, "oidc", ISS, "sub-1")
    c = claims(aud=[CID, "x"])
    c["azp"] = CID
    assert login(oidc, c)["id"] == uid


def test_invalid_policy_arguments_fail_closed(env):
    db, oidc = env
    with pytest.raises(oidc.LoginRejected):
        login(oidc, claims(), admission_mode="open")


def test_callback_uses_native_role_and_ignores_userinfo_only(env, monkeypatch):
    db, oidc = env
    import flask
    uid = mk(db, "alice", role="user")
    db.link_external_identity(uid, "oidc", ISS, "sub-1")
    monkeypatch.setattr(oidc.cfg, "OIDC_ENABLED", True)
    monkeypatch.setattr(oidc.cfg, "OIDC_ISSUER_URL", ISS)
    monkeypatch.setattr(oidc.cfg, "OIDC_CLIENT_ID", CID)
    monkeypatch.setattr(oidc.cfg, "OIDC_CLIENT_SECRET", "synthetic")
    app = flask.Flask(__name__)
    app.secret_key = "t"
    app.add_url_rule("/login", "login_view", lambda: "login")
    oidc.install(app)

    class Fake:
        token = {}

        def authorize_access_token(self, **kw):
            assert kw["claims_options"]["iss"]["value"] == ISS
            return self.token

    fake = Fake()
    monkeypatch.setattr(oidc, "_oauth", type("O", (), {"oidc": fake})())
    c = app.test_client()
    with c.session_transaction() as s:
        s["stale"] = 1
        s["_oidc_next"] = "//evil.example"
    fake.token = {"userinfo": claims(groups=["admins"])}  # no id_token: unverified
    r = c.get("/oidc/callback")
    assert "error=oidc" in r.headers["Location"]
    fake.token = {"id_token": "x", "userinfo": claims(groups=["admins"])}
    r = c.get("/oidc/callback")
    assert r.headers["Location"] == "/ui"
    with c.session_transaction() as s:
        assert s["role"] == "user" and s["user_id"] == uid and "stale" not in s
    fake.token = {"id_token": "x", "userinfo": claims(sub="nobody")}
    r = c.get("/oidc/callback")
    assert "error=oidc_unlinked" in r.headers["Location"]
    with c.session_transaction() as s:
        assert "user_id" not in s


# ── real Authlib ID-token verification (synthetic RSA key, no network) ─────────

import time  # noqa: E402


def _keys():
    from authlib.jose import JsonWebKey
    good = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "k1"})
    other = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "k1"})
    return good, other


@pytest.fixture()
def real(env, monkeypatch):
    """Flask app wired to a real Authlib client whose metadata and keys are inline."""
    db, oidc = env
    import flask
    from authlib.integrations.flask_client import OAuth
    from authlib.jose import jwt
    good, other = _keys()
    uid = mk(db, "alice", role="user")
    db.link_external_identity(uid, "oidc", ISS, "sub-1")
    monkeypatch.setattr(oidc.cfg, "OIDC_ISSUER_URL", ISS)
    monkeypatch.setattr(oidc.cfg, "OIDC_CLIENT_ID", CID)
    app = flask.Flask(__name__)
    app.secret_key = "t"
    app.add_url_rule("/login", "login_view", lambda: "login")
    oauth = OAuth(app)
    oauth.register(
        name="oidc", client_id=CID, client_secret="synthetic", client_kwargs={"scope": "openid"},
        issuer=ISS, authorization_endpoint=ISS + "/auth", token_endpoint=ISS + "/token",
        jwks={"keys": [good.as_dict(is_private=False)]},
        id_token_signing_alg_values_supported=["RS256"],
    )
    monkeypatch.setattr(oidc.cfg, "OIDC_ENABLED", True)
    monkeypatch.setattr(oidc.cfg, "OIDC_CLIENT_SECRET", "synthetic")
    # install() would re-register the client from the network metadata URL, so register
    # only the routes by calling it with a stub OAuth registration step.
    import authlib.integrations.flask_client as fc
    monkeypatch.setattr(fc, "OAuth", lambda a: oauth)
    monkeypatch.setattr(oauth, "register", lambda *a, **k: None)
    oidc.install(app)
    c = app.test_client()

    def attempt(*, key=good, nonce_override=None, state_override=None, **claim_over):
        r = c.get("/login/oidc?next=/x")
        assert r.status_code == 302
        from urllib.parse import parse_qs, urlsplit
        q = parse_qs(urlsplit(r.headers["Location"]).query)
        state, nonce = q["state"][0], q["nonce"][0]
        now = int(time.time())
        payload = {"iss": ISS, "aud": CID, "sub": "sub-1", "iat": now, "exp": now + 300,
                   "nonce": nonce_override or nonce, "preferred_username": "alice"}
        payload.update(claim_over)
        payload = {k: v for k, v in payload.items() if v is not None}
        tok = jwt.encode({"alg": "RS256", "kid": "k1"}, payload, key).decode()
        monkeypatch.setattr(type(oauth.oidc), "fetch_access_token",
                            lambda self, **kw: {"access_token": "a", "token_type": "Bearer", "id_token": tok},
                            raising=True)
        return c.get(f"/oidc/callback?code=c&state={state_override or state}")

    return db, oidc, uid, c, attempt, good, other


def test_real_token_valid_signs_in(real):
    db, oidc, uid, c, attempt, good, other = real
    r = attempt()
    assert r.headers["Location"] == "/x"
    with c.session_transaction() as s:
        assert s["user_id"] == uid and s["role"] == "user"


@pytest.mark.parametrize("label,kw", [
    ("wrong key", {"key": "other"}),
    ("wrong nonce", {"nonce_override": "forged"}),
    ("wrong state", {"state_override": "forged"}),
    ("expired", {"exp": 1000, "iat": 900}),
    ("wrong issuer", {"iss": "https://evil.example/realm"}),
    ("issuer slash", {"iss": ISS + "/"}),
    ("wrong audience", {"aud": "other-client"}),
    ("azp mismatch", {"azp": "other-client"}),
    ("multi aud no azp", {"aud": [CID, "x"]}),
    ("missing sub", {"sub": None}),
])
def test_real_token_failures_never_sign_in(real, label, kw):
    db, oidc, uid, c, attempt, good, other = real
    kw = dict(kw)
    if kw.get("key") == "other":
        kw["key"] = other
    r = attempt(**kw)
    assert "/login" in r.headers["Location"] and "error=oidc" in r.headers["Location"], label
    with c.session_transaction() as s:
        assert "user_id" not in s


def test_real_token_unlinked_subject_is_friendly_reject(real):
    db, oidc, uid, c, attempt, good, other = real
    r = attempt(sub="nobody")
    assert "error=oidc_unlinked" in r.headers["Location"]
