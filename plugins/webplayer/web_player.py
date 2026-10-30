import json
import logging
import math
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import requests as req_lib
import requests.exceptions as _req_exc

import db
import health_cache
import settings as _settings
import subtitles as _subtitles
import torbox
import torrentio
import zilean

log = logging.getLogger(__name__)

PLAYER_TMP_DIR       = Path("/tmp/mycelium-player")
SEGMENT_WAIT_COUNT   = 5
SEGMENT_WAIT_TIMEOUT = 90
SESSION_IDLE_CLEANUP = 1800
CDN_URL_MAX_AGE_S    = 1800   # refresh TorBox signed URL after 30 minutes

_VAAPI_DEV = "/dev/dri/renderD128"
_vaapi_ok  = False   # updated by _init_vaapi() in background thread


def _init_vaapi() -> None:
    global _vaapi_ok
    if not Path(_VAAPI_DEV).exists():
        log.info("web_player: VA-API device not found - software transcode only")
        return
    try:
        # Actually test-encode a tiny frame to confirm the iHD driver is usable,
        # not just that h264_vaapi is compiled in.
        r = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-init_hw_device", f"vaapi=va:{_VAAPI_DEV}",
                "-f", "lavfi", "-i", "color=black:s=128x128:d=0.04",
                "-vf", "format=nv12,hwupload",
                "-c:v", "h264_vaapi",
                "-f", "null", "-",
            ],
            capture_output=True, timeout=15,
        )
        _vaapi_ok = r.returncode == 0
        if _vaapi_ok:
            log.info("web_player: VA-API hardware transcode available (%s)", _VAAPI_DEV)
        else:
            log.warning(
                "web_player: VA-API test failed (rc=%d) - software transcode only\n%s",
                r.returncode, r.stderr.decode(errors="replace"),
            )
    except Exception as exc:
        log.warning("web_player: VA-API probe failed: %s - software transcode only", exc)


_vaapi_probe_lock = threading.Lock()
_vaapi_probed = False


def _ensure_vaapi() -> None:
    """Probe only when a real HLS transcode needs it, never at import."""
    global _vaapi_probed
    with _vaapi_probe_lock:
        if not _vaapi_probed:
            _init_vaapi()
            _vaapi_probed = True

_BROWSER_AUDIO_OK    = {"aac"}   # vorbis/opus not reliable in mpegts HLS
_NO_BROWSER_VIDEO_RE = re.compile(r"\b(av1|vp9|vp8)\b", re.IGNORECASE)
_HDR_NAME_RE         = re.compile(r"\bhdr10\+|\bhdr10plus\b|\bhdr10\b|\bhdr\b|\bhlg\b|\bpq10\b", re.IGNORECASE)
_HEVC_NAME_RE        = re.compile(r"\b(x265|hevc|h\.?265)\b", re.IGNORECASE)
_AAC_SAMPLE_RATE     = "48000"   # browsers require consistent sample rate in TS
_H264_RE             = re.compile(r"\bx264\b|\bh\.?264\b|\bavc\b", re.IGNORECASE)
_AAC_NAME_RE         = re.compile(r"\baac\b", re.IGNORECASE)
_BAD_AUDIO_RE        = re.compile(r"\bdts\b|\btruehd\b|\batmos\b|\bdts.?hd\b|\bdts.?ma\b", re.IGNORECASE)
_TEXT_SUB_CODECS  = {"subrip", "ass", "ssa", "webvtt", "mov_text", "srt"}


def _parse_browser_caps(user_agent: str) -> dict:
    """Derive codec capabilities from User-Agent string.

    Used to tune candidate scoring per browser:
    - Firefox has no HEVC playback support (always needs server transcode).
    - Chrome/Edge on desktop support HEVC via hardware, but not guaranteed on Linux.
    - Safari supports HEVC natively.
    """
    ua = (user_agent or "").lower()
    is_firefox = "firefox/" in ua
    # Chromium on Linux rarely has HEVC hardware decode; desktop Windows/Mac usually does.
    is_linux   = "linux" in ua and "android" not in ua
    is_chrome  = ("chrome/" in ua or "chromium/" in ua) and "edg" not in ua
    hevc_ok    = not is_firefox and not (is_chrome and is_linux)
    return {"hevc_ok": hevc_ok}


# ── Torrent selection ──────────────────────────────────────────────────────────

def _web_score(stream: torrentio.TorrentioStream,
               caps: dict | None = None) -> int:
    caps = caps or {}
    blob = f"{stream.name} {stream.title}"
    if torrentio._DV_RE.search(blob):      return -1  # Dolby Vision: browser-incompatible
    if _NO_BROWSER_VIDEO_RE.search(blob):  return -1  # AV1/VP9/VP8: no browser HLS support
    if _HDR_NAME_RE.search(blob):          return -1  # HDR: browsers can't tone-map

    max_gb = _settings.get("WEB_PLAYER_MAX_SIZE_GB", 15) or 15
    if 0 < stream.size_gb > max_gb:
        return -1

    score = 0
    if stream.quality == "1080p":   score += 100
    elif stream.quality == "2160p": return -1   # 4K = altijd HEVC + groot, niet geschikt voor web
    elif stream.quality == "720p":  score += 50

    if torrentio._WEBDL_RE.search(blob): score += 40

    is_h264 = bool(_H264_RE.search(blob))
    is_aac  = bool(_AAC_NAME_RE.search(blob))
    if is_h264:            score += 100  # direct play in every browser
    if is_aac:             score += 50   # direct play audio, no transcode needed
    if is_h264 and is_aac: score += 50   # perfect combo: zero server work

    # Hard-block HEVC for browsers that can't hardware-decode it.
    # Firefox has no HEVC; Chrome on Linux lacks hardware HEVC decode.
    # NAS CPUs cannot transcode HEVC to H264 in real time, so there is
    # no point selecting HEVC for these browsers at all.
    if not caps.get("hevc_ok", True) and _HEVC_NAME_RE.search(blob):
        return -1

    if _BAD_AUDIO_RE.search(blob): score -= 150  # DTS/TrueHD/Atmos: no browser support, always transcode
    if stream.seeders > 10:        score += 10

    # Smaller = faster initial buffering.
    if   0 < stream.size_gb < 0.5: score += 55
    elif stream.size_gb     < 2:   score += 40
    elif stream.size_gb     < 4:   score += 30
    elif stream.size_gb     < 8:   score += 18
    elif stream.size_gb     < 12:  score += 8

    return score


def find_web_candidates(imdb_id: str, media_type: str,
                        season: int | None = None,
                        episode: int | None = None,
                        browser_caps: dict | None = None) -> list[torrentio.TorrentioStream]:
    streams: list[torrentio.TorrentioStream] = []
    seen: set[str] = set()

    if _settings.get("ZILEAN_ENABLED", False) and health_cache.is_up("zilean"):
        try:
            for s in zilean.fetch_streams(imdb_id, season=season, episode=episode):
                if s.info_hash not in seen:
                    seen.add(s.info_hash)
                    streams.append(s)
        except Exception as exc:
            log.warning("web_player: zilean fetch failed")

    if health_cache.is_up("torrentio"):
        kind = "movie" if media_type == "movie" else "series"
        try:
            for s in torrentio.fetch_streams(kind, imdb_id, season=season,
                                             episode=episode, timeout=12):
                if s.info_hash not in seen:
                    seen.add(s.info_hash)
                    streams.append(s)
        except Exception as exc:
            log.warning("web_player: torrentio fetch failed")

    scored = sorted(
        ((s, _web_score(s, browser_caps)) for s in streams
         if _web_score(s, browser_caps) >= 0),
        key=lambda x: x[1], reverse=True,
    )
    return [s for s, _ in scored]


# ── Job lifecycle ──────────────────────────────────────────────────────────────

class JobStatus(str, Enum):
    SEARCHING     = "searching"
    MATERIALIZING = "materializing"
    PROBING       = "probing"
    PREPARING     = "preparing"
    READY         = "ready"
    ERROR         = "error"


@dataclass
class PrepareJob:
    job_id:       str
    owner_id:     int
    imdb_id:      str
    media_type:   str
    season:       int | None
    episode:      int | None
    browser_caps: dict = field(default_factory=dict)
    status:       JobStatus = JobStatus.SEARCHING
    message:      str = ""
    token:        str | None = None
    stream_url:   str | None = None
    stream_type:  str = "hls"
    cdn_url:      str | None = None
    file_info:    dict | None = None
    error:        str | None = None
    created_at:   float = field(default_factory=time.monotonic)
    _thread:      threading.Thread = field(default=None, repr=False)


_jobs: dict[str, PrepareJob] = {}
_jobs_lock = threading.Lock()


MAX_PREPARE_JOBS = 128
MAX_ACTIVE_JOBS = 8
MAX_ACTIVE_PER_OWNER = 2


def _target(imdb_id, media_type, season, episode) -> None:
    if not isinstance(imdb_id, str) or not re.fullmatch(r"tt[0-9]{7,10}", imdb_id):
        raise ValueError("A valid IMDb identity is required")
    if media_type not in ("movie", "tv"):
        raise ValueError("Unknown media type")
    if media_type == "movie":
        if season is not None or episode is not None:
            raise ValueError("Movies cannot carry episode coordinates")
    elif (type(season) is not int or not 0 <= season <= 999
          or type(episode) is not int or not 1 <= episode <= 9999):
        raise ValueError("Choose one explicit season and episode")


def _owner_enabled(owner_id) -> bool:
    if type(owner_id) is not int or owner_id <= 0:
        return False
    rec = db.get_user(owner_id)
    return bool(rec and rec.get("enabled") and rec.get("webplayer_enabled"))


def start_prepare_job(imdb_id: str, media_type: str,
                      season: int | None = None,
                      episode: int | None = None,
                      user_agent: str = "", *, owner_id: int) -> str:
    _target(imdb_id, media_type, season, episode)
    if not _owner_enabled(owner_id):
        raise PermissionError("Player is unavailable")
    job_id = uuid.uuid4().hex
    job = PrepareJob(job_id=job_id, owner_id=owner_id, imdb_id=imdb_id,
                     media_type=media_type, season=season, episode=episode,
                     browser_caps=_parse_browser_caps(user_agent))
    with _jobs_lock:
        cutoff = time.monotonic() - SESSION_IDLE_CLEANUP
        for key, old in list(_jobs.items()):
            if old.status in (JobStatus.READY, JobStatus.ERROR) and old.created_at < cutoff:
                del _jobs[key]
        active = [j for j in _jobs.values()
                  if j.status not in (JobStatus.READY, JobStatus.ERROR)]
        if (len(_jobs) >= MAX_PREPARE_JOBS or len(active) >= MAX_ACTIVE_JOBS
                or sum(j.owner_id == owner_id for j in active) >= MAX_ACTIVE_PER_OWNER):
            raise RuntimeError("Player preparation is busy")
        _jobs[job_id] = job
    t = threading.Thread(target=_run_job, args=(job,), daemon=True)
    job._thread = t
    try:
        t.start()
    except Exception:
        with _jobs_lock:
            _jobs.pop(job_id, None)
        raise
    return job_id


def get_job(job_id: str, *, owner_id: int) -> PrepareJob | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return job if job and job.owner_id == owner_id else None


# ── Pipeline ───────────────────────────────────────────────────────────────────

_VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts"}
_EPISODE_TAG = re.compile(r"(?i)(?<![a-z0-9])(?:s([0-9]{1,3})e([0-9]{1,4})|([0-9]{1,3})x([0-9]{1,4}))(?![a-z0-9])")
_EXTRA_FILE = re.compile(r"(?i)(?:^|[ ._/-])(?:samples?|trailers?|extras?|featurettes?)(?:$|[ ._/-])")


def _select_file(files, media_type, season=None, episode=None):
    """Unique explicit file identity only. Never infer pack order or largest file.

    Filename coordinates are selection evidence, not a catalog/grant proof.
    Multi-episode files and untagged episode fallbacks remain unqualified.
    """
    if not isinstance(files, list) or len(files) > 10000:
        return None
    candidates = []
    ids = set()
    for item in files:
        if not isinstance(item, dict):
            return None
        fid, name = item.get("id"), item.get("name")
        if type(fid) is not int or fid < 0 or fid in ids:
            return None
        ids.add(fid)
        if not isinstance(name, str) or len(name) > 4096 or "\x00" in name:
            return None
        name = name.replace("\\", "/")
        if Path(name).suffix.lower() not in _VIDEO_EXTENSIONS or _EXTRA_FILE.search(name):
            continue
        # Directories may carry a season label, but the file must identify its episode.
        basename = name.replace("\\", "/").rsplit("/", 1)[-1]
        tags = list(_EPISODE_TAG.finditer(basename))
        if media_type == "movie":
            if not tags:
                candidates.append(item)
        elif media_type == "tv" and len(tags) == 1:
            tag = tags[0]
            # S01E01E02, S01E01-02 and 1x01-1x02 are not single episodes.
            if re.match(r"(?i)(?:[ ._]*[ex][0-9]|[ ._]*[-+&][ ._]*(?:s?[0-9]|e[0-9]))", basename[tag.end():]):
                continue
            coordinates = (int(tag[1] or tag[3]), int(tag[2] or tag[4]))
            if coordinates == (season, episode):
                candidates.append(item)
    return candidates[0] if len(candidates) == 1 else None


def _get_cdn_url(stream: torrentio.TorrentioStream, media_type: str,
                 season: int | None = None, episode: int | None = None) -> tuple[str | None, int | None, int | None]:
    """Resolve a TorrentioStream to (cdn_url, torrent_id, file_id).

    The caller guarantees that either:
    - the hash is already in the user's TorBox library, OR
    - TorBox has it cached (instant add).

    We never wait for a full download here.
    """
    item = torbox.find_by_hash(stream.info_hash)

    if item is None:
        # Not in library yet  -  add it (instant because caller verified cache).
        log.info("web_player: adding cached magnet hash=%s", stream.info_hash)
        try:
            result     = torbox.add_magnet(stream.magnet, reason="web_player")
            torrent_id = (result or {}).get("torrent_id") or (result or {}).get("id")
        except torbox.RateLimited:
            log.warning("web_player: TorBox rate-limited on add_magnet hash=%s", stream.info_hash)
            return None, None, None
        except (RuntimeError, _req_exc.RequestException) as exc:
            log.warning("web_player: add_magnet failed for hash=%s", stream.info_hash)
            return None, None, None
        item = torbox.wait_until_ready(stream.info_hash, timeout=60,
                                       torrent_id=torrent_id)
    elif not torbox._is_ready(item):
        item = torbox.wait_until_ready(stream.info_hash, timeout=60,
                                       torrent_id=item.get("id"))

    if not item:
        return None, None, None

    torrent_id = item.get("id")
    files      = item.get("files") or []
    if not files:
        fresh = torbox.find_by_id(torrent_id)
        files = (fresh or {}).get("files") or []
    if not files:
        return None, None, None

    if type(torrent_id) is not int or torrent_id < 0:
        return None, None, None
    main = _select_file(files, media_type, season, episode)
    if main is None:
        return None, None, None
    file_id = main["id"]

    url = _request_dl(torrent_id, file_id)
    return url, torrent_id, file_id


def _request_dl(torrent_id: int, file_id: int) -> str | None:
    """Call TorBox requestdl and return the CDN URL.

    Retries transient 5xx / 429 / network errors with a short linear backoff,
    since TorBox's requestdl intermittently returns HTTP 500 or 429 and recovers
    within seconds. The other 4xx (auth / gone / bad request) fail immediately.
    Tunable via REQUESTDL_RETRIES / REQUESTDL_BACKOFF_MS.
    """
    import config as _config
    base = (_settings.get("TORBOX_BASE_URL") or _config.TORBOX_BASE_URL).rstrip("/")
    url  = f"{base}/torrents/requestdl"
    params = {
        "token":      _settings.get("TORBOX_API_KEY") or _config.TORBOX_API_KEY,
        "torrent_id": torrent_id,
        "file_id":    file_id,
        "zip_link":   "false",
    }
    attempts = max(1, _config.REQUESTDL_RETRIES)
    last = "no response"
    for attempt in range(1, attempts + 1):
        try:
            resp = req_lib.get(url, params=params, timeout=15)
            # 429 (rate-limit) is transient like 5xx and is retried; every other
            # 4xx is deterministic and fails immediately via raise_for_status.
            if resp.status_code != 429 and resp.status_code < 500:
                resp.raise_for_status()
                return (resp.json() or {}).get("data") or None
            last = f"HTTP {resp.status_code}"
        except req_lib.HTTPError as exc:
            log.warning("web_player: requestdl rejected (HTTP error)")
            return None
        except req_lib.RequestException as exc:
            last = "network unavailable"
        if attempt < attempts:
            delay = (_config.REQUESTDL_BACKOFF_MS / 1000.0) * attempt
            log.info("web_player: requestdl transient (%s); retry %d/%d in %.1fs",
                     last, attempt, attempts - 1, delay)
            time.sleep(delay)
    log.warning("web_player: requestdl failed after %d attempt(s): %s", attempts, last)
    return None


def _run_job(job: PrepareJob) -> None:
    try:
        if not _owner_enabled(job.owner_id):
            raise PermissionError("Player is unavailable")
        job.status  = JobStatus.SEARCHING
        job.message = "Looking for a web-compatible version…"

        candidates = find_web_candidates(
            job.imdb_id, job.media_type, job.season, job.episode,
            browser_caps=job.browser_caps,
        )

        # If the browser's codec filter excluded everything (e.g. only HEVC
        # releases are cached in TorBox for this title), fall back to HEVC.
        # The HLS path will transcode at ultrafast + 720p.
        hevc_fallback = False
        if not candidates and not job.browser_caps.get("hevc_ok", True):
            log.info("web_player: no H264 candidates for %s, trying HEVC fallback",
                     job.imdb_id)
            candidates = find_web_candidates(
                job.imdb_id, job.media_type, job.season, job.episode,
                browser_caps={"hevc_ok": True},
            )
            hevc_fallback = bool(candidates)

        if not candidates:
            job.status = JobStatus.ERROR
            job.error  = "No web-compatible version found. Use Jellyfin."
            return

        # Priority 1: already in user's TorBox library (instant CDN URL).
        best = None
        try:
            for c in candidates:
                if torbox.find_by_hash(c.info_hash):
                    best = c
                    log.info("web_player: found in TorBox library hash=%s", c.info_hash)
                    break
        except (torbox.RateLimited, RuntimeError, _req_exc.RequestException) as exc:
            log.warning("web_player: library lookup failed")

        # Priority 2: TorBox has it cached (instant add, no download wait).
        if best is None:
            hashes = [c.info_hash for c in candidates]
            try:
                cached_set = torbox.check_cached(hashes)
            except torbox.RateLimited:
                log.warning("web_player: TorBox rate-limited on check_cached")
                job.status = JobStatus.ERROR
                job.error  = "TorBox rate limit hit. Wait a moment and try again."
                return
            except (RuntimeError, _req_exc.RequestException) as exc:
                log.warning("web_player: check_cached failed")
                job.status = JobStatus.ERROR
                job.error  = "TorBox temporarily unavailable. Try again in a moment."
                return
            # Pick the highest-scored cached candidate (candidates already sorted).
            for c in candidates:
                if c.info_hash in cached_set:
                    best = c
                    log.info("web_player: TorBox-cached hash=%s", c.info_hash)
                    break

        if best is None:
            job.status = JobStatus.ERROR
            job.error  = "No instantly available version found. Use Jellyfin."
            return

        # Probe each candidate; skip any that turn out to be HDR after all
        # (name-based filter may miss unlabelled HDR releases).
        cdn_url     = None
        file_info   = None
        session_key = None
        torrent_id  = None
        file_id     = None
        for candidate in ([best] + [c for c in candidates if c is not best]):
            # Only use instantly-available content.
            _hash = candidate.info_hash
            try:
                in_library = torbox.find_by_hash(_hash)
            except (torbox.RateLimited, RuntimeError, _req_exc.RequestException):
                in_library = None
            if not in_library:
                try:
                    single_cached = torbox.check_cached([_hash])
                except (torbox.RateLimited, RuntimeError, _req_exc.RequestException):
                    single_cached = set()
                if _hash not in single_cached:
                    continue

            log.info("web_player: selected %r hash=%s", candidate.title, _hash)

            # A release hash is content identity, never a playback session identity.
            # Every job has independent ownership, episode binding and seek state.
            job.status  = JobStatus.MATERIALIZING
            job.message = "Fetching via TorBox…"
            _cdn, _torrent_id, _file_id = _get_cdn_url(candidate, job.media_type, job.season, job.episode)
            if not _cdn:
                log.warning("web_player: requestdl failed for hash=%s, skipping", _hash)
                continue

            # Skip probe — serve directly and let the browser decide.
            # ffprobe runs lazily only if HLS fallback is triggered.
            cdn_url     = _cdn
            torrent_id  = _torrent_id
            file_id     = _file_id
            file_info   = _file_info_from_candidate(candidate)
            session_key = job.job_id
            break

        if not cdn_url or not file_info or not session_key:
            job.status = JobStatus.ERROR
            job.error  = "No instantly available version found. Use Jellyfin."
            return

        job.cdn_url   = None
        job.file_info = file_info

        # Always transcode HEVC to HLS - browsers cannot reliably decode raw HEVC
        # from a CDN MP4 regardless of whether they claim codec support.
        needs_hls = hevc_fallback or file_info.get('video_codec') == 'hevc'

        job.status  = JobStatus.PREPARING
        job.message = "Transcoding HEVC to H264..." if needs_hls else "Preparing for playback..."

        if not _owner_enabled(job.owner_id):
            raise PermissionError("Player is unavailable")
        _start_direct(session_key, file_info, cdn_url,
                      torrent_id=torrent_id, file_id=file_id,
                      owner_id=job.owner_id, content_hash=candidate.info_hash,
                      target=(job.imdb_id, job.media_type, job.season, job.episode))
        job.token = session_key

        if needs_hls:
            _do_hls_conversion(session_key, file_info)
            converted = get_session(session_key, owner_id=job.owner_id)
            if converted is None:
                job.status = JobStatus.ERROR
                job.error = "Conversion failed. Please reopen the player."
                return
            job.file_info = converted.file_info
            job.stream_type = "hls"
            job.stream_url = _playback_state(converted)["stream_url"]
        else:
            job.stream_type = "direct"
            job.stream_url  = f"/stream/{session_key}/direct"

        job.status  = JobStatus.READY
        job.message = "Ready"

    except torbox.RateLimited:
        log.warning("web_player: job %s hit TorBox rate limit", job.job_id)
        job.status = JobStatus.ERROR
        job.error  = "TorBox rate limit hit. Wait a moment and try again."
    except Exception:
        log.warning("web_player: prepare job %s failed", job.job_id)
        job.status = JobStatus.ERROR
        job.error  = "Playback preparation failed. Please try again."


# ── FFprobe ────────────────────────────────────────────────────────────────────

def _probe(cdn_url: str) -> dict | None:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet",
             "-probesize", "10M", "-analyzeduration", "3M",
             "-print_format", "json",
             "-show_streams", "-show_format", cdn_url],
            capture_output=True, timeout=20,
        )
        data    = json.loads(result.stdout)
    except Exception as exc:
        log.warning("web_player: ffprobe failed")
        return None

    streams = data.get("streams", [])

    video = next((s for s in streams if s["codec_type"] == "video"), {})
    audio = [s for s in streams if s["codec_type"] == "audio"]
    subs  = [s for s in streams if s["codec_type"] == "subtitle"]

    def _tag(s, key, default=""):
        return s.get("tags", {}).get(key, default)

    # HDR detection: PQ (HDR10/HDR10+) or HLG transfer functions signal HDR.
    _HDR_TRANSFERS = {"smpte2084", "arib-std-b67", "smpte428"}
    color_transfer  = video.get("color_transfer", "")
    is_hdr          = color_transfer in _HDR_TRANSFERS
    fmt_name        = data.get("format", {}).get("format_name", "")
    return {
        "duration_s":      float(data.get("format", {}).get("duration", 0)),
        "video_codec":     video.get("codec_name", "unknown"),
        "width":           video.get("width"),
        "height":          video.get("height"),
        "is_hdr":          is_hdr,
        "color_transfer":  color_transfer,
        "container":       fmt_name,
        "audio_tracks":    [
            {"index": i, "codec": t["codec_name"],
             "language": _tag(t, "language", "und"),
             "title":    _tag(t, "title"),
             "channels": t.get("channels", 2)}
            for i, t in enumerate(audio)
        ],
        "subtitle_tracks": [
            {"index": i, "codec": t["codec_name"],
             "language": _tag(t, "language", "und"),
             "title":    _tag(t, "title")}
            for i, t in enumerate(subs)
        ],
    }


# ── Direct play ───────────────────────────────────────────────────────────────

def _content_type_for(file_info: dict) -> str:
    fmt = file_info.get("container", "")
    if "matroska" in fmt or "webm" in fmt:
        return "video/x-matroska"
    return "video/mp4"


@dataclass
class DirectSession:
    token:        str
    owner_id:     int
    content_hash: str
    target:       tuple
    content_type: str
    cdn_url:      str
    file_info:    dict
    torrent_id:   int | None = None
    file_id:      int | None = None
    converting:   bool = False
    conversion_error: str | None = None
    generation_attempts: int = 0
    retiring:     bool = False
    operation_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    started_at:   float = field(default_factory=time.monotonic)
    last_request: float = field(default_factory=time.monotonic)
    _hb:          threading.Thread = field(default=None, repr=False)

    def touch(self):
        self.last_request = time.monotonic()

    def start_heartbeat(self):
        def _beat():
            while True:
                time.sleep(60)
                if time.monotonic() - self.last_request > SESSION_IDLE_CLEANUP:
                    break
                db.touch_virtual_item(self.content_hash)
        self._hb = threading.Thread(target=_beat, daemon=True)
        self._hb.start()


_direct_sessions: dict[str, DirectSession] = {}
_direct_lock = threading.Lock()


def get_direct_session(token: str, *, owner_id: int | None = None) -> DirectSession | None:
    with _direct_lock:
        session = _direct_sessions.get(token)
        return session if session and not session.retiring and (owner_id is None or session.owner_id == owner_id) else None


def touch_playback(token: str, *, owner_id: int, generation: str | None = None) -> bool:
    """Renew only a still-published owned playback, serialized with retirement."""
    with _direct_lock:
        direct = _direct_sessions.get(token)
        if direct is None or direct.retiring or direct.owner_id != owner_id:
            return False
        with _sessions_lock:
            session = None
            if generation is not None:
                session = _generations.get((token, generation))
                if session is None or session.owner_id != owner_id:
                    return False
            direct.touch()
            if session is not None:
                session.touch()
        return True


def _refresh_direct_cdn_url(session: DirectSession) -> None:
    """Fetch a fresh TorBox signed URL for an existing direct session in-place."""
    if session.torrent_id is None or session.file_id is None:
        return
    new_url = _request_dl(session.torrent_id, session.file_id)
    if new_url:
        session.cdn_url    = new_url
        session.started_at = time.monotonic()
        log.info("web_player: refreshed CDN URL for token=%s", session.token)
    else:
        log.warning("web_player: CDN URL refresh failed for token=%s", session.token)


def _start_direct(token: str, file_info: dict, cdn_url: str,
                  torrent_id: int | None = None,
                  file_id:    int | None = None, *, owner_id: int,
                  content_hash: str, target: tuple) -> DirectSession:
    session = DirectSession(token=token, owner_id=owner_id, content_hash=content_hash,
                            target=target, content_type=_content_type_for(file_info),
                            cdn_url=cdn_url, file_info=file_info,
                            torrent_id=torrent_id, file_id=file_id)
    session.start_heartbeat()
    with _direct_lock:
        _direct_sessions[token] = session
    log.info("web_player: direct play session started token=%s", token)
    return session


def start_hls_conversion(token: str, *, owner_id: int) -> bool:
    """One conversion per owned playback; competing calls cannot spawn workers."""
    with _direct_lock:
        s = _direct_sessions.get(token)
        if not s or s.retiring or s.owner_id != owner_id:
            return False
        if s.converting or get_session(token) is not None:
            return True
        if s.conversion_error or not s.operation_lock.acquire(blocking=False):
            return False
        s.converting = True
    try:
        threading.Thread(target=_do_hls_conversion, args=(token, s.file_info),
                         kwargs={"reserved": True}, daemon=True).start()
    except Exception:
        s.converting = False
        s.operation_lock.release()
        raise
    return True


def _file_info_from_candidate(candidate) -> dict:
    """Minimal file_info derived from torrent name — no ffprobe needed."""
    blob = f"{candidate.name} {candidate.title}".lower()
    codec = 'unknown'
    if 'x265' in blob or 'hevc' in blob or 'h265' in blob:
        codec = 'hevc'
    elif 'x264' in blob or 'h264' in blob or 'avc' in blob:
        codec = 'h264'
    height = {'2160p': 2160, '1080p': 1080, '720p': 720, '480p': 480}.get(
        candidate.quality or '', 0)
    container = 'matroska' if ('.mkv' in blob or blob.split().count('mkv') > 0) else 'mp4'
    return {
        'video_codec':      codec,
        'height':           height,
        'width':            0,
        'duration_s':       0,
        'is_hdr':           False,
        'color_transfer':   '',
        'container':        container,
        'audio_tracks':     [],
        'subtitle_tracks':  [],
    }


MAX_HLS_GENERATIONS = 8  # Includes failed attempts; no recycling of old output.


def _generation_dir(session: DirectSession) -> Path:
    if session.generation_attempts >= MAX_HLS_GENERATIONS:
        raise RuntimeError("Playback generation limit reached")
    session.generation_attempts += 1
    path = PLAYER_TMP_DIR / session.token / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    return path


def _stop_process(proc) -> None:
    """Reap our transcode before returning, including its timeout path."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    else:
        proc.wait(timeout=2)


def _playback_state(session) -> dict:
    playlist = "master.m3u8" if len(session.file_info.get("audio_tracks", [])) > 1 else "playlist.m3u8"
    return {"stream_url": f"/stream/{session.token}/hls/{session.generation}/{playlist}",
            "generation": session.generation, "seek_offset_s": session.seek_offset,
            "duration_s": session.file_info.get("duration_s", 0), "file_info": session.file_info}


def _publish_hls(session, previous=None) -> None:
    if not _owner_enabled(session.owner_id):
        raise PermissionError("Player is unavailable")
    with _sessions_lock:
        if _sessions.get(session.token) is not previous:
            raise RuntimeError("Playback generation changed")
        _sessions[session.token] = session
        _generations[(session.token, session.generation)] = session
    try:
        session.start_heartbeat()
    except Exception:
        with _sessions_lock:
            if _sessions.get(session.token) is session:
                if previous is None:
                    _sessions.pop(session.token, None)
                else:
                    _sessions[session.token] = previous
            _generations.pop((session.token, session.generation), None)
        raise


def _do_hls_conversion(token: str, file_info: dict, *, reserved=False) -> None:
    direct = get_direct_session(token)
    if direct is None:
        return
    # The prepare worker may arrive while the browser's fallback already owns
    # conversion. It waits for that same finite operation rather than duplicating it.
    if not reserved and not direct.operation_lock.acquire(timeout=SEGMENT_WAIT_TIMEOUT + 45):
        return
    session = None
    try:
        if get_session(token) is not None:
            return
        direct.converting = True
        if not _owner_enabled(direct.owner_id):
            raise PermissionError("Player is unavailable")
        tmp_dir = _generation_dir(direct)
        if not file_info.get("audio_tracks"):
            file_info = _probe(direct.cdn_url)
            if file_info is None or file_info.get("is_hdr"):
                raise RuntimeError("Unsupported file")
            direct.file_info = file_info
        session = _start_hls(token, direct.cdn_url, file_info, tmp_dir)
        _publish_hls(session)
        direct.conversion_error = None
        try:
            threading.Thread(target=_extract_subtitles,
                             args=(direct.cdn_url, file_info.get("subtitle_tracks", []), token, tmp_dir),
                             daemon=True).start()
        except Exception:
            log.warning("web_player: subtitle worker unavailable token=%s", token)
    except Exception:
        if session is not None and get_session(token) is not session:
            _stop_process(session.proc)
        # Do not expose ffmpeg stderr, provider URLs or exception payloads.
        direct.conversion_error = "Conversion failed. Please reopen the player."
        log.warning("web_player: conversion failed token=%s", token)
    finally:
        direct.converting = False
        direct.operation_lock.release()


# ── HLS session ────────────────────────────────────────────────────────────────

@dataclass
class HLSSession:
    token:        str
    owner_id:     int
    content_hash: str
    proc:         subprocess.Popen
    tmp_dir:      Path
    cdn_url:      str  = ""
    file_info:    dict = field(default_factory=dict)
    started_at:   float = field(default_factory=time.monotonic)
    last_request: float = field(default_factory=time.monotonic)
    seeking:      bool = False
    seek_offset:  float = 0.0

    @property
    def generation(self):
        return self.tmp_dir.name
    _hb:          threading.Thread = field(default=None, repr=False)

    def touch(self):
        self.last_request = time.monotonic()

    def start_heartbeat(self):
        def _beat():
            while self.proc.poll() is None:
                db.touch_virtual_item(self.content_hash)
                time.sleep(60)
        self._hb = threading.Thread(target=_beat, daemon=True)
        self._hb.start()


_sessions: dict[str, HLSSession] = {}
_generations: dict[tuple[str, str], HLSSession] = {}
_sessions_lock = threading.Lock()


def get_session(token: str, *, owner_id: int | None = None) -> HLSSession | None:
    with _sessions_lock:
        session = _sessions.get(token)
        return session if session and (owner_id is None or session.owner_id == owner_id) else None


def get_generation(token: str, generation: str, *, owner_id: int) -> HLSSession | None:
    if not re.fullmatch(r"[a-f0-9]{32}", generation):
        return None
    with _sessions_lock:
        session = _generations.get((token, generation))
        return session if session and session.owner_id == owner_id else None


def seek_session(token: str, position_s: float, *, owner_id: int) -> dict | None:
    """Publish a ready fresh generation. Earlier generations are never overwritten.

    Stopping our old transcode does not assert that its readers have disappeared.
    Existing segments stay available under their original generation URL.
    """
    if (type(position_s) not in (int, float) or not math.isfinite(position_s)
            or not 0 <= position_s <= 604800):
        raise ValueError("Invalid seek position")
    direct = get_direct_session(token, owner_id=owner_id)
    if not direct or not _owner_enabled(owner_id) or not direct.operation_lock.acquire(blocking=False):
        return None
    previous = None
    session = None
    try:
        previous = get_session(token, owner_id=owner_id)
        if previous is None:
            return None
        duration = previous.file_info.get("duration_s") or 0
        if duration > 0 and position_s > duration:
            raise ValueError("Seek exceeds duration")
        tmp_dir = _generation_dir(direct)
        previous.seeking = True
        _stop_process(previous.proc)
        session = _start_hls(token, previous.cdn_url, previous.file_info, tmp_dir,
                             seek_offset=float(position_s))
        # WebVTT timestamps use the original media timeline. Preserve them
        # unchanged under the new generation URL, including external tracks.
        for subtitle in previous.tmp_dir.glob("sub_*.vtt"):
            if (re.fullmatch(r"sub_[a-zA-Z0-9_]+[.]vtt", subtitle.name)
                    and subtitle.is_file() and not subtitle.is_symlink()):
                with subtitle.open("rb") as source:
                    data = source.read(8 * 1024 * 1024 + 1)
                if len(data) <= 8 * 1024 * 1024:
                    (tmp_dir / subtitle.name).write_bytes(data)
        _publish_hls(session, previous)
        # A published VTT proves completion of that track only. A previous
        # worker may still be extracting later tracks; finish every missing one.
        try:
            threading.Thread(target=_extract_subtitles,
                             args=(previous.cdn_url, previous.file_info.get("subtitle_tracks", []), token, tmp_dir),
                             daemon=True).start()
        except Exception:
            log.warning("web_player: subtitle worker unavailable token=%s", token)
        return _playback_state(session)
    except ValueError:
        raise
    except Exception:
        if session is not None and get_session(token) is not session:
            _stop_process(session.proc)
        log.warning("web_player: seek unavailable token=%s", token)
        return None
    finally:
        if previous is not None:
            previous.seeking = False
        direct.operation_lock.release()


def _audio_copy_or_transcode(track: dict, out_index: int) -> list[str]:
    """Return FFmpeg args for a single audio output track.

    Compatible codecs (AAC stereo) are copied as-is; everything else
    (TrueHD, DTS, multichannel, Opus, Vorbis) is transcoded to AAC-LC stereo.
    """
    codec_ok = track["codec"] in _BROWSER_AUDIO_OK and track.get("channels", 2) <= 2
    if codec_ok:
        return [f"-c:a:{out_index}", "copy"]
    return [
        f"-c:a:{out_index}", "aac",
        f"-profile:a:{out_index}", "aac_low",
        f"-ar:a:{out_index}", _AAC_SAMPLE_RATE,
        f"-ac:a:{out_index}", "2",
        f"-b:a:{out_index}", "192k",
    ]


def _spawn_hls(token: str, cdn_url: str, file_info: dict, tmp_dir: Path,
               seek_offset: float = 0.0) -> HLSSession:
    """Start (or restart) HLS segmentation.

    Strategy:
    - H.264: mpegts segments, video copy, audio copy-or-aac. Near-zero CPU.
    - HEVC/other + VA-API available: hardware decode+encode via Intel QSV.
      No scaling - original resolution preserved.
    - HEVC/other + no VA-API: software ultrafast + 720p cap (last resort).

    seek_offset > 0 = fast keyframe seek in input before generating segments.
    """
    direct = get_direct_session(token)
    if not direct or not _owner_enabled(direct.owner_id):
        raise PermissionError("Player is unavailable")
    if file_info.get("video_codec") not in ("h264", "avc"):
        _ensure_vaapi()
    audio_tracks = file_info["audio_tracks"]
    multi_audio  = len(audio_tracks) > 1

    # Video encoding strategy:
    # H.264:      copy into mpegts (zero CPU, universally supported).
    # HEVC/other: transcode to H.264 mpegts, ultrafast, 720p cap (NAS CPU budget).
    video_codec = (file_info.get("video_codec") or "h264").lower()
    _H264_OK    = {"h264", "avc"}
    use_fmp4    = False
    seg_type    = "mpegts"
    seg_ext     = "ts"

    src_height = file_info.get("height") or 0

    if video_codec in _H264_OK:
        v_enc      = ["-c:v", "copy"]
        hw_pre     = []
        mode_label = "ts-copy"
    elif _vaapi_ok:
        # Hardware decode + encode via VA-API.
        # -hwaccel_output_format nv12 forces the VA-API decoder to output 8-bit
        # NV12 to system memory, handling 10-bit HEVC without green corruption.
        # hwupload then moves the NV12 frame to a VA-API surface for h264_vaapi.
        hw_pre = [
            "-hwaccel", "vaapi",
            "-hwaccel_device", _VAAPI_DEV,
            "-hwaccel_output_format", "nv12",
        ]
        v_enc  = ["-vf", "format=nv12,hwupload=extra_hw_frames=64",
                  "-c:v", "h264_vaapi", "-qp", "23"]
        mode_label = f"ts-vaapi-full(from {video_codec})"
    else:
        # Software fallback: ultrafast + 720p cap.
        hw_pre     = []
        scale      = ["-vf", "scale=-2:720"] if src_height > 720 else []
        v_enc      = scale + ["-c:v", "libx264", "-preset", "ultrafast",
                               "-crf", "23", "-pix_fmt", "yuv420p"]
        mode_label = f"ts-x264-ultrafast(from {video_codec})"

    # hwaccel args must come before -ss and -i.
    seek_args  = ["-ss", f"{seek_offset:.3f}"] if seek_offset > 0 else []
    input_args = hw_pre + seek_args + ["-i", cdn_url]

    if multi_audio:
        # Multi-audio: video-only output + one output per audio track,
        # bound together by a master.m3u8.
        cmd = ["ffmpeg", "-y"] + input_args

        # Output 0: video only
        cmd += [
            "-map", "0:v:0", *v_enc,
            "-hls_time", "2", "-hls_list_size", "0",
            "-hls_flags", "independent_segments",
            "-hls_segment_type", seg_type,
            "-hls_segment_filename", str(tmp_dir / f"seg_v%05d.{seg_ext}"),
        ]
        cmd.append(str(tmp_dir / "video.m3u8"))

        # Outputs 1…N: one audio stream each
        for i, track in enumerate(audio_tracks):
            cmd += [
                "-map", f"0:a:{i}", *_audio_copy_or_transcode(track, 0),
                "-hls_time", "2", "-hls_list_size", "0",
                "-hls_flags", "independent_segments",
                "-hls_segment_type", seg_type,
                "-hls_segment_filename", str(tmp_dir / f"seg_a{i}_%05d.{seg_ext}"),
            ]
            cmd.append(str(tmp_dir / f"audio_{i}.m3u8"))

        # Write master.m3u8
        lines  = ["#EXTM3U"]
        default = "YES"
        for i, track in enumerate(audio_tracks):
            raw_lang = track.get("language")
            lang = raw_lang.lower() if isinstance(raw_lang, str) and re.fullmatch(r"[A-Za-z]{2,8}", raw_lang) else "und"
            raw_name = track.get("title")
            name = re.sub(r'["\\\x00-\x1f\x7f]', "", raw_name)[:128] if isinstance(raw_name, str) else ""
            name = name or lang.upper()
            lines.append(
                f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",'
                f'NAME="{name}",DEFAULT={default},LANGUAGE="{lang}",'
                f'URI="audio_{i}.m3u8"'
            )
            default = "NO"
        lines.append('#EXT-X-STREAM-INF:BANDWIDTH=4000000,AUDIO="audio"')
        lines.append("video.m3u8")
        log.info("web_player: FFmpeg multi-audio/%s token=%s seek=%.1f",
                 mode_label, token, seek_offset)
        with open(tmp_dir / "ffmpeg.log", "w") as stderr_log:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr_log)

        session = HLSSession(token=token, owner_id=direct.owner_id,
                             content_hash=direct.content_hash, proc=proc, tmp_dir=tmp_dir,
                             cdn_url=cdn_url, file_info=file_info, seek_offset=seek_offset)

        try:
            (tmp_dir / "master.m3u8").write_text("\n".join(lines) + "\n")
        except Exception:
            _stop_process(proc)
            raise

    else:
        # Single audio: combined video+audio output
        track = audio_tracks[0] if audio_tracks else None
        a_args = (_audio_copy_or_transcode(track, 0) if track else [])

        cmd = [
            "ffmpeg", "-y", *input_args,
            "-map", "0:v:0",
            *((["-map", "0:a:0"] + a_args) if track else ["-an"]),
            *v_enc,
            "-hls_time", "2", "-hls_list_size", "0",
            "-hls_flags", "independent_segments",
            "-hls_segment_type", seg_type,
            "-hls_segment_filename", str(tmp_dir / f"seg%05d.{seg_ext}"),
        ]
        cmd.append(str(tmp_dir / "playlist.m3u8"))

        log.info("web_player: FFmpeg single-audio/%s token=%s seek=%.1f",
                 mode_label, token, seek_offset)
        with open(tmp_dir / "ffmpeg.log", "w") as stderr_log:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr_log)

        session = HLSSession(token=token, owner_id=direct.owner_id,
                             content_hash=direct.content_hash, proc=proc, tmp_dir=tmp_dir,
                             cdn_url=cdn_url, file_info=file_info, seek_offset=seek_offset)

    return session


def _start_hls(token: str, cdn_url: str, file_info: dict, tmp_dir: Path,
               seek_offset: float = 0.0) -> HLSSession:
    session = _spawn_hls(token, cdn_url, file_info, tmp_dir, seek_offset)
    try:
        tracks = file_info.get("audio_tracks", [])
        patterns = (["seg_v*.ts"] + [f"seg_a{i}_*.ts" for i in range(len(tracks))]
                    if len(tracks) > 1 else ["seg*.ts"])
        deadline = time.monotonic() + SEGMENT_WAIT_TIMEOUT
        for pattern in patterns:
            remaining = max(0, deadline - time.monotonic())
            if not _wait_segments_pattern(tmp_dir, pattern, SEGMENT_WAIT_COUNT,
                                          remaining, proc=session.proc):
                raise RuntimeError("HLS output not ready")
        # A failed process must not be declared ready even if it left segments.
        if session.proc.poll() not in (None, 0):
            raise RuntimeError("Transcode failed")
        playlists = (["master.m3u8", "video.m3u8"] + [f"audio_{i}.m3u8" for i in range(len(tracks))]
                     if len(tracks) > 1 else ["playlist.m3u8"])
        if not all((tmp_dir / name).is_file() for name in playlists):
            raise RuntimeError("HLS playlists missing")
        return session
    except BaseException:
        _stop_process(session.proc)
        raise


def _wait_segments(tmp_dir: Path, count: int, timeout: float,
                   proc: subprocess.Popen | None = None) -> bool:
    """Wait until at least `count` segments exist (either .ts or .m4s).

    If proc is given and exits with a non-zero code before enough segments
    are produced, return False immediately so the caller can surface the error
    without waiting out the full timeout.

    A zero exit code (success) is not treated as failure — ffmpeg can finish
    normally (short files, copy mode) before we've polled enough segments.
    In that case we do one final re-count before returning False on timeout.
    """
    deadline = time.monotonic() + timeout
    exited_ok = False
    while time.monotonic() < deadline:
        found = len(list(tmp_dir.glob("seg*.ts"))) + len(list(tmp_dir.glob("seg*.m4s")))
        if found >= count:
            return True
        if proc is not None:
            rc = proc.poll()
            if rc is not None and rc != 0:
                return False
            if rc == 0:
                exited_ok = True
        if exited_ok:
            # Process exited cleanly; do a final count after a short pause
            # to capture any segments written just before ffmpeg closed files.
            time.sleep(0.2)
            found = len(list(tmp_dir.glob("seg*.ts"))) + len(list(tmp_dir.glob("seg*.m4s")))
            return found >= count
        time.sleep(0.5)
    return False


def _wait_segments_pattern(tmp_dir: Path, pattern: str, count: int, timeout: float,
                           proc: subprocess.Popen | None = None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(list(tmp_dir.glob(pattern))) >= count:
            return True
        if proc is not None:
            rc = proc.poll()
            if rc is not None and rc != 0:
                return False
            if rc == 0:
                time.sleep(0.2)
                return _completed_hls_pattern(tmp_dir, pattern)
        time.sleep(0.5)
    return False


def _completed_hls_pattern(tmp_dir: Path, pattern: str) -> bool:
    """A successful finite tail may have fewer segments than the live buffer.

    Require the closed media playlist and every referenced local segment.
    Running/failed encoders and empty or incomplete playlists never qualify.
    """
    if pattern == "seg*.ts":
        name = "playlist.m3u8"
    elif pattern == "seg_v*.ts":
        name = "video.m3u8"
    else:
        audio = re.fullmatch(r"seg_a([0-9]+)_\*[.]ts", pattern)
        if audio is None:
            return False
        name = f"audio_{audio[1]}.m3u8"
    playlist = tmp_dir / name
    if not playlist.is_file() or playlist.is_symlink() or playlist.stat().st_size > 1048576:
        return False
    lines = playlist.read_text().splitlines()
    if "#EXT-X-ENDLIST" not in lines:
        return False
    segments = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    available = {p.name for p in tmp_dir.glob(pattern) if p.is_file() and not p.is_symlink() and p.stat().st_size > 0}
    return bool(segments) and all(segment in available for segment in segments)


# ── Subtitles ──────────────────────────────────────────────────────────────────

def _extract_subtitles(cdn_url: str, sub_tracks: list,
                       token: str, tmp_dir: Path) -> None:
    for track in sub_tracks:
        if track["codec"] not in _TEXT_SUB_CODECS:
            continue
        lang = track["language"]
        out  = tmp_dir / f"sub_{track['index']}_{lang}.vtt"
        if out.is_file() and not out.is_symlink():
            continue  # Final names are published only after successful extraction.
        pending = tmp_dir / f".{out.stem}.{uuid.uuid4().hex}.vtt"
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", cdn_url,
                 "-map", f"0:s:{track['index']}", "-c:s", "webvtt", str(pending)],
                capture_output=True, timeout=120,
            )
            if result.returncode == 0 and pending.is_file() and not pending.is_symlink():
                pending.replace(out)
            else:
                log.warning("web_player: sub extract failed track=%d", track["index"])
        except Exception:
            log.warning("web_player: sub extract failed track=%d", track["index"])
        finally:
            pending.unlink(missing_ok=True)


def _subtitles_task(cdn_url: str, file_info: dict,
                    imdb_id: str, media_type: str,
                    season: int | None, episode: int | None,
                    token: str, tmp_dir: Path) -> None:
    """Extract embedded subs; fall back to OpenSubtitles when none found."""
    _extract_subtitles(cdn_url, file_info["subtitle_tracks"], token, tmp_dir)

    # If embedded extraction produced at least one VTT, we're done.
    if list(tmp_dir.glob("sub_*.vtt")):
        return

    _fetch_external_subtitles(imdb_id, media_type, season, episode, tmp_dir)


def _fetch_external_subtitles(imdb_id: str, media_type: str,
                               season: int | None, episode: int | None,
                               tmp_dir: Path) -> None:
    """Download subtitles from OpenSubtitles and convert to WebVTT."""
    api_key = _settings.get("OPENSUBTITLES_API_KEY", "")
    if not api_key:
        return

    langs = _settings.get("OPENSUBTITLES_LANGUAGES") or []
    if isinstance(langs, str):
        langs = [l.strip() for l in langs.split(",") if l.strip()]
    if not langs:
        langs = ["en"]

    for lang in langs:
        vtt_path = tmp_dir / f"sub_ext_{lang}.vtt"
        if vtt_path.exists():
            continue

        results = _subtitles._search(imdb_id, season, episode, lang)
        if not results:
            log.info("web_player: no external subtitles for %s lang=%s", imdb_id, lang)
            continue

        top   = results[0]
        files = (top.get("attributes") or {}).get("files") or []
        if not files:
            continue
        file_id = files[0].get("file_id")
        if not file_id:
            continue

        url = _subtitles._request_download_url(file_id)
        if not url:
            continue

        try:
            resp = req_lib.get(url, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("web_player: subtitle download failed lang=%s: %s", lang, exc)
            continue

        # Write raw file, then let ffmpeg normalise to WebVTT.
        raw_path = tmp_dir / f"sub_ext_{lang}.raw"
        raw_path.write_bytes(resp.content)
        pending = tmp_dir / f".{vtt_path.stem}.{uuid.uuid4().hex}.vtt"
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", str(raw_path), str(pending)],
                capture_output=True, timeout=30,
            )
            if result.returncode == 0 and pending.is_file() and not pending.is_symlink():
                pending.replace(vtt_path)
                log.info("web_player: external subtitle saved lang=%s token=%s", lang, tmp_dir.name)
            else:
                log.warning("web_player: ffmpeg vtt conversion failed lang=%s: %s",
                            lang, result.stderr[-300:])
        except Exception as exc:
            log.warning("web_player: vtt conversion error lang=%s: %s", lang, exc)
        finally:
            pending.unlink(missing_ok=True)
            raw_path.unlink(missing_ok=True)


def list_subtitles(token: str, *, owner_id: int) -> list[dict]:
    s = get_session(token, owner_id=owner_id)
    if not s:
        return []
    out = []
    for p in sorted(s.tmp_dir.glob("sub_*.vtt")):
        parts = p.stem.split("_")   # sub_0_eng  or  sub_ext_nl
        lang  = parts[-1]
        source = "External" if "ext" in parts else "Embedded"
        out.append({
            "language": lang,
            "label":    source,
            "url":      f"/stream/{token}/subtitles/{s.generation}/{p.name}",
        })
    return out


# ── Cleanup ────────────────────────────────────────────────────────────────────

def cleanup_idle_sessions() -> None:
    """Retire the entire token after its last owned reader becomes idle.

    Keep earlier seek output while any generation is active. The operation lock
    excludes conversions, seeks and position writes. Failed process/file cleanup
    leaves an inaccessible retiring record so a later sweep can finish reaping.
    """
    cutoff = time.monotonic() - SESSION_IDLE_CLEANUP
    with _direct_lock:
        candidates = list(_direct_sessions.items())
    for token, direct in candidates:
        if not direct.operation_lock.acquire(blocking=False):
            continue
        try:
            # Match the registry object and recheck activity after reserving it.
            # Lock order is the same as conversion and touch_playback.
            with _direct_lock, _sessions_lock:
                if _direct_sessions.get(token) is not direct:
                    continue
                generations = [s for (t, _), s in _generations.items() if t == token]
                current = _sessions.get(token)
                if current is not None and all(current is not s for s in generations):
                    generations.append(current)
                latest = max([direct.last_request] + [s.last_request for s in generations])
                if not direct.retiring and latest >= cutoff:
                    continue
                direct.retiring = True
            # Do not hold global registry locks during process waits or disk I/O.
            # New requests refuse the retiring owner; its operation stays locked.
            for session in generations:
                _stop_process(session.proc)
            root = PLAYER_TMP_DIR / token
            if root.exists():
                shutil.rmtree(root)
            with _direct_lock, _sessions_lock:
                if _direct_sessions.get(token) is direct:
                    _direct_sessions.pop(token)
                    _sessions.pop(token, None)
                    for key in [key for key in _generations if key[0] == token]:
                        _generations.pop(key)
            log.info("web_player: cleaned up idle playback token=%s", token)
        except Exception:
            # Keep evidence/references for a later cleanup sweep, never revive it.
            log.warning("web_player: idle playback cleanup incomplete token=%s", token)
        finally:
            direct.operation_lock.release()
