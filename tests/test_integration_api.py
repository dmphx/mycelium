"""HTTP boundary for the signed integration API on a minimal Flask app with the real
receiver, the real auth hook, real CSRF protection and a real temporary database.
Synthetic keys and data only."""
import base64
import hashlib
import json
import sys

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from flask import Flask, jsonify, session

import integration_api as api
import integration_auth as ia

ISSUER = "https://signer.example.test/issuer"
SERVER = "11111111111141118111111111111111"
VIEWER = "22222222222242228222222222222222"
NOW = 1_800_000_000
_n = [0]
BODY = b"{}"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwk(key, kid):
    nums = key.public_key().public_numbers()
    enc = lambda i: b64(i.to_bytes((i.bit_length() + 7) // 8, "big"))  # noqa: E731
    return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid, "n": enc(nums.n), "e": enc(nums.e)}


@pytest.fixture(scope="module")
def keys():
    return {k: rsa.generate_private_key(public_exponent=65537, key_size=2048) for k in ("viewer", "service")}


def token(key, kid, *, method="GET", target="/integration/v1/me", body=b"", scope="catalog:read",
          idem=None, role="viewer", sub=VIEWER, **over):
    _n[0] += 1
    claims = {"iss": ISSUER, "aud": "mycelium.integration.v1" if role == "viewer" else "mycelium.playback.v1",
              "sub": sub if role == "viewer" else SERVER, "jf_sid": SERVER, "iat": NOW, "nbf": NOW,
              "exp": NOW + 30, "jti": f"jti-{_n[0]:08d}-" + "a" * 8, "htm": method, "target": target,
              "body_sha256": hashlib.sha256(body).hexdigest(), "scope": scope}
    if idem:
        claims["idempotency_key"] = idem
    claims.update(over)
    h = b64(json.dumps({"alg": "RS256", "typ": "JWT", "kid": kid}, separators=(",", ":")).encode())
    p = b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = key.sign((h + "." + p).encode(), padding.PKCS1v15(), hashes.SHA256())
    return h + "." + p + "." + b64(sig)


@pytest.fixture()
def env(monkeypatch, tmp_path, keys):
    for m in ("db", "settings", "config", "request_intents", "auth"):
        sys.modules.pop(m, None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    import config
    config.DB_PATH = str(tmp_path / "t.db")
    config.TMDB_API_KEY = ""
    import db
    db.init()
    import auth
    import request_intents as ri
    from flask_wtf.csrf import CSRFProtect

    monkeypatch.setattr(auth, "is_enabled", lambda: True)
    monkeypatch.setattr(auth, "_authenticated", lambda: False)
    app = Flask(__name__)
    app.config.update(SECRET_KEY="synthetic-test-secret", TESTING=True)
    csrf = CSRFProtect(app)
    csrf.exempt(api.bp)
    app.register_blueprint(api.bp)

    @app.get("/ui/api/x")
    def _x():
        return jsonify(ok=True)

    @app.post("/ui/api/y")
    def _y():
        return jsonify(ok=True)

    @app.get("/cookie")
    def _cookie():
        session["user_id"] = 1
        return jsonify(ok=True)

    auth.install_before_request(app)
    viewer, service = ia.build_keyrings({"keys": [jwk(keys["viewer"], "v1")]},
                                        {"keys": [jwk(keys["service"], "s1")]})
    api.reset_state()
    api.STATE.settings = ia.Settings(ISSUER, SERVER, viewer, service)
    api.STATE.loaded = True
    api.STATE.replay = ia.ReplayStore(path=str(tmp_path / "replay.db"))
    sqlite_init = __import__("sqlite3").connect(str(tmp_path / "replay.db"), isolation_level=None)
    ia.init_schema(sqlite_init)
    sqlite_init.close()
    api.STATE.clock = lambda: NOW
    uid = db.create_user("synthetic-viewer", "scrypt$x", role="user")
    assert db.link_external_identity(uid, "jellyfin", SERVER, VIEWER)
    yield type("Env", (), {"app": app, "client": app.test_client(), "db": db, "ri": ri, "uid": uid,
                           "keys": keys, "auth": auth})
    api.reset_state()
    api.STATE.clock = None


def call(env, method="GET", path="/integration/v1/me", *, raw=None, body=b"", scope="catalog:read",
         idem=None, role="viewer", headers=None, sign=True, raw_env=True, **tok):
    raw = path if raw is None else raw
    hdr = dict(headers or {})
    if sign:
        key, kid = (env.keys["viewer"], "v1") if role == "viewer" else (env.keys["service"], "s1")
        args = dict(method=method, target=raw, body=body, scope=scope, idem=idem, role=role)
        args.update(tok)
        hdr["Authorization"] = "Bearer " + token(key, kid, **args)
    if method in ("POST", "DELETE"):
        hdr.setdefault("Content-Type", "application/json")
        if idem:
            hdr["Idempotency-Key"] = idem
    overrides = {"RAW_URI": raw if raw_env else None}
    return env.client.open(path, method=method, data=body or None, headers=hdr, environ_overrides=overrides)


def err(resp):
    return resp.get_json()["error"]["code"]


def test_disabled_by_default_is_a_plain_404(env):
    api.STATE.settings = None
    r = call(env)
    assert r.status_code == 404 and err(r) == "not_found"


def test_me_capabilities_and_envelope(env):
    r = call(env)
    j = r.get_json()
    assert r.status_code == 200 and j["api_version"] == "1" and j["meta"]["generated_at"].endswith("Z")
    assert r.headers["Cache-Control"] == "no-store" and "Set-Cookie" not in r.headers
    d = j["data"]
    assert d["viewer"] == {"id": env.uid, "display_name": "synthetic-viewer"}
    assert d["policy"]["request_approval"] == "required"
    assert d["policy"]["monthly_quota"] == {"limit": None, "used": 0, "remaining": None}
    assert d["operations"] == ["me.read", "genre-tabs.read", "follows.read", "requests.read"]
    c = call(env, path="/integration/v1/capabilities").get_json()["data"]
    assert c["operations"] == d["operations"]
    assert c["limits"] == {"request_body_bytes": 65536, "authorization_bytes": 6144,
                           "request_episodes": 500, "request_seasons": 100, "list_page_size": 100}
    assert c["incomplete"] == sorted(c["incomplete"]) and "requests.write" in c["incomplete"] \
        and "follows.write" in c["incomplete"] and "me.read" not in c["incomplete"]


def test_me_quota_and_admin_bypass(env):
    env.db.update_user(env.uid, quota_monthly=3, auto_approve=1)
    for status in ("approved", "pending", "denied"):
        env.db.create_user_request(env.uid, "tt1234567", 1, "movie", "t", status=status)
    q = call(env).get_json()["data"]
    assert q["policy"]["request_approval"] == "automatic"
    assert q["policy"]["monthly_quota"] == {"limit": 3, "used": 2, "remaining": 1}
    env.db.update_user(env.uid, role="admin")
    q = call(env).get_json()["data"]["policy"]["monthly_quota"]
    assert q["limit"] is None and q["remaining"] is None and q["used"] == 2


def test_cookie_alone_never_authenticates_and_no_cookie_is_set(env):
    with env.client.session_transaction() as s:
        s["user_id"] = env.uid
    r = call(env, sign=False)
    assert r.status_code == 401 and err(r) == "unauthorized" and "Set-Cookie" not in r.headers
    ok = call(env)
    assert ok.status_code == 200 and "Set-Cookie" not in ok.headers


def test_global_gate_is_still_closed_and_carveout_is_exact(env):
    anon = env.app.test_client()
    assert anon.get("/ui/api/x").status_code == 401
    assert "/integration/v1" not in env.auth._PUBLIC_PATHS
    assert anon.post("/ui/api/y").status_code in (400, 401)  # CSRF/auth still enforced


def test_actor_states(env):
    env.db.update_user(env.uid, enabled=0)
    assert call(env).status_code == 403
    env.db.update_user(env.uid, enabled=1)
    assert call(env).status_code == 200
    r = call(env, sub="44444444444444448444444444444444")
    assert r.status_code == 403 and err(r) == "unlinked"


def test_rings_and_service_route(env):
    bad = env.client.get("/integration/v1/me", environ_overrides={"RAW_URI": "/integration/v1/me"},
                         headers={"Authorization": "Bearer " + token(env.keys["service"], "s1", role="viewer")})
    assert bad.status_code == 401
    bad = env.client.post("/integration/v1/readers", data=BODY, content_type="application/json",
                          environ_overrides={"RAW_URI": "/integration/v1/readers"},
                          headers={"Idempotency-Key": "synthetic_idem_0001", "Authorization": "Bearer " + token(
                              env.keys["viewer"], "v1", method="POST", target="/integration/v1/readers", body=BODY,
                              scope="readers:write", role="service", idem="synthetic_idem_0001")})
    assert bad.status_code == 401
    good = call(env, "POST", "/integration/v1/readers", body=BODY, scope="readers:write", role="service",
                idem="synthetic_idem_0001")
    assert good.status_code == 503 and err(good) == "capability_unqualified"
    viewer_on_readers = call(env, "POST", "/integration/v1/readers", body=BODY, scope="readers:write",
                             idem="synthetic_idem_0002")
    assert viewer_on_readers.status_code in (401, 403)


def test_binding_failures(env):
    assert call(env, htm="POST").status_code == 401
    assert call(env, target="/integration/v1/capabilities").status_code == 401
    assert call(env, "POST", "/integration/v1/watchlist", body=BODY, scope="watchlist:write",
                idem="synthetic_idem_0003", body_sha256="0" * 64).status_code == 401
    assert call(env, "POST", "/integration/v1/watchlist", body=BODY, scope="watchlist:write").status_code == 401
    assert call(env, scope="requests:read").status_code == 403


@pytest.mark.parametrize("path,scope", [
    ("/integration/v1/me?x=1", "catalog:read"), ("/integration/v1/requests?limit=0", "requests:read"),
    ("/integration/v1/requests?limit=101", "requests:read"), ("/integration/v1/requests?limit=010", "requests:read"),
    ("/integration/v1/requests?limit=abc", "requests:read"), ("/integration/v1/requests?before_id=0", "requests:read"),
    ("/integration/v1/requests?before_id=-1", "requests:read"), ("/integration/v1/requests?limit=5&limit=6", "requests:read"),
    ("/integration/v1/requests?limit=5&before_id=3", "requests:read"),  # unsorted
    ("/integration/v1/requests?limit=%35", "requests:read"), ("/integration/v1/requests?page=1", "requests:read"),
])
def test_query_rejections(env, path, scope):
    r = call(env, path=path, scope=scope)
    assert r.status_code == 400 and r.get_json()["error"]["code"] == "bad_request"


@pytest.mark.parametrize("raw", ["/integration/v1/me/", "/integration/v1//me", "/integration/v1/%2Fme",
                                 "/integration/v1/./me", "/integration/v1/../me", "/integration/v1/%2e%2e/me",
                                 "/integration/v1/me;x=1", "/integration/v1/me#f", "/integration/v1/me?"])
def test_aliases_are_400_never_redirects(env, raw):
    r = call(env, path="/integration/v1/me", raw=raw)
    assert r.status_code == 400 and "Location" not in r.headers
    r2 = call(env, path=raw, raw=raw)
    assert r2.status_code < 300 or r2.status_code >= 400
    assert r2.status_code != 308 and r2.status_code != 301 and "Location" not in r2.headers


def test_raw_target_must_be_server_owned(env):
    r = call(env, raw_env=False)
    assert r.status_code == 503 and err(r) == "raw_target_unavailable"
    r = call(env, raw="http://evil.example/integration/v1/me")
    assert r.status_code == 503
    r = call(env, headers={"X-Original-URI": "/integration/v1/capabilities",
                           "X-Forwarded-Uri": "/integration/v1/capabilities", "X-Rewrite-Url": "/x"})
    assert r.status_code == 200 and "viewer" in r.get_json()["data"]  # headers never change the target
    r = call(env, path="/integration/v1/capabilities", raw="/integration/v1/me")  # PATH_INFO ignored
    assert r.status_code == 200 and "viewer" in r.get_json()["data"]


def test_uniform_404_and_explicit_405(env):
    a = call(env, path="/integration/v1/nope", sign=False)
    b = call(env, "GET", "/integration/v1/availability", sign=False)  # known path, wrong method
    c = call(env, path="/integration/v1/", raw="/integration/v1/", sign=False)
    d = call(env, path="/integration/v1/requests/abc", sign=False)
    for r in (a, b, c, d):
        assert r.status_code in (400, 404)
    assert a.status_code == b.status_code == d.status_code == 404
    assert a.get_json()["error"] == b.get_json()["error"] == d.get_json()["error"]
    for method in ("PUT", "PATCH", "OPTIONS", "HEAD"):
        r = env.client.open("/integration/v1/me", method=method, environ_overrides={"RAW_URI": "/integration/v1/me"})
        assert r.status_code == 405 and r.headers["Allow"] == "GET, POST, DELETE"
        assert "Access-Control-Allow-Origin" not in r.headers
        if method != "HEAD":
            assert r.get_json()["error"]["code"] == "method_not_allowed"


UNQUALIFIED = [
    ("GET", "/integration/v1/watchlist?limit=5", "watchlist:read"),
    ("GET", "/integration/v1/issues", "issues:read"), ("GET", "/integration/v1/issues/7", "issues:read"),
    ("POST", "/integration/v1/watchlist", "watchlist:write"), ("DELETE", "/integration/v1/watchlist", "watchlist:write"),
    ("POST", "/integration/v1/requests", "requests:write"), ("POST", "/integration/v1/availability", "availability:read"),
    ("POST", "/integration/v1/playback-contexts", "playback-contexts:write"),
    ("POST", "/integration/v1/issues", "issues:write"),
]


@pytest.mark.parametrize("method,path,scope", UNQUALIFIED)
def test_unqualified_operations_503_without_side_effects(env, monkeypatch, method, path, scope):
    def boom(*a, **k):
        raise AssertionError("must not be called")
    monkeypatch.setattr(env.ri, "admit", boom)
    import tmdb
    import tmdb_checked
    monkeypatch.setattr(tmdb, "_get", boom)
    monkeypatch.setattr(tmdb_checked, "fetch", boom)
    before = (len(env.ri.list_for_user(env.uid, 50)), len(env.db.get_watchlist(env.uid)),
              len(env.db.get_favorite_actors(env.uid)))
    body = BODY if method in ("POST", "DELETE") else b""
    idem = "synthetic_idem_0010" if body else None
    tok = token(env.keys["viewer"], "v1", method=method, target=path, body=body, scope=scope, idem=idem)
    hdr = {"Authorization": "Bearer " + tok}
    if body:
        hdr.update({"Content-Type": "application/json", "Idempotency-Key": idem})
    r = env.client.open(path, method=method, data=body or None, headers=hdr, environ_overrides={"RAW_URI": path})
    assert r.status_code == 503 and err(r) == "capability_unqualified"
    assert r.get_json()["error"]["retryable"] is False
    assert (len(env.ri.list_for_user(env.uid, 50)), len(env.db.get_watchlist(env.uid)),
            len(env.db.get_favorite_actors(env.uid))) == before
    again = env.client.open(path, method=method, data=body or None, headers=hdr, environ_overrides={"RAW_URI": path})
    assert again.status_code == 401  # the 503 consumed its jti


def test_unqualified_write_needs_valid_auth_and_csrf_is_exempt(env):
    r = call(env, "POST", "/integration/v1/requests", body=BODY, scope="requests:write", idem="synthetic_idem_0011",
             sign=False)
    assert r.status_code == 401  # not 400: CSRF never gets a vote on this blueprint
    ok = call(env, "POST", "/integration/v1/requests", body=BODY, scope="requests:write", idem="synthetic_idem_0012")
    assert ok.status_code == 503


def _admit(env, uid, tid, n):
    return env.ri.admit(uid, {"media": {"type": "tv", "tmdb_id": tid}, "scope": {"kind": "all_seasons"}},
                        f"key-{n:0>14}", {"title": f"Synthetic {tid}", "imdb_id": "tt1234567"})


def test_requests_list_get_pagination_and_ownership(env):
    other = env.db.create_user("synthetic-other", "scrypt$x", role="user")
    mine = [_admit(env, env.uid, 100 + i, i)["request_id"] for i in range(5)]
    theirs = _admit(env, other, 900, 99)["request_id"]
    page1 = call(env, path="/integration/v1/requests?limit=2", scope="requests:read").get_json()["data"]
    assert [i["request_id"] for i in page1["items"]] == mine[::-1][:2] and page1["next_before_id"] == mine[3]
    page2 = call(env, path=f"/integration/v1/requests?before_id={page1['next_before_id']}&limit=2",
                 scope="requests:read").get_json()["data"]
    assert [i["request_id"] for i in page2["items"]] == mine[::-1][2:4]
    last = call(env, path=f"/integration/v1/requests?before_id={page2['next_before_id']}&limit=2",
                scope="requests:read").get_json()["data"]
    assert [i["request_id"] for i in last["items"]] == [mine[0]] and last["next_before_id"] is None
    exact = call(env, path="/integration/v1/requests?limit=5", scope="requests:read").get_json()["data"]
    assert len(exact["items"]) == 5 and exact["next_before_id"] is None  # no cursor without more rows
    full = call(env, path="/integration/v1/requests?limit=100", scope="requests:read").get_json()["data"]
    assert theirs not in [i["request_id"] for i in full["items"]]
    item = full["items"][0]
    assert set(item) <= {"request_id", "media", "scope", "request_status", "processing", "availability", "outcome"}
    assert item["availability"] == {"state": "unknown", "entitlement": "unknown", "lookup": "unknown",
                                    "playability": "unknown"}
    one = call(env, path=f"/integration/v1/requests/{mine[0]}", scope="requests:read")
    assert one.status_code == 200 and one.get_json()["data"]["request_id"] == mine[0]
    foreign = call(env, path=f"/integration/v1/requests/{theirs}", scope="requests:read")
    missing = call(env, path="/integration/v1/requests/999999", scope="requests:read")
    huge = call(env, path="/integration/v1/requests/" + "9" * 16, scope="requests:read")
    assert foreign.status_code == missing.status_code == huge.status_code == 404
    assert foreign.get_json()["error"] == missing.get_json()["error"] == huge.get_json()["error"]


def test_unexpected_errors_are_generic(env, monkeypatch, caplog):
    def boom(vr, found):
        raise RuntimeError("secret-token-value /private/path")
    monkeypatch.setitem(api._HANDLERS, "me", boom)
    r = call(env)
    assert r.status_code == 500 and "secret" not in r.get_data(as_text=True) \
        and r.get_json()["error"]["code"] == "internal_error"
    assert "secret-token-value" not in caplog.text and "/private/path" not in caplog.text


@pytest.mark.parametrize("path,helper", [("/integration/v1/requests", "list_for_user"),
                                         ("/integration/v1/requests/1", "get")])
def test_disable_after_verification_stays_forbidden(env, monkeypatch, path, helper):
    def disabled(*args, **kwargs):
        raise env.ri.IntentError("user_required", 403, "disabled")
    monkeypatch.setattr(env.ri, helper, disabled)
    response = call(env, path=path, scope="requests:read")
    assert response.status_code == 403 and err(response) == "disabled"


def test_rate_limit_is_bounded_and_reported(env):
    api.STATE.buckets = api.Buckets(burst=1, rate=0.001, max_keys=4)
    assert call(env).status_code == 200
    r = call(env)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1 and r.get_json()["error"]["retryable"]
    b = api.Buckets(burst=1, rate=1, max_keys=8)
    for i in range(5000):
        b.take(i)
    assert len(b._state) <= 8


def test_oversized_and_malformed_bodies(env):
    big = b"{" + b" " * 70000 + b"}"
    r = env.client.post("/integration/v1/requests", data=big, content_type="application/json",
                        headers={"Authorization": "Bearer " + token(env.keys["viewer"], "v1", method="POST",
                                                                    target="/integration/v1/requests", body=big,
                                                                    scope="requests:write", idem="synthetic_idem_0020"),
                                 "Idempotency-Key": "synthetic_idem_0020"},
                        environ_overrides={"RAW_URI": "/integration/v1/requests"})
    assert r.status_code == 400
    bad = call(env, "POST", "/integration/v1/requests", body=b'{"a":1,"a":2}', scope="requests:write",
               idem="synthetic_idem_0021")
    assert bad.status_code == 400
    empty = call(env, "POST", "/integration/v1/requests", body=b"", scope="requests:write", idem="synthetic_idem_0022")
    assert empty.status_code == 400


def test_truncated_wsgi_body_returns_safe_400(env):
    path = "/integration/v1/requests"
    idem = "synthetic_truncated_001"
    auth = token(env.keys["viewer"], "v1", method="POST", target=path, body=b"{}",
                 scope="requests:write", idem=idem)
    response = env.client.post(path, data=b"{}", content_type="application/json",
                               headers={"Authorization": "Bearer " + auth, "Idempotency-Key": idem},
                               environ_overrides={"RAW_URI": path, "CONTENT_LENGTH": "10"})
    assert response.status_code == 400 and err(response) == "bad_request"


def test_registry_matches_the_frozen_proposal():
    import os
    import re as _re
    path = os.environ.get("MYCELIUM_INTEGRATION_CONTRACT")
    if not path:
        pytest.skip("MYCELIUM_INTEGRATION_CONTRACT not set")
    routes = json.load(open(path))["route_proposal"]
    fill = {"rail": "trending", "type": "movie", "tmdb_id": "603", "season": "1", "person_id": "5",
            "issue_id": "7", "request_id": "7", "opaque_issue_id": "7"}

    def concrete(template):
        return _re.sub(r"<(\w+)>", lambda m: fill[m.group(1)], template)

    seen = set()
    for r in routes:
        base, _, opt = r["path"].partition("[")
        variants = [base] + ([base + opt.rstrip("]")] if opt else [])
        for method in r["method"].split("/"):
            for template in variants:
                concrete_path = concrete(template)
                hit = [x for x in api.ROUTES if method in x.methods and x.pattern.match(concrete_path)]
                with_id = template != base
                is_follows = base.endswith("/follows")
                if (is_follows and ((method == "GET" and with_id) or (method != "GET" and not with_id))) \
                        or (not is_follows and with_id and method != "GET"):
                    assert not hit, (method, concrete_path)  # combinations the closed registry omits
                    continue
                assert len(hit) == 1, (method, concrete_path)
                seen.add(hit[0].name)
    assert {x.name for x in api.ROUTES} - seen == set()
    assert {r.name for r in api.ROUTES if r.role == "service"} == {"readers"}
    assert set(api.ADVERTISED) == set(api.catalog.BASE_OPERATIONS)
    assert set(api.catalog.CATALOG_OPERATIONS) <= {r.handler for r in api.ROUTES}
    assert {r.scope for r in api.ROUTES if r.handler in api.catalog.CATALOG_OPERATIONS} == {"catalog:read"}


def test_real_application_signed_boundary(tmp_path):
    """No collection mocks: real startup, SQLite, auth, CSRF and default resolver/store."""
    import os
    import subprocess
    script = r'''
import json, os, runpy, socket
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import rsa
def no_network(*args, **kwargs):
    raise AssertionError('unexpected network in isolated application test')
socket.socket.connect = no_network
socket.create_connection = no_network
socket.getaddrinfo = no_network
helpers = runpy.run_path('tests/test_integration_api.py')
viewer = rsa.generate_private_key(public_exponent=65537, key_size=2048)
service = rsa.generate_private_key(public_exponent=65537, key_size=2048)
root = Path(os.environ['FIXTURE_DIR'])
for name, key, kid in [('viewer', viewer, 'v1'), ('service', service, 's1')]:
    path = root / (name + '.public.json')
    path.write_text(json.dumps({'keys':[helpers['jwk'](key, kid)]}))
    os.environ['INTEGRATION_' + name.upper() + '_JWKS_PATH'] = str(path)
os.environ.update(INTEGRATION_ENABLED='true', INTEGRATION_ISSUER=helpers['ISSUER'],
                  INTEGRATION_SERVER_GUID=helpers['SERVER'])
import app as real
import db, integration_api as api, request_intents as ri
api.STATE.clock = lambda: helpers['NOW']
real.app.config['TESTING'] = True
rules = [r for r in real.app.url_map.iter_rules() if r.endpoint == api.ENDPOINT]
assert len(rules) == 3 and all(not r.merge_slashes and not r.strict_slashes for r in rules)
assert api.bp in real._csrf._exempt_blueprints
admin = db.create_user('synthetic-admin', 'disabled', role='admin')
uid = db.create_user('synthetic-viewer', 'disabled', role='user')
db.update_user(uid, quota_monthly=5)
db.link_external_identity(uid, 'jellyfin', helpers['SERVER'], helpers['VIEWER'])
receipt = ri.admit(uid, {'media':{'type':'movie','tmdb_id':321},'scope':{'kind':'movie'}},
                   'synthetic_request_0001', {'title':'Synthetic film','imdb_id':'tt1234567'})
c = real.app.test_client()
with c.session_transaction() as session:
    session.update(user_id=admin,user='synthetic-admin',role='admin',auth_source='password')
path = '/integration/v1/me'
assert c.get(path, environ_overrides={'RAW_URI':path}).status_code == 401
def call(path=path, method='GET', body=b'', scope='catalog:read', idem=None, tok=None):
    tok = tok or helpers['token'](viewer,'v1',method=method,target=path,body=body,scope=scope,idem=idem)
    headers = {'Authorization':'Bearer '+tok}
    if method != 'GET': headers.update({'Content-Type':'application/json','Idempotency-Key':idem})
    return c.open(path,method=method,data=body or None,headers=headers,environ_overrides={'RAW_URI':path})
token = helpers['token'](viewer,'v1')
r = call(tok=token)
assert r.status_code == 200 and r.json['api_version'] == '1', r.json
assert r.json['data']['viewer']['id'] == uid and r.json['data']['policy']['request_approval'] == 'required'
assert r.json['data']['policy']['monthly_quota'] == {'limit':5,'used':1,'remaining':4}
assert 'Set-Cookie' not in r.headers and r.headers['Cache-Control'] == 'no-store'
assert call(tok=token).status_code == 401
assert call(tok=helpers['token'](service,'s1')).status_code == 401
path = '/integration/v1/requests/' + str(receipt['request_id'])
r = call(path,scope='requests:read')
assert r.status_code == 200 and r.json['data']['scope'] == {'kind':'movie'}
assert set(r.json['data']['availability'].values()) == {'unknown'}
db.update_user(uid, enabled=0)
assert call().status_code == 403
db.update_user(uid, enabled=1)
assert call().status_code == 200
before = len(db.get_user_requests(user_id=uid))
r = call('/integration/v1/requests','POST',b'{}','requests:write','synthetic_post_0001')
assert r.status_code == 503 and r.json['error']['code'] == 'capability_unqualified'
assert len(db.get_user_requests(user_id=uid)) == before
assert c.post('/ui/api/discover/add',json={'tmdb_id':1,'media_type':'movie','title':'Synthetic'}).status_code == 400
for raw in ['/integration/v1//me','/integration/v1/%2Fme','/integration/v1/me/']:
    assert c.get(raw,environ_overrides={'RAW_URI':raw}).status_code == 400
print('REAL_APPLICATION_BOUNDARY_PASS')
'''
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'TMPDIR')}
    env.update(DB_PATH=str(tmp_path / 'fixture.db'), FIXTURE_DIR=str(tmp_path),
               AUTH_ENABLED='true', AUTH_SESSION_SECRET='synthetic-session-secret-' * 3,
               TORBOX_API_KEY='synthetic', MEDIA_PATH=str(tmp_path / 'media'),
               SPORE_MEDIA_PATH=str(tmp_path / 'spore'), LITE_MODE='true',
               INSECURE_ALLOW_ANON='false', PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-c', script], cwd=__import__('pathlib').Path(__file__).resolve().parents[1],
                            env=env, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0 and 'REAL_APPLICATION_BOUNDARY_PASS' in result.stdout, \
        result.stdout[-1000:] + result.stderr[-2000:]


@pytest.fixture()
def catalog_env(env, monkeypatch):
    import config
    import tmdb_checked as tc
    config.TMDB_API_KEY = "synthetic-catalog-key"
    calls = []

    def fetch(path, params=None):
        from test_integration_catalog_service import detail
        calls.append((path, params))
        if path.startswith("/search/") or path.startswith("/trending/") or path.endswith("/popular"):
            return {"page": (params or {}).get("page", 1), "total_pages": 0, "results": []}
        if path.startswith("/genre/"):
            return {"genres": []}
        if path.startswith("/watch/providers/"):
            return {"results": []}
        if path.startswith("/person/"):
            return {"id": 9, "name": "Person", "combined_credits": {"cast": [], "crew": []}}
        if path == "/tv/1399":
            return {"id": 1399, "seasons": [{"season_number": 0, "episode_count": 0}]}
        if path == "/tv/1399/season/0":
            return {"id": 25, "season_number": 0, "episodes": []}
        if path == "/movie/603":
            return detail()
        raise AssertionError("unexpected catalog fetch")

    monkeypatch.setattr(tc, "fetch", fetch)
    return env, calls


CATALOG_PATHS = [
    ("/integration/v1/catalog/search?q=a", 1),
    ("/integration/v1/catalog/rails/trending?type=all&window=week", 1),
    ("/integration/v1/catalog/movie/603", 1),
    ("/integration/v1/catalog/tv/1399/seasons/0", 2),
    ("/integration/v1/genres?type=tv", 1),
    ("/integration/v1/providers?type=movie", 1),
    ("/integration/v1/genre-tabs", 0),
    ("/integration/v1/people/9", 1),
]


@pytest.mark.parametrize("path,count", CATALOG_PATHS)
def test_catalog_http_envelope_and_fetch_count(catalog_env, path, count):
    env, calls = catalog_env
    before = (len(env.ri.list_for_user(env.uid, 50)), len(env.db.get_watchlist(env.uid)),
              len(env.db.get_favorite_actors(env.uid)))
    response = call(env, path=path)
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert set(body) == {"api_version", "data", "meta"} and body["api_version"] == "1"
    assert set(body["meta"]) == {"correlation_id", "generated_at"}
    assert response.headers["Cache-Control"] == "no-store" and "Set-Cookie" not in response.headers
    assert len(calls) == count
    assert (len(env.ri.list_for_user(env.uid, 50)), len(env.db.get_watchlist(env.uid)),
            len(env.db.get_favorite_actors(env.uid))) == before
    assert not {"availability", "entitlement", "can_play", "in_library"} & body["data"].keys()


@pytest.mark.parametrize("path,count", CATALOG_PATHS)
def test_each_catalog_route_auth_before_fetch(catalog_env, path, count):
    env, calls = catalog_env
    with env.client.session_transaction() as session:
        session["user_id"] = env.uid
    assert call(env, path=path, sign=False).status_code == 401
    assert call(env, path=path, scope="requests:read").status_code == 403
    assert call(env, path=path, role="service", scope="readers:write").status_code == 401
    assert call(env, path=path, raw_env=False).status_code == 503
    assert call(env, path=path, target="/integration/v1/me").status_code == 401
    assert call(env, path=path, body=b"{}").status_code == 400
    env.db.update_user(env.uid, enabled=0)
    assert call(env, path=path).status_code == 403
    env.db.update_user(env.uid, enabled=1)
    assert call(env, path=path, sub="44444444444444448444444444444444").status_code == 403
    assert calls == []
    tok = token(env.keys["viewer"], "v1", target=path)
    headers = {"Authorization": "Bearer " + tok}
    good = env.client.get(path, headers=headers, environ_overrides={"RAW_URI": path})
    assert good.status_code == 200
    replay = env.client.get(path, headers=headers, environ_overrides={"RAW_URI": path})
    assert replay.status_code == 401 and len(calls) == count


@pytest.mark.parametrize("raw", [
    "/integration/v1/catalog/search?q=%C0%AF", "/integration/v1/catalog/search?q=%FF",
    "/integration/v1/catalog/search?q=%41", "/integration/v1/catalog/search?q=%c3%a9",
    "/integration/v1/catalog/search?q=a&q=b", "/integration/v1/catalog/search?type=tv&q=a",
    "/integration/v1/catalog/search?q=a+b", "/integration/v1/catalog/search?q=a&unknown=1",
    "/integration/v1/catalog/movie/603?region=NL", "/integration/v1/providers",
    "/integration/v1/catalog/rails/trending?region=NL", "/integration/v1/catalog/rails/upcoming?type=tv",
])
def test_catalog_http_invalid_queries_never_fetch(catalog_env, raw):
    env, calls = catalog_env
    response = call(env, path="/integration/v1/me", raw=raw)
    assert response.status_code == 400 and calls == []


def test_catalog_uses_only_verified_raw_query_and_region(catalog_env):
    env, calls = catalog_env
    response = call(env, path="/integration/v1/catalog/search?q=wrong", raw="/integration/v1/catalog/search?q=%2541")
    assert response.status_code == 200 and calls[-1][1]["query"] == "%41"
    env.db.update_user(env.uid, region="US")
    response = call(env, path="/integration/v1/catalog/movie/603", headers={"X-Region": "NL"})
    assert response.get_json()["data"]["watch_providers"]["region"] == "US"
    env.db.update_user(env.uid, region="invalid")
    before = len(calls)
    response = call(env, path="/integration/v1/catalog/movie/603")
    assert response.status_code == 503 and err(response) == "preference_invalid" and len(calls) == before
    response = call(env, path="/integration/v1/providers?type=movie")
    assert response.status_code == 400 and err(response) == "region_required" and len(calls) == before
    response = call(env, path="/integration/v1/providers?region=NL&type=movie")
    assert response.status_code == 200 and response.get_json()["data"]["region"] == "NL"


def test_all_catalog_routes_stay_disabled_by_default(catalog_env):
    env, calls = catalog_env
    api.STATE.settings = None
    for path, _count in CATALOG_PATHS:
        response = call(env, path=path)
        assert response.status_code == 404 and err(response) == "not_found"
    assert calls == []


def test_catalog_dynamic_capabilities_and_safe_config_failures(catalog_env, monkeypatch):
    env, calls = catalog_env
    expected = set(api.catalog.BASE_OPERATIONS + api.catalog.CATALOG_OPERATIONS + ("follows.read", "follows.write"))
    capabilities = call(env, path="/integration/v1/capabilities").get_json()["data"]
    me = call(env).get_json()["data"]
    assert set(capabilities["operations"]) == set(me["operations"]) == expected
    assert set(capabilities["incomplete"]) == set(api.OPERATION_NAMES) - expected
    assert "requests.write" in capabilities["incomplete"] and calls == []
    env.db.set_setting("DISCOVER_GENRE_TABS", "broken")
    c = call(env, path="/integration/v1/capabilities").get_json()["data"]
    assert "genre-tabs.read" not in c["operations"] and "catalog.search" in c["operations"]
    r = call(env, path="/integration/v1/genre-tabs")
    assert r.status_code == 503 and r.get_json()["error"]["retryable"] is False

    def failure(key):
        raise OSError("private configuration path")
    monkeypatch.setattr(env.db, "get_setting", failure)
    c = call(env, path="/integration/v1/capabilities").get_json()["data"]
    assert c["operations"] == ["me.read", "follows.read", "requests.read"]
    assert call(env).status_code == 200
    assert call(env, path="/integration/v1/requests", scope="requests:read").status_code == 200
    response = call(env, path="/integration/v1/genre-tabs")
    assert response.status_code == 503 and response.get_json()["error"]["retryable"] is True
    response = call(env, path="/integration/v1/catalog/search?q=a")
    assert response.status_code == 503 and calls == []
    assert "private" not in response.get_data(as_text=True)


def test_catalog_http_outage_missing_size_and_capability_stability(catalog_env, monkeypatch):
    import tmdb_checked as tc
    env, calls = catalog_env
    expected = call(env, path="/integration/v1/capabilities").get_json()["data"]["operations"]
    def outage(*args):
        raise tc.TmdbUnavailable("upstream_unreachable")
    monkeypatch.setattr(tc, "fetch", outage)
    response = call(env, path="/integration/v1/catalog/search?q=a")
    assert response.status_code == 503 and response.get_json()["error"]["retryable"] is True
    assert response.headers["Retry-After"] == "1"
    assert call(env, path="/integration/v1/capabilities").get_json()["data"]["operations"] == expected
    def missing(*args):
        raise tc.TmdbNotFound("not_found")
    monkeypatch.setattr(tc, "fetch", missing)
    assert call(env, path="/integration/v1/catalog/movie/603").status_code == 404
    assert call(env, path="/integration/v1/catalog/search?q=a").status_code == 503
    monkeypatch.setattr(tc, "fetch", lambda *a: {"page": 1, "total_pages": 1, "results": [
        {"id": n, "media_type": "movie", "title": "Synthetic", "overview": "x" * 200000}
        for n in range(1, 6)]})
    response = call(env, path="/integration/v1/catalog/search?q=a")
    assert response.status_code == 503 and not response.get_json()["error"]["retryable"]


FOLLOW_ROUTES = [("GET", "/integration/v1/follows", "watchlist:read"),
                 ("POST", "/integration/v1/follows/9", "watchlist:write"),
                 ("DELETE", "/integration/v1/follows/9", "watchlist:write")]


@pytest.mark.parametrize("method,path,scope", FOLLOW_ROUTES)
def test_signed_follows_boundary_before_metadata(catalog_env, method, path, scope):
    env, calls = catalog_env
    body = b"{}" if method != "GET" else b""
    idem = "synthetic_follow_boundary" if body else None
    kwargs = {"body": body, "scope": scope, "idem": idem}
    with env.client.session_transaction() as session:
        session["user_id"] = env.uid
    assert call(env, method, path, sign=False, **kwargs).status_code == 401
    assert call(env, method, path, **dict(kwargs, scope="catalog:read")).status_code == 403
    assert call(env, method, path, role="service", **dict(kwargs, scope="readers:write")).status_code == 401
    assert call(env, method, path, raw_env=False, **kwargs).status_code == 503
    assert call(env, method, path, target="/integration/v1/me", **kwargs).status_code == 401
    assert call(env, method, path, body_sha256="0" * 64, **kwargs).status_code == 401
    env.db.update_user(env.uid, enabled=0)
    assert call(env, method, path, **kwargs).status_code == 403
    env.db.update_user(env.uid, enabled=1)
    assert calls == []
    signed = token(env.keys["viewer"], "v1", method=method, target=path, **kwargs)
    headers = {"Authorization": "Bearer " + signed}
    if body:
        headers.update({"Idempotency-Key": idem, "Content-Type": "application/json"})
    good = env.client.open(path, method=method, data=body or None, headers=headers, environ_overrides={"RAW_URI": path})
    assert good.status_code == 200, good.get_json()
    assert good.headers["Cache-Control"] == "no-store" and "Set-Cookie" not in good.headers
    replay = env.client.open(path, method=method, data=body or None, headers=headers, environ_overrides={"RAW_URI": path})
    assert replay.status_code == 401 and len(calls) == (1 if method == "POST" else 0)


@pytest.mark.parametrize("body", [b'{"name":"Caller"}', b'{"owner":1}', b'{"profile_url":"https://example.test/a"}',
    b'{"automatic_requests":false}', b'{"person_id":9}', b'[]', b'{"a":1,"a":2}', b'{"a":NaN}'])
def test_follows_exact_body_before_fetch(catalog_env, body):
    env, calls = catalog_env
    response = call(env, "POST", "/integration/v1/follows/9", body=body, scope="watchlist:write",
                    idem="synthetic_follow_body_0001")
    assert response.status_code == 400 and calls == []


@pytest.mark.parametrize("path,status", [("/integration/v1/follows/2147483648", 400),
    ("/integration/v1/follows/0", 404), ("/integration/v1/follows/09", 404),
    ("/integration/v1/follows/9?owner=1", 400), ("/integration/v1/follows/9/", 400)])
def test_follows_exact_path_language_and_id(catalog_env, path, status):
    env, calls = catalog_env
    response = call(env, "POST", path, body=b"{}", scope="watchlist:write", idem="synthetic_follow_path_0001")
    assert response.status_code == status and calls == []


def test_signed_follows_historical_receipts_and_fresh_jti(catalog_env):
    env, calls = catalog_env
    env.db.add_favorite_actor(env.uid, 12, "Legacy automatic favorite")
    assert call(env, path="/integration/v1/follows", scope="watchlist:read").get_json()["data"] == {
        "items": [], "complete": True, "automatic_requests": False}
    def write(method="POST", path="/integration/v1/follows/9", idem="synthetic_follow_history_1", body=b"{}"):
        return call(env, method, path, body=body, scope="watchlist:write", idem=idem)
    original = write()
    assert original.status_code == 200
    assert original.get_json()["data"] == {"person_id": 9, "following": True, "automatic_requests": False}
    assert write(body=b" { } ").get_json()["data"] == original.get_json()["data"]
    assert len(calls) == 1
    assert write(method="DELETE").status_code == 409
    assert write(path="/integration/v1/follows/10").status_code == 409
    assert write(method="DELETE", idem="synthetic_follow_history_2").get_json()["data"]["following"] is False
    assert write().get_json()["data"] == original.get_json()["data"]
    assert call(env, path="/integration/v1/follows", scope="watchlist:read").get_json()["data"]["items"] == []
    assert len(calls) == 1 and len(env.db.get_favorite_actors(env.uid)) == 1
    env.db.update_user(env.uid, enabled=0)
    assert write().status_code == 403


def test_follows_configuration_and_source_failure_do_not_disable_catalog(catalog_env):
    import config
    env, calls = catalog_env
    config.TMDB_API_KEY = ""
    capabilities = call(env, path="/integration/v1/capabilities").get_json()["data"]
    assert "follows.read" in capabilities["operations"] and "follows.write" in capabilities["incomplete"]
    response = call(env, "POST", "/integration/v1/follows/9", body=b"{}", scope="watchlist:write",
                    idem="synthetic_follow_no_key_1")
    assert response.status_code == 503 and calls == []
    assert call(env, "DELETE", "/integration/v1/follows/9", body=b"{}", scope="watchlist:write",
                idem="synthetic_follow_no_key_2").status_code == 200
    config.TMDB_API_KEY = "synthetic-catalog-key"
    with env.db._connect() as conn:
        conn.execute("DROP TABLE integration_saved_people")
    c = call(env, path="/integration/v1/capabilities").get_json()["data"]
    assert not {"follows.read", "follows.write"} & set(c["operations"])
    assert "catalog.search" in c["operations"] and "requests.read" in c["operations"]
    assert call(env).status_code == 200
    response = call(env, path="/integration/v1/follows", scope="watchlist:read")
    assert response.status_code == 503
    assert "integration_saved_people" not in response.get_data(as_text=True)


def test_follows_provider_failure_is_not_success_or_capability_change(catalog_env, monkeypatch):
    import tmdb_checked as tc
    env, _ = catalog_env
    expected = call(env, path="/integration/v1/capabilities").get_json()["data"]["operations"]
    for exception, status, retryable in ((tc.TmdbNotFound("not_found"), 404, False),
                                       (tc.TmdbUnavailable("upstream_unreachable"), 503, True),
                                       (tc.TmdbUnavailable("bad_json"), 503, False)):
        def fail(*args):
            raise exception
        monkeypatch.setattr(tc, "fetch", fail)
        response = call(env, "POST", "/integration/v1/follows/9", body=b"{}", scope="watchlist:write",
                        idem="synthetic_follow_failure_1")
        assert response.status_code == status and response.get_json()["error"]["retryable"] is retryable
        assert call(env, path="/integration/v1/follows", scope="watchlist:read").get_json()["data"]["items"] == []
        assert call(env, path="/integration/v1/capabilities").get_json()["data"]["operations"] == expected
