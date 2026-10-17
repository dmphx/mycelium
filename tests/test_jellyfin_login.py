"""Jellyfin login: real sqlite db + mocked HTTP, synthetic users only."""
import importlib
import os
import sys
import tempfile
from unittest.mock import MagicMock

import pytest
import requests

SID = "0123456789abcdef0123456789abcdef"
UID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SUBJ = UID.replace("-", "")


@pytest.fixture()
def env(monkeypatch, tmp_path):
    saved = {m: sys.modules.get(m) for m in ("db", "settings", "config", "jellyfin_login")}
    for m in saved:
        sys.modules.pop(m, None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JELLYFIN_LOGIN_ENABLED", "true")
    monkeypatch.setenv("JELLYFIN_AUTH_SERVER_ID", SID)
    monkeypatch.setenv("JELLYFIN_URL", "http://jf.invalid:8096")
    import config
    config.DB_PATH = str(tmp_path / "t.db")
    import db
    db.init()
    import jellyfin_login as jl
    monkeypatch.setattr(jl, "_base_url", lambda: "http://jf.invalid:8096")
    calls = []

    def reply(status=200, server=SID, uid=UID, name="Alice", disabled=False):
        body = {"AccessToken": "tok", "ServerId": server,
                "User": {"Id": uid, "Name": name,
                         "Policy": {"IsDisabled": disabled, "IsAdministrator": True}}}
        def post(url, **kw):
            calls.append((url, kw))
            r = MagicMock(); r.status_code = status; r.json.return_value = body
            return r
        monkeypatch.setattr(jl.requests, "post", post)
    yield db, jl, reply, calls
    for m, v in saved.items():
        sys.modules.pop(m, None)
        if v is not None:
            sys.modules[m] = v


def test_success_provisions_plain_user_and_revokes(env):
    db, jl, reply, calls = env
    reply()
    sess = {}
    assert jl.login("alice", "pw", sess)
    u = db.get_user(sess["user_id"])
    assert u["role"] == "user" and u["auto_approve"] == 0 and u["enabled"] == 1
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][0].endswith("/Users/AuthenticateByName")
    assert calls[1][0].endswith("/Sessions/Logout")
    assert "tok" not in repr(sess) and u["password_hash"] == jl.PASSWORD_SENTINEL


def test_wrong_password_and_timeout(env, monkeypatch):
    db, jl, reply, calls = env
    reply(status=401)
    assert not jl.login("alice", "bad", {})
    def boom(*a, **k): raise requests.Timeout()
    monkeypatch.setattr(jl.requests, "post", boom)
    assert not jl.login("alice", "pw", {})
    assert db.user_count() == 0


def test_disabled_and_wrong_server_refused(env):
    db, jl, reply, calls = env
    reply(disabled=True)
    assert not jl.login("alice", "pw", {})
    reply(server="f" * 32)
    assert not jl.login("alice", "pw", {})
    reply(uid="not-a-uuid")
    assert not jl.login("alice", "pw", {})
    assert db.user_count() == 0


def test_same_username_cannot_take_over_admin(env):
    db, jl, reply, calls = env
    admin = db.create_user("Alice", "scrypt$x$y", role="admin", auto_approve=True)
    reply(name="alice")
    sess = {}
    assert jl.login("alice", "pw", sess)
    assert sess["user_id"] != admin and sess["role"] == "user"
    assert db.get_user(admin)["role"] == "admin"
    assert db.get_user(sess["user_id"])["username"] != "Alice"


def test_explicit_link_and_rename_preserves_history(env):
    db, jl, reply, calls = env
    admin = db.create_user("owner", "scrypt$x$y", role="admin")
    db.link_external_identity(admin, "jellyfin", SID, SUBJ, "owner")
    reply(name="renamed")
    sess = {}
    assert jl.login("renamed", "pw", sess)
    assert sess["user_id"] == admin and sess["role"] == "admin"
    assert db.user_count() == 1
    with pytest.raises(ValueError):
        other = db.create_user("other", "scrypt$x$y")
        db.link_external_identity(other, "jellyfin", SID, SUBJ)


def test_disabled_native_user_cannot_login(env):
    db, jl, reply, calls = env
    u = db.create_user("bob", "scrypt$x$y")
    db.link_external_identity(u, "jellyfin", SID, SUBJ)
    db.update_user(u, enabled=0)
    reply()
    assert not jl.login("bob", "pw", {})


def test_disabled_user_session_revoked(env):
    db, jl, reply, calls = env
    import flask, auth
    u = db.create_user("carol", "scrypt$x$y")
    app = flask.Flask(__name__); app.secret_key = "k"
    with app.test_request_context():
        flask.session["user"] = "carol"; flask.session["user_id"] = u
        assert not auth.session_revoked()
        db.update_user(u, enabled=0)
        assert auth.session_revoked() and not flask.session.get("user")


def test_login_route_csrf_and_jellyfin_flow(env, monkeypatch):
    db, jl, reply, calls = env
    import flask
    from flask_wtf.csrf import CSRFProtect
    app = flask.Flask(__name__); app.secret_key = "k"
    CSRFProtect(app)
    @app.post("/login")
    def _l():
        return "ok"
    c = app.test_client()
    assert c.post("/login", data={"method": "jellyfin", "username": "a", "password": "b"}).status_code == 400


def test_oidc_cannot_merge_into_jellyfin_linked_account(env):
    db, jl, reply, calls = env
    u = db.create_user("dana", "scrypt$x$y")
    db.link_external_identity(u, "jellyfin", SID, SUBJ)
    with pytest.raises(ValueError):
        db.upsert_oidc_user("dana", role="admin")
    assert db.get_user(u)["role"] == "user"
    assert db.upsert_oidc_user("plain", role="user")


def test_proxy_header_cannot_claim_linked_account_and_deleted_user_session(env):
    db, jl, reply, calls = env
    import flask, auth, settings
    u = db.create_user("erin", "scrypt$x$y")
    db.link_external_identity(u, "jellyfin", SID, SUBJ)
    app = flask.Flask(__name__); app.secret_key = "k"
    orig = auth._proxy_user
    auth._proxy_user = lambda: "erin"
    try:
        with app.test_request_context():
            assert auth.current_user_record() is None
        gone = db.create_user("gone", "scrypt$x$y", role="admin")
        with app.test_request_context():
            flask.session.update(user="gone", user_id=gone, role="admin")
            db.delete_user(gone)
            assert auth.current_user_record() is None and not flask.session.get("role")
    finally:
        auth._proxy_user = orig


def test_rate_limit_key_trusts_real_ip_only_from_trusted_peer(env, monkeypatch):
    db, jl, reply, calls = env
    import flask, auth, settings
    monkeypatch.setattr(auth.settings, "get", lambda k, d=None: "10.0.0.0/8" if k == "TRUSTED_PROXY_NETWORKS" else d)
    app = flask.Flask(__name__)
    hdr = {"X-Real-IP": "203.0.113.5"}
    with app.test_request_context(environ_base={"REMOTE_ADDR": "10.1.2.3"}, headers=hdr):
        assert auth.rate_limit_key() == "203.0.113.5"
    with app.test_request_context(environ_base={"REMOTE_ADDR": "198.51.100.9"}, headers=hdr):
        assert auth.rate_limit_key() == "198.51.100.9"
    with app.test_request_context(environ_base={"REMOTE_ADDR": "10.1.2.3"}, headers={"X-Real-IP": "junk"}):
        assert auth.rate_limit_key() == "10.1.2.3"
