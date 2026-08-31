"""Grab-time release sanity checks (mislabeled-pack guard).

Prevents mycelium from latching onto a cached torrent that obviously is not the
requested single movie or single episode. The classic failure this blocks:

    A request for the movie "2099: The Soldier Protocol" (imdb tt6437228) whose
    TMDB original_title is "The Wheel" scrapes back "The.Wheel.2019.*" results.
    None are cached, but a hash sharing the imdb_id IS cached  -  a 525 GB torrent
    named "Star Trek Complete Series in Stardate Watch Order - 1080p x265". Grab
    it and the request is marked success, yet playback 404s ("no playable file
    found") because there is no single-movie file to serve.

Two layers, cheap first:

  * NAME/SIZE heuristic (`movie_name_size_reject`)  -  pure, no network. A single
    movie far larger than MAX_MOVIE_SIZE_GB, or whose release name matches a
    season/series/multi-title pack pattern, is dropped at ranking time.

  * CACHED-FILE verification (`verify_entry` / `filter_cached` / `check_hash`)  -
    inspects the actual TorBox cached-files listing for a chosen hash and rejects
    it when it resolves to a pack / oversized file / no video / the wrong episode
    before the .strm is written and the request marked success.

The real fix lives at grab/selection time so a bad hash is never stored; the
catbox materialize guard is only a backstop. Both layers fail OPEN on infra
errors (an un-fetchable TorBox listing never blocks a grab) so a TorBox blip
cannot strand every request in 'wanted'.
"""
import logging
import re

log = logging.getLogger(__name__)

_BYTES_PER_GB = 1024 ** 3

# Fallbacks used only when the running config.py predates these knobs (e.g. a
# stale image during a rolling deploy). config.py is the authoritative source
# once deployed; keep these in rough sync with it.
_DEFAULT_MAX_MOVIE_SIZE_GB = 100.0
_DEFAULT_VERIFY = True
_DEFAULT_PACK_REGEX = (
    r"(?:"
    r"complete[\s._-]*(?:series|collection|seasons?|set|pack|saga|anthology)"
    r"|complete[\s._-]*(?:tv[\s._-]*)?show"
    r"|\bseasons\b"
    r"|\bseason[\s._-]*\d{1,2}[\s._-]*[-–][\s._-]*\d{1,2}\b"
    r"|\bseason[\s._-]*\d{1,2}\b"
    r"|\bs\d{1,2}[\s._-]*[-–][\s._-]*s?\d{1,2}\b"
    r"|\b\d{1,2}[\s._-]*[-–][\s._-]*\d{1,2}[\s._-]*seasons?\b"
    r"|\b(?:tri|du|quadri|penta|hexa)logy\b"
    r"|\b\d{1,3}[\s._-]*(?:movie|film)s?[\s._-]*(?:collection|pack|set|anthology)\b"
    r"|\b\d{2,3}[\s._-]*(?:movies|films)\b"
    r"|\bmega[\s._-]*pack\b"
    r"|\bbox[\s._-]?set\b"
    r"|\ball[\s._-]*\d{1,2}[\s._-]*(?:movies|films)\b"
    r")"
)

# Some release names spell out exactly which episodes they carry:
# "Серии 1-7 из 22" (RU, "episodes 1-7 of 22"), "Episodes 1-7", "E01-E07".
# That PARTIAL pack is the one pack shape which genuinely does NOT hold every
# episode of its season, so it needs a rule of its own  -  the generic pack-name
# pattern above deliberately accepts packs (see _verify_episode) and waves it
# through. The dash class is built with chr() so no literal en/em dash sits in
# this source file; release names in the wild use all three.
_EP_DASHES = "-" + chr(0x2013) + chr(0x2014)  # hyphen, en dash, em dash
_DASH = "[" + _EP_DASHES + "]"
# One episode number or a run of them: "5", "1-7", "1 to 7", "1-31, 33-77".
# Discontinuous runs are read whole so a gap-listing pack keeps its real upper
# bound ("Серии 1-31, 33-77 из 78" must not read as merely 1-31).
_EP_RUN = (r"\d{1,3}(?:\s*(?:" + _DASH + r"|to)\s*\d{1,3})?"
           r"(?:\s*,\s*\d{1,3}(?:\s*(?:" + _DASH + r"|to)\s*\d{1,3})?)*")
_EP_SPAN_RES = (
    # Cyrillic "Серия/Серии/Серий 1-7"; a trailing "из 22" (of 22) is the season
    # total, so the run stops before it.
    re.compile(r"сери[ияй][\s._]*(" + _EP_RUN + r")", re.IGNORECASE),
    # "Episodes 1-7", "Eps 1 to 7", "Ep 5"
    re.compile(r"\bep(?:isode)?s?[\s._]*(" + _EP_RUN + r")", re.IGNORECASE),
    # "S04E01-E07": an E on both ends, so spacing around the dash is safe. The
    # lookbehind allows the season digits before it while still keeping the
    # pattern from firing mid-word ("Se7en", "The01").
    re.compile(r"(?<![a-z])e(\d{1,3})[\s._]*" + _DASH + r"[\s._]*e(\d{1,3})\b",
               re.IGNORECASE),
    # "S05E09-10": bare second number, so the dash must be TIGHT. Loose spacing
    # here reads "Gunsmoke - S07E13 - 246 - Marry Me.avi" as episodes 13-246,
    # which would wave through every wrong-episode request for that file.
    re.compile(r"(?<![a-z])e(\d{1,3})" + _DASH + r"(\d{1,3})\b", re.IGNORECASE),
)

# Compiled-regex cache keyed on the pattern string so a per-candidate call does
# not recompile the (non-trivial) pack regex thousands of times in a sweep.
_pack_re_cache: dict[str, "re.Pattern | None"] = {}


def _config_val(name: str, fallback):
    """Read a config.py attribute, tolerating a stale config that predates it."""
    try:
        import config
        return getattr(config, name, fallback)
    except Exception:
        return fallback


def enabled() -> bool:
    """Whether grab-time cached-file verification is on (rank-time name/size
    filtering always runs; it is a pure sort input, not a network gate)."""
    import settings as _settings
    return bool(_settings.get("VERIFY_RELEASE_BEFORE_GRAB",
                              _config_val("VERIFY_RELEASE_BEFORE_GRAB", _DEFAULT_VERIFY)))


def _cfg() -> tuple[float, "re.Pattern | None"]:
    """(max_movie_size_gb, compiled pack regex) from the settings overlay with a
    config.py fallback, so both are retunable from the UI without a redeploy."""
    import settings as _settings
    max_movie_default = _config_val("MAX_MOVIE_SIZE_GB", _DEFAULT_MAX_MOVIE_SIZE_GB)
    regex_default = _config_val("SERIES_PACK_NAME_REGEX", _DEFAULT_PACK_REGEX)
    try:
        max_gb = float(_settings.get("MAX_MOVIE_SIZE_GB", max_movie_default) or 0)
    except (TypeError, ValueError):
        max_gb = float(max_movie_default or 0)
    pattern = _settings.get("SERIES_PACK_NAME_REGEX", regex_default)
    # A malformed DB override (or a mocked settings layer) must not crash ranking:
    # fall back to the config default when the value isn't a usable pattern string.
    if not isinstance(pattern, str):
        pattern = regex_default if isinstance(regex_default, str) else _DEFAULT_PACK_REGEX
    if pattern not in _pack_re_cache:
        try:
            _pack_re_cache[pattern] = re.compile(pattern, re.IGNORECASE) if pattern else None
        except re.error as exc:
            log.warning("Invalid SERIES_PACK_NAME_REGEX %r: %s  -  pack-name check disabled",
                        pattern, exc)
            _pack_re_cache[pattern] = None
    return max_gb, _pack_re_cache[pattern]


def name_looks_like_pack(name: str) -> bool:
    """True if a release name matches the season/series/multi-title pack pattern."""
    if not name:
        return False
    _, pack_re = _cfg()
    return bool(pack_re and pack_re.search(name))


def _declared_episode_span(name: str) -> tuple[int, int] | None:
    """(first, last) episode numbers a release name explicitly claims, or None.

    A bare single number yields (n, n). A span that runs backwards or starts
    below 1 is treated as a misparse and ignored.
    """
    if not name:
        return None
    for rx in _EP_SPAN_RES:
        m = rx.search(name)
        if not m:
            continue
        groups = [g for g in m.groups() if g]
        if not groups:
            continue
        # Word-form patterns capture the whole run as one string ("1-31, 33-77");
        # the ExxEyy forms capture the two endpoints. Min/max spans both, and a
        # gap inside a discontinuous run resolves toward accepting.
        nums = [int(n) for n in re.findall(r"\d{1,3}", " ".join(groups))]
        if not nums or min(nums) < 1:
            continue
        return min(nums), max(nums)
    return None


def _absolute_episode(imdb_id: str | None, season: int, episode: int) -> int | None:
    """Series-absolute number for S<season>E<episode>, or None if unavailable."""
    try:
        import numbering
        return numbering.to_absolute(imdb_id, int(season), int(episode))
    except Exception:
        return None


def _span_reject(span: tuple[int, int], top_name: str, season: int, episode: int,
                 imdb_id: str | None) -> str | None:
    """Reject a pack whose NAME declares an episode span missing the requested
    episode ("Сезон 22 Серии 1-7 из 22" grabbed for S22E10).

    Compared against absolute numbering too, since some packs number episodes
    across the whole series rather than per season. A single declared number in
    an otherwise pack-named release is too ambiguous to act on ("Complete Series
    Episode 1" is a full pack), so only a real span rejects in that case.
    """
    lo, hi = span
    if lo == hi and name_looks_like_pack(top_name):
        return None
    wanted = [int(episode)]
    absolute = _absolute_episode(imdb_id, season, episode)
    if absolute:
        # A broad E001-E167 or multi-season span is absolute numbering. Do not
        # let the raw within-season episode (for example TMDB Bleach S02E46)
        # collide with classic absolute E046 when the true TMDB absolute is 412.
        multi_season = bool(re.search(
            r"\bs(?:eason)?[ ._-]*\d{1,2}\s*[-–—~]\s*s?(?:eason)?[ ._-]*\d{1,2}\b",
            top_name or "", re.IGNORECASE,
        ))
        if int(absolute) != int(episode) and (hi >= 100 or multi_season):
            wanted = [int(absolute)]
        else:
            wanted.append(int(absolute))
    if any(lo <= n <= hi for n in wanted):
        return None
    declared = f"episode {lo}" if lo == hi else f"episodes {lo}-{hi}"
    return (f"name declares {declared}, not E{int(episode):02d} "
            f"({_short(top_name)})")


def movie_name_size_reject(name: str, size_gb: float) -> str | None:
    """Reason this NAME/SIZE cannot be a single movie, or None if it's plausible.

    Pure heuristic (no network). Used at ranking time to drop pack/oversized
    candidates before they can ever be added to TorBox.
    """
    max_gb, pack_re = _cfg()
    if size_gb and max_gb and size_gb > max_gb:
        return (f"size {size_gb:.1f}GB over movie cap {max_gb:.0f}GB "
                f"(almost always a pack/collection)")
    if pack_re and name and pack_re.search(name):
        return f"name matches season/series-pack pattern ({_short(name)})"
    return None


# ── cached-files verification ────────────────────────────────────────────────

def _entry_files(entry: dict) -> list[dict]:
    """Normalize a TorBox checkcached entry to a list of {name, size} dicts.

    Single-file torrents (the common movie case) carry name/size at the top
    level with no `files` list; multi-file torrents (season packs / collections)
    additionally carry `files`. File sizes are bytes.
    """
    files = entry.get("files") or []
    norm = [
        {"name": (f.get("name") or f.get("short_name") or ""), "size": f.get("size") or 0}
        for f in files
    ]
    if not norm:
        norm = [{"name": entry.get("name") or "", "size": entry.get("size") or 0}]
    return norm


def verify_entry(entry: dict, kind: str, *, season: int | None = None,
                 episode: int | None = None, imdb_id: str | None = None,
                 episodes: list[int] | None = None) -> str | None:
    """Reason a cached torrent listing can't be the requested item, or None if it
    passes. `entry` is one hash's value from torbox.check_cached_files().

    kind: 'movie' | 'episode' | 'season_pack'.
    """
    if not entry:
        return None  # nothing to check (uncached / no listing)  -  fail open
    if kind == "movie":
        return _verify_movie(entry)
    if kind == "episode":
        return _verify_episode(entry, season, episode, imdb_id)
    if kind == "season_pack":
        return _verify_season_pack(entry, season, episodes, imdb_id)
    return None


def _verify_movie(entry: dict) -> str | None:
    import strm_generator
    max_gb, pack_re = _cfg()
    top_name = entry.get("name") or ""
    raw_files = entry.get("files") or []

    if raw_files:
        norm = _entry_files(entry)
        videos = [f for f in norm
                  if strm_generator._is_video(f["name"]) and not strm_generator._is_trailer(f)]
        if not videos:
            return "cached torrent has no playable video file"
        # Many episode-tagged video files = a series pack mislabeled onto a movie
        # imdb, even when each file is individually small (< the movie cap).
        ep_tagged = sum(1 for f in videos if strm_generator._file_episode(f["name"]))
        if ep_tagged >= 2:
            return f"cached torrent holds {ep_tagged} episode-tagged video files (series pack)"
        main = strm_generator._pick_main_movie_file(videos)
        if not main:
            return "cached torrent has no non-trailer video file"
        main_gb = (main.get("size") or 0) / _BYTES_PER_GB
        if max_gb and main_gb > max_gb:
            return (f"main video {main_gb:.1f}GB over movie cap {max_gb:.0f}GB "
                    f"(pack/collection, not one film)")
        # A pack-named torrent carrying several full-size videos is a movie
        # collection, not the single requested film.
        big = [f for f in videos if (f.get("size") or 0) >= strm_generator._MIN_MOVIE_SIZE]
        if len(big) >= 3 and pack_re and pack_re.search(top_name):
            return f"cached torrent is a {len(big)}-title collection ({_short(top_name)})"
        return None

    # Single-file / no files list: judge on the top-level size + name only.
    total_gb = (entry.get("size") or 0) / _BYTES_PER_GB
    if max_gb and total_gb > max_gb:
        return (f"cached size {total_gb:.1f}GB over movie cap {max_gb:.0f}GB "
                f"(pack/collection, not one film)")
    if pack_re and pack_re.search(top_name):
        return f"cached torrent name matches pack pattern ({_short(top_name)})"
    return None


def _verify_episode(entry: dict, season: int | None, episode: int | None,
                    imdb_id: str | None) -> str | None:
    import strm_generator
    top_name = entry.get("name") or ""
    raw_files = entry.get("files") or []

    if not raw_files:
        # No per-file listing from checkcached (common: TorBox returns only a
        # top-level name/size). We can only reliably reject when the torrent name
        # tags a DIFFERENT specific episode than the one requested. A season/
        # complete-series PACK name is NOT rejected here: the pack legitimately
        # contains this episode and playback resolves the right file via
        # find_by_id (full file list). Rejecting on pack-name alone would wrongly
        # kill every episode served from a cached season pack.
        if season is not None and episode is not None:
            # One exception to "a pack is fine": a pack that names its own
            # episode span is PARTIAL, so an episode outside that span really is
            # absent and find_by_id will never resolve it.
            span = _declared_episode_span(top_name)
            tag = strm_generator._file_episode(top_name)
            # A real RANGE is the most specific thing the name states, so it
            # outranks the tag: "S04E01-E07" does hold E03 even though
            # _file_episode reads that name as S04E01. A LONE number does not  -
            # it is usually an absolute number printed next to the real tag, as
            # in "One-Punch.Man.S03E04.Episode.28", where trusting it over the
            # tag would reject the correct file.
            if span and span[0] < span[1]:
                return _span_reject(span, top_name, int(season), int(episode), imdb_id)
            if tag:
                if tag != (int(season), int(episode)):
                    return (f"single file tags S{tag[0]:02d}E{tag[1]:02d}, "
                            f"not S{int(season):02d}E{int(episode):02d}")
                return None
            if span:
                return _span_reject(span, top_name, int(season), int(episode), imdb_id)
        return None

    # Multi-file pack: require the specific episode file to be identifiable.
    if season is None or episode is None:
        return None
    norm = _entry_files(entry)
    absolute = _absolute_episode(imdb_id, season, episode)
    main = strm_generator._pick_episode_file(norm, int(season), int(episode), absolute=absolute)
    if not main:
        return (f"season/series pack with no identifiable "
                f"S{int(season):02d}E{int(episode):02d} file among {len(norm)} files")
    return None


def _verify_season_pack(entry: dict, season: int | None = None,
                        episodes: list[int] | None = None,
                        imdb_id: str | None = None) -> str | None:
    """A season pack is MEANT to be large and multi-file, so size/pack-name are
    not disqualifying here; only reject a pack whose file listing exists AND
    contains no playable video. When checkcached returns no file listing
    (files=0), fail open: the torrent name is a folder name (no extension) and
    playback resolves real files via find_by_id."""
    import strm_generator
    raw_files = entry.get("files") or []
    if not raw_files:
        return None
    videos = [f for f in _entry_files(entry)
              if strm_generator._is_video(f["name"]) and not strm_generator._is_trailer(f)]
    if not videos:
        return "cached season pack file listing has no playable video file"
    if season is None or not episodes:
        return None
    missing = []
    for wanted in episodes:
        absolute = _absolute_episode(imdb_id, int(season), int(wanted))
        if not strm_generator._pick_episode_file(
            videos, int(season), int(wanted), absolute=absolute
        ):
            missing.append(int(wanted))
    if missing:
        preview = ", ".join(f"E{ep:02d}" for ep in missing[:5])
        suffix = "" if len(missing) <= 5 else f" and {len(missing) - 5} more"
        return (f"cached season pack cannot identify {preview}{suffix} "
                f"among {len(videos)} video files")
    return None


# ── selection-time helpers (used by processor / monitor / catbox) ─────────────

def filter_cached(candidates: list, kind: str, *, season: int | None = None,
                  episode: int | None = None, imdb_id: str | None = None,
                  episodes: list[int] | None = None,
                  label: str = "") -> list:
    """Drop cached candidates whose actual TorBox files fail the sanity check.

    `candidates` are TorrentioStream-likes already known to be cached. Batches a
    single check_cached_files() call and verifies each locally. Preserves order.
    Fails OPEN: an un-fetchable listing (TorBox error / hash absent) is kept, so
    a TorBox blip never empties an otherwise-good candidate set.
    """
    if not candidates or not enabled():
        return candidates
    import torbox
    try:
        entries = torbox.check_cached_files([c.info_hash for c in candidates])
    except Exception as exc:
        log.debug("release sanity: batch check_cached_files failed (%s)  -  not blocking", exc)
        return candidates
    kept = []
    for c in candidates:
        if kind == "episode" and season is not None and episode is not None:
            candidate_name = f"{c.name or ''} {c.title or ''}".strip()
            span = _declared_episode_span(candidate_name)
            reason = (_span_reject(
                span, candidate_name, int(season), int(episode), imdb_id
            ) if span else None)
            if reason:
                log.warning("Release sanity: rejected %s cached candidate %s  -  %s",
                            label or kind, c.info_hash, reason)
                _record_reject(kind, reason)
                continue
        entry = entries.get((c.info_hash or "").lower())
        if not entry:
            kept.append(c)
            continue
        reason = verify_entry(
            entry, kind, season=season, episode=episode, imdb_id=imdb_id,
            episodes=episodes,
        )
        if reason:
            log.warning("Release sanity: rejected %s cached candidate %s  -  %s",
                        label or kind, c.info_hash, reason)
            _record_reject(kind, reason)
        else:
            kept.append(c)
    return kept


def check_hash(info_hash: str, kind: str, *, season: int | None = None,
               episode: int | None = None, imdb_id: str | None = None,
               label: str = "") -> tuple[bool, str]:
    """Verify a single already-chosen hash. Returns (ok, reason).

    ok=True with reason='' means it passed OR could not be checked (uncached /
    TorBox error). Used by monitor and the catbox materialize backstop.
    """
    if not enabled():
        return True, ""
    import torbox
    try:
        entry = torbox.check_cached_files([info_hash]).get((info_hash or "").lower())
    except Exception as exc:
        log.debug("release sanity: check_cached_files(%s) failed: %s  -  not blocking",
                  info_hash, exc)
        return True, ""
    reason = verify_entry(entry, kind, season=season, episode=episode, imdb_id=imdb_id)
    if reason:
        log.warning("Release sanity: rejected %s hash %s  -  %s", label or kind, info_hash, reason)
        _record_reject(kind, reason)
        return False, reason
    return True, ""


def verify_live_torrent(live: dict, kind: str, *, season: int | None = None,
                        episode: int | None = None, imdb_id: str | None = None) -> str | None:
    """Backstop for catbox: `live` is a TorBox mylist/find_by_id torrent dict
    (has top-level name/size + a `files` list). Reshape it into the checkcached
    entry shape and reuse verify_entry."""
    entry = {
        "name": live.get("name") or "",
        "size": live.get("size") or 0,
        "files": live.get("files") or [],
    }
    return verify_entry(entry, kind, season=season, episode=episode, imdb_id=imdb_id)


def _record_reject(kind: str, reason: str) -> None:
    try:
        import db
        db.record_metric("release_sanity_reject", kind, value_int=1)
    except Exception:
        pass


def _short(text: str, limit: int = 80) -> str:
    text = (text or "").strip()
    return repr(text if len(text) <= limit else text[:limit] + "…")
