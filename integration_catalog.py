"""Pure builders for the frozen integration v1 catalog data (data_contract).

Every builder takes raw TMDB JSON that the caller already fetched (see
tmdb_checked) and returns the exact contract object, or raises CatalogUnavailable
(the route maps it to 503). Nothing here does I/O, reads settings or knows about
library presence, entitlement, IMDb ids or provider secrets. The clock is injected
through `today`.
"""
import datetime
import json
import math
import re

MAX_ID = 2147483647
MAX_PAGE = 500
MAX_LIST_ITEMS = 100
MAX_CREDITS = 3000
MAX_FILMOGRAPHY = 100
MAX_PAYLOAD_BYTES = 900000
MAX_TOTAL_PAGES = 1000000

_IMG = "https://image.tmdb.org/t/p/"
_IMG_PATH_RE = re.compile(r"/[A-Za-z0-9_-]{1,100}\.(?:jpg|jpeg|png)")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_REGION_RE = re.compile(r"[A-Z]{2}")
_YT_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_INFO_URL_RE = re.compile(r"https://www\.themoviedb\.org/[A-Za-z0-9/_?=&.%-]{0,300}")
_MONETIZATION = ("flatrate", "rent", "buy")
_TYPES = ("movie", "tv")
MAX_SHORT = 300
MAX_LONG_SHORT = 1000
MAX_PROSE = 200000


class CatalogUnavailable(Exception):
    """Raw metadata is missing, malformed or too large; the route answers 503."""


class CatalogNotFound(CatalogUnavailable):
    """Well-formed raw data that genuinely lacks the requested item (later 404)."""


def _bad(reason):
    raise CatalogUnavailable(reason)


# --- primitives -------------------------------------------------------------

def _int_id(value, reason="bad_id"):
    if type(value) is not int or not 1 <= value <= MAX_ID:
        _bad(reason)
    return value


def _nonneg_int(value, reason):
    if type(value) is not int or not 0 <= value <= MAX_ID:
        _bad(reason)
    return value


def _same(value, expected, reason="parent_mismatch"):
    """Strict equality: real int (not bool/float) AND equal."""
    if type(value) is not int or type(expected) is not int or value != expected:
        _bad(reason)


def _same_if_present(obj, key, expected, reason="parent_mismatch"):
    if key in obj:
        _same(obj[key], expected, reason)


def _runtime(value, reason):
    """None/0 mean unknown; otherwise a real int 1..1440 (no bool, no float)."""
    if value is None or (type(value) is int and value == 0):
        return None
    if type(value) is not int or not 1 <= value <= 1440:
        _bad(reason)
    return value


def _forbidden(ch, allow_ws):
    o = ord(ch)
    if allow_ws and ch in "\n\t":
        return False
    return o < 0x20 or 0x7F <= o <= 0x9F or 0xD800 <= o <= 0xDFFF


def short_text(value, reason="bad_text", *, required=True, limit=MAX_SHORT):
    """Names and titles: no control characters at all (not even LF or tab), at
    most `limit` characters. Unicode format characters such as ZWJ are kept."""
    if type(value) is not str or len(value) > limit or any(_forbidden(c, False) for c in value):
        _bad(reason)
    if required and not value.strip():
        _bad(reason)
    return value


def prose(value, reason="bad_text"):
    """Overview/biography: LF and tab allowed, CRLF and CR normalised to LF,
    every other C0/C1 control and surrogate rejected, at most 200000 characters."""
    if value is None:
        return ""
    if type(value) is not str or len(value) > MAX_PROSE:
        _bad(reason)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if any(_forbidden(c, True) for c in value):
        _bad(reason)
    return value


def _date(value, reason="bad_date"):
    """None or "" -> None. Otherwise a real YYYY-MM-DD calendar date."""
    if value is None or value == "":
        return None
    if type(value) is not str or not _DATE_RE.fullmatch(value):
        _bad(reason)
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        _bad(reason)
    return value


def _rating(value):
    if value is None:
        return None
    if type(value) not in (int, float) or not 0 <= value <= 10 or not math.isfinite(value):
        _bad("bad_rating")
    return value


def _image(path, size):
    """Optional image: a bad path is omitted, never an error."""
    if type(path) is not str or not _IMG_PATH_RE.fullmatch(path):
        return None
    return f"{_IMG}{size}{path}"


def _put_image(out, key, path, size):
    url = _image(path, size)
    if url:
        out[key] = url


def _obj(value, reason="bad_object"):
    if type(value) is not dict:
        _bad(reason)
    return value


def _list(value, reason="bad_array", cap=None):
    if type(value) is not list or (cap is not None and len(value) > cap):
        _bad(reason)
    return value


def _media_type(value):
    if value not in _TYPES:
        _bad("bad_media_type")
    return value


def _region(value):
    if type(value) is not str or not _REGION_RE.fullmatch(value):
        _bad("bad_region")
    return value


def _page(value):
    if type(value) is not int or not 1 <= value <= MAX_PAGE:
        _bad("bad_page")
    return value


def _total_pages(value):
    if type(value) is not int or not 0 <= value <= MAX_TOTAL_PAGES:
        _bad("bad_total_pages")
    return value


def _popularity(value):
    if value is None:
        return 0.0
    if type(value) not in (int, float):
        _bad("bad_popularity")
    try:
        if not math.isfinite(value):
            _bad("bad_popularity")
    except OverflowError:
        _bad("bad_popularity")
    return float(value)


# --- Title ------------------------------------------------------------------

def title(raw, media_type=None):
    """Title object. `media_type` fixes the type for typed endpoints (a raw item
    that states a conflicting media_type is rejected); for multi search leave it
    None and the raw item's own media_type decides."""
    raw = _obj(raw, "bad_title")
    if media_type is not None:
        kind = _media_type(media_type)
        if "media_type" in raw and raw["media_type"] != kind:
            _bad("media_type_conflict")
    else:
        kind = _media_type(raw.get("media_type"))
    out = {
        "tmdb_id": _int_id(raw.get("id")),
        "media_type": kind,
        "title": short_text(raw.get("title" if kind == "movie" else "name"), "bad_title"),
        "overview": prose(raw.get("overview")),
    }
    _put_image(out, "poster_url", raw.get("poster_path"), "w500")
    _put_image(out, "backdrop_url", raw.get("backdrop_path"), "w1280")
    when = _date(raw.get("release_date" if kind == "movie" else "first_air_date"))
    if when:
        out["release_date" if kind == "movie" else "first_air_date"] = when
    rating = _rating(raw.get("vote_average"))
    if rating is not None:
        out["vote_average"] = rating
    return out


def _titles(results, media_type, *, cap=MAX_LIST_ITEMS, filter_person=False):
    items = []
    for item in _list(results, "bad_results", cap):
        if filter_person and type(item) is dict and item.get("media_type") == "person":
            continue
        items.append(title(item, media_type))
    return items


def _paged(raw, page, kind, filter_person):
    raw = _obj(raw)
    page = _page(page)
    _same(raw.get("page"), page, "page_mismatch")
    items = _titles(raw.get("results"), kind, filter_person=filter_person)
    total = _total_pages(raw.get("total_pages"))
    if raw["results"] and (total == 0 or page > total):
        _bad("page_mismatch")
    if "total_results" in raw:
        count = _nonneg_int(raw["total_results"], "bad_total_results")
        if (total == 0 and count != 0) or (count == 0 and raw["results"]):
            _bad("page_mismatch")
    return {"items": items, "page": page, "total_pages": total}


def catalog_list(raw, media_type, page):
    """Typed rail/discover list. page is the requested page (1..500); raw page
    must be a real int equal to it."""
    return _paged(raw, page, _media_type(media_type), False)


def search(raw, page, media_type=None):
    """Search. media_type None = multi (raw items carry their own type, person
    results are filtered out); movie/tv = typed search."""
    kind = None if media_type is None else _media_type(media_type)
    return _paged(raw, page, kind, kind is None)


def trending(raw, page, media_type="all"):
    """/trending/{all|movie|tv}: 'all' is multi-typed, the others typed."""
    return search(raw, page, None if media_type == "all" else media_type)


# --- Detail -----------------------------------------------------------------

def _genres(items):
    out = []
    for g in _list(items, "bad_genres"):
        g = _obj(g, "bad_genres")
        out.append({"genre_id": _int_id(g.get("id"), "bad_genres"),
                    "name": short_text(g.get("name"), "bad_genres")})
    return out


def _block(raw, key, tmdb_id):
    """Appended block must exist as an object; its own id, when present, must be
    the parent id (child ids inside are not compared)."""
    block = _obj(raw.get(key), "missing_" + key.replace("/", "_"))
    _same_if_present(block, "id", tmdb_id)
    return block


def _credit_rows(rows, field):
    out = []
    for c in _list(rows, "bad_credits", MAX_CREDITS):
        c = _obj(c, "bad_credits")
        row = {"person_id": _int_id(c.get("id"), "bad_credits"),
               "name": short_text(c.get("name"), "bad_credits"),
               field: short_text("" if c.get(field) is None else c[field], "bad_credits", required=False,
                                 limit=MAX_LONG_SHORT)}
        _put_image(row, "profile_url", c.get("profile_path"), "w185")
        out.append(row)
    return out


def _credits(raw, tmdb_id):
    block = _block(raw, "credits", tmdb_id)
    return {"cast": _credit_rows(block.get("cast"), "character"),
            "crew": _credit_rows(block.get("crew"), "job")}


def _recommendations(raw, tmdb_id, kind):
    block = _block(raw, "recommendations", tmdb_id)
    return {"source": "tmdb", "personalized": False,
            "items": _titles(block.get("results"), kind, cap=MAX_CREDITS)}


def _trailers(raw, tmdb_id):
    block = _block(raw, "videos", tmdb_id)
    out = []
    for v in _list(block.get("results"), "bad_videos", MAX_CREDITS):
        # optional extras: a malformed or non-YouTube entry is skipped
        if type(v) is not dict or v.get("site") != "YouTube" or v.get("type") != "Trailer":
            continue
        key, name = v.get("key"), v.get("name")
        if type(key) is str and _YT_KEY_RE.fullmatch(key) and type(name) is str \
                and len(name) <= MAX_SHORT and not any(_forbidden(c, False) for c in name):
            out.append({"site": "YouTube", "key": key, "name": name})
    return out


def _info_url(link):
    if type(link) is str and _INFO_URL_RE.fullmatch(link):
        return link
    return None


def watch_providers(block, region):
    """`block` = raw {"results": {REGION: {link, flatrate, rent, buy}}}; the
    results object must exist (empty is a valid empty map). No invented fallback:
    a region TMDB has no entry for yields an empty list."""
    region = _region(region)
    results = _obj(_obj(block, "missing_watch_providers").get("results"), "missing_watch_providers")
    entry = results.get(region)
    out = {"region": region, "items": []}
    if entry is None:
        return out
    entry = _obj(entry)
    seen = set()
    for mono in _MONETIZATION:
        rows = entry.get(mono)
        for p in ([] if rows is None else _list(rows, "bad_providers", MAX_CREDITS)):
            p = _obj(p, "bad_providers")
            pid = _int_id(p.get("provider_id"), "bad_providers")
            if (pid, mono) in seen:
                continue
            seen.add((pid, mono))
            row = {"provider_id": pid,
                   "name": short_text(p.get("provider_name"), "bad_providers"),
                   "monetization": mono}
            _put_image(row, "logo_url", p.get("logo_path"), "w92")
            out["items"].append(row)
    url = _info_url(entry.get("link"))
    if url:
        out["info_url"] = url
    return out


def _season_rows(raw):
    """Every show.seasons member is validated; a duplicate or malformed row is a
    503 even if it is not the one requested."""
    rows, seen = [], set()
    for s in _list(raw.get("seasons"), "bad_seasons", MAX_CREDITS):
        s = _obj(s, "bad_seasons")
        num = _nonneg_int(s.get("season_number"), "bad_seasons")
        if num in seen:
            _bad("bad_seasons")
        seen.add(num)
        rows.append((s, num, _nonneg_int(s.get("episode_count"), "bad_seasons")))
    return rows


def _season_summaries(raw):
    out = []
    for s, num, count in _season_rows(raw):
        row = {"season_number": num,
               "name": short_text(s.get("name"), "bad_seasons"),
               "episode_count": count,
               "air_date": _date(s.get("air_date"), "bad_seasons")}
        _put_image(row, "poster_url", s.get("poster_path"), "w500")
        out.append(row)
    return out


def detail(raw, media_type, tmdb_id, region):
    """Raw /movie|tv/{id} with appended credits, recommendations, videos and
    watch/providers. All appended blocks must be present (empty valid arrays and
    an empty region map are fine). Arrays are returned in full, never truncated."""
    kind = _media_type(media_type)
    tmdb_id = _int_id(tmdb_id)
    region = _region(region)
    raw = _obj(raw)
    _same(raw.get("id"), tmdb_id)
    out = title(raw, kind)
    out["genres"] = _genres(raw.get("genres"))
    if kind == "movie":
        runtime = raw.get("runtime")
    else:
        runs = raw.get("episode_run_time")
        runs = [] if runs is None else _list(runs, "bad_runtime", 100)
        for run in runs:
            _runtime(run, "bad_runtime")
        runtime = runs[0] if runs else None
    out["runtime_minutes"] = _runtime(runtime, "bad_runtime")
    out["status"] = short_text("" if raw.get("status") is None else raw["status"], "bad_status", required=False)
    if kind == "tv":
        out["seasons"] = _season_summaries(raw)
    out["credits"] = _credits(raw, tmdb_id)
    out["recommendations"] = _recommendations(raw, tmdb_id, kind)
    out["trailers"] = _trailers(raw, tmdb_id)
    out["watch_providers"] = watch_providers(_block(raw, "watch/providers", tmdb_id), region)
    return out


# --- Season -----------------------------------------------------------------

def season(raw_season, show_raw, tmdb_id, season_number, today):
    """Raw TMDB season numbering only. `today` is a datetime.date (UTC) used for
    air_state. complete is True only when the raw episode list matches the
    show's declared episode_count exactly (zero is a valid count). A well-formed
    show that does not declare the season raises CatalogNotFound."""
    tmdb_id = _int_id(tmdb_id)
    if type(season_number) is not int or not 0 <= season_number <= 9999:
        _bad("bad_season")
    if type(today) is not datetime.date:
        _bad("bad_today")
    show_raw = _obj(show_raw)
    _same(show_raw.get("id"), tmdb_id)
    declared = [(num, count) for _s, num, count in _season_rows(show_raw) if num == season_number]
    if not declared:
        raise CatalogNotFound("season_not_declared")
    expected = declared[0][1]
    raw_season = _obj(raw_season)
    _same(raw_season.get("season_number"), season_number, "season_mismatch")
    if "id" in raw_season:  # the season's own id, not the show id
        _int_id(raw_season["id"], "bad_season")
    _same_if_present(raw_season, "show_id", tmdb_id)
    eps = _list(raw_season.get("episodes"), "bad_episodes", MAX_CREDITS)
    if len(eps) != expected:
        _bad("incomplete_season")
    out, ids, pairs = [], set(), set()
    for e in eps:
        e = _obj(e, "bad_episodes")
        _same_if_present(e, "show_id", tmdb_id)
        eid = _int_id(e.get("id"), "bad_episodes")
        _same(e.get("season_number"), season_number, "season_mismatch")
        num = e.get("episode_number")
        if type(num) is not int or not 1 <= num <= 9999:
            _bad("bad_episodes")
        if eid in ids or (season_number, num) in pairs:
            _bad("duplicate_episode")
        ids.add(eid)
        pairs.add((season_number, num))
        aired = _date(e.get("air_date"), "bad_episodes")
        row = {"episode_id": eid, "season": season_number, "episode": num,
               "title": short_text(e.get("name"), "bad_episodes"),
               "overview": prose(e.get("overview")),
               "air_date": aired, "runtime_minutes": _runtime(e.get("runtime"), "bad_episodes")}
        _put_image(row, "still_url", e.get("still_path"), "w300")
        if aired is None:
            row["air_state"] = "unknown"
        else:
            row["air_state"] = "aired" if datetime.date.fromisoformat(aired) <= today else "unaired"
        out.append(row)
    out.sort(key=lambda r: r["episode"])
    return {"media": {"type": "tv", "tmdb_id": tmdb_id}, "season": season_number,
            "numbering": "tmdb", "complete": True, "episodes": out}


# --- Genres, providers, tabs ------------------------------------------------

def genres(raw, media_type):
    """`media_type` is explicit (movie|tv) so the two lists are never mixed."""
    _media_type(media_type)
    return {"items": _genres(_obj(raw).get("genres"))}


def providers(raw, media_type, region):
    _media_type(media_type)
    region = _region(region)
    items, seen = [], set()
    for p in _list(_obj(raw).get("results"), "bad_providers", MAX_CREDITS):
        p = _obj(p, "bad_providers")
        pid = _int_id(p.get("provider_id"), "bad_providers")
        if pid in seen:
            _bad("bad_providers")
        seen.add(pid)
        row = {"provider_id": pid, "name": short_text(p.get("provider_name"), "bad_providers")}
        _put_image(row, "logo_url", p.get("logo_path"), "w92")
        items.append(row)
    return {"region": region, "items": items}


def _year(v):
    if type(v) is not int or not 1800 <= v <= 3000:
        _bad("bad_tabs")
    return v


def genre_tabs(tabs):
    """`tabs` is the already-configured visible list (preference wiring comes
    later); validated, never invented. year_from must not exceed year_to."""
    out = []
    for t in _list(tabs, "bad_tabs", 200):
        t = _obj(t, "bad_tabs")
        row = {"media_type": _media_type(t.get("media_type")),
               "genre_id": _int_id(t.get("genre_id"), "bad_tabs"),
               "name": short_text(t.get("name"), "bad_tabs")}
        for key in ("year_from", "year_to"):
            v = t.get(key)
            row[key] = None if v is None else _year(v)
        if row["year_from"] is not None and row["year_to"] is not None \
                and row["year_from"] > row["year_to"]:
            _bad("bad_tabs")
        out.append(row)
    return {"items": out}


# --- Person -----------------------------------------------------------------

def person(raw, person_id):
    """Raw /person/{id} with appended combined_credits (cast and crew arrays
    required). Credits are merged and deduplicated by (media_type, id), a cast
    character wins, then ordered by popularity descending (absent = 0) then
    (media_type, id) before the 100 cap, so truncation is deterministic."""
    person_id = _int_id(person_id)
    raw = _obj(raw)
    _same(raw.get("id"), person_id)
    cc = _obj(raw.get("combined_credits"), "missing_combined_credits")
    _same_if_present(cc, "id", person_id)
    cast = _list(cc.get("cast"), "bad_credits", MAX_CREDITS)
    crew = _list(cc.get("crew"), "bad_credits", MAX_CREDITS)
    merged = {}
    for source, rows in (("cast", cast), ("crew", crew)):
        for c in rows:
            c = _obj(c, "bad_credits")
            entry = title(c)
            key = (entry["media_type"], entry["tmdb_id"])
            popularity = _popularity(c.get("popularity"))
            char = ""
            if source == "cast":
                char = short_text("" if c.get("character") is None else c["character"], "bad_credits", required=False,
                                  limit=MAX_LONG_SHORT)
            if key not in merged:
                merged[key] = (popularity, entry)
            else:
                old_popularity, old = merged[key]
                if {k: v for k, v in old.items() if k != "character"} != entry:
                    _bad("conflicting_credit")
                merged[key] = (max(old_popularity, popularity), old)
            if char:
                old_char = merged[key][1].get("character")
                merged[key][1]["character"] = min(old_char, char) if old_char else char
    ranked = sorted(merged.items(), key=lambda kv: (-kv[1][0], kv[0][0], kv[0][1]))
    films = [kv[1][1] for kv in ranked]
    out = {
        "person_id": person_id,
        "name": short_text(raw.get("name"), "bad_person"),
        "biography": prose(raw.get("biography")),
        "birth_date": _date(raw.get("birthday"), "bad_person"),
        "place_of_birth": (None if raw.get("place_of_birth") in (None, "") else
                           short_text(raw["place_of_birth"], "bad_person")),
        "filmography": films[:MAX_FILMOGRAPHY],
        "filmography_complete": len(films) <= MAX_FILMOGRAPHY,
    }
    _put_image(out, "profile_url", raw.get("profile_path"), "w185")
    return out


# --- Size check -------------------------------------------------------------

def serialize(data):
    """Size-check representation only, NOT the HTTP response. Returns compact
    ASCII bytes of {"api_version","data"} and raises CatalogUnavailable over
    900000 bytes (oversize is 503 until paging is designed; the peer BFF limit
    is 1 MiB). It carries no `meta`: routes must build the response with the
    existing ok() envelope, applying this check to `data` first, and must never
    emit these bytes as the contract response."""
    try:
        body = json.dumps({"api_version": "1", "data": data}, ensure_ascii=True,
                          separators=(",", ":"), allow_nan=False).encode("ascii")
    except (TypeError, ValueError, RecursionError):
        _bad("unserializable")
    if len(body) > MAX_PAYLOAD_BYTES:
        _bad("payload_too_large")
    return body
