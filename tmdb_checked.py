"""Checked TMDB fetch for the integration catalog.

Separate from the legacy tmdb helpers (which swallow errors and return None): this
one distinguishes "TMDB says not found" from "TMDB unreachable or wrong", never
follows redirects, bounds size, time and concurrency, and never puts a URL, key,
header or upstream text into an exception or log line. The key is read lazily
(once per call) through tmdb._api_key so importing this module touches no
settings or db.

Time budget: DEADLINE_SECONDS is an elapsed budget measured with the injected
clock after the response headers arrive and between body reads. A single blocking
socket read can still run up to READ_TIMEOUT beyond it, so the worst case is about
DEADLINE_SECONDS + READ_TIMEOUT; this is NOT a hard wall-clock limit. Slow-server
behaviour against the real edge is not yet qualified.
"""
import datetime
import json
import logging
import math
import re
import threading
import time

import requests

log = logging.getLogger(__name__)

BASE = "https://api.themoviedb.org/3"
MAX_BYTES = 4 * 1024 * 1024
CONNECT_TIMEOUT = 3
READ_TIMEOUT = 10
DEADLINE_SECONDS = 15
MAX_CONCURRENT = 8
MAX_ID = 2147483647
MAX_QUERY = 100
_CHUNK = 64 * 1024

_ID = r"[1-9][0-9]{0,9}"
_ALLOWED_PATHS = tuple(re.compile(p) for p in (
    r"/(?:movie|tv)/" + _ID,
    r"/tv/" + _ID + r"/season/(?:0|[1-9][0-9]{0,3})",
    r"/person/" + _ID,
    r"/search/(?:multi|movie|tv)",
    r"/trending/(?:all|movie|tv)/(?:day|week)",
    r"/(?:movie|tv)/(?:popular|top_rated|now_playing|upcoming|on_the_air|airing_today)",
    r"/discover/(?:movie|tv)",
    r"/genre/(?:movie|tv)/list",
    r"/watch/providers/(?:movie|tv)",
))
_APPEND = frozenset({"credits", "videos", "external_ids", "watch/providers",
                     "recommendations", "combined_credits"})
_ID_LIST_RE = re.compile(r"[1-9][0-9]{0,9}(?:[,|][1-9][0-9]{0,9}){0,9}")
_PAGE_RE = re.compile(r"[1-9][0-9]{0,2}")
_REGION_RE = re.compile(r"[A-Z]{2}")
_LANG_RE = re.compile(r"[a-z]{2}(?:-[A-Z]{2})?")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DIGITS_RE = re.compile(r"[0-9]+")

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


class TmdbNotFound(Exception):
    """TMDB answered 404 for a well-formed request."""


class TmdbUnavailable(Exception):
    """Anything else: outage, auth, size, redirect, malformed JSON. The message is
    a fixed reason code, safe to log."""


def _fail(reason):
    log.warning("TMDB checked fetch failed: %s", reason)
    raise TmdbUnavailable(reason) from None


def _reject_duplicates(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def _reject_constant(_name):
    raise ValueError("non-finite number")


def _parse_float(text):
    value = float(text)
    if not math.isfinite(value):  # 1e999 overflows to inf
        raise ValueError("non-finite number")
    return value


def _parse_int(text):
    if len(text) > 19:  # oversized integers are never legitimate TMDB values
        raise ValueError("oversized integer")
    return int(text)


def parse_json_object(body: bytes) -> dict:
    """Strict: UTF-8, no duplicate keys, no NaN/Infinity/overflowing floats or
    oversized integers, top level object."""
    try:
        data = json.loads(body.decode("utf-8"), object_pairs_hook=_reject_duplicates,
                          parse_constant=_reject_constant, parse_float=_parse_float,
                          parse_int=_parse_int)
    except (ValueError, RecursionError, OverflowError):
        _fail("bad_json")
    if type(data) is not dict:
        _fail("bad_json")
    return data


def _auth():
    # lazy: tmdb pulls in settings/db, which tests and importers should not need.
    # The key is read once; v4-vs-v3 is decided from that same value.
    try:
        import tmdb
        key = tmdb._api_key()
    except Exception:
        _fail("auth_error")
    if type(key) is not str or not key or len(key) > 2048 \
            or not key.isascii() or not key.isprintable() or key != key.strip():
        _fail("no_key")
    if key.startswith("eyJ"):
        return {"Authorization": f"Bearer {key}"}, {}
    return {}, {"api_key": key}


def _bad_param():
    _fail("param_not_allowed")


def _text(value):
    if type(value) is int and type(value) is not bool:
        return str(value)
    if type(value) is not str:
        _bad_param()
    return value


def _ids_ok(text):
    return all(1 <= int(n) <= MAX_ID for n in _DIGITS_RE.findall(text))


def _check_date(text):
    if not _DATE_RE.fullmatch(text):
        _bad_param()
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        _bad_param()


def _check_param(key, value):
    if key == "query":
        if type(value) is not str or not 1 <= len(value) <= MAX_QUERY:
            _bad_param()
        for ch in value:
            o = ord(ch)
            if o < 0x20 or 0x7F <= o <= 0x9F or 0xD800 <= o <= 0xDFFF:
                _bad_param()
        return value
    if key == "include_adult":
        if value is False or value == "false":
            return "false"
        _bad_param()
    text = _text(value)
    if key == "page":
        if not _PAGE_RE.fullmatch(text) or not 1 <= int(text) <= 500:
            _bad_param()
    elif key in ("region", "watch_region"):
        if not _REGION_RE.fullmatch(text):
            _bad_param()
    elif key == "language":
        if not _LANG_RE.fullmatch(text):
            _bad_param()
    elif key in ("with_genres", "with_watch_providers"):
        if not _ID_LIST_RE.fullmatch(text) or not _ids_ok(text):
            _bad_param()
    elif key == "with_watch_monetization_types":
        if text != "flatrate":
            _bad_param()
    elif key == "append_to_response":
        parts = text.split(",")
        if len(set(parts)) != len(parts) or not set(parts) <= _APPEND:
            _bad_param()
    elif key == "sort_by":
        if text != "popularity.desc":
            _bad_param()
    elif key in ("primary_release_date.gte", "primary_release_date.lte",
                 "first_air_date.gte", "first_air_date.lte"):
        _check_date(text)
    else:
        _bad_param()
    return text


def _check_request(path, params):
    if type(path) is not str or not any(p.fullmatch(path) for p in _ALLOWED_PATHS):
        _fail("path_not_allowed")
    # Season zero is a valid specials identity; show/person/movie ids remain positive.
    parts = path.split("/")
    for index, part in enumerate(parts):
        if part.isdigit() and not (part == "0" and parts[index - 1] == "season") \
                and not 1 <= int(part) <= MAX_ID:
            _fail("path_not_allowed")
    if params is None:
        return {}
    if type(params) is not dict:
        _bad_param()
    clean = {}
    for k, v in params.items():
        if type(k) is not str:
            _bad_param()
        clean[k] = _check_param(k, v)
    return clean


def fetch(path, params=None, *, session_factory=requests.Session, clock=time.monotonic):
    """GET one allowlisted TMDB path and return the parsed JSON object.

    Raises TmdbNotFound on 404 and TmdbUnavailable on every other failure.
    `session_factory` and `clock` are injectable for tests. Cross-parameter rules
    (date ordering, which params suit which path) belong to the orchestrator."""
    clean = _check_request(path, params)
    if not _slots.acquire(blocking=False):
        _fail("busy")
    try:
        return _fetch(path, clean, session_factory, clock)
    finally:
        _slots.release()


def _quiet_close(obj):
    try:
        obj.close()
    except Exception:
        log.warning("TMDB checked fetch: close failed")


def _content_length(headers):
    declared = headers.get("Content-Length")
    if declared is None:
        return None
    if type(declared) is not str or not declared.isascii() or not declared.isdigit() \
            or len(declared) > 12:
        _fail("too_large")
    value = int(declared)
    if value > MAX_BYTES:
        _fail("too_large")
    return value


def _fetch(path, clean, session_factory, clock):
    deadline = clock() + DEADLINE_SECONDS
    auth_headers, auth_params = _auth()
    headers = {"Accept": "application/json", "Accept-Encoding": "identity", **auth_headers}
    try:
        session = session_factory()  # fresh per call: no shared cookie jar
        session.trust_env = False
    except Exception:
        _fail("session_error")
    resp = None
    try:
        try:
            resp = session.get(BASE + path, headers=headers, params={**clean, **auth_params},
                               timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                               allow_redirects=False, stream=True)
        except requests.RequestException:
            _fail("upstream_unreachable")
        except Exception:
            _fail("upstream_error")
        if clock() > deadline:
            _fail("deadline")
        status = resp.status_code
        if status == 404:
            raise TmdbNotFound("not_found")
        if status != 200:
            _fail("upstream_status")
        enc = (resp.headers.get("Content-Encoding") or "identity").strip().lower()
        if enc != "identity":
            _fail("encoded_body")
        declared = _content_length(resp.headers)
        buf = bytearray()
        try:
            for chunk in resp.iter_content(chunk_size=_CHUNK):
                if clock() > deadline:
                    _fail("deadline")
                buf += chunk
                if len(buf) > MAX_BYTES:
                    _fail("too_large")
        except requests.RequestException:
            _fail("upstream_unreachable")
        except TmdbUnavailable:
            raise
        except Exception:
            _fail("upstream_error")
        if clock() > deadline:
            _fail("deadline")
        if declared is not None and declared != len(buf):
            _fail("length_mismatch")
        return parse_json_object(bytes(buf))
    finally:
        if resp is not None:
            _quiet_close(resp)
        _quiet_close(session)
