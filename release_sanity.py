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

A third check, SERIES IDENTITY (`series_identity` / `classify_release`), keeps
another national version of a show off the wrong series: "The.Traitors.UK.S03"
must never fill The Traitors (US). See the section header below.
"""
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass

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


# ── series identity: national versions of one format ─────────────────────────
#
# Many formats run as separate national versions under one title: The Traitors
# (UK 2022, US 2023, Australia, India, ...), Ghosts (UK 2019, US 2021), The
# Office, Shameless, Love Island, Big Brother. Title-matching catalogs (zilean's
# DMM index, comet) return every version for every IMDb id, which is how
# "The.Traitors.UK.S03..." landed on the US show and "The Traitors US S04E09"
# on the UK one. The reliable signal in a release name is the qualifier between
# the title and the episode tag: a country ("The.Traitors.US.S04E01") or a
# premiere year ("The.Traitors.2023.S04E01"). A qualifier that names another
# version drops the release. A name without one stays, because most releases
# of the original version carry none (fail open).
#
# Verdicts, strongest first:
#   reject   a country tag outside the show's TMDB origin_country; the
#            premiere year of a same-named, same-language version that is 2+
#            years off or comes from other countries (The Traitors UK 2022 vs
#            US 2023); an Indian-language-only release of an English show.
#   soft     the premiere year of a same-named version one year off that shares
#            a country with this show (or has none on TMDB). TMDB and scene
#            premiere years can drift by one for the same production
#            (Battlestar Galactica is TMDB 2004, scene 2003, and TMDB also lists
#            the Canadian 2003 miniseries), so a soft candidate is dropped only
#            when a cleaner one remains in the same pool.
#   match    a qualifier that names this show (its country or premiere year);
#            used only as a ranking tie-breaker, never as a filter.
#   neutral  no qualifier, or one that points at no known version.

IDENTITY_MATCH = "match"
IDENTITY_NEUTRAL = "neutral"
IDENTITY_SOFT = "soft"
IDENTITY_REJECT = "reject"

# Qualifier words a release puts after the show title, mapped to ISO 3166
# alpha-2 codes as TMDB reports them in origin_country. Two-letter codes are
# limited to the English-language franchises that actually collide; other
# countries only count when spelled out, since short codes such as DE, NL, ES,
# IT or NO double as language tags or plain words.
_COUNTRY_WORDS: dict[tuple[str, ...], str] = {
    ("us",): "US", ("usa",): "US", ("america",): "US",
    ("uk",): "GB", ("gb",): "GB",
    ("au",): "AU", ("aus",): "AU", ("australia",): "AU",
    ("nz",): "NZ", ("new", "zealand"): "NZ", ("aotearoa",): "NZ",
    ("ca",): "CA", ("canada",): "CA", ("quebec",): "CA",
    ("ie",): "IE", ("ireland",): "IE",
    ("india",): "IN",
    ("south", "africa"): "ZA",
    ("france",): "FR", ("germany",): "DE", ("italy",): "IT", ("italia",): "IT",
    ("spain",): "ES", ("mexico",): "MX", ("brazil",): "BR", ("brasil",): "BR",
    ("netherlands",): "NL", ("belgium",): "BE", ("sweden",): "SE",
    ("norway",): "NO", ("norge",): "NO", ("denmark",): "DK", ("danmark",): "DK",
    ("finland",): "FI", ("poland",): "PL", ("turkiye",): "TR",
    ("philippines",): "PH",
}

# Audio languages of the Indian versions (The Traitors India is released as
# "The Traitors S01E02 ... AMZN WEB-DL Hindi DDP5.1", no country tag at all).
_INDIC_LANGUAGES = frozenset({
    "hindi", "tamil", "telugu", "malayalam", "kannada", "bengali",
    "marathi", "punjabi", "gujarati",
})
_ENGLISH_AUDIO_WORDS = frozenset({"eng", "english", "dual", "multi"})

# The episode/season tag that ends the show title in a release name.
_EPISODE_TAG_RE = re.compile(
    r"(?<![a-z0-9])(?:"
    r"s\d{1,3}(?:[ ._-]?e\d{1,4})*"
    r"|\d{1,2}x\d{2,3}"
    r"|season[ ._-]?\d{1,3}"
    r"|series[ ._-]?\d{1,3}"
    r"|complete[ ._-]?(?:series|seasons?|collection|pack)"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)
# Characters a release title is spelled with. Anything else (an emoji, a pipe, a
# slash, "💾") ends the run, so addon decoration such as "💾 1.2 GB" or
# "⚙️ ThePirateBay" never reads as part of the name. The right single quote is
# built with chr() to keep typographic punctuation out of this source file.
_TITLE_STOP_RE = re.compile(r"[^\w .\-'" + chr(0x2019) + r"()\[\]&!,:+]")
# Site tags ("www.site.org - ", "[eztv.re]") carry TLDs such as .ca or .uk.
_SITE_TAG_RE = re.compile(
    r"(?:https?://)?www\.[^\s\])]+|\[[^\]]*\.[a-z]{2,4}\]|\([^)]*\.[a-z]{2,4}\)",
    re.IGNORECASE,
)
_YEAR_WORD_RE = re.compile(r"(?:19|20)\d{2}")
_TITLE_QUALIFIER_RE = re.compile(
    r"^(?P<base>.*?)\s*[(\[]\s*(?P<q>[A-Za-z]{2,3}|(?:19|20)\d{2})\s*[)\]]\s*$")

_IDENTITY_TTL_SEC = 12 * 3600
_IDENTITY_RETRY_SEC = 600
_identity_cache: dict[str, tuple[float, "ShowIdentity | None"]] = {}
_identity_lock = threading.Lock()


@dataclass(frozen=True)
class ShowIdentity:
    """What a release name must not contradict for one series."""
    imdb_id: str
    name: str
    regions: frozenset = frozenset()      # TMDB origin_country codes
    year: int | None = None               # first-air year
    language: str | None = None           # TMDB original_language
    title_tokens: frozenset = frozenset()  # words of the show's own names
    # (premiere year, origin countries) of other same-named, same-language shows
    namesakes: frozenset = frozenset()


def _words(text: str) -> list[str]:
    """Lowercase ASCII words, accents folded ("Türkiye" -> "turkiye")."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", text.lower())


def _countries(words: list[str]) -> set[str]:
    found = set()
    for i, word in enumerate(words):
        code = _COUNTRY_WORDS.get((word,))
        if code:
            found.add(code)
        if i + 1 < len(words):
            code = _COUNTRY_WORDS.get((word, words[i + 1]))
            if code:
                found.add(code)
    return found


def _base_words(name: str | None) -> tuple[str, ...]:
    """A show name without its country words: "Love Island USA" -> love island."""
    words = _words(name or "")
    out: list[str] = []
    i = 0
    while i < len(words):
        if i + 1 < len(words) and (words[i], words[i + 1]) in _COUNTRY_WORDS:
            i += 2
            continue
        if (words[i],) not in _COUNTRY_WORDS:
            out.append(words[i])
        i += 1
    return tuple(out)


def _year_of(date_value) -> int | None:
    match = re.match(r"((?:19|20)\d{2})", str(date_value or ""))
    return int(match.group(1)) if match else None


def _qualifier_runs(text: str) -> list[list[str]]:
    """Words of the title run right before each episode tag in `text`.

    "The.Traitors.UK.S03.1080p" gives [the, traitors, uk]. Each line of an
    addon's multi-line description is read on its own.
    """
    runs = []
    for line in (text or "").splitlines():
        for tag in _EPISODE_TAG_RE.finditer(line):
            before = line[:tag.start()]
            stop = None
            for stop in _TITLE_STOP_RE.finditer(before):
                pass
            run = before[stop.end():] if stop else before
            words = _words(_SITE_TAG_RE.sub(" ", run))
            if words:
                runs.append(words)
    return runs


def _indic_only_audio(text: str, identity: "ShowIdentity") -> str:
    """Name of the Indian audio language when that is all an English-language
    show's release declares ("... WEB-DL Hindi DDP5.1"), else ''."""
    if (identity.language or "") != "en":
        return ""
    words = set(_words(text)) - identity.title_tokens
    indic = words & _INDIC_LANGUAGES
    if indic and not words & _ENGLISH_AUDIO_WORDS:
        return sorted(indic)[0].capitalize()
    return ""


def classify_release(text: str, identity: "ShowIdentity | None") -> tuple[str, str]:
    """(verdict, reason) for one release name against the show it would fill.

    `text` may hold several lines (torrent name, file name, addon description);
    every episode-tagged line is read. Pure: no network, no DB.
    """
    if identity is None or not text:
        return IDENTITY_NEUTRAL, ""
    matched = False
    foreign: set[str] = set()
    years: set[int] = set()
    for words in _qualifier_runs(text):
        found = _countries(words)
        if found & identity.regions:
            matched = True
        extra = [w for w in words if w not in identity.title_tokens]
        if identity.regions:
            foreign |= _countries(extra) - identity.regions
        years |= {int(w) for w in extra if _YEAR_WORD_RE.fullmatch(w)}
    if foreign:
        return IDENTITY_REJECT, (
            f"release is tagged {'/'.join(sorted(foreign))}, the show is "
            f"{'/'.join(sorted(identity.regions))}")
    language = _indic_only_audio(text, identity)
    if language:
        return IDENTITY_REJECT, (
            f"{language}-only audio for an {identity.language}-language show")
    soft = ""
    if identity.year:
        for year in sorted(years):
            if year == identity.year:
                matched = True
                continue
            versions = [regions for when, regions in identity.namesakes if when == year]
            if not versions:
                continue
            reason = (f"release year {year} is another version of the show "
                      f"(this one premiered {identity.year})")
            foreign_version = bool(identity.regions) and all(
                regions and not regions & identity.regions for regions in versions)
            if abs(year - identity.year) >= 2 or foreign_version:
                return IDENTITY_REJECT, reason
            soft = reason
    if soft:
        return IDENTITY_SOFT, soft
    return (IDENTITY_MATCH if matched else IDENTITY_NEUTRAL), ""


def identity_check_enabled() -> bool:
    import settings as _settings
    return bool(_settings.get("SERIES_IDENTITY_CHECK",
                              _config_val("SERIES_IDENTITY_CHECK", True)))


def _title_qualifiers(title: str | None) -> tuple[str, frozenset, int | None]:
    """Split "The Traitors (US)" / "The Traitors (2022)" into base, regions, year."""
    title = (title or "").strip()
    match = _TITLE_QUALIFIER_RE.match(title)
    if not match:
        return title, frozenset(), None
    base, qualifier = match.group("base").strip(), match.group("q")
    if qualifier.isdigit():
        return base, frozenset(), int(qualifier)
    code = _COUNTRY_WORDS.get((qualifier.lower(),))
    return (base, frozenset({code}), None) if code else (title, frozenset(), None)


def _namesakes(tmdb_id, name: str, language: str | None) -> frozenset | None:
    """(premiere year, origin countries) of other TMDB shows with the same base
    name and original language, or None when the search failed (so the caller
    retries soon)."""
    base = _base_words(name)
    if not base:
        return frozenset()
    import tmdb
    data = tmdb._get("/search/tv", params={"query": " ".join(base)})
    if data is None:
        return None
    found = set()
    for row in data.get("results") or []:
        if row.get("id") == tmdb_id:
            continue
        if language and (row.get("original_language") or "").lower() != language:
            continue
        if base not in (_base_words(row.get("name")), _base_words(row.get("original_name"))):
            continue
        year = _year_of(row.get("first_air_date"))
        if year:
            regions = frozenset(str(code).upper() for code in (row.get("origin_country") or [])
                                if code)
            found.add((year, regions))
    return frozenset(found)


def _load_series_identity(imdb_id: str,
                          title_hint: str | None) -> tuple["ShowIdentity | None", bool]:
    """(identity, complete). complete=False means a lookup failed and the
    result should only be cached briefly."""
    import tmdb
    hint_title, hint_regions, hint_year = _title_qualifiers(title_hint)
    tmdb_id = tmdb.find_by_imdb(imdb_id, kind="tv")
    show = tmdb.get_show_info(tmdb_id) if tmdb_id else None
    if not show:
        if hint_regions or hint_year:
            return ShowIdentity(
                imdb_id=imdb_id, name=hint_title, regions=hint_regions,
                year=hint_year, title_tokens=frozenset(_words(hint_title)),
            ), False
        return None, False
    name = show.get("name") or hint_title or ""
    language = (show.get("original_language") or "").lower() or None
    namesakes = _namesakes(tmdb_id, name, language)
    return ShowIdentity(
        imdb_id=imdb_id,
        name=name,
        regions=frozenset(str(code).upper() for code in (show.get("origin_country") or [])
                          if code) or hint_regions,
        year=_year_of(show.get("first_air_date")) or hint_year,
        language=language,
        title_tokens=frozenset(_words(name)) | frozenset(_words(show.get("original_name")))
        | frozenset(_words(hint_title)),
        namesakes=namesakes or frozenset(),
    ), namesakes is not None


def series_identity(imdb_id: str | None, title_hint: str | None = None) -> "ShowIdentity | None":
    """Cached identity of a series for the release-name check, or None when the
    check is off or TMDB knows nothing (the check then fails open).

    title_hint ("The Traitors (US)") only fills in what TMDB cannot provide.
    """
    if not imdb_id or not identity_check_enabled():
        return None
    now = time.monotonic()
    with _identity_lock:
        cached = _identity_cache.get(imdb_id)
        if cached and cached[0] > now:
            return cached[1]
    try:
        identity, complete = _load_series_identity(imdb_id, title_hint)
    except Exception as exc:
        log.debug("series identity lookup failed for %s: %s  -  not blocking", imdb_id, exc)
        identity, complete = None, False
    ttl = _IDENTITY_TTL_SEC if complete else _IDENTITY_RETRY_SEC
    with _identity_lock:
        _identity_cache[imdb_id] = (now + ttl, identity)
    return identity


def _stream_text(candidate) -> str:
    return f"{getattr(candidate, 'name', '') or ''}\n{getattr(candidate, 'title', '') or ''}"


def _entry_names(entry: dict) -> str:
    names = [entry.get("name") or ""]
    names.extend(f.get("name") or f.get("short_name") or "" for f in entry.get("files") or [])
    return "\n".join(name for name in names if name)


def _release_label(candidate) -> str:
    """First non-empty line of a candidate's title or name, for log lines."""
    for text in (getattr(candidate, "title", ""), getattr(candidate, "name", "")):
        for line in (text or "").splitlines():
            if line.strip():
                return line.strip()
    return getattr(candidate, "info_hash", "") or "?"


def _settle_identity(candidates: list, verdicts: dict, label: str) -> list:
    """Apply per-candidate verdicts to a pool. Rejects always go; soft ones go
    only while at least one cleaner candidate remains."""
    rejected = [c for c in candidates if verdicts[id(c)][0] == IDENTITY_REJECT]
    kept = [c for c in candidates if verdicts[id(c)][0] != IDENTITY_REJECT]
    soft = [c for c in kept if verdicts[id(c)][0] == IDENTITY_SOFT]
    if soft and len(soft) < len(kept):
        rejected.extend(soft)
        kept = [c for c in kept if verdicts[id(c)][0] != IDENTITY_SOFT]
    if rejected:
        sample = rejected[0]
        reason = verdicts[id(sample)][1]
        log.info("Release identity: dropped %d/%d candidate(s) for %s  -  e.g. %s (%s)",
                 len(rejected), len(candidates), label or "series",
                 _short(_release_label(sample)), reason)
        _record_reject("series_identity", reason, count=len(rejected))
    return kept


def apply_series_identity(candidates: list, identity: "ShowIdentity | None",
                          label: str = "") -> tuple[list, dict]:
    """Drop candidates whose release name places them in another version of the
    show. Returns (kept, match_rank): match_rank maps id(candidate) to 0 when
    its qualifier names this show and 1 otherwise, for use as a sort key."""
    if identity is None or not candidates:
        return list(candidates or []), {}
    verdicts = {id(c): classify_release(_stream_text(c), identity) for c in candidates}
    kept = _settle_identity(list(candidates), verdicts, label)
    return kept, {id(c): 0 if verdicts[id(c)][0] == IDENTITY_MATCH else 1 for c in kept}


def _identity_filter(candidates: list, identity: "ShowIdentity | None",
                     entries: dict, label: str) -> list:
    """apply_series_identity for cached candidates, also reading the TorBox
    cached torrent and file names (an unnamed catalog hit still has one there)."""
    if identity is None or not candidates:
        return candidates
    verdicts = {}
    for c in candidates:
        text = _stream_text(c)
        entry = entries.get((c.info_hash or "").lower())
        if entry:
            text = f"{text}\n{_entry_names(entry)}"
        verdicts[id(c)] = classify_release(text, identity)
    return _settle_identity(candidates, verdicts, label)


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

    Series kinds with an imdb_id also get the series identity check, on the
    candidate's own name and on the TorBox cached torrent/file names.
    """
    if not candidates:
        return candidates
    identity = (series_identity(imdb_id)
                if kind in ("episode", "season_pack") and imdb_id else None)
    if not enabled():
        return _identity_filter(candidates, identity, {}, label or kind)
    import torbox
    try:
        entries = torbox.check_cached_files([c.info_hash for c in candidates])
    except Exception as exc:
        log.debug("release sanity: batch check_cached_files failed (%s)  -  not blocking", exc)
        return _identity_filter(candidates, identity, {}, label or kind)
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
    return _identity_filter(kept, identity, entries, label or kind)


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


def _record_reject(kind: str, reason: str, count: int = 1) -> None:
    try:
        import db
        db.record_metric("release_sanity_reject", kind, value_int=count)
    except Exception:
        pass


def _short(text: str, limit: int = 80) -> str:
    text = (text or "").strip()
    return repr(text if len(text) <= limit else text[:limit] + "…")
