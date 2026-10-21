"""Pure receiver primitives for the signed integration API.

No HTTP routes, no executor and no network access live here. A route layer passes
the exact raw request target, the exact raw body bytes and the transmitted headers
into verify_request() together with the trusted endpoint role and the exact scope
that endpoint needs. Nothing in a token is trusted for role, quota or enabled
state: the native user is re-read from the database on every call.

Key material is loaded from local JWKS files only. The endpoint role (viewer or
service) selects the keyring BEFORE the token's kid or aud is looked at, and a key
that appears in both rings is refused at load time.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

VIEWER = "viewer"
SERVICE = "service"
AUDIENCES = {VIEWER: "mycelium.integration.v1", SERVICE: "mycelium.playback.v1"}
VIEWER_SCOPES = frozenset({
    "catalog:read", "availability:read", "watchlist:read", "watchlist:write",
    "requests:read", "requests:write", "issues:read", "issues:write",
    "playback-contexts:write",
})
SERVICE_SCOPES = frozenset({"readers:write"})
ROLE_SCOPES = {VIEWER: VIEWER_SCOPES, SERVICE: SERVICE_SCOPES}

MAX_AUTHORIZATION_BYTES = 6144
MAX_BODY_BYTES = 65536
MAX_TARGET_CHARS = 4096
MAX_JSON_DEPTH = 24
MAX_TTL_SECONDS = 60
MAX_SKEW_SECONDS = 5
MAX_EPOCH = 4102444800
MAX_LOOKUPS = 4
MAX_KEYS_PER_RING = 8
MAX_JWKS_BYTES = 1 << 20
MAX_ISSUER_CHARS = 512
MAX_COUNTER = 10_000_000
REPLAY_CAP = 100000
METHODS = frozenset({"GET", "POST", "DELETE"})
WRITE_METHODS = frozenset({"POST", "DELETE"})

_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_GUID = re.compile(r"^[0-9a-f]{32}$")
_IDEM = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_LENGTH = re.compile(r"^(?:0|[1-9][0-9]{0,4})$")
_CONTENT_TYPE = re.compile(r"^application/json(?:; ?charset=utf-8)?$", re.IGNORECASE)
_JTI = re.compile(r"^[\x21-\x7e]{16,128}$")
_KID = re.compile(r"^[A-Za-z0-9_-]{1,96}$")
_PATH = re.compile(r"^/integration/v1/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$")
_CLAIMS = frozenset({
    "iss", "aud", "sub", "jf_sid", "iat", "nbf", "exp", "jti", "htm", "target",
    "body_sha256", "scope", "library_grants", "library_grants_count", "library_grants_sha256",
    "idempotency_key",
})
_PRIVATE_JWK_FIELDS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "k"})
_JWK_FIELDS = frozenset({"kty", "use", "alg", "kid", "n", "e", "key_ops"})
_SINGLE_HEADERS = ("authorization", "idempotency-key", "content-type", "content-length",
                   "content-encoding", "transfer-encoding")


class IntegrationError(Exception):
    """Safe error: a stable code, an HTTP status and a message with no secrets."""

    def __init__(self, code: str, status: int, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


def _bad(message: str = "invalid request", code: str = "bad_request") -> IntegrationError:
    return IntegrationError(code, 400, message)


def _unauth(message: str = "authentication failed", code: str = "unauthorized") -> IntegrationError:
    return IntegrationError(code, 401, message)


def _forbidden(message: str = "not permitted", code: str = "forbidden") -> IntegrationError:
    return IntegrationError(code, 403, message)


# ── canonical target ─────────────────────────────────────────────────────────

_UNRESERVED = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _decode_component(raw: str) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "%":
            if i + 2 >= len(raw) or not _is_hex(raw[i + 1]) or not _is_hex(raw[i + 2]):
                raise _bad("malformed target")
            out.append(int(raw[i + 1:i + 3], 16))
            i += 3
        else:
            out.append(ord(ch))
            i += 1
    try:
        value = bytes(out).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _bad("malformed target") from None
    # Unicode category Cc (includes DEL and the C1 range). No normalization and
    # no second percent decode.
    if any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value):
        raise _bad("malformed target")
    return value


def _is_hex(ch: str) -> bool:
    return ch in "0123456789abcdefABCDEF"


def _encode_component(value: str) -> str:
    out = []
    for b in value.encode("utf-8"):
        out.append(chr(b) if b in _UNRESERVED else "%%%02X" % b)
    return "".join(out)


def canonical_target(raw_target, allowed_query_keys: Iterable[str] | None = None,
                     strict_raw: bool = True) -> str:
    """Return the canonical form of a raw request target.

    With strict_raw=True the raw wire form must already BE canonical (sorted keys,
    uppercase escapes, no escaped unreserved characters); anything else raises.
    strict_raw=False only normalizes, for vector comparison. allowed_query_keys,
    when given, is the route's exact set of accepted query keys.
    """
    if isinstance(raw_target, (bytes, bytearray)):
        try:
            raw_target = bytes(raw_target).decode("ascii")
        except UnicodeDecodeError:
            raise _bad("malformed target") from None
    if not isinstance(raw_target, str):
        raise _bad("malformed target")
    if not raw_target or len(raw_target) > MAX_TARGET_CHARS:
        raise _bad("malformed target")
    if any(ord(c) <= 32 or ord(c) >= 127 or c in "#+;" for c in raw_target):
        raise _bad("malformed target")
    path, sep, raw_query = raw_target.partition("?")
    if not _PATH.fullmatch(path):
        raise _bad("malformed target")
    if not sep:
        return path
    if not raw_query or "?" in raw_query:
        raise _bad("malformed target")
    allowed = None if allowed_query_keys is None else frozenset(allowed_query_keys)
    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for segment in raw_query.split("&"):
        eq = segment.find("=")
        if eq <= 0:
            raise _bad("malformed target")
        key = _decode_component(segment[:eq])
        value = _decode_component(segment[eq + 1:])
        if key in seen:
            raise _bad("malformed target")
        seen.add(key)
        if allowed is not None and key not in allowed:
            raise _bad("malformed target")
        pairs.append((_encode_component(key), _encode_component(value)))
    pairs.sort()
    result = path + "?" + "&".join(k + "=" + v for k, v in pairs)
    if strict_raw and result != raw_target:
        raise _bad("target is not canonical")
    return result


# ── strict JSON ──────────────────────────────────────────────────────────────

def _no_dupes(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _reject_constant(_name):
    raise ValueError("non-finite number")


def _parse_int(text):
    if text == "-0":
        raise ValueError("negative zero")
    value = int(text)
    try:
        float(value)  # must be representable as a finite double, like the signer
    except OverflowError:
        raise ValueError("number out of range") from None
    return value


def _parse_float(text):
    value = float(text)
    if not math.isfinite(value) or (value == 0.0 and math.copysign(1.0, value) < 0):
        raise ValueError("bad number")
    return value


def _check_depth(text: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("too deep")
        elif ch in "]}":
            depth -= 1


def _check_strings(value) -> None:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            item.encode("utf-8")  # raises on a lone surrogate
        elif isinstance(item, dict):
            for k, v in item.items():
                k.encode("utf-8")
                stack.append(v)
        elif isinstance(item, list):
            stack.extend(item)


def strict_json_object(raw: bytes, max_bytes: int = MAX_BODY_BYTES) -> dict:
    """Parse exact UTF-8 bytes as one JSON object, or raise IntegrationError(400).

    Rejects a BOM, duplicate keys, NaN/Infinity, negative zero, lone surrogates,
    nesting deeper than 24, trailing data and any non-object top level. No value is
    reinterpreted: the caller gets exactly what the bytes say.
    """
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > max_bytes:
        raise _bad("invalid body")
    try:
        text = bytes(raw).decode("utf-8", errors="strict")
        if text.startswith("﻿"):
            raise ValueError("bom")
        _check_depth(text)
        value = json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_reject_constant,
                           parse_int=_parse_int, parse_float=_parse_float)
        _check_strings(value)
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        raise _bad("invalid body") from None
    if not isinstance(value, dict):
        raise _bad("invalid body")
    return value


# ── keys ─────────────────────────────────────────────────────────────────────

def _b64u_decode(text: str) -> bytes:
    if not isinstance(text, str) or not _B64URL.fullmatch(text):
        raise ValueError("not base64url")
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except Exception:
        raise ValueError("not base64url") from None
    if base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii") != text:
        raise ValueError("non-canonical base64url")
    return data


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class Keyring:
    role: str
    keys: Mapping[str, rsa.RSAPublicKey]
    fingerprints: frozenset = field(default_factory=frozenset)


def _acceptable_numbers(n: int, e: int) -> bool:
    return 2048 <= n.bit_length() <= 8192 and 65537 <= e < 1 << 64 and e % 2 == 1


def _ring_prints(ring) -> frozenset:
    """Recompute (n, e) pairs from the actual key objects; never trust stored fingerprints."""
    if not isinstance(ring, Keyring) or not ring.keys or len(ring.keys) > MAX_KEYS_PER_RING:
        raise ValueError("invalid keyring")
    prints = set()
    for kid, key in ring.keys.items():
        if not isinstance(kid, str) or not _KID.fullmatch(kid) \
                or not isinstance(key, rsa.RSAPublicKey):
            raise ValueError("invalid keyring entry")
        nums = key.public_numbers()
        if not _acceptable_numbers(nums.n, nums.e) or (nums.n, nums.e) in prints:
            raise ValueError("invalid keyring entry")
        prints.add((nums.n, nums.e))
    return frozenset(prints)


def keyring_from_jwks(jwks, role: str) -> Keyring:
    if role not in (VIEWER, SERVICE):
        raise ValueError("unknown role")
    if not isinstance(jwks, dict) or set(jwks) != {"keys"} or not isinstance(jwks["keys"], list):
        raise ValueError("invalid JWKS")
    if not jwks["keys"] or len(jwks["keys"]) > MAX_KEYS_PER_RING:
        raise ValueError("JWKS must hold 1 to 8 keys")
    keys: dict[str, rsa.RSAPublicKey] = {}
    prints: set[tuple[int, int]] = set()
    for jwk in jwks["keys"]:
        if not isinstance(jwk, dict) or _PRIVATE_JWK_FIELDS & set(jwk) or set(jwk) - _JWK_FIELDS:
            raise ValueError("invalid JWK")
        if jwk.get("kty") != "RSA" or jwk.get("use", "sig") != "sig" \
                or jwk.get("alg", "RS256") != "RS256":
            raise ValueError("invalid JWK")
        ops = jwk.get("key_ops")
        if ops is not None and ops != ["verify"]:
            raise ValueError("invalid JWK")
        kid = jwk.get("kid")
        if not isinstance(kid, str) or not _KID.fullmatch(kid) or kid in keys:
            raise ValueError("invalid JWK kid")
        n_bytes, e_bytes = _b64u_decode(jwk.get("n")), _b64u_decode(jwk.get("e"))
        if not n_bytes or not e_bytes or n_bytes[0] == 0 or e_bytes[0] == 0:
            raise ValueError("non-canonical integer")
        n, e = int.from_bytes(n_bytes, "big"), int.from_bytes(e_bytes, "big")
        if not _acceptable_numbers(n, e):
            raise ValueError("unacceptable RSA key")
        if (n, e) in prints:
            raise ValueError("duplicate key material")
        prints.add((n, e))
        keys[kid] = rsa.RSAPublicNumbers(e, n).public_key()
    return Keyring(role=role, keys=keys, fingerprints=frozenset(prints))


def build_keyrings(viewer_jwks, service_jwks) -> tuple[Keyring, Keyring]:
    viewer = keyring_from_jwks(viewer_jwks, VIEWER)
    service = keyring_from_jwks(service_jwks, SERVICE)
    if viewer.fingerprints & service.fingerprints:
        raise ValueError("key material shared between viewer and service rings")
    return viewer, service


def load_jwks_file(path: str):
    """Read a local JWKS file. There is no URL fetch anywhere in this module."""
    with open(path, "rb") as fh:
        data = fh.read(MAX_JWKS_BYTES + 1)
    if len(data) > MAX_JWKS_BYTES:
        raise ValueError("JWKS file too large")
    return strict_json_object(data, max_bytes=MAX_JWKS_BYTES)


@dataclass(frozen=True)
class Settings:
    issuer: str
    server_guid: str
    viewer: Keyring
    service: Keyring

    def __post_init__(self):
        _check_issuer(self.issuer)
        if not _valid_guid(self.server_guid):
            raise ValueError("server guid must be 32 lowercase hex, non-zero")
        if not isinstance(self.viewer, Keyring) or self.viewer.role != VIEWER \
                or not isinstance(self.service, Keyring) or self.service.role != SERVICE:
            raise ValueError("keyring roles do not match")
        if _ring_prints(self.viewer) & _ring_prints(self.service):
            raise ValueError("key material shared between viewer and service rings")

    def ring(self, role: str) -> Keyring:
        if role == VIEWER:
            return self.viewer
        if role == SERVICE:
            return self.service
        raise IntegrationError("bad_role", 500, "internal error")


def _check_issuer(issuer) -> None:
    """Strict HTTPS authority with no userinfo, query or fragment. Parsing only, no network."""
    if not isinstance(issuer, str) or not issuer.isascii() or len(issuer) > MAX_ISSUER_CHARS \
            or any(ord(c) <= 32 or ord(c) == 127 or c in "\\#?" for c in issuer):
        raise ValueError("issuer must be an exact https URL")
    try:
        parts = urlsplit(issuer)
        port = parts.port
    except ValueError:
        raise ValueError("issuer must be an exact https URL") from None
    if not issuer.startswith("https://") or parts.scheme != "https" or not parts.hostname or "@" in parts.netloc \
            or parts.query or parts.fragment or (port is not None and not 1 <= port <= 65535):
        raise ValueError("issuer must be an exact https URL")


def _valid_guid(value) -> bool:
    return isinstance(value, str) and bool(_GUID.fullmatch(value)) and value != "0" * 32


def settings_from_config(cfg=None) -> Settings | None:
    """Build Settings from config.py. Returns None while integration is disabled."""
    if cfg is None:
        import config as cfg  # noqa: PLC0415
    if not getattr(cfg, "INTEGRATION_ENABLED", False):
        return None
    viewer, service = build_keyrings(load_jwks_file(cfg.INTEGRATION_VIEWER_JWKS_PATH),
                                     load_jwks_file(cfg.INTEGRATION_SERVICE_JWKS_PATH))
    return Settings(cfg.INTEGRATION_ISSUER, cfg.INTEGRATION_SERVER_GUID, viewer, service)


# ── durable replay store ─────────────────────────────────────────────────────

_REPLAY_DDL = (
    """CREATE TABLE IF NOT EXISTS integration_replay (
           issuer     TEXT    NOT NULL,
           jti        TEXT    NOT NULL,
           expires_at INTEGER NOT NULL,
           PRIMARY KEY (issuer, jti)
       ) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS idx_integration_replay_exp ON integration_replay(expires_at)",
    """CREATE TABLE IF NOT EXISTS integration_clock (
           id         INTEGER PRIMARY KEY CHECK (id = 1),
           high_water INTEGER NOT NULL
       )""",
)


def init_schema(conn) -> None:
    for stmt in _REPLAY_DDL:
        conn.execute(stmt)


class ReplayStore:
    """Durable issuer+jti store. Every failure to prove freshness fails closed (503)."""

    def __init__(self, connect: Callable[[], sqlite3.Connection] | None = None,
                 path: str | None = None, cap: int = REPLAY_CAP):
        if connect is not None and path is not None:
            raise ValueError("pass either connect or path, not both")
        if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= MAX_COUNTER:
            raise ValueError("invalid cap")
        if path is not None and (not isinstance(path, str) or not path):
            raise ValueError("invalid path")
        if connect is not None and not callable(connect):
            raise ValueError("connect must be callable")
        if connect is None and path is None:
            def connect():  # noqa: E306
                import db  # noqa: PLC0415
                return db._thread_conn()
        elif connect is None:
            def connect(_path=path):  # noqa: E306
                conn = sqlite3.connect(_path, timeout=0.25, isolation_level=None)
                return conn
        self._connect = connect
        self._owned = path is not None
        self._cap = cap

    def consume(self, issuer: str, jti: str, exp: int, now: int) -> None:
        expires_at = int(exp) + MAX_SKEW_SECONDS
        conn = None
        opened = False
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            opened = True
            row = conn.execute("SELECT high_water FROM integration_clock WHERE id=1").fetchone()
            high = int(row[0]) if row else 0
            if now < high - MAX_SKEW_SECONDS:
                conn.execute("ROLLBACK")
                opened = False
                raise IntegrationError("clock_unsafe", 503, "temporarily unavailable", True)
            if now > high:
                conn.execute("INSERT INTO integration_clock(id, high_water) VALUES (1, ?) "
                             "ON CONFLICT(id) DO UPDATE SET high_water=excluded.high_water", (now,))
            conn.execute("DELETE FROM integration_replay WHERE expires_at < ?", (now,))
            count = conn.execute("SELECT COUNT(*) FROM integration_replay").fetchone()[0]
            if count >= self._cap:
                conn.execute("ROLLBACK")
                opened = False
                raise IntegrationError("replay_store_full", 503, "temporarily unavailable", True)
            try:
                conn.execute("INSERT INTO integration_replay(issuer, jti, expires_at) VALUES (?,?,?)",
                             (issuer, jti, expires_at))
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                opened = False
                raise _unauth("assertion already used", "replay") from None
            conn.execute("COMMIT")
            opened = False
        except IntegrationError:
            raise
        except sqlite3.Error:
            if conn is not None and opened:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise IntegrationError("replay_store_unavailable", 503,
                                   "temporarily unavailable", True) from None
        finally:
            if conn is not None and self._owned:
                conn.close()


# ── request verification ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class VerifiedRequest:
    role: str
    scopes: frozenset
    jti: str
    subject: str
    user: dict | None
    method: str
    target: str
    body: dict | None
    body_sha256: str
    idempotency_key: str | None
    claims: dict
    grants_unqualified: dict | None


def _header_map(headers) -> dict[str, str]:
    """Lowercase header map; duplicate security-relevant headers are refused."""
    items = headers.items() if isinstance(headers, Mapping) else headers
    out: dict[str, str] = {}
    for name, value in items:
        lname = str(name).lower()
        if lname in out and (lname in _SINGLE_HEADERS):
            raise _bad("duplicate header")
        out[lname] = value
    return out


def _parse_jwt(token: str, ring: Keyring):
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise _unauth()
    try:
        header_raw, payload_raw, sig = (_b64u_decode(p) for p in parts)
        header = strict_json_object(header_raw, 1024)
        payload = strict_json_object(payload_raw, MAX_AUTHORIZATION_BYTES)
    except (ValueError, IntegrationError):
        raise _unauth() from None
    if set(header) != {"alg", "typ", "kid"} or header["alg"] != "RS256" \
            or header["typ"] != "JWT" or not isinstance(header["kid"], str):
        raise _unauth()
    key = ring.keys.get(header["kid"])
    if key is None:
        raise _unauth()
    try:
        key.verify(sig, (parts[0] + "." + parts[1]).encode("ascii"),
                   padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise _unauth() from None
    return payload


def _check_grants(claims: dict, now: int):
    """Check envelope integrity only. Source observations remain unqualified;
    no value returned here establishes entitlement or action authority."""
    grants = claims.get("library_grants")
    count, digest = claims.get("library_grants_count"), claims.get("library_grants_sha256")
    if grants is None:
        if "library_grants_count" in claims or "library_grants_sha256" in claims:
            raise _unauth()
        return None
    if not isinstance(grants, dict) or set(grants) != {
            "version", "assertion_jti", "observed_at", "expires_at", "source_state", "lookups"}:
        raise _unauth()
    if grants["version"] != "1" or grants["assertion_jti"] != claims["jti"]:
        raise _unauth()
    observed, expires = grants["observed_at"], grants["expires_at"]
    if type(observed) is not int or type(expires) is not int \
            or not 0 < observed < expires <= min(claims["exp"], observed + 30) \
            or observed > now + MAX_SKEW_SECONDS or now >= expires:
        raise _unauth()
    state = grants["source_state"]
    if not isinstance(state, dict) or set(state) != {
            "publisher_epoch", "policy_revision", "catalog_revision"} \
            or any(not isinstance(v, str) or not 1 <= len(v) <= 128
                   or any(ord(c) < 33 or ord(c) > 126 for c in v) for v in state.values()):
        raise _unauth()
    lookups = grants["lookups"]
    if not isinstance(lookups, list) or len(lookups) > MAX_LOOKUPS:
        raise _unauth()
    if type(count) is not int or count != len(lookups):
        raise _unauth()
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise _unauth()
    def proof_value(value):
        if isinstance(value, str):
            return all(32 <= ord(c) <= 126 for c in value)
        if type(value) in (int, bool):
            return True
        if isinstance(value, list):
            return all(proof_value(v) for v in value)
        if isinstance(value, dict):
            return all(proof_value(k) and proof_value(v) for k, v in value.items())
        return False
    if not proof_value(grants):
        raise _unauth()
    encoded = json.dumps(grants, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if not hmac.compare_digest(hashlib.sha256(encoded).hexdigest(), digest):
        raise _unauth()
    return dict(grants)


def _int_claim(claims: dict, name: str) -> int:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < MAX_EPOCH:
        raise _unauth()
    return value


def _default_resolver(provider: str, server_id: str, subject: str):
    import db  # noqa: PLC0415
    return db.get_user_by_external_identity(provider, server_id, subject)


def verify_request(*, role: str, method: str, raw_target, body: bytes, headers,
                   required_scope: str, allowed_query_keys: Iterable[str],
                   settings: Settings, replay: ReplayStore, now: int | None = None,
                   resolve_user: Callable | None = None) -> VerifiedRequest:
    """Verify one signed request and consume its jti. Raises IntegrationError.

    role is the TRUSTED endpoint role chosen by the route, never by the token.
    required_scope is the exact scope that route needs. raw_target and body are the
    transmitted bytes. Signature, time, binding, scope, actor and body checks all
    pass before the jti is consumed; a rejected request never burns a jti.
    """
    if role not in (VIEWER, SERVICE) or allowed_query_keys is None \
            or required_scope not in ROLE_SCOPES[role]:
        raise IntegrationError("route_misconfigured", 500, "internal error")
    if method not in METHODS:
        raise _bad("unsupported method")
    if not isinstance(body, (bytes, bytearray)):
        raise _bad("invalid body")
    body = bytes(body)
    now = int(time.time()) if now is None else int(now)
    hdr = _header_map(headers)
    write = method in WRITE_METHODS

    auth = hdr.get("authorization")
    if not isinstance(auth, str) or len(auth.encode("utf-8", "replace")) > MAX_AUTHORIZATION_BYTES \
            or not auth.startswith("Bearer ") or " " in auth[7:]:
        raise _unauth()
    ring = settings.ring(role)
    claims = _parse_jwt(auth[7:], ring)

    if set(claims) - _CLAIMS:
        raise _unauth()
    if claims.get("iss") != settings.issuer or claims.get("aud") != AUDIENCES[role]:
        raise _unauth()
    sub, sid = claims.get("sub"), claims.get("jf_sid")
    if not _valid_guid(sub) or not _valid_guid(sid) or sid != settings.server_guid:
        raise _unauth()
    if role == VIEWER and sub == settings.server_guid:
        raise _unauth()
    if role == SERVICE and sub != settings.server_guid:
        raise _unauth()

    iat, nbf, exp = _int_claim(claims, "iat"), _int_claim(claims, "nbf"), _int_claim(claims, "exp")
    if nbf != iat or exp <= iat or exp - iat > MAX_TTL_SECONDS \
            or iat > now + MAX_SKEW_SECONDS or now > exp + MAX_SKEW_SECONDS:
        raise _unauth()
    jti = claims.get("jti")
    if not isinstance(jti, str) or not _JTI.fullmatch(jti):
        raise _unauth()

    scope = claims.get("scope")
    if not isinstance(scope, str):
        raise _unauth()
    names = scope.split(" ")
    allowed = ROLE_SCOPES[role]
    if not names or len(set(names)) != len(names) or any(n not in allowed for n in names) \
            or required_scope not in names:
        raise _forbidden("missing scope", "scope")

    if claims.get("htm") != method:
        raise _unauth()
    target = canonical_target(raw_target, allowed_query_keys, strict_raw=True)
    signed_target = claims.get("target")
    if not isinstance(signed_target, str) \
            or not hmac.compare_digest(signed_target.encode("utf-8"), target.encode("ascii")):
        raise _unauth()
    digest = hashlib.sha256(body).hexdigest()
    signed_digest = claims.get("body_sha256")
    if not isinstance(signed_digest, str) or not _SHA256.fullmatch(signed_digest) \
            or not hmac.compare_digest(signed_digest.encode("ascii"), digest.encode("ascii")):
        raise _unauth()

    idem = hdr.get("idempotency-key")
    if write:
        if not isinstance(idem, str) or not _IDEM.fullmatch(idem) \
                or claims.get("idempotency_key") != idem:
            raise _unauth()
    elif body or idem is not None or "idempotency_key" in claims:
        raise _bad("unexpected body or idempotency key")
    else:
        idem = None

    if "content-encoding" in hdr or "transfer-encoding" in hdr:
        raise _bad("unsupported encoding")
    if len(body) > MAX_BODY_BYTES:
        raise _bad("body too large")
    length = hdr.get("content-length")
    if length is not None and (not isinstance(length, str) or not _CONTENT_LENGTH.fullmatch(length)
                               or int(length) > MAX_BODY_BYTES or int(length) != len(body)):
        raise _bad("invalid length")
    ctype = hdr.get("content-type")
    if write:
        # Every write carries a JSON object body (at least {}) and a precise content type.
        if not body or not isinstance(ctype, str) or not _CONTENT_TYPE.fullmatch(ctype):
            raise _bad("unsupported content type")
    elif ctype is not None:
        raise _bad("unexpected content type")

    grants = _check_grants(claims, now)

    user = None
    if role == VIEWER:
        resolver = resolve_user or _default_resolver
        user = resolver("jellyfin", settings.server_guid, sub)
        if not user:
            raise _forbidden("account not linked", "unlinked")
        if not user.get("enabled"):
            raise _forbidden("account disabled", "disabled")

    parsed = strict_json_object(body) if body else None

    replay.consume(settings.issuer, jti, exp, now)
    return VerifiedRequest(
        role=role, scopes=frozenset(names), jti=jti, subject=sub, user=user, method=method,
        target=target, body=parsed, body_sha256=digest, idempotency_key=idem,
        claims=claims, grants_unqualified=grants)
