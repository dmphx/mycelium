"""Strict file proof for the cached-pack exception, never a ranking fallback.

This version admits only the episode-title identity route. It does not infer
numbering aliases from a pack name, a largest file, or a fail-open sanity check.
"""
from __future__ import annotations

import json
import hashlib
from pathlib import PurePosixPath
import re
from types import SimpleNamespace

OUTCOMES = frozenset({
    "not_evaluated", "notcached", "missing_listing", "file_refusal",
    "numbering_refusal", "title_refusal", "identity_refusal", "eligible",
    "writer_conflict", "provider_error", "selected", "nzb_unusable",
})
_TAG = re.compile(r"(?<![A-Za-z0-9])S(\d{1,4})E(\d{1,4})(?![A-Za-z0-9])", re.I)
_HASH = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_FIELDS = {"version", "info_hash", "imdb_id", "show_title", "season", "episode",
           "episode_title", "file_name", "file_size"}


def _path(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    value = value.replace("\\", "/")
    path = PurePosixPath(value)
    if (path.is_absolute() or any(p in (".", "..") for p in value.split("/"))
            or any(ord(c) < 32 for c in value) or any(c in value for c in ":?#")
            or re.search(r"token\s*=|api[_-]?key\s*=|authorization\s*=", value, re.I)):
        return None
    return value


def _text(value):
    return (isinstance(value, str) and 0 < len(value) <= 512
            and not any(ord(c) < 32 for c in value)
            and not re.search(r"https?://|token[=:]|api[_-]?key[=:]", value, re.I))


def validate_proof(value):
    """Validate only the finite, nonsecret proof representation we persist."""
    if isinstance(value, str):
        if len(value) > 8192:
            raise ValueError("episode file proof bound")
        def unique(rows):
            result = {}
            for key, val in rows:
                if key in result:
                    raise ValueError("duplicate proof key")
                result[key] = val
            return result
        value = json.loads(value, object_pairs_hook=unique)
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("episode file proof fields")
    if (type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["info_hash"], str)
            or not _HASH.fullmatch(value["info_hash"])
            or not isinstance(value["imdb_id"], str)
            or not re.fullmatch(r"tt\d+", value["imdb_id"])
            or not all(_text(value[k]) for k in ("show_title", "episode_title"))
            or _path(value["file_name"]) != value["file_name"]
            or any(type(value[k]) is not int or not 1 <= value[k] <= 9999
                   for k in ("season", "episode"))
            or type(value["file_size"]) is not int or value["file_size"] <= 0):
        raise ValueError("episode file proof values")
    if not _identity_in_file(value["file_name"], value["show_title"],
                             value["season"], value["episode"], value["episode_title"]):
        raise ValueError("episode file proof identity")
    return dict(value)


def _identity_in_file(name, show, season, episode, title):
    import torrentio
    from strm_generator import _is_video, _is_trailer
    if not _path(name) or not _is_video(name) or _is_trailer({"name": name}):
        return False
    base = PurePosixPath(name).name
    tags = list(_TAG.finditer(base))
    if len(tags) != 1 or tuple(map(int, tags[0].groups())) != (season, episode):
        return False
    # Multi-episode ranges/concatenations cannot prove one selected episode.
    if re.match(r"^(?:[ ._]*E\d|[ ._]*[-+&]\s*E?\d|[ ._]+to[ ._]+E?\d)",
                base[tags[0].end():], re.I):
        return False
    # The exact show must precede the episode tag. A different show in a
    # coincidentally named directory is not identity evidence.
    prefix = torrentio._normalized_words(base[:tags[0].start()])
    wanted = torrentio._normalized_words(show)
    if prefix != wanted:
        return False
    expected = torrentio._normalized_words(title)
    if len(expected) < 3 or re.fullmatch(r"(?:episode \d+|tba|tbd)", expected):
        return False
    return torrentio._episode_title_match(SimpleNamespace(name=base, title=""), title)


def prove_file(candidate, entry, *, imdb_id, show_title, season, episode, episode_title):
    """(proof, outcome). Missing listings and unknown show identities refuse.

    Caller supplies an already ranked, cached and sanity-filtered candidate.
    We independently retain the existing listing and series identity guards.
    No successful generic-pack exception is possible without a specific file.
    """
    import release_sanity
    if not isinstance(entry, dict) or not isinstance(entry.get("files"), list) or not entry["files"]:
        return None, "missing_listing"
    if len(entry["files"]) > 10000:
        return None, "file_refusal"
    declared_hash = entry.get('hash') or entry.get('info_hash')
    if declared_hash is not None and str(declared_hash).lower() != candidate.info_hash.lower():
        return None, 'identity_refusal'
    identity = release_sanity.series_identity(imdb_id, show_title)
    if (identity is None or identity.imdb_id != imdb_id or not identity.name
            or not _text(episode_title)):
        return None, "identity_refusal"
    if release_sanity.verify_entry(entry, "episode", season=season, episode=episode,
                                   imdb_id=imdb_id):
        return None, "file_refusal"
    found = []
    saw_number = False
    for file in entry["files"]:
        if not isinstance(file, dict):
            return None, "file_refusal"
        name = _path(file.get("name") or file.get("short_name"))
        size = file.get("size")
        if not name or type(size) is not int or size <= 0:
            continue
        tags = list(_TAG.finditer(PurePosixPath(name).name))
        if len(tags) == 1 and tuple(map(int, tags[0].groups())) == (season, episode):
            saw_number = True
        if not _identity_in_file(name, identity.name, season, episode, episode_title):
            continue
        if release_sanity.executable_reason(name):
            return None, "file_refusal"
        verdict, _ = release_sanity.classify_release(name, identity)
        if verdict not in (release_sanity.IDENTITY_MATCH, release_sanity.IDENTITY_NEUTRAL):
            return None, "identity_refusal"
        found.append({"version": 1, "info_hash": candidate.info_hash.lower(),
                      "imdb_id": imdb_id, "show_title": identity.name,
                      "season": season, "episode": episode, "episode_title": episode_title,
                      "file_name": name, "file_size": size})
    if len(found) != 1:
        return None, "file_refusal" if found else ("title_refusal" if saw_number else "numbering_refusal")
    try:
        return validate_proof(found[0]), "eligible"
    except ValueError:
        return None, "identity_refusal"


def match_proven_file(proof, listing, item):
    """Resolve exactly the proved file, including after a provider id changes."""
    try:
        proof = validate_proof(proof)
    except (ValueError, TypeError, KeyError):
        return None
    if (proof["info_hash"] != str(item.get("info_hash") or "").lower()
            or proof["imdb_id"] != item.get("imdb_id")
            or proof["season"] != item.get("season") or proof["episode"] != item.get("episode")
            or not isinstance(listing, dict) or not isinstance(listing.get("files"), list)):
        return None
    matches = [file for file in listing["files"] if isinstance(file, dict)
               and _path(file.get("name") or file.get("short_name")) == proof["file_name"]
               and type(file.get("size")) is int and file["size"] == proof["file_size"]
               and type(file.get("id")) is int and file["id"] > 0]
    return matches[0] if len(matches) == 1 else None


def item_binding(item):
    """Bind a cached URL to the same proof and virtual-item identity."""
    if not isinstance(item, dict) or item.get('episode_file_proof') is None:
        return None
    proof = validate_proof(item['episode_file_proof'])
    if (proof['info_hash'] != item.get('info_hash') or proof['imdb_id'] != item.get('imdb_id')
            or proof['season'] != item.get('season') or proof['episode'] != item.get('episode')
            or item.get('media_type') != 'series'
            or (item.get('protocol') or 'torrent') != 'torrent'
            or (item.get('debrid_provider') or 'torbox') != 'torbox'):
        raise ValueError('proved item binding changed')
    return hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
