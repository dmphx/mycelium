"""Read-only orchestration behind the signed integration boundary.

Inputs are the receiver's verified canonical target and native user, never Flask
arguments or browser identity. No library enrichment or mutation belongs here.
"""
import datetime
import json
import math
import re
from urllib.parse import unquote_to_bytes

import integration_auth as ia
import integration_catalog as ic
import tmdb_checked as tc

TMDB_OPERATIONS = (
    "catalog.search", "catalog.rails", "catalog.details", "catalog.seasons",
    "genres.read", "providers.read", "people.read",
)
CATALOG_OPERATIONS = TMDB_OPERATIONS + ("genre-tabs.read",)
BASE_OPERATIONS = ("me.read", "requests.read")
RETRYABLE_REASONS = frozenset({
    "busy", "deadline", "upstream_unreachable", "upstream_error", "session_error",
    "upstream_status",
})
_REGION = re.compile(r"[A-Z]{2}")
_INTEGER = re.compile(r"[1-9][0-9]{0,9}")
_RAIL_KEYS = {
    "trending": {"type", "window", "page"},
    "popular": {"type", "region", "page"},
    "top-rated": {"type", "page"},
    "now-playing": {"type", "region", "page"},
    "upcoming": {"type", "region", "page"},
    "on-the-air": {"type", "page"},
    "by-provider": {"type", "provider_id", "region", "page"},
    "by-genre": {"type", "genre_id", "year_from", "year_to", "page"},
}


def _bad():
    raise ia.IntegrationError("bad_request", 400, "invalid catalog parameters")


def _unavailable(retryable=False, code="catalog_unavailable"):
    raise ia.IntegrationError(code, 503, "catalog temporarily unavailable", retryable)


def _read_setting(key):
    """Do not use settings.get: its DB failure fallback can invent empty data."""
    import config
    import db
    value = db.get_setting(key)
    return getattr(config, key, None) if value is None else value


def _setting(reader, key):
    try:
        return reader(key)
    except Exception:
        _unavailable(True)


def _require_key(reader):
    key = _setting(reader, "TMDB_API_KEY")
    # Match the checked transport's local key grammar, without returning it.
    if type(key) is not str or not key or len(key) > 2048 or not key.isascii() \
            or not key.isprintable() or key != key.strip():
        _unavailable()


def query(target):
    """Decode the already verified canonical target once, strictly as UTF-8."""
    if "?" not in target:
        return {}
    out = {}
    try:
        for pair in target.split("?", 1)[1].split("&"):
            key, value = pair.split("=", 1)
            key = unquote_to_bytes(key).decode("utf-8", "strict")
            value = unquote_to_bytes(value).decode("utf-8", "strict")
            if key in out:
                _bad()
            out[key] = value
    except (ValueError, UnicodeError):
        _bad()
    return out


def _number(value, low=1, high=ic.MAX_ID):
    if type(value) is not str or not _INTEGER.fullmatch(value):
        _bad()
    number = int(value)
    if not low <= number <= high:
        _bad()
    return number


def _kind(params, default=None, all_types=False):
    kind = params.get("type", default)
    if kind not in (("all", "movie", "tv") if all_types else ("movie", "tv")):
        _bad()
    return kind


def _native_region(user):
    value = user.get("region")
    return value if type(value) is str and _REGION.fullmatch(value) else None


def _region(params, user, details=False):
    if "region" in params:
        value = params["region"]
        if not _REGION.fullmatch(value):
            _bad()
        return value
    value = _native_region(user)
    if value is None:
        if details:
            _unavailable(code="preference_invalid")
        raise ia.IntegrationError("region_required", 400, "region required")
    return value


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate")
        out[key] = value
    return out


def _constant(_value):
    raise ValueError("nonfinite")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite")
    return number


def _integer(value):
    if len(value) > 19:
        raise ValueError("oversize integer")
    return int(value)


def _tabs(reader):
    raw = _setting(reader, "DISCOVER_GENRE_TABS")
    if raw is None:  # An absent setting, never a failed read.
        raw = "[]"
    if type(raw) is not str or len(raw) > ic.MAX_PAYLOAD_BYTES:
        _unavailable()
    try:
        rows = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant,
                          parse_float=_float, parse_int=_integer)
        if type(rows) is not list or len(rows) > 200:
            raise ValueError("bad tabs")
        mapped, enabled, seen = [], [], set()
        required = {"media_type", "genre_id", "genre_name", "enabled"}
        allowed = required | {"year_from", "year_to"}
        for row in rows:
            if type(row) is not dict or not required <= row.keys() or row.keys() - allowed \
                    or type(row["enabled"]) is not bool:
                raise ValueError("bad tabs")
            mapped.append({"media_type": row["media_type"], "genre_id": row["genre_id"],
                           "name": row["genre_name"], "year_from": row.get("year_from"),
                           "year_to": row.get("year_to")})
            enabled.append(row["enabled"])
        # Validate disabled rows too, before identity comparison or filtering.
        validated = ic.genre_tabs(mapped)["items"]
        for row in validated:
            identity = tuple(row[k] for k in ("media_type", "genre_id", "year_from", "year_to"))
            if identity in seen:
                raise ValueError("duplicate tab")
            seen.add(identity)
        return {"items": [row for row, visible in zip(validated, enabled) if visible]}
    except (ValueError, TypeError, RecursionError, OverflowError, ic.CatalogUnavailable):
        _unavailable()


def operations(user, *, read_setting=None):
    """Configuration readiness only. Provider outages never change support."""
    reader = read_setting or _read_setting
    supported = set(BASE_OPERATIONS)
    try:
        _require_key(reader)
        supported.update(TMDB_OPERATIONS)
        if _native_region(user) is None:
            supported.discard("catalog.details")
    except ia.IntegrationError:
        pass
    try:
        _tabs(reader)
        supported.add("genre-tabs.read")
    except ia.IntegrationError:
        pass
    return supported


def _rail(rail, params, user, fetch):
    if rail not in _RAIL_KEYS or params.keys() - _RAIL_KEYS[rail]:
        _bad()
    page = _number(params.get("page", "1"), high=500)
    kind = _kind(params, "all" if rail == "trending" else
                 "tv" if rail == "on-the-air" else "movie", rail == "trending")
    args = {"page": page}
    if rail == "trending":
        window = params.get("window", "week")
        if window not in ("day", "week"):
            _bad()
        return ic.trending(fetch(f"/trending/{kind}/{window}", args), page, kind)
    if rail in ("now-playing", "upcoming") and kind != "movie" \
            or rail == "on-the-air" and kind != "tv":
        _bad()
    if rail in ("popular", "now-playing", "upcoming"):
        args["region"] = _region(params, user)
    if rail in ("by-provider", "by-genre"):
        args.update(sort_by="popularity.desc", include_adult=False)
        if rail == "by-provider":
            args.update(with_watch_providers=_number(params.get("provider_id")),
                        watch_region=_region(params, user),
                        with_watch_monetization_types="flatrate")
        else:
            args["with_genres"] = _number(params.get("genre_id"))
            lower = _number(params["year_from"], 1800, 3000) if "year_from" in params else None
            upper = _number(params["year_to"], 1800, 3000) if "year_to" in params else None
            if lower is not None and upper is not None and lower > upper:
                _bad()
            field = "primary_release_date" if kind == "movie" else "first_air_date"
            if lower is not None:
                args[field + ".gte"] = f"{lower}-01-01"
            if upper is not None:
                args[field + ".lte"] = f"{upper}-12-31"
        path = f"/discover/{kind}"
    else:
        path = f"/{kind}/{rail.replace('-', '_')}"
    return ic.catalog_list(fetch(path, args), kind, page)


def execute(operation, target, user, identity=(), *, fetch=None, read_setting=None, today=None):
    """Build route data, or a safe IntegrationError. Caller supplies verified inputs."""
    reader = read_setting or _read_setting
    checked_fetch = fetch or tc.fetch

    def get(path, params=None):
        # Validate all client inputs first; fail closed on configuration read errors.
        _require_key(reader)
        return checked_fetch(path, params)

    try:
        params = query(target)
        if operation == "catalog.search":
            if params.keys() - {"q", "page", "type"}:
                _bad()
            text = params.get("q", "").strip()
            if not text or len(text) > 100 or any(ord(c) < 32 or 127 <= ord(c) <= 159
                                                or 0xD800 <= ord(c) <= 0xDFFF for c in text):
                _bad()
            page = _number(params.get("page", "1"), high=500)
            kind = _kind(params, "all", True)
            raw = get("/search/" + ("multi" if kind == "all" else kind),
                      {"query": text, "page": page, "include_adult": False})
            data = ic.search(raw, page, None if kind == "all" else kind)
        elif operation == "catalog.rails":
            data = _rail(identity[0], params, user, get)
        elif operation in ("genres.read", "providers.read"):
            if params.keys() - ({"type"} if operation == "genres.read" else {"type", "region"}):
                _bad()
            kind = _kind(params)
            if operation == "genres.read":
                data = ic.genres(get(f"/genre/{kind}/list"), kind)
            else:
                region = _region(params, user)
                data = ic.providers(get(f"/watch/providers/{kind}", {"watch_region": region}), kind, region)
        else:
            if params:
                _bad()
            if operation == "catalog.details":
                kind = _kind({"type": identity[0]})
                tid = _number(identity[1])
                region = _region({}, user, True)
                raw = get(f"/{kind}/{tid}", {
                    "append_to_response": "credits,videos,watch/providers,recommendations"})
                data = ic.detail(raw, kind, tid, region)
            elif operation == "catalog.seasons":
                tid = _number(identity[0])
                season_text = identity[1]
                number = 0 if season_text == "0" else _number(season_text, high=9999)
                show = get(f"/tv/{tid}")
                season = get(f"/tv/{tid}/season/{number}")
                date = today if today is not None else datetime.datetime.now(datetime.timezone.utc).date()
                data = ic.season(season, show, tid, number, date)
            elif operation == "people.read":
                pid = _number(identity[0])
                data = ic.person(get(f"/person/{pid}", {"append_to_response": "combined_credits"}), pid)
            elif operation == "genre-tabs.read":
                data = _tabs(reader)
            else:
                _bad()
        ic.serialize(data)  # Size check only; the HTTP caller must use ok(data).
        return data
    except ic.CatalogNotFound:
        raise ia.IntegrationError("not_found", 404, "not found") from None
    except ic.CatalogUnavailable:
        _unavailable()
    except tc.TmdbNotFound:
        if operation in ("catalog.details", "catalog.seasons", "people.read"):
            raise ia.IntegrationError("not_found", 404, "not found") from None
        _unavailable()
    except tc.TmdbUnavailable as exc:
        _unavailable(str(exc) in RETRYABLE_REASONS)
