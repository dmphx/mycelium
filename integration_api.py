"""Signed integration API facade: the HTTP boundary only.

Implemented: GET me, capabilities, requests (list and one), and catalog metadata
reads through integration_catalog_service. Other allowlisted operations authenticate
and answer 503 capability_unqualified. No mutation beyond the replay store.

Authentication is the receiver in integration_auth.py. This blueprint never reads the
browser session or cookies, never trusts forwarded-URI headers, and never builds the
signed target from path plus query string: the raw request target must come from the
server-owned WSGI variable RAW_URI (set by gunicorn from the request line). If it is
absent, or is not origin-form, the request fails closed with 503.

Header note: WSGI folds duplicate headers into one comma-joined value before this code
runs, so duplicate-header framing is not provable here. Comma-folded security headers
fail closed in the receiver, and edge/gunicorn duplicate-header behaviour still needs
its own qualification.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from flask import Blueprint, Response, jsonify, request
from werkzeug.routing import BaseConverter
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

import integration_auth as ia
import integration_catalog_service as catalog

log = logging.getLogger(__name__)

API_VERSION = "1"
ENDPOINT = "integration_v1.dispatch"
PREFIX = "/integration/v1"
ALLOWED_METHODS = ("GET", "POST", "DELETE")
LIST_MAX = 100
UNKNOWN_AVAILABILITY = {"state": "unknown", "entitlement": "unknown",
                        "lookup": "unknown", "playability": "unknown"}
LIMITS = {"request_body_bytes": ia.MAX_BODY_BYTES,
          "authorization_bytes": ia.MAX_AUTHORIZATION_BYTES,
          "request_episodes": 500, "request_seasons": 100, "list_page_size": LIST_MAX}
OPERATION_NAMES = (
    "me.read", "catalog.search", "catalog.rails", "catalog.details", "catalog.seasons",
    "genres.read", "providers.read", "genre-tabs.read", "people.read", "follows.read",
    "follows.write", "availability.read", "watchlist.read", "watchlist.write",
    "requests.read", "requests.write", "playback-contexts.write", "issues.read", "issues.write",
)
ADVERTISED = ("me.read", "requests.read")

_ID = r"[1-9][0-9]{0,9}"
_RID = r"[1-9][0-9]{0,15}"
_RAILS = "trending|popular|top-rated|now-playing|upcoming|on-the-air|by-provider|by-genre"
_RAIL_KEYS = frozenset({"type", "region", "window", "provider_id", "genre_id",
                        "year_from", "year_to", "page"})
_LIST_KEYS = frozenset({"before_id", "limit"})
_NONE: frozenset = frozenset()


@dataclass(frozen=True)
class Route:
    name: str
    methods: frozenset
    pattern: re.Pattern
    scope: str
    keys: frozenset
    role: str = ia.VIEWER
    handler: str = "unqualified"


def _r(name, methods, pattern, scope, keys=_NONE, role=ia.VIEWER, handler="unqualified"):
    return Route(name, frozenset(methods.split("/")),
                 re.compile("^" + PREFIX + pattern + "$"), scope, keys, role, handler)


ROUTES: tuple[Route, ...] = (
    _r("me", "GET", "/me", "catalog:read", handler="me"),
    _r("capabilities", "GET", "/capabilities", "catalog:read", handler="capabilities"),
    _r("catalog.search", "GET", "/catalog/search", "catalog:read", frozenset({"q", "page", "type"}), handler="catalog.search"),
    _r("catalog.rails", "GET", "/catalog/rails/(" + _RAILS + ")", "catalog:read", _RAIL_KEYS, handler="catalog.rails"),
    _r("catalog.details", "GET", "/catalog/(movie|tv)/(" + _ID + ")", "catalog:read", handler="catalog.details"),
    _r("catalog.seasons", "GET", "/catalog/tv/(" + _ID + ")/seasons/(0|[1-9][0-9]{0,3})", "catalog:read", handler="catalog.seasons"),
    _r("genres", "GET", "/genres", "catalog:read", frozenset({"type"}), handler="genres.read"),
    _r("providers", "GET", "/providers", "catalog:read", frozenset({"type", "region"}), handler="providers.read"),
    _r("genre-tabs", "GET", "/genre-tabs", "catalog:read", handler="genre-tabs.read"),
    _r("people", "GET", "/people/(" + _ID + ")", "catalog:read", handler="people.read"),
    _r("follows.read", "GET", "/follows", "watchlist:read"),
    _r("follows.write", "POST/DELETE", "/follows/" + _ID, "watchlist:write"),
    _r("watchlist.read", "GET", "/watchlist", "watchlist:read", _LIST_KEYS),
    _r("watchlist.write", "POST/DELETE", "/watchlist", "watchlist:write"),
    _r("requests.list", "GET", "/requests", "requests:read", _LIST_KEYS, handler="requests_list"),
    _r("requests.get", "GET", "/requests/(" + _RID + ")", "requests:read", handler="requests_get"),
    _r("requests.write", "POST", "/requests", "requests:write"),
    _r("availability", "POST", "/availability", "availability:read"),
    _r("playback-contexts", "POST", "/playback-contexts", "playback-contexts:write"),
    _r("issues.list", "GET", "/issues", "issues:read", _LIST_KEYS),
    _r("issues.get", "GET", "/issues/(" + _RID + ")", "issues:read"),
    _r("issues.write", "POST", "/issues", "issues:write"),
    _r("readers", "POST", "/readers", "readers:write", role=ia.SERVICE),
)


# ── envelope ────────────────────────────────────────────────────────────────

def _meta() -> dict:
    return {"correlation_id": str(uuid.uuid4()),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def _finish(response: Response, status: int, extra: dict | None = None) -> Response:
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    for name, value in (extra or {}).items():
        response.headers[name] = value
    return response


def ok(data: dict, status: int = 200) -> Response:
    return _finish(jsonify({"api_version": API_VERSION, "data": data, "meta": _meta()}), status)


def fail(code: str, status: int, message: str, retryable: bool = False,
         extra: dict | None = None) -> Response:
    body = {"api_version": API_VERSION,
            "error": {"code": code, "message": message, "retryable": bool(retryable)},
            "meta": _meta()}
    headers = dict(extra or {})
    if status == 401:
        headers.setdefault("WWW-Authenticate", "Bearer")
    return _finish(jsonify(body), status, headers)


def not_found() -> Response:
    return fail("not_found", 404, "not found")


# ── per-viewer bounded token bucket ─────────────────────────────────────────

class Buckets:
    """Bounded in-memory limiter: at most max_keys users tracked (LRU), burst tokens,
    refilled at rate per second. Single-process only, which matches the one-worker
    deployment; a restart resets it."""

    def __init__(self, burst: float = 60.0, rate: float = 20.0, max_keys: int = 2048):
        self._burst, self._rate, self._max = float(burst), float(rate), int(max_keys)
        self._state: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def take(self, key, now: float | None = None) -> float:
        """Return 0.0 if allowed, else seconds until a token is available."""
        now = time.monotonic() if now is None else now
        with self._lock:
            tokens, stamp = self._state.pop(key, (self._burst, now))
            tokens = min(self._burst, tokens + max(0.0, now - stamp) * self._rate)
            wait = 0.0
            if tokens >= 1.0:
                tokens -= 1.0
            else:
                wait = (1.0 - tokens) / self._rate
            self._state[key] = (tokens, now)
            while len(self._state) > self._max:
                self._state.popitem(last=False)
            return wait


# ── receiver state (replaceable by tests) ───────────────────────────────────

class _State:
    lock = threading.Lock()
    settings = None
    loaded = False
    replay: ia.ReplayStore | None = None
    buckets = Buckets()
    clock: Callable[[], int] | None = None  # tests only; production uses the system clock


STATE = _State()


def _load_settings():
    with STATE.lock:
        if not STATE.loaded:
            STATE.settings = ia.settings_from_config()
            STATE.loaded = True
        if STATE.replay is None:
            STATE.replay = ia.ReplayStore()
        return STATE.settings, STATE.replay


def reset_state() -> None:
    """Drop cached settings (tests and config reloads)."""
    with STATE.lock:
        STATE.settings, STATE.loaded, STATE.replay = None, False, None
        STATE.buckets = Buckets()


# ── dispatch ────────────────────────────────────────────────────────────────

bp = Blueprint("integration_v1", __name__)


class _RawPath(BaseConverter):
    """Matches anything after the prefix, including empty segments and a leading slash,
    so every malformed alias reaches dispatch() and gets the envelope instead of a
    redirect or the global login redirect. Routing never decides anything: dispatch()
    works from the raw target only."""
    regex = ".*"
    part_isolating = False


@bp.record_once
def _register_converter(state):
    state.app.url_map.converters["rawpath"] = _RawPath


def _raw_target() -> str | None:
    raw = request.environ.get("RAW_URI")
    if not isinstance(raw, str) or not raw.startswith(PREFIX):
        return None  # missing, or absolute-form / foreign: never reconstructed
    return raw


def _read_body() -> bytes:
    length = request.headers.get("Content-Length")
    if length is not None and length.isascii() and length.isdigit() \
            and len(length) <= 12 and int(length) > ia.MAX_BODY_BYTES:
        raise ia.IntegrationError("bad_request", 400, "body too large")
    return request.stream.read(ia.MAX_BODY_BYTES + 1)


def _match(method: str, path: str):
    for route in ROUTES:
        found = route.pattern.match(path)
        if found and method in route.methods:
            return route, found
    return None, None


@bp.route(PREFIX, defaults={"rest": ""}, methods=["GET", "POST", "DELETE", "HEAD", "OPTIONS", "PUT", "PATCH"],
          strict_slashes=False, merge_slashes=False, provide_automatic_options=False)
@bp.route(PREFIX + "/", defaults={"rest": ""}, methods=["GET", "POST", "DELETE", "HEAD", "OPTIONS", "PUT", "PATCH"],
          strict_slashes=False, merge_slashes=False, provide_automatic_options=False)
@bp.route(PREFIX + "/<rawpath:rest>", methods=["GET", "POST", "DELETE", "HEAD", "OPTIONS", "PUT", "PATCH"],
          strict_slashes=False, merge_slashes=False, provide_automatic_options=False)
def dispatch(rest: str):  # noqa: ARG001  (routing is by the raw target, never by `rest`)
    try:
        return _dispatch()
    except ia.IntegrationError as exc:
        extra = {"Retry-After": "1"} if exc.status in (429, 503) and exc.retryable else None
        return fail(exc.code, exc.status, exc.message, exc.retryable, extra)
    except (BadRequest, RequestEntityTooLarge):
        return fail("bad_request", 400, "invalid request body")
    except Exception as exc:  # noqa: BLE001
        log.error("integration request failed (%s)", type(exc).__name__)
        return fail("internal_error", 500, "internal error")


def _dispatch() -> Response:
    method = request.method
    if method not in ALLOWED_METHODS:
        return fail("method_not_allowed", 405, "method not allowed",
                    extra={"Allow": ", ".join(ALLOWED_METHODS)})
    try:
        settings, replay = _load_settings()
    except (ValueError, OSError) as exc:
        log.error("integration settings invalid (%s)", type(exc).__name__)
        return fail("integration_unavailable", 503, "temporarily unavailable", True)
    if settings is None:
        return not_found()  # disabled looks exactly like an unknown route
    raw = _raw_target()
    if raw is None:
        return fail("raw_target_unavailable", 503, "temporarily unavailable", True)
    ia.canonical_target(raw, None, strict_raw=True)  # 400 on any alias or malformed form
    route, found = _match(method, raw.split("?", 1)[0])
    if route is None:
        return not_found()
    vr = ia.verify_request(
        role=route.role, method=method, raw_target=raw, body=_read_body(),
        headers=list(request.headers.items()), required_scope=route.scope,
        allowed_query_keys=route.keys, settings=settings, replay=replay,
        now=STATE.clock() if STATE.clock else None)
    if route.role == ia.VIEWER:
        wait = STATE.buckets.take(vr.user["id"])
        if wait > 0:
            return fail("rate_limited", 429, "too many requests", True,
                        {"Retry-After": str(max(1, int(wait + 0.999)))})
    handler = _HANDLERS[route.handler]
    return handler(vr, found)


# ── handlers ────────────────────────────────────────────────────────────────

def _unqualified(vr, found) -> Response:  # noqa: ARG001
    return fail("capability_unqualified", 503, "capability is not qualified", False)


_NAME_OK = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")


def _display_name(user: dict) -> str:
    candidate = user.get("display_name")
    if isinstance(candidate, str) and _NAME_OK.fullmatch(candidate.strip() or "\x00"):
        return candidate.strip()
    return str(user.get("username") or "")[:64]


def _monthly_used(user_id: int) -> int:
    """UTC calendar-month request count, denied excluded: the same rule request intent
    admission uses for its quota check."""
    import db
    with db._connect() as conn:
        return int(conn.execute(
            """SELECT COUNT(*) FROM user_requests
               WHERE user_id=? AND status <> 'denied'
                 AND created_at >= strftime('%Y-%m-01 00:00:00','now')""",
            (user_id,)).fetchone()[0])


def _me(vr, found) -> Response:  # noqa: ARG001
    import db
    user = db.get_user(vr.user["id"])
    if not user or not user.get("enabled"):
        raise ia.IntegrationError("disabled", 403, "account disabled")
    admin = user.get("role") == "admin"
    quota = int(user.get("quota_monthly") or 0)
    limit = quota if quota > 0 and not admin else None
    used = _monthly_used(user["id"])
    return ok({
        "viewer": {"id": user["id"], "display_name": _display_name(user)},
        "policy": {
            "request_approval": "automatic" if admin or user.get("auto_approve") else "required",
            "monthly_quota": {"limit": limit, "used": used,
                              "remaining": None if limit is None else max(0, limit - used)},
        },
        "operations": _operations(user),
    })


def _capabilities(vr, found) -> Response:  # noqa: ARG001
    operations = _operations(vr.user)
    return ok({"operations": operations, "limits": dict(LIMITS),
               "incomplete": sorted(set(OPERATION_NAMES) - set(operations))})


def _operations(user):
    supported = catalog.operations(user)
    return [name for name in OPERATION_NAMES if name in supported]


def _catalog_handler(operation):
    def handler(vr, found):
        return ok(catalog.execute(operation, vr.target, vr.user, found.groups()))
    return handler


def _query(target: str) -> dict:
    """Canonical target query as a dict. Repeats and bad shapes were already refused."""
    if "?" not in target:
        return {}
    return dict(pair.split("=", 1) for pair in target.split("?", 1)[1].split("&"))


_LIMIT_RE = re.compile(r"^[1-9][0-9]{0,2}$")
_BEFORE_RE = re.compile(r"^[1-9][0-9]{0,15}$")


def _public_receipt(receipt: dict) -> dict:
    out = {"request_id": receipt["request_id"], "media": receipt["media"],
           "scope": receipt["scope"], "request_status": receipt["request_status"],
           "processing": receipt["processing"], "availability": dict(UNKNOWN_AVAILABILITY)}
    if receipt.get("outcome") in ("created", "existing_request"):
        out["outcome"] = receipt["outcome"]
    return out


def _requests_list(vr, found) -> Response:  # noqa: ARG001
    import request_intents
    params = _query(vr.target)
    limit = 50
    if "limit" in params:
        if not _LIMIT_RE.fullmatch(params["limit"]) or int(params["limit"]) > LIST_MAX:
            raise ia.IntegrationError("bad_request", 400, "invalid limit")
        limit = int(params["limit"])
    before = None
    if "before_id" in params:
        if not _BEFORE_RE.fullmatch(params["before_id"]):
            raise ia.IntegrationError("bad_request", 400, "invalid cursor")
        before = int(params["before_id"])
    try:
        rows = request_intents.list_for_user(vr.user["id"], limit + 1, before)
    except request_intents.IntentError as exc:
        if exc.status == 403:
            raise ia.IntegrationError("disabled", 403, "account disabled") from None
        raise ia.IntegrationError("bad_request", 400, "invalid cursor") from None
    more = len(rows) > limit
    items = [_public_receipt(r) for r in rows[:limit]]
    return ok({"items": items, "next_before_id": items[-1]["request_id"] if more else None})


def _requests_get(vr, found) -> Response:
    import request_intents
    try:
        receipt = request_intents.get(vr.user["id"], int(found.group(1)))
    except request_intents.IntentError as exc:
        if exc.status == 403:
            raise ia.IntegrationError("disabled", 403, "account disabled") from None
        receipt = None
    return ok(_public_receipt(receipt)) if receipt else not_found()


_HANDLERS = {"me": _me, "capabilities": _capabilities, "requests_list": _requests_list,
             "requests_get": _requests_get, "unqualified": _unqualified}
_HANDLERS.update({operation: _catalog_handler(operation) for operation in catalog.CATALOG_OPERATIONS})
