"""Strict raw TMDB metadata resolver for request intents (pure, no database).

Resolves the trusted title, IMDb id and the exact episodes an intent scope names,
from raw TMDB ids only. It never maps one TMDB show onto another, never looks a
title up by IMDb id, and never calls helpers that turn an outage into an empty
result. Anything that cannot be fully validated is a 503 `metadata_unavailable`;
only a valid, complete response that lacks a named season or episode is a 404
`scope_not_found`. Resolving says nothing about registration or playability.

Invalid caller input raises request_intents.IntentError (400) from
normalize_payload. Nothing here writes, charges quota or imports the database.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone

MAX_SEASONS = 100
MAX_EPISODES = 5000
MAX_TITLE = 300
MAX_RUNTIME_MIN = 1440
_IMDB = re.compile(r"tt[0-9]{6,10}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class MetadataError(Exception):
    def __init__(self, code: str, status: int, message: str = ""):
        super().__init__(message or code)
        self.code, self.status, self.message = code, status, message or code


def _unavailable(msg: str = "metadata unavailable"):
    raise MetadataError("metadata_unavailable", 503, msg)


def _not_found(msg: str):
    raise MetadataError("scope_not_found", 404, msg)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _iso(value, *, nullable: bool) -> str | None:
    """A real calendar date as YYYY-MM-DD, or None where allowed."""
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        _unavailable("bad date")
    try:
        date.fromisoformat(value)
    except ValueError:
        _unavailable("bad date")
    return value


def _year(value) -> int | None:
    """Catalog year from a release or first-air date; empty/absent means unknown."""
    if value is None or value == "":
        return None
    return int(_iso(value, nullable=False)[:4])


def _title(value) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_TITLE \
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        _unavailable("bad title")
    return value.strip()


def _today(today) -> date:
    if today is None:
        return datetime.now(timezone.utc).date()
    if isinstance(today, datetime):
        return (today.astimezone(timezone.utc) if today.tzinfo else today).date()
    if isinstance(today, date):
        return today
    if isinstance(today, str):
        return date.fromisoformat(_iso(today, nullable=False))
    raise TypeError("today must be a date or ISO string")


def _default_fetch(path: str):
    import tmdb  # lazy: keeps this module importable without TMDB configuration
    return tmdb._get(path)


class _Source:
    def __init__(self, fetch):
        self._fetch = fetch or _default_fetch

    def get(self, path: str) -> dict:
        try:
            data = self._fetch(path)
        except Exception:
            _unavailable()
        if not isinstance(data, dict) or not data:
            _unavailable()
        return data


def _imdb(src: _Source, kind: str, tmdb_id: int) -> str:
    data = src.get(f"/{kind}/{tmdb_id}/external_ids")
    if "id" in data and (not _is_int(data["id"]) or data["id"] != tmdb_id):
        _unavailable("external ids identity mismatch")
    imdb = data.get("imdb_id")
    if not isinstance(imdb, str) or not _IMDB.fullmatch(imdb):
        _unavailable("no usable imdb id")
    return imdb


def _show_seasons(show: dict) -> dict[int, int | None]:
    raw = show.get("seasons")
    if not isinstance(raw, list) or len(raw) > 1000:
        _unavailable("seasons missing")
    out: dict[int, int | None] = {}
    for row in raw:
        if not isinstance(row, dict) or not _is_int(row.get("season_number")) \
                or not 0 <= row["season_number"] <= 999 or row["season_number"] in out:
            _unavailable("bad season list")
        count = row.get("episode_count")
        if count is not None and (not _is_int(count) or count < 0):
            _unavailable("bad episode_count")
        out[row["season_number"]] = count
    return out


def _season_episodes(src: _Source, show_id: int, season: int, expected: int | None) -> list[dict]:
    data = src.get(f"/tv/{show_id}/season/{season}")
    # The id in a season response is the season's own id, never the show's.
    if not _is_int(data.get("season_number")) or data["season_number"] != season:
        _unavailable("season identity mismatch")
    eps = data.get("episodes")
    if not isinstance(eps, list) or len(eps) > MAX_EPISODES:
        _unavailable("episodes missing")
    if expected is not None and len(eps) != expected:
        _unavailable("incomplete season")
    out, pairs, ids = [], set(), set()
    for e in eps:
        if not isinstance(e, dict):
            _unavailable("bad episode")
        eid, s, n = e.get("id"), e.get("season_number"), e.get("episode_number")
        if not (_is_int(eid) and 0 < eid <= 2**31 - 1 and _is_int(s) and s == season
                and _is_int(n) and 1 <= n <= 9999):
            _unavailable("bad episode identity")
        if "show_id" in e and (not _is_int(e["show_id"]) or e["show_id"] != show_id):
            _unavailable("episode belongs to another show")
        if (s, n) in pairs or eid in ids:
            _unavailable("duplicate episode")
        pairs.add((s, n))
        ids.add(eid)
        if "air_date" not in e:
            _unavailable("air_date missing")
        rt = e.get("runtime")
        if rt is not None and (not _is_int(rt) or not 0 <= rt <= MAX_RUNTIME_MIN):
            _unavailable("bad runtime")
        if isinstance(rt, float) and not math.isfinite(rt):
            _unavailable("bad runtime")
        out.append({"tmdb_episode_id": eid, "season": s, "episode": n,
                    "name": _title(e.get("name")),
                    "air_date": _iso(e.get("air_date"), nullable=True),
                    "runtime_min": rt})
    return out


def _air_state(air_date: str | None, today: date) -> str:
    if air_date is None:
        return "unknown"
    return "aired" if date.fromisoformat(air_date) <= today else "unaired"


def resolve(payload, *, today=None, fetch=None) -> dict:
    """Resolve a normalized (or normalizable) {media, scope} payload.

    Returns {media, scope, title, imdb_id, year, cutoff, episodes[]} where each
    episode is {tmdb_episode_id, season, episode, name, air_date, runtime_min,
    air_state}. `fetch(path) -> dict | None` may be injected for tests; the default
    is tmdb._get. `today` (date or ISO string) defaults to the UTC date and is the
    `future` cutoff, with same-day episodes included."""
    import request_intents
    norm = request_intents.normalize_payload(payload)
    media, scope = norm["media"], norm["scope"]
    day = _today(today)
    src = _Source(fetch)
    tid = media["tmdb_id"]

    if media["type"] == "movie":
        movie = src.get(f"/movie/{tid}")
        if not _is_int(movie.get("id")) or movie["id"] != tid:
            _unavailable("movie identity mismatch")
        return {"media": media, "scope": scope, "title": _title(movie.get("title")),
                "imdb_id": _imdb(src, "movie", tid), "year": _year(movie.get("release_date")),
                "cutoff": day.isoformat(), "episodes": []}

    show = src.get(f"/tv/{tid}")
    if not _is_int(show.get("id")) or show["id"] != tid:
        _unavailable("show identity mismatch")
    title = _title(show.get("name"))
    year = _year(show.get("first_air_date"))
    known = _show_seasons(show)

    kind = scope["kind"]
    if kind == "episodes":
        wanted: dict[int, set] = {}
        for e in scope["episodes"]:
            wanted.setdefault(e["season"], set()).add(e["episode"])
        seasons = sorted(wanted)
    elif kind == "selected_seasons":
        seasons = list(scope["seasons"])
    else:  # all_seasons, future: regular seasons only; specials need an explicit scope
        seasons = sorted(s for s in known if s >= 1)
    if len(seasons) > MAX_SEASONS:
        _unavailable("too many seasons")
    for s in seasons:
        if s not in known:
            _not_found(f"season {s} not found")

    imdb = _imdb(src, "tv", tid)
    episodes: list[dict] = []
    observed_ids, observed_count = set(), 0
    for s in seasons:
        rows = _season_episodes(src, tid, s, known[s])
        observed_count += len(rows)
        ids = {r["tmdb_episode_id"] for r in rows}
        if observed_count > MAX_EPISODES or observed_ids & ids:
            _unavailable("inconsistent or over-budget episode catalog")
        observed_ids.update(ids)
        if kind == "episodes":
            by_num = {r["episode"]: r for r in rows}
            for n in sorted(wanted[s]):
                if n not in by_num:
                    _not_found(f"season {s} episode {n} not found")
            rows = [by_num[n] for n in sorted(wanted[s])]
        elif kind == "future":
            rows = [r for r in rows
                    if r["air_date"] is None or date.fromisoformat(r["air_date"]) >= day]
        for r in rows:
            r["air_state"] = _air_state(r["air_date"], day)
        episodes.extend(rows)
        if len(episodes) > MAX_EPISODES:
            _unavailable("too many episodes")
    return {"media": media, "scope": scope, "title": title, "imdb_id": imdb, "year": year,
            "cutoff": day.isoformat(), "episodes": episodes}
