"""Opt-in internal Seerr API compatibility for the Discord/Onyx Media clients.

Serves only the handful of Seerr endpoints those clients call, from the native
database and TMDB. Authentication is the shared SEERR_API_KEY in X-Api-Key,
compared in constant time, and is independent of any browser session. Requests
are created for ONE configured native admin (SEERR_COMPAT_USER_ID) and kicked
off through the normal native pipeline. Reads never process anything.
"""
from __future__ import annotations

import hmac
import re
import threading

from flask import Blueprint, jsonify, request

import config as cfg

# Exact paths served (checked by auth.py so unknown /api/v1 paths get no bypass).
ROUTE_RE = re.compile(r"^/api/v1/(status|request|search|movie/\d+|tv/\d+)$")

_REQ_STATUS = {"pending": 1, "approved": 2, "denied": 3, "failed": 4,
               "completed": 5, "available": 5}
IMDB_RE = re.compile(r"^tt\d{5,}$")
_create_lock = threading.Lock()


def is_enabled() -> bool:
    return bool(cfg.SEERR_COMPAT_ENABLED)


def _owner(db):
    """The configured admin row, or None if the configuration is not valid."""
    raw = (cfg.SEERR_COMPAT_USER_ID or "").strip()
    if not raw.isdigit() or not (cfg.SEERR_API_KEY or "").strip():
        return None
    u = db.get_user(int(raw))
    if u and u.get("enabled") and u.get("role") == "admin":
        return u
    return None


def _library_media_status(lib: str | None) -> int | None:
    # Seerr media status: 2 pending, 3 processing, 4 partial, 5 available.
    return {"success": 5, "pending": 2, "wanted": 3, "upcoming": 3,
            "processing": 3}.get(lib or "")


def _request_status(row: dict) -> int:
    lib = row.get("library_status")
    if lib == "failed":
        return 4
    base = _REQ_STATUS.get(row.get("status") or "", 1)
    if base == 2 and lib == "success":
        return 5
    return base


def _request_json(row: dict) -> dict:
    seasons = [int(s) for s in (row.get("seasons") or "").split(",") if s.strip().isdigit()]
    media = {"mediaType": row["media_type"], "tmdbId": row.get("tmdb_id"),
             "status": _library_media_status(row.get("library_status")) or 1}
    if row.get("imdb_id"):
        media["imdbId"] = row["imdb_id"]
    return {"id": row["id"], "status": _request_status(row), "type": row["media_type"],
            "media": media, "createdAt": row.get("created_at"),
            "requestedBy": {"id": row["user_id"], "displayName": row.get("username", "")},
            "seasons": [{"seasonNumber": s} for s in seasons]}


def _media_info(db, imdb_id: str | None) -> dict | None:
    if not imdb_id:
        return None
    r = db.get_request_by_imdb(imdb_id)
    st = _library_media_status((r or {}).get("status"))
    return {"status": st} if st else None


def _int(value, lo=1, hi=2 ** 31 - 1):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        n = int(value)
    except ValueError:
        return None
    return n if lo <= n <= hi else None


def create_blueprint(db, tmdb, kick_off, version: str) -> Blueprint:
    """kick_off(title, imdb_id, media_type, tmdb_id) starts native processing."""
    bp = Blueprint("seerr_compat", __name__, url_prefix="/api/v1")

    @bp.before_request
    def _guard():
        if not is_enabled():
            return jsonify(message="not found"), 404
        if _owner(db) is None:
            return jsonify(message="service not configured"), 503
        if request.endpoint == "seerr_compat.status":
            return None
        sent = request.headers.get("X-Api-Key", "")
        if not sent or not hmac.compare_digest(sent.encode(), cfg.SEERR_API_KEY.encode()):
            return jsonify(message="unauthorized"), 401
        return None

    @bp.get("/status")
    def status():
        return jsonify(version=version, commitTag="", status="ok")

    @bp.get("/request")
    def list_requests():
        take = _int(request.args.get("take", 20), 1, 100)
        skip = _int(request.args.get("skip", 0), 0, 10 ** 6)
        if take is None or skip is None:
            return jsonify(message="invalid paging"), 400
        if request.args.get("sort", "added") != "added":
            return jsonify(message="unsupported sort"), 400
        rows, total = db.list_user_requests_page(take, skip)
        return jsonify(pageInfo={"pages": (total + take - 1) // take,
                                 "pageSize": take, "results": total,
                                 "page": skip // take + 1},
                       results=[_request_json(r) for r in rows])

    @bp.get("/search")
    def search():
        q = (request.args.get("query") or "").strip()
        if not q or len(q) > 200:
            return jsonify(message="query required"), 400
        page = _int(request.args.get("page", 1), 1, 50) or 1
        try:
            items = tmdb.multi_search(q, page=page)
        except Exception:
            return jsonify(message="upstream unavailable"), 502
        out = []
        for it in items:
            kind = it.get("media_type")
            row = {"id": it.get("tmdb_id"), "mediaType": kind,
                   "overview": it.get("overview", ""), "posterPath": it.get("poster_path")}
            library_status = db.library_status_by_tmdb(kind, it.get("tmdb_id"))
            media_status = _library_media_status(library_status)
            if media_status:
                row["mediaInfo"] = {"status": media_status}
            if kind == "tv":
                row.update(name=it.get("title"), firstAirDate=it.get("release_date", ""))
            else:
                row.update(title=it.get("title"), releaseDate=it.get("release_date", ""))
            out.append(row)
        return jsonify(page=page, totalPages=page, totalResults=len(out), results=out)

    def _detail(kind: str, tmdb_id: int):
        try:
            d = tmdb.details(kind, tmdb_id)
        except Exception:
            return jsonify(message="upstream unavailable"), 502
        if not d:
            return jsonify(message="not found"), 404
        out = {"id": tmdb_id, "overview": d.get("overview", ""), "posterPath": d.get("poster_path")}
        mi = _media_info(db, d.get("imdb_id"))
        if mi:
            out["mediaInfo"] = mi
        if kind == "tv":
            out.update(name=d.get("title"), firstAirDate=d.get("release_date", ""),
                       numberOfSeasons=d.get("number_of_seasons") or 1)
        else:
            out.update(title=d.get("title"), releaseDate=d.get("release_date", ""))
        return jsonify(out)

    @bp.get("/movie/<int:tmdb_id>")
    def movie(tmdb_id):
        return _detail("movie", tmdb_id)

    @bp.get("/tv/<int:tmdb_id>")
    def tv(tmdb_id):
        return _detail("tv", tmdb_id)

    @bp.post("/request")
    def create_request():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(message="invalid body"), 400
        kind = body.get("mediaType")
        tmdb_id = _int(body.get("mediaId"))
        seasons = body.get("seasons")
        if kind not in ("movie", "tv") or tmdb_id is None:
            return jsonify(message="mediaType and mediaId required"), 400
        if seasons is not None and not (isinstance(seasons, list) and all(
                _int(s, 0, 1000) is not None for s in seasons)):
            return jsonify(message="invalid seasons"), 400
        owner = _owner(db)
        with _create_lock:
            existing = db.find_active_user_request(owner["id"], kind, tmdb_id)
            if existing:
                existing["username"] = owner["username"]
                existing["library_status"] = (db.get_request_by_imdb(existing["imdb_id"]) or {}).get("status")
                return jsonify(_request_json(existing)), 200
            try:
                d = tmdb.details(kind, tmdb_id)
                imdb_id = (d or {}).get("imdb_id") or tmdb.tmdb_to_imdb(tmdb_id, media_type=kind)
            except Exception:
                return jsonify(message="upstream unavailable"), 502
            title = (d or {}).get("title") or ""
            if not d or not title:
                return jsonify(message="could not resolve media"), 400
            if not IMDB_RE.match(imdb_id or ""):
                return jsonify(message="metadata identity unresolved"), 400
            # Seasons are not honoured: every TV season is requested.
            rid = db.create_user_request(owner["id"], imdb_id, tmdb_id, kind, title,
                                         status="approved")
        kick_off(title, imdb_id, kind, tmdb_id)
        row = db.get_user_request(rid)
        row["username"] = owner["username"]
        row["library_status"] = (db.get_request_by_imdb(imdb_id) or {}).get("status")
        return jsonify(_request_json(row)), 201

    return bp
