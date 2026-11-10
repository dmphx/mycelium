"""Explicit, reviewed Season 0 episode identities.

A season number never grants inclusion to all specials. Policies contain native
metadata IDs, titles and dates so numbering changes require review. This module
performs no network, database, filesystem or acquisition operation.
"""
import datetime
import re
import unicodedata

_MAX_SELECTED = 256
_IMDB = re.compile(r"tt[0-9]{7,10}\Z", re.ASCII)


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"invalid {name}")
    return value


def _title(value):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("invalid selected special title")
    value = " ".join(unicodedata.normalize("NFC", value).split())
    if not value or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("invalid selected special title")
    return value


def _date(value):
    if not isinstance(value, str):
        raise ValueError("selected special needs a known air date")
    parsed = datetime.date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("noncanonical selected special air date")
    return value


def normalize_policy(imdb_id, tmdb_id, episodes):
    """Return a canonical policy; an empty or ambiguous selection is refused."""
    if not isinstance(imdb_id, str) or not _IMDB.fullmatch(imdb_id):
        raise ValueError("invalid selected show identity")
    _integer(tmdb_id, 1, 2**63 - 1, "selected show identity")
    if not isinstance(episodes, list) or not 1 <= len(episodes) <= _MAX_SELECTED:
        raise ValueError("an explicit bounded special selection is required")
    rows = []
    numbers, native_ids = set(), set()
    for ep in episodes:
        if not isinstance(ep, dict) or set(ep) != {"episode", "tmdb_episode_id", "title", "air_date"}:
            raise ValueError("invalid selected special policy fields")
        number = _integer(ep["episode"], 1, 9999, "special episode number")
        native_id = _integer(ep["tmdb_episode_id"], 1, 2**63 - 1, "special native identity")
        if number in numbers or native_id in native_ids:
            raise ValueError("duplicate selected special identity")
        numbers.add(number)
        native_ids.add(native_id)
        rows.append({"episode": number, "tmdb_episode_id": native_id,
                     "title": _title(ep["title"]), "air_date": _date(ep["air_date"])})
    return {"imdb_id": imdb_id, "tmdb_id": tmdb_id,
            "episodes": sorted(rows, key=lambda ep: ep["episode"])}


def select_catalog(policy, imdb_id, tmdb_id, episodes):
    """Select exact current metadata rows, refusing a missing or changed identity.

    Unselected extras are ignored. A selected number must have exactly one row
    and retain its native ID, name, date and Season 0 identity. The caller must
    obtain the catalogue for the supplied show ID; an echoed show ID, when
    supplied by the metadata service, is additionally checked here.
    """
    if not isinstance(policy, dict) or set(policy) != {"imdb_id", "tmdb_id", "episodes"}:
        raise ValueError("invalid special selection policy")
    checked = normalize_policy(policy["imdb_id"], policy["tmdb_id"], policy["episodes"])
    if checked["imdb_id"] != imdb_id or checked["tmdb_id"] != tmdb_id:
        raise ValueError("selected show identity changed")
    if not isinstance(episodes, list) or len(episodes) > 10000:
        raise ValueError("invalid special catalogue size")
    wanted = {ep["episode"]: ep for ep in checked["episodes"]}
    found = {}
    for row in episodes:
        if not isinstance(row, dict):
            raise ValueError("malformed special catalogue row")
        number = row.get("episode_number")
        if type(number) is not int or number not in wanted:
            continue
        if number in found:
            raise ValueError("ambiguous selected special identity")
        expected = wanted[number]
        if (type(row.get("id")) is not int or row["id"] != expected["tmdb_episode_id"]
                or type(row.get("season_number")) is not int or row["season_number"] != 0
                or ("show_id" in row and (type(row["show_id"]) is not int or row["show_id"] != tmdb_id))
                or _title(row.get("name")) != expected["title"]
                or _date(row.get("air_date")) != expected["air_date"]):
            raise ValueError("selected special metadata identity changed")
        found[number] = row.copy()
    if set(found) != set(wanted):
        raise ValueError("missing selected special metadata identity")
    return [found[number] for number in sorted(found)]
