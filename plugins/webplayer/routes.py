import math
import re

import db
import auth
from flask import Blueprint, abort, jsonify, redirect, request, send_file, Response
from . import web_player, positions

bp = Blueprint("webplayer_routes", __name__)


def _check_enabled():
    rec = auth.current_user_record()
    if (not rec or type(rec.get("id")) is not int or rec["id"] <= 0
            or not rec.get("enabled") or not rec.get("webplayer_enabled")):
        abort(403)
    return rec


def _body():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        abort(400)
    return d


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 604800:
        abort(400)
    return float(value)


def _owned_direct(token, owner_id):
    s = web_player.get_direct_session(token, owner_id=owner_id)
    if not s:
        abort(404)
    return s


def _owned_hls(token, owner_id):
    s = web_player.get_session(token, owner_id=owner_id)
    if not s:
        abort(404)
    return s


def _touch_owned(token, owner_id, generation=None):
    if not web_player.touch_playback(token, owner_id=owner_id, generation=generation):
        abort(404)


def _owned_generation(token, owner_id, filename):
    _owned_direct(token, owner_id)
    generation, sep, basename = filename.partition("/")
    if not sep:
        abort(404)
    s = web_player.get_generation(token, generation, owner_id=owner_id)
    if not s:
        abort(404)
    return s, basename


def _session_file(session, filename, *, subtitle=False):
    pattern = (r"sub_[a-zA-Z0-9_]+[.]vtt" if subtitle else
               r"(?:playlist|master|video|audio_[0-9]+)[.]m3u8|seg[a-zA-Z0-9_]*[.](?:ts|m4s)|init[a-zA-Z0-9_]*[.]mp4")
    if not re.fullmatch(pattern, filename):
        abort(400)
    base = session.tmp_dir.resolve()
    path = session.tmp_dir / filename
    if path.is_symlink() or path.resolve().parent != base or not path.is_file():
        abort(404)
    return path


@bp.post("/ui/api/web-player/prepare")
def web_player_prepare():
    rec = _check_enabled()
    d = _body()
    if not {"imdb_id", "media_type"} <= d.keys() or d.keys() - {"imdb_id", "media_type", "season", "episode"}:
        abort(400)
    try:
        job_id = web_player.start_prepare_job(
            imdb_id=d["imdb_id"], media_type=d["media_type"],
            season=d.get("season"), episode=d.get("episode"),
            user_agent=request.headers.get("User-Agent", ""), owner_id=rec["id"],
        )
    except ValueError:
        abort(400)
    except PermissionError:
        abort(403)
    except RuntimeError:
        return jsonify(error="Player preparation is busy. Please try again."), 503
    return jsonify(job_id=job_id)


@bp.get("/ui/api/web-player/status/<job_id>")
def web_player_status(job_id: str):
    rec = _check_enabled()
    job = web_player.get_job(job_id, owner_id=rec["id"])
    if not job:
        abort(404)
    return jsonify(status=job.status.value, message=job.message, token=job.token,
                   stream_url=job.stream_url, stream_type=job.stream_type,
                   file_info=job.file_info, error=job.error)


@bp.get("/stream/<token>/hls/<path:filename>")
def stream_hls_file(token: str, filename: str):
    rec = _check_enabled()
    s, basename = _owned_generation(token, rec["id"], filename)
    p = _session_file(s, basename)
    _touch_owned(token, rec["id"], s.generation)
    mimetype = {".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t",
                ".m4s": "video/iso.segment", ".mp4": "video/mp4"}[p.suffix]
    return send_file(p, mimetype=mimetype)


@bp.get("/stream/<token>/direct")
def stream_direct(token: str):
    rec = _check_enabled()
    s = _owned_direct(token, rec["id"])
    _touch_owned(token, rec["id"])
    return redirect(s.cdn_url, code=302)


@bp.post("/stream/<token>/convert-hls")
def stream_convert_hls(token: str):
    rec = _check_enabled()
    if not web_player.start_hls_conversion(token, owner_id=rec["id"]):
        abort(404)
    return jsonify(ok=True)


@bp.get("/stream/<token>/hls-status")
def stream_hls_status(token: str):
    rec = _check_enabled()
    s = _owned_direct(token, rec["id"])
    current = web_player.get_session(token, owner_id=rec["id"])
    if current is not None:
        state = web_player._playback_state(current)
        _touch_owned(token, rec["id"], current.generation)
        return jsonify(status="ready", url=state.pop("stream_url"), **state)
    if s.conversion_error:
        return jsonify(status="error", error="Conversion failed. Please reopen the player.")
    _touch_owned(token, rec["id"])
    return jsonify(status="converting")


@bp.get("/stream/<token>/subtitles")
def stream_subtitles_list(token: str):
    rec = _check_enabled()
    _owned_direct(token, rec["id"])
    subtitles = web_player.list_subtitles(token, owner_id=rec["id"])
    _touch_owned(token, rec["id"])
    return jsonify(subtitles=subtitles)


@bp.get("/stream/<token>/subtitles/<path:filename>")
def stream_subtitle_file(token: str, filename: str):
    rec = _check_enabled()
    s, basename = _owned_generation(token, rec["id"], filename)
    p = _session_file(s, basename, subtitle=True)
    _touch_owned(token, rec["id"], s.generation)
    return send_file(p, mimetype="text/vtt")


@bp.post("/stream/<token>/seek")
def stream_seek(token: str):
    rec = _check_enabled()
    _owned_hls(token, rec["id"])
    d = _body()
    if set(d) != {"position_s"}:
        abort(400)
    try:
        state = web_player.seek_session(token, _number(d["position_s"]), owner_id=rec["id"])
    except ValueError:
        abort(400)
    if state is None:
        return jsonify(error="Seek is unavailable. Please reopen the player."), 409
    return jsonify(**state)


@bp.post("/stream/<token>/position")
def stream_save_position(token: str):
    rec = _check_enabled()
    session = _owned_direct(token, rec["id"])
    d = _body()
    if not {"position_s"} <= d.keys() or d.keys() - {"position_s", "duration_s", "generation"}:
        abort(400)
    if not session.operation_lock.acquire(blocking=False):
        return jsonify(error="Playback is changing."), 409
    try:
        hls = web_player.get_session(token, owner_id=rec["id"])
        if (hls is not None and d.get("generation") != hls.generation) or (hls is None and d.get("generation") is not None):
            return jsonify(error="Playback generation changed."), 409
        position = _number(d["position_s"])
        duration = _number(d["duration_s"]) if d.get("duration_s") is not None else None
        if duration is not None and position > duration:
            abort(400)
        known_duration = (hls.file_info if hls is not None else session.file_info).get("duration_s") or 0
        if known_duration > 0 and position > known_duration:
            abort(400)
        try:
            positions.save(session, position, duration)
        except ValueError:
            abort(400)
        except PermissionError:
            abort(403)
        _touch_owned(token, rec["id"], hls.generation if hls is not None else None)
        return jsonify(ok=True)
    finally:
        session.operation_lock.release()


_INSTALLER_SCRIPT = r"""#!/usr/bin/env bash
# Mycelium native player installer for macOS
# Registers the mycelium:// URL scheme  -  opens streams in IINA, mpv, or VLC.
set -euo pipefail

APP_NAME="MyceliumPlayer"
APP_DIR="/Applications/${APP_NAME}.app"
CONTENTS="${APP_DIR}/Contents"
MACOS="${CONTENTS}/MacOS"
RES="${CONTENTS}/Resources"

echo "Installing ${APP_NAME}.app …"

# ── App bundle skeleton ─────────────────────────────────────────────────────
mkdir -p "${MACOS}" "${RES}"

# ── Handler script ──────────────────────────────────────────────────────────
cat > "${MACOS}/${APP_NAME}" << 'HANDLER'
#!/usr/bin/env bash
# $1 = mycelium://play?url=<encoded-url>
RAW_SCHEME="${1:-}"
if [ -z "${RAW_SCHEME}" ]; then exit 1; fi

# Extract the video URL from the mycelium://play?url=... argument
VIDEO_URL=$(python3 -c "
import sys, urllib.parse
raw = sys.argv[1]
qs  = urllib.parse.urlparse(raw).query
print(urllib.parse.parse_qs(qs).get('url', [''])[0])
" "${RAW_SCHEME}")

if [ -z "${VIDEO_URL}" ]; then exit 1; fi

# Player preference: IINA > mpv > VLC
if [ -d "/Applications/IINA.app" ]; then
    ENCODED=$(python3 -c "import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1],safe=''))" "${VIDEO_URL}")
    open "iina://open?url=${ENCODED}"
elif command -v mpv &>/dev/null; then
    exec mpv --no-terminal "${VIDEO_URL}"
elif [ -d "/Applications/VLC.app" ]; then
    open -a VLC "${VIDEO_URL}"
else
    osascript -e 'display alert "Mycelium Player" message "No supported player found.\nInstall IINA (iina.io) or mpv (mpv.io)."'
fi
HANDLER
chmod +x "${MACOS}/${APP_NAME}"

# ── Info.plist ──────────────────────────────────────────────────────────────
cat > "${CONTENTS}/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>nl.mycelium.player</string>
    <key>CFBundleName</key>
    <string>MyceliumPlayer</string>
    <key>CFBundleDisplayName</key>
    <string>Mycelium Player</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleExecutable</key>
    <string>MyceliumPlayer</string>
    <key>CFBundleURLTypes</key>
    <array>
        <dict>
            <key>CFBundleURLName</key>
            <string>Mycelium stream</string>
            <key>CFBundleURLSchemes</key>
            <array>
                <string>mycelium</string>
            </array>
        </dict>
    </array>
    <key>LSBackgroundOnly</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
</dict>
</plist>
PLIST

# ── Register with Launch Services ───────────────────────────────────────────
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "${APP_DIR}" 2>/dev/null || true

echo ""
echo "Done! MyceliumPlayer.app installed in /Applications."
echo "The mycelium:// URL scheme is now active."
echo ""
echo "Player priority: IINA > mpv > VLC"
echo "Install IINA from https://iina.io for best results."
"""


@bp.get("/ui/api/web-player/install-macos")
def web_player_install_macos():
    """Download the macOS installer shell script for the mycelium:// URL scheme."""
    _check_enabled()
    return Response(
        _INSTALLER_SCRIPT,
        mimetype="application/x-sh",
        headers={"Content-Disposition": 'attachment; filename="install-mycelium-player.sh"'},
    )
