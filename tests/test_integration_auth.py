"""Receiver primitives with real RSA keys generated here. Synthetic data only."""
import base64
import hashlib
import json
import sqlite3
import sys
import threading

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import integration_auth as ia

ISSUER = "https://signer.example.test/issuer"
SERVER = "11111111111141118111111111111111"
VIEWER_SUB = "22222222222242228222222222222222"
NOW = 1_800_000_000
BODY = b'{"media":{"type":"tv","tmdb_id":123},"scope":{"kind":"episodes","episodes":[{"season":1,"episode":6}]}}'
IDEM = "synthetic_request_0001"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwk(key, kid):
    nums = key.public_key().public_numbers()
    def enc(i):
        return b64(i.to_bytes((i.bit_length() + 7) // 8, "big"))
    return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid, "n": enc(nums.n), "e": enc(nums.e)}


def gen(bits=2048):
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


@pytest.fixture(scope="module")
def keys():
    return {"viewer": gen(), "service": gen(), "other": gen()}


@pytest.fixture()
def settings(keys):
    viewer, service = ia.build_keyrings({"keys": [jwk(keys["viewer"], "v1")]},
                                        {"keys": [jwk(keys["service"], "s1")]})
    return ia.Settings(ISSUER, SERVER, viewer, service)


@pytest.fixture()
def store(tmp_path):
    path = str(tmp_path / "replay.db")
    con = sqlite3.connect(path, isolation_level=None)
    ia.init_schema(con)
    con.close()
    return path


def sign(key, claims=None, header=None, kid="v1", raw_payload=None, mangle=None):
    hdr = {"alg": "RS256", "typ": "JWT", "kid": kid} if header is None else header
    h = b64(json.dumps(hdr, separators=(",", ":")).encode())
    payload = raw_payload if raw_payload is not None else json.dumps(claims, separators=(",", ":")).encode()
    p = b64(payload)
    sig = key.sign((h + "." + p).encode(), padding.PKCS1v15(), hashes.SHA256())
    token = h + "." + p + "." + b64(sig)
    return mangle(token) if mangle else token


def claims(**over):
    c = {"iss": ISSUER, "aud": "mycelium.integration.v1", "sub": VIEWER_SUB, "jf_sid": SERVER,
         "iat": NOW, "nbf": NOW, "exp": NOW + 30, "jti": "jti-" + "a" * 20, "htm": "POST",
         "target": "/integration/v1/requests", "body_sha256": hashlib.sha256(BODY).hexdigest(),
         "scope": "requests:write", "idempotency_key": IDEM}
    c.update(over)
    return {k: v for k, v in c.items() if v is not ...}


USER = {"id": 7, "enabled": 1}


def resolver(user=USER):
    return lambda p, s, sub: user if (p, s, sub) == ("jellyfin", SERVER, VIEWER_SUB) else None


def call(settings, store, token, *, role="viewer", method="POST", target="/integration/v1/requests",
         body=BODY, headers=None, scope="requests:write", query=(), now=NOW, user=USER):
    h = [("Authorization", "Bearer " + token)]
    if method != "GET":
        h += [("Content-Type", "application/json"), ("Idempotency-Key", IDEM)]
    h += headers or []
    return ia.verify_request(role=role, method=method, raw_target=target, body=body, headers=h,
                             required_scope=scope, allowed_query_keys=frozenset(query),
                             settings=settings, replay=ia.ReplayStore(path=store), now=now,
                             resolve_user=resolver(user))


def err(fn, status=None, code=None):
    with pytest.raises(ia.IntegrationError) as exc:
        fn()
    if status is not None:
        assert exc.value.status == status
    if code is not None:
        assert exc.value.code == code
    return exc.value


def test_valid_viewer_request(settings, store, keys):
    r = call(settings, store, sign(keys["viewer"], claims()))
    assert r.user["id"] == 7 and r.body["media"]["tmdb_id"] == 123 and r.idempotency_key == IDEM
    assert r.grants_unqualified is None


# ── keyrings ────────────────────────────────────────────────────────────────

def test_ring_confusion_both_directions(settings, store, keys):
    err(lambda: call(settings, store, sign(keys["service"], claims(), kid="s1")), 401)
    svc = claims(aud="mycelium.playback.v1", sub=SERVER, scope="readers:write")
    err(lambda: call(settings, store, sign(keys["viewer"], svc, kid="v1"), role="service",
                     scope="readers:write"), 401)
    r = call(settings, store, sign(keys["service"], svc, kid="s1"), role="service", scope="readers:write")
    assert r.user is None


def test_viewer_key_cannot_sign_service_even_with_service_kid(settings, store, keys):
    svc = claims(aud="mycelium.playback.v1", sub=SERVER, scope="readers:write")
    err(lambda: call(settings, store, sign(keys["viewer"], svc, kid="s1"), role="service",
                     scope="readers:write"), 401)


def test_overlap_rejected_under_different_kid(keys):
    err_ok = pytest.raises(ValueError)
    with err_ok:
        ia.build_keyrings({"keys": [jwk(keys["viewer"], "a")]}, {"keys": [jwk(keys["viewer"], "b")]})
    with pytest.raises(ValueError):
        ia.keyring_from_jwks({"keys": [jwk(keys["viewer"], "a"), jwk(keys["viewer"], "b")]}, "viewer")


def test_unknown_kid_and_bad_keys(settings, store, keys):
    err(lambda: call(settings, store, sign(keys["viewer"], claims(), kid="nope")), 401)
    k1024 = gen(1024)
    with pytest.raises(ValueError):
        ia.keyring_from_jwks({"keys": [jwk(k1024, "small")]}, "viewer")
    good = jwk(keys["viewer"], "v1")
    for bad in (dict(good, e=b64((3).to_bytes(1, "big"))), dict(good, e=b64((65538).to_bytes(3, "big"))),
                dict(good, d="AQAB"), dict(good, n=good["n"] + "="), dict(good, kty="EC"),
                dict(good, n=b64(b"\x00" + bytes.fromhex("ff" * 256))), dict(good, extra=1)):
        with pytest.raises(ValueError):
            ia.keyring_from_jwks({"keys": [bad]}, "viewer")
    with pytest.raises(ValueError):
        ia.keyring_from_jwks({"keys": [good, dict(good)]}, "viewer")  # duplicate kid


def test_jwks_loaded_from_local_file_only(tmp_path, keys):
    path = tmp_path / "k.json"
    path.write_text(json.dumps({"keys": [jwk(keys["viewer"], "v1")]}))
    assert "v1" in ia.keyring_from_jwks(ia.load_jwks_file(str(path)), "viewer").keys
    with pytest.raises(OSError):
        ia.load_jwks_file("https://example.test/jwks.json")


# ── token shape ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["jku", "jwk", "x5u", "x5c", "crit", "cty"])
def test_forbidden_header_fields(settings, store, keys, name):
    hdr = {"alg": "RS256", "typ": "JWT", "kid": "v1", name: "x"}
    err(lambda: call(settings, store, sign(keys["viewer"], claims(), header=hdr)), 401)


@pytest.mark.parametrize("hdr", [{"alg": "none", "typ": "JWT", "kid": "v1"},
                                 {"alg": "HS256", "typ": "JWT", "kid": "v1"},
                                 {"alg": "RS256", "typ": "jwt", "kid": "v1"},
                                 {"alg": "RS256", "typ": "JWT"}])
def test_header_algorithm_and_type(settings, store, keys, hdr):
    err(lambda: call(settings, store, sign(keys["viewer"], claims(), header=hdr)), 401)


def test_duplicate_json_keys_and_base64(settings, store, keys):
    dup = b'{"iss":"x","iss":"' + ISSUER.encode() + b'"}'
    err(lambda: call(settings, store, sign(keys["viewer"], raw_payload=dup)), 401)
    good = sign(keys["viewer"], claims())
    err(lambda: call(settings, store, good + "."), 401)
    err(lambda: call(settings, store, good + "AAAA"), 401)  # not canonical / bad signature
    h, p, s = good.split(".")
    err(lambda: call(settings, store, h + "=." + p + "." + s), 401)
    err(lambda: call(settings, store, ".".join([h, p])), 401)
    err(lambda: call(settings, store, "x" * 7000), 401)


def test_authorization_header_forms(settings, store, keys):
    tok = sign(keys["viewer"], claims())
    for hdr in ("bearer " + tok, "Bearer  " + tok, "Basic " + tok, "Bearer " + tok + " x"):
        with pytest.raises(ia.IntegrationError):
            ia.verify_request(role="viewer", method="POST", raw_target="/integration/v1/requests", body=BODY,
                              headers=[("Authorization", hdr), ("Content-Type", "application/json"),
                                       ("Idempotency-Key", IDEM)], required_scope="requests:write",
                              allowed_query_keys=frozenset(), settings=settings,
                              replay=ia.ReplayStore(path=store), now=NOW, resolve_user=resolver())
    err(lambda: call(settings, store, tok, headers=[("authorization", "Bearer " + tok)]), 400)


# ── claims ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("over", [
    {"iss": "https://signer.example.test/other"}, {"aud": ["mycelium.integration.v1"]},
    {"aud": "mycelium.playback.v1"}, {"iat": NOW + 6, "nbf": NOW + 6, "exp": NOW + 30},
    {"exp": NOW - 6}, {"exp": NOW + 61}, {"nbf": NOW - 1}, {"exp": NOW}, {"iat": "1800000000"},
    {"iat": float(NOW)}, {"iat": True}, {"jti": "short"}, {"jti": "x" * 129}, {"jti": "has space " + "a" * 12},
    {"jti": "ünicode-" + "a" * 12}, {"sub": SERVER}, {"sub": "ABCDEF" + "0" * 26}, {"sub": "0" * 32},
    {"jf_sid": "33333333333343338333333333333333"}, {"htm": "post"}, {"htm": "DELETE"},
    {"body_sha256": "0" * 64}, {"body_sha256": hashlib.sha256(BODY).hexdigest().upper()},
    {"idempotency_key": "another_key_value_0002"}, {"idempotency_key": ...}, {"extra": 1},
])
def test_claim_rejections(settings, store, keys, over):
    err(lambda: call(settings, store, sign(keys["viewer"], claims(**over))), 401)


def test_time_edges_accepted(settings, store, keys):
    call(settings, store, sign(keys["viewer"], claims(jti="edge-a-" + "a" * 12)), now=NOW - 5)
    call(settings, store, sign(keys["viewer"], claims(jti="edge-b-" + "a" * 12)), now=NOW + 35)
    err(lambda: call(settings, store, sign(keys["viewer"], claims(jti="edge-c-" + "a" * 12)), now=NOW + 36), 401)


def test_scope_rules(settings, store, keys):
    for scope in ("requests:read", "requests:write requests:write", "requests:write readers:write",
                  "requests:write  catalog:read", "admin", ""):
        err(lambda: call(settings, store, sign(keys["viewer"], claims(scope=scope))), 403, "scope")
    r = call(settings, store, sign(keys["viewer"], claims(scope="catalog:read requests:write")))
    assert r.scopes == {"catalog:read", "requests:write"}
    err(lambda: call(settings, store, sign(keys["viewer"], claims()), scope="readers:write"), 500)


def test_actor_resolution(settings, store, keys):
    err(lambda: call(settings, store, sign(keys["viewer"], claims()), user=None), 403, "unlinked")
    err(lambda: call(settings, store, sign(keys["viewer"], claims()), user={"id": 7, "enabled": 0}),
        403, "disabled")
    other = "44444444444444448444444444444444"
    err(lambda: call(settings, store, sign(keys["viewer"], claims(sub=other))), 403, "unlinked")


def test_role_claims_in_token_are_ignored_and_unknown_claims_rejected(settings, store, keys):
    err(lambda: call(settings, store, sign(keys["viewer"], claims(role="admin"))), 401)


# ── binding ─────────────────────────────────────────────────────────────────

def test_method_target_body_binding(settings, store, keys):
    tok = sign(keys["viewer"], claims())
    err(lambda: call(settings, store, tok, method="DELETE"), 401)
    err(lambda: call(settings, store, tok, target="/integration/v1/issues"), 401)
    err(lambda: call(settings, store, tok, body=BODY + b" "), 401)
    err(lambda: call(settings, store, tok, target="/integration/v1/requests?"), 400)
    err(lambda: call(settings, store, tok, target="/integration/v1//requests"), 400)


def test_get_catalog_query_binding(settings, store, keys):
    target = "/integration/v1/catalog/search?page=1&q=a%20b&type=tv"
    c = claims(htm="GET", target=target, body_sha256=hashlib.sha256(b"").hexdigest(),
               scope="catalog:read", idempotency_key=...)
    q = ("page", "q", "type")
    r = call(settings, store, sign(keys["viewer"], c), method="GET", target=target, body=b"",
             scope="catalog:read", query=q)
    assert r.target == target and r.idempotency_key is None
    unsorted = "/integration/v1/catalog/search?q=a%20b&page=1&type=tv"
    c2 = dict(c, target=unsorted, jti="get-2-" + "a" * 14)
    err(lambda: call(settings, store, sign(keys["viewer"], c2), method="GET", target=unsorted, body=b"",
                     scope="catalog:read", query=q), 400)
    err(lambda: call(settings, store, sign(keys["viewer"], dict(c, jti="get-3-" + "a" * 14)), method="GET",
                     target=target, body=b"", scope="catalog:read", query=("page", "q")), 400)
    err(lambda: call(settings, store, sign(keys["viewer"], dict(c, jti="get-4-" + "a" * 14)), method="GET",
                     target=target, body=b"", scope="catalog:read", query=q,
                     headers=[("Idempotency-Key", IDEM)]), 400)
    c5 = dict(c, jti="get-5-" + "a" * 14, idempotency_key=IDEM)
    err(lambda: call(settings, store, sign(keys["viewer"], c5), method="GET", target=target, body=b"",
                     scope="catalog:read", query=q), 400)


def test_idempotency_header_rules(settings, store, keys):
    tok = sign(keys["viewer"], claims())
    base = [("Content-Type", "application/json")]
    def run(extra, token=tok):
        h = [("Authorization", "Bearer " + token)] + base + extra
        return ia.verify_request(role="viewer", method="POST", raw_target="/integration/v1/requests",
                                 body=BODY, headers=h, required_scope="requests:write",
                                 allowed_query_keys=frozenset(), settings=settings,
                                 replay=ia.ReplayStore(path=store), now=NOW, resolve_user=resolver())
    err(lambda: run([]), 401)
    err(lambda: run([("Idempotency-Key", "short")]), 401)
    err(lambda: run([("Idempotency-Key", IDEM + ",x")]), 401)
    err(lambda: run([("Idempotency-Key", IDEM), ("idempotency-key", IDEM)]), 400)
    err(lambda: run([("Idempotency-Key", IDEM.upper())]), 401)


@pytest.mark.parametrize("extra,status", [
    ([("Content-Type", "text/plain")], 400), ([("Content-Type", "application/json; charset=utf-16")], 400),
    ([("Content-Encoding", "gzip")], 400), ([("Transfer-Encoding", "chunked")], 400),
    ([("Content-Length", "1")], 400), ([("Content-Type", "application/json, text/plain")], 400),
])
def test_content_headers(settings, store, keys, extra, status):
    tok = sign(keys["viewer"], claims())
    h = [("Authorization", "Bearer " + tok), ("Idempotency-Key", IDEM)]
    if not any(k == "Content-Type" for k, _ in extra):
        h.append(("Content-Type", "application/json"))
    err(lambda: ia.verify_request(role="viewer", method="POST", raw_target="/integration/v1/requests",
                                  body=BODY, headers=h + extra, required_scope="requests:write",
                                  allowed_query_keys=frozenset(), settings=settings,
                                  replay=ia.ReplayStore(path=store), now=NOW, resolve_user=resolver()), status)


def test_charset_utf8_content_type_is_accepted(settings, store, keys):
    tok = sign(keys["viewer"], claims())
    ia.verify_request(role="viewer", method="POST", raw_target="/integration/v1/requests", body=BODY,
                      headers=[("Authorization", "Bearer " + tok), ("Idempotency-Key", IDEM),
                               ("Content-Type", "application/json; charset=utf-8"),
                               ("Content-Length", str(len(BODY)))],
                      required_scope="requests:write", allowed_query_keys=frozenset(), settings=settings,
                      replay=ia.ReplayStore(path=store), now=NOW, resolve_user=resolver())


@pytest.mark.parametrize("raw", [
    b'\xef\xbb\xbf{"a":1}', b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":-0}', b'{"a":-0.0}',
    b'{"a":1e999}', b'{"a":"\\ud800"}', b'{"\\udc00":1}', b'[1]', b'"x"', b'1', b'', b'{"a":1} x',
    b'{"a":' + b'[' * 30 + b']' * 30 + b'}', b'\xff', b'{"a":1}' + b' ' * 70000, b"{'a':1}",
])
def test_strict_json_rejections(raw):
    err(lambda: ia.strict_json_object(raw), 400)


def test_strict_json_accepts_plain_object():
    assert ia.strict_json_object(b'{"a":[1,2.5,{"b":null}],"c":"\xc3\xa9"}')["c"] == "é"
    assert ia.strict_json_object(b'{"a":' + b'[' * 23 + b']' * 23 + b'}')


def test_body_is_parsed_after_signature_and_never_burns_jti(settings, store, keys):
    bad = b'{"a":1,"a":2}'
    tok = sign(keys["viewer"], claims(body_sha256=hashlib.sha256(bad).hexdigest(), jti="burn-" + "a" * 14))
    err(lambda: call(settings, store, tok, body=bad), 400)
    err(lambda: call(settings, store, tok, scope="requests:read"), 403)
    tok2 = sign(keys["viewer"], claims(jti="burn2-" + "a" * 14))
    err(lambda: call(settings, store, tok2, scope="requests:read"), 403)
    assert call(settings, store, tok2).jti == "burn2-" + "a" * 14  # rejected attempt did not consume


# ── canonical target ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "/integration/v1/../me", "/integration/v1/./me", "/integration/v1//me", "/integration/v1/me/",
    "/integration/v1/%2E/me", "/integration/v1/%2fme", "/integration/v1/me;a=b", "/integration/v1/me#x",
    "/integration/v1/me?", "/integration/v1/me?q", "/integration/v1/me?q=a+b", "/integration/v1/me?q=%4",
    "/integration/v1/me?q=%C0%AF", "/integration/v1/me?q=%ED%A0%80", "/integration/v1/me?q=%00",
    "/integration/v1/me?q=%7F", "/integration/v1/me?a=1&a=2", "/integration/v1/me?a=1&%61=2",
    "/integration/v1/me?=x", "/integration/v1/me?q=x&&a=2", "/other/v1/me", "/integration/v1/m e",
    "/integration/v1/me?q=é", "", "/integration/v1/me?q=x?y=z",
])
def test_canonical_rejections(raw):
    err(lambda: ia.canonical_target(raw, strict_raw=False), 400)


def test_canonical_strictness_and_literals():
    assert ia.canonical_target("/integration/v1/s?q=%2541") == "/integration/v1/s?q=%2541"
    assert ia.canonical_target("/integration/v1/s?q=e%CC%81") == "/integration/v1/s?q=e%CC%81"
    assert ia.canonical_target("/integration/v1/s?q=") == "/integration/v1/s?q="
    assert ia.canonical_target("/integration/v1/s?b=2&a=1", strict_raw=False) == "/integration/v1/s?a=1&b=2"
    assert ia.canonical_target("/integration/v1/s?%71=%41", strict_raw=False) == "/integration/v1/s?q=A"
    for raw in ("/integration/v1/s?b=2&a=1", "/integration/v1/s?q=%c3%a9", "/integration/v1/s?%71=A"):
        err(lambda raw=raw: ia.canonical_target(raw, strict_raw=True), 400)
    err(lambda: ia.canonical_target("/integration/v1/s?x=1", ["q"]), 400)
    assert ia.canonical_target(b"/integration/v1/me") == "/integration/v1/me"


# ── replay ──────────────────────────────────────────────────────────────────

def test_replay_rejected_and_survives_restart(settings, store, keys):
    tok = sign(keys["viewer"], claims())
    call(settings, store, tok)
    err(lambda: call(settings, store, tok), 401, "replay")
    err(lambda: call(settings, store, tok), 401, "replay")  # new ReplayStore object, same file


def test_pruned_assertion_cannot_return_after_expiry(settings, store, keys):
    tok = sign(keys["viewer"], claims(jti="prune-" + "a" * 14))
    call(settings, store, tok)
    other = sign(keys["viewer"], claims(jti="prune2-" + "a" * 14, iat=NOW + 100, nbf=NOW + 100, exp=NOW + 130))
    call(settings, store, other, now=NOW + 100)  # prunes the first (expired) row
    err(lambda: call(settings, store, tok, now=NOW + 100), 401)  # expired by time, not by store


def test_concurrent_replay_has_one_winner(settings, store, keys):
    tok = sign(keys["viewer"], claims(jti="race-" + "a" * 15))
    results = []
    gate = threading.Barrier(12)

    def worker():
        gate.wait()
        try:
            call(settings, store, tok)
            results.append("ok")
        except ia.IntegrationError as exc:
            results.append(exc.status)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(results) == 12
    assert results.count("ok") == 1
    assert all(r in (401, 503) for r in results if r != "ok")


def test_locked_store_fails_closed(settings, store, keys):
    holder = sqlite3.connect(store, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        err(lambda: call(settings, store, sign(keys["viewer"], claims())), 503)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    call(settings, store, sign(keys["viewer"], claims()))


def test_full_store_and_unavailable_store_fail_closed(store):
    rs = ia.ReplayStore(path=store, cap=2)
    rs.consume(ISSUER, "a" * 16, NOW + 30, NOW)
    rs.consume(ISSUER, "b" * 16, NOW + 30, NOW)
    err(lambda: rs.consume(ISSUER, "c" * 16, NOW + 30, NOW), 503, "replay_store_full")
    err(lambda: rs.consume(ISSUER, "a" * 16, NOW + 30, NOW), 503)  # full wins over replay: still fail closed
    broken = ia.ReplayStore(path="/nonexistent-dir/x.db")
    err(lambda: broken.consume(ISSUER, "d" * 16, NOW + 30, NOW), 503)
    empty = sqlite3.connect(":memory:", isolation_level=None)  # tables missing
    err(lambda: ia.ReplayStore(connect=lambda: empty).consume(ISSUER, "e" * 16, NOW + 30, NOW), 503)


def test_clock_rollback_fails_closed(store):
    rs = ia.ReplayStore(path=store)
    rs.consume(ISSUER, "a" * 16, NOW + 30, NOW + 1000)
    rs.consume(ISSUER, "b" * 16, NOW + 30, NOW + 996)  # inside skew
    err(lambda: rs.consume(ISSUER, "c" * 16, NOW + 1030, NOW + 900), 503, "clock_unsafe")


# ── settings / config ───────────────────────────────────────────────────────

def test_settings_validation(keys):
    v, s = ia.build_keyrings({"keys": [jwk(keys["viewer"], "v1")]}, {"keys": [jwk(keys["service"], "s1")]})
    for issuer in ("http://x.test", "https://", "https://a b.test", ""):
        with pytest.raises(ValueError):
            ia.Settings(issuer, SERVER, v, s)
    for guid in ("0" * 32, "ABCDEF" + "1" * 26, "short"):
        with pytest.raises(ValueError):
            ia.Settings(ISSUER, guid, v, s)


def test_disabled_by_default_and_no_network_imports():
    class Cfg:
        INTEGRATION_ENABLED = False
    assert ia.settings_from_config(Cfg) is None
    import ast
    tree = ast.parse(open(ia.__file__).read())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add(n.module or "")
    banned = {"requests", "urllib.request", "urllib3", "http", "http.client", "socket", "ssl",
              "httpx", "flask", "aiohttp"}
    assert not {m for m in mods if m in banned or m.split(".")[0] in {"requests", "httpx", "socket"}}


# ── B1 receipt reuse through verified bodies ────────────────────────────────

def test_fresh_jti_same_key_same_body_reuses_receipt_and_new_body_conflicts(settings, store, keys,
                                                                            monkeypatch, tmp_path):
    for m in ("db", "settings", "config", "request_intents"):
        sys.modules.pop(m, None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    import config
    config.DB_PATH = str(tmp_path / "t.db")
    import db
    db.init()
    import request_intents as ri
    uid = db.create_user("synthetic-viewer", "scrypt$x", role="user")
    md = {"title": "Synthetic Show", "imdb_id": "tt1234567"}
    r1 = call(settings, store, sign(keys["viewer"], claims(jti="rcpt-1-" + "a" * 12)))
    first = ri.admit(uid, r1.body, r1.idempotency_key, md, raw_body=BODY)
    r2 = call(settings, store, sign(keys["viewer"], claims(jti="rcpt-2-" + "a" * 12)))
    again = ri.admit(uid, r2.body, r2.idempotency_key, md, raw_body=BODY)
    assert again["request_id"] == first["request_id"]
    changed = json.dumps({"media": {"type": "tv", "tmdb_id": 123},
                          "scope": {"kind": "episodes", "episodes": [{"season": 1, "episode": 7}]}}).encode()
    r3 = call(settings, store, sign(keys["viewer"], claims(jti="rcpt-3-" + "a" * 12,
                                                          body_sha256=hashlib.sha256(changed).hexdigest())),
              body=changed)
    with pytest.raises(ri.IntentError) as exc:
        ri.admit(uid, r3.body, r3.idempotency_key, md, raw_body=changed)
    assert exc.value.status == 409


# ── tightening follow-up ────────────────────────────────────────────────────

def _post(settings, store, tok, extra, body=BODY, with_ct=True):
    h = [("Authorization", "Bearer " + tok), ("Idempotency-Key", IDEM)]
    if with_ct:
        h.append(("Content-Type", "application/json"))
    return ia.verify_request(role="viewer", method="POST", raw_target="/integration/v1/requests", body=body,
                             headers=h + extra, required_scope="requests:write",
                             allowed_query_keys=frozenset(), settings=settings,
                             replay=ia.ReplayStore(path=store), now=NOW, resolve_user=resolver())


@pytest.mark.parametrize("ctype", [
    "applic ation/json", "application/json;\tcharset=utf-8", "application/json; charset=\"utf-8\"",
    "application/json; charset=utf-8; x=y", "application/json; charset=UTF-16", "application/jsonx",
    "application/json ", " application/json", "application/json;  charset=utf-8", "application/json,",
    "application/json; boundary=x", "text/json", "",
])
def test_content_type_is_a_precise_match(settings, store, keys, ctype):
    tok = sign(keys["viewer"], claims())
    h = [("Authorization", "Bearer " + tok), ("Idempotency-Key", IDEM), ("Content-Type", ctype)]
    err(lambda: ia.verify_request(role="viewer", method="POST", raw_target="/integration/v1/requests",
                                  body=BODY, headers=h, required_scope="requests:write",
                                  allowed_query_keys=frozenset(), settings=settings,
                                  replay=ia.ReplayStore(path=store), now=NOW, resolve_user=resolver()), 400)


@pytest.mark.parametrize("ctype", ["application/json", "Application/JSON; charset=UTF-8",
                                   "application/json;charset=utf-8"])
def test_content_type_accepted_forms(settings, store, keys, ctype):
    tok = sign(keys["viewer"], claims(jti="ct-" + "a" * 16))
    h = [("Authorization", "Bearer " + tok), ("Idempotency-Key", IDEM), ("Content-Type", ctype)]
    ia.verify_request(role="viewer", method="POST", raw_target="/integration/v1/requests", body=BODY,
                      headers=h, required_scope="requests:write", allowed_query_keys=frozenset(),
                      settings=settings, replay=ia.ReplayStore(path=store), now=NOW, resolve_user=resolver())


def test_every_write_needs_a_nonempty_json_object_body(settings, store, keys):
    empty = hashlib.sha256(b"").hexdigest()
    for method in ("POST", "DELETE"):
        c = claims(htm=method, body_sha256=empty, jti=f"nb-{method}-" + "a" * 10)
        h = [("Authorization", "Bearer " + sign(keys["viewer"], c)), ("Idempotency-Key", IDEM),
             ("Content-Type", "application/json")]
        err(lambda: ia.verify_request(role="viewer", method=method, raw_target="/integration/v1/requests",
                                      body=b"", headers=h, required_scope="requests:write",
                                      allowed_query_keys=frozenset(), settings=settings,
                                      replay=ia.ReplayStore(path=store), now=NOW,
                                      resolve_user=resolver()), 400)
    assert "body_required" not in ia.verify_request.__code__.co_varnames[:ia.verify_request.__code__.co_argcount + ia.verify_request.__code__.co_kwonlyargcount]
    tiny = b"{}"
    tok = sign(keys["viewer"], claims(body_sha256=hashlib.sha256(tiny).hexdigest(), jti="tiny-" + "a" * 14))
    assert _post(settings, store, tok, [], body=tiny).body == {}
    ctless = sign(keys["viewer"], claims(jti="noct-" + "a" * 14))
    err(lambda: _post(settings, store, ctless, [], with_ct=False), 400)
    arr = b"[]"
    tok = sign(keys["viewer"], claims(body_sha256=hashlib.sha256(arr).hexdigest(), jti="arr-" + "a" * 15))
    err(lambda: _post(settings, store, tok, [], body=arr), 400)


@pytest.mark.parametrize("value", ["9" * 5000, "007", "+5", "1,2", " 5", "5 ", "-1", "65537", "0x10",
                                   "１２", "1.0", ""])
def test_content_length_is_canonical_and_bounded(settings, store, keys, value):
    tok = sign(keys["viewer"], claims())
    err(lambda: _post(settings, store, tok, [("Content-Length", value)]), 400)
    err(lambda: _post(settings, store, tok, [("Content-Length", 5)]), 400)


def test_oversized_body_rejected_before_anything_else(settings, store, keys):
    big = b"{" + b" " * 65536 + b"}"
    tok = sign(keys["viewer"], claims(body_sha256=hashlib.sha256(big).hexdigest()))
    err(lambda: _post(settings, store, tok, [], body=big), 400)


@pytest.mark.parametrize("issuer", [
    "https:///x", "https://user@host.test", "https://user:pw@host.test", "https://host.test#f",
    "https://host.test?q=1", "https://host.test:99999", "https://host.test:0", "https://host.test:abc",
    "https://", "http://host.test", "HTTPS://host.test", "https://host .test", "https://host.test\\x",
    "https://" + "a" * 520 + ".test", "https://h\u00e9st.test", "https://host.test\n",
])
def test_issuer_must_be_a_strict_https_authority(keys, issuer):
    v, s = ia.build_keyrings({"keys": [jwk(keys["viewer"], "v1")]}, {"keys": [jwk(keys["service"], "s1")]})
    with pytest.raises(ValueError):
        ia.Settings(issuer, SERVER, v, s)
    ia.Settings("https://host.test:8443/path", SERVER, v, s)


def test_settings_recompute_roles_and_key_overlap(keys):
    v, s = ia.build_keyrings({"keys": [jwk(keys["viewer"], "v1")]}, {"keys": [jwk(keys["service"], "s1")]})
    with pytest.raises(ValueError):
        ia.Settings(ISSUER, SERVER, s, v)  # roles swapped
    forged = ia.Keyring(role="service", keys={"x1": keys["viewer"].public_key()}, fingerprints=frozenset())
    with pytest.raises(ValueError):
        ia.Settings(ISSUER, SERVER, v, forged)  # empty stored fingerprints cannot hide shared material
    small = ia.Keyring(role="service", keys={"x1": gen(1024).public_key()})
    with pytest.raises(ValueError):
        ia.Settings(ISSUER, SERVER, v, small)
    badkid = ia.Keyring(role="service", keys={"bad kid": keys["service"].public_key()})
    with pytest.raises(ValueError):
        ia.Settings(ISSUER, SERVER, v, badkid)


def test_kid_pattern_and_ring_bounds(keys):
    good = jwk(keys["viewer"], "A" * 96)
    ia.keyring_from_jwks({"keys": [good]}, "viewer")
    for kid in ("A" * 97, "has space", "dot.kid", "kid/slash", "", "ünï"):
        with pytest.raises(ValueError):
            ia.keyring_from_jwks({"keys": [dict(good, kid=kid)]}, "viewer")
    many = [jwk(gen(), f"k{i}") for i in range(9)]
    with pytest.raises(ValueError):
        ia.keyring_from_jwks({"keys": many}, "viewer")
    ia.keyring_from_jwks({"keys": many[:8]}, "viewer")


def test_jwks_file_read_is_bounded(tmp_path, keys):
    path = tmp_path / "big.json"
    path.write_bytes(b'{"keys":[]}' + b" " * (ia.MAX_JWKS_BYTES + 1))
    with pytest.raises(ValueError):
        ia.load_jwks_file(str(path))


@pytest.mark.parametrize("raw", [b'{"a":' + b"9" * 400 + b"}", b'{"a":-' + b"9" * 400 + b"}",
                                 b'{"a":1' + b"0" * 309 + b"}", b'{"a":' + b"9" * 5000 + b"}"])
def test_numbers_must_fit_a_finite_double(raw):
    err(lambda: ia.strict_json_object(raw), 400)


def test_large_but_finite_integers_are_still_parsed():
    assert ia.strict_json_object(b'{"a":1' + b"0" * 300 + b"}")["a"] == 10 ** 300


def test_replay_store_constructor_validation(store):
    for kwargs in ({"connect": lambda: None, "path": store}, {"path": store, "cap": 0},
                   {"path": store, "cap": -1}, {"path": store, "cap": True}, {"path": store, "cap": 1.5},
                   {"path": ""}, {"connect": "notcallable"}, {"path": store, "cap": 10 ** 9}):
        with pytest.raises(ValueError):
            ia.ReplayStore(**kwargs)


def _grants(**over):
    g = {"version": "1", "assertion_jti": claims()["jti"], "observed_at": NOW, "expires_at": NOW + 30,
         "source_state": {"publisher_epoch": "synthetic-epoch", "policy_revision": "1", "catalog_revision": "2"},
         "lookups": [{"k": i} for i in range(2)]}
    g.update(over)
    return g


def grant_claims(g):
    raw = json.dumps(g, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return {"library_grants": g, "library_grants_count": len(g["lookups"]),
            "library_grants_sha256": hashlib.sha256(raw).hexdigest()}


def test_grant_object_is_carried_unqualified(settings, store, keys):
    g = _grants()
    tok = sign(keys["viewer"], claims(**grant_claims(g)))
    r = call(settings, store, tok)
    assert r.grants_unqualified == g
    g2 = _grants(lookups=[], assertion_jti="grant2-" + "a" * 11)
    plain = call(settings, store, sign(keys["viewer"], claims(**grant_claims(g2), jti=g2["assertion_jti"])))
    assert plain.grants_unqualified["lookups"] == []


@pytest.mark.parametrize("over", [
    {"library_grants": [{"old": "format"}]}, {"library_grants": "x"},
    {"library_grants": _grants(lookups=[{}] * 5), "library_grants_count": 5},
    {"library_grants": _grants(), "library_grants_count": 3},
    {"library_grants": _grants(), "library_grants_count": True},
    {"library_grants": _grants(), "library_grants_count": "2"},
    {"library_grants": _grants(), "library_grants_sha256": "A" * 64},
    {"library_grants": _grants(), "library_grants_sha256": "a" * 63},
    {"library_grants": _grants(), "library_grants_sha256": 5},
    {"library_grants": _grants(extra=1)}, {"library_grants": {"version": 1}},
    {"library_grants": _grants(lookups="x")},
    {"library_grants_count": 0}, {"library_grants_sha256": "a" * 64},
])
def test_grant_shape_rejections(settings, store, keys, over):
    err(lambda: call(settings, store, sign(keys["viewer"], claims(**over))), 401)


@pytest.mark.parametrize("over", [
    {"version": 1}, {"assertion_jti": "other-assertion-12345"},
    {"observed_at": True}, {"expires_at": True}, {"observed_at": NOW + 6},
    {"observed_at": NOW - 31}, {"expires_at": NOW}, {"expires_at": NOW + 31},
    {"source_state": {}}, {"source_state": {"publisher_epoch": "x", "policy_revision": 1, "catalog_revision": "2"}},
    {"source_state": {"publisher_epoch": "", "policy_revision": "1", "catalog_revision": "2"}},
    {"lookups": [{"x": None}]}, {"lookups": [{"x": 1.5}]},
    {"lookups": [{"x": "\u00e9"}]}, {"lookups": [{"x": "\n"}]},
])
def test_grant_binding_freshness_and_canonical_values(settings, store, keys, over):
    g = _grants(**over)
    err(lambda: call(settings, store, sign(keys["viewer"], claims(**grant_claims(g)))), 401)


@pytest.mark.parametrize("mutation", ["missing_count", "missing_hash", "wrong_hash", "tampered"])
def test_grant_integrity_rejected_without_consuming_token(settings, store, keys, mutation):
    proof = grant_claims(_grants())
    if mutation == "missing_count":
        proof.pop("library_grants_count")
    elif mutation == "missing_hash":
        proof.pop("library_grants_sha256")
    elif mutation == "wrong_hash":
        proof["library_grants_sha256"] = "a" * 64
    else:
        proof["library_grants"]["lookups"][0]["k"] = 99
    err(lambda: call(settings, store, sign(keys["viewer"], claims(**proof))), 401)
    assert call(settings, store, sign(keys["viewer"], claims(**grant_claims(_grants())))).user == USER


# ── default store and default resolver on a real temporary database ─────────

@pytest.fixture()
def real_db(monkeypatch, tmp_path):
    for m in ("db", "settings", "config", "request_intents"):
        sys.modules.pop(m, None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    import config
    config.DB_PATH = str(tmp_path / "t.db")
    import db
    db.init()
    uid = db.create_user("synthetic-viewer", "scrypt$x", role="user")
    assert db.link_external_identity(uid, "jellyfin", SERVER, VIEWER_SUB)
    return db, uid


def test_default_store_and_resolver_on_real_db(settings, keys, real_db):
    db, uid = real_db
    store = ia.ReplayStore()

    def run(jti):
        tok = sign(keys["viewer"], claims(jti=jti))
        return ia.verify_request(role="viewer", method="POST", raw_target="/integration/v1/requests",
                                 body=BODY, headers=[("Authorization", "Bearer " + tok),
                                                     ("Content-Type", "application/json"),
                                                     ("Idempotency-Key", IDEM)],
                                 required_scope="requests:write", allowed_query_keys=frozenset(),
                                 settings=settings, replay=store, now=NOW)

    assert run("real-1-" + "a" * 12).user["id"] == uid
    err(lambda: run("real-1-" + "a" * 12), 401, "replay")
    with db._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM integration_replay").fetchone()[0] == 1
    db.update_user(uid, enabled=0)
    err(lambda: run("real-2-" + "a" * 12), 403, "disabled")
    db.update_user(uid, enabled=1)
    assert run("real-3-" + "a" * 12).user["id"] == uid  # rejected attempt left real-2 unused


def test_default_store_concurrency_and_clock_on_real_db(settings, keys, real_db):
    store = ia.ReplayStore()
    results = []
    gate = threading.Barrier(12)
    tok = sign(keys["viewer"], claims(jti="real-race-" + "a" * 8))

    def worker():
        gate.wait()
        try:
            ia.verify_request(role="viewer", method="POST", raw_target="/integration/v1/requests", body=BODY,
                              headers=[("Authorization", "Bearer " + tok), ("Content-Type", "application/json"),
                                       ("Idempotency-Key", IDEM)], required_scope="requests:write",
                              allowed_query_keys=frozenset(), settings=settings, replay=store, now=NOW)
            results.append("ok")
        except ia.IntegrationError as exc:
            results.append(exc.status)

    ts = [threading.Thread(target=worker) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(results) == 12 and results.count("ok") == 1
    err(lambda: store.consume(ISSUER, "z" * 16, NOW + 5, NOW - 100), 503, "clock_unsafe")
