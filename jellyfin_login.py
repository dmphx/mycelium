"""Jellyfin-backed dashboard login (opt-in, default off).

Users keep their existing Jellyfin username and password. The password is
forwarded once to the configured Jellyfin server (POST /Users/AuthenticateByName)
and is never logged or stored. The Jellyfin access token that comes back is used
only to log that freshly minted Jellyfin session out again; it is never kept.

Trust model:
  * The server answer must carry the pinned ServerId (JELLYFIN_AUTH_SERVER_ID).
  * Identity is the Jellyfin user GUID, mapped via external_identities
    (provider='jellyfin', server_id=<pin>, subject=<normalised GUID>) to a
    native users row. Usernames are never used to find or merge accounts.
  * Role never derives from Jellyfin's admin flag. New users are role='user',
    auto_approve=0. Admins are linked explicitly (db.link_external_identity).
"""
from __future__ import annotations

import logging
import re
import uuid

import requests

import config as cfg

log = logging.getLogger(__name__)

PROVIDER = "jellyfin"
# Sentinel: not a valid scrypt$ hash, so local password/WebDAV paths always fail.
PASSWORD_SENTINEL = "jellyfin$" + "x" * 16
_TIMEOUT = (3.05, 8)
_CLIENT_HEADER = ('MediaBrowser Client="Mycelium", Device="Mycelium", '
                  'DeviceId="mycelium-login", Version="1"')
_MAX_FIELD = 256


class JellyfinLoginError(Exception):
    """Generic failure; carries no detail that is safe to show beyond .code."""
    def __init__(self, code: str = "failed"):
        super().__init__(code)
        self.code = code


def is_enabled() -> bool:
    return bool(cfg.JELLYFIN_LOGIN_ENABLED and _base_url() and pinned_server_id())


def pinned_server_id() -> str:
    return _norm_id(cfg.JELLYFIN_AUTH_SERVER_ID) if cfg.JELLYFIN_AUTH_SERVER_ID else ""


def _base_url() -> str:
    import settings
    url = (settings.get("JELLYFIN_URL", "") or "").strip().rstrip("/")
    return url if url.lower().startswith(("http://", "https://")) else ""


def _norm_id(value) -> str:
    """Lower-case hex without dashes, or '' if not a UUID/32-hex string."""
    if not isinstance(value, str):
        return ""
    try:
        return uuid.UUID(value.strip()).hex
    except ValueError:
        return ""


def _display_name(name) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", "", name if isinstance(name, str) else "").strip()
    return name[:_MAX_FIELD]


def authenticate(username: str, password: str) -> dict:
    """Verify credentials with Jellyfin. Returns {'subject','server_id','name'}.

    Raises JellyfinLoginError on every failure (bad credentials, timeout,
    wrong server, disabled user, malformed answer)."""
    if not is_enabled():
        raise JellyfinLoginError("disabled")
    if not username or not password or len(username) > _MAX_FIELD or len(password) > 1024:
        raise JellyfinLoginError("credentials")
    base = _base_url()
    client_header = _CLIENT_HEADER.replace("mycelium-login", "mycelium-login-" + uuid.uuid4().hex)
    headers = {"Authorization": client_header, "Content-Type": "application/json"}
    try:
        resp = requests.post(f"{base}/Users/AuthenticateByName",
                             json={"Username": username, "Pw": password},
                             headers=headers, timeout=_TIMEOUT, allow_redirects=False)
    except requests.RequestException as exc:
        log.warning("Jellyfin login: server unreachable (%s)", type(exc).__name__)
        raise JellyfinLoginError("unreachable")
    if resp.status_code != 200:
        raise JellyfinLoginError("credentials" if resp.status_code in (400, 401, 403) else "failed")
    try:
        data = resp.json()
    except ValueError:
        raise JellyfinLoginError("failed")
    token = data.get("AccessToken") if isinstance(data, dict) else None
    try:
        if not isinstance(data, dict) or not isinstance(data.get("User"), dict):
            raise JellyfinLoginError("failed")
        server_id = _norm_id(data.get("ServerId"))
        user = data["User"]
        subject = _norm_id(user.get("Id"))
        policy = user.get("Policy") if isinstance(user.get("Policy"), dict) else {}
        if not server_id or server_id != pinned_server_id():
            log.warning("Jellyfin login: ServerId does not match the pin; refused")
            raise JellyfinLoginError("server")
        if not subject:
            raise JellyfinLoginError("failed")
        if policy.get("IsDisabled") is not False and policy.get("IsDisabled") is not None:
            raise JellyfinLoginError("disabled")
        name = _display_name(user.get("Name")) or _display_name(username)
        if not name:
            raise JellyfinLoginError("failed")
        return {"subject": subject, "server_id": server_id, "name": name}
    finally:
        if isinstance(token, str) and token:
            _revoke(base, token, client_header)
        token = None


def _revoke(base: str, token: str, client_header: str = _CLIENT_HEADER) -> None:
    """Best effort: end the Jellyfin session we just created."""
    try:
        requests.post(f"{base}/Sessions/Logout",
                      headers={"Authorization": client_header + f', Token="{token}"'},
                      timeout=_TIMEOUT, allow_redirects=False)
    except requests.RequestException:
        log.debug("Jellyfin login: session revoke failed")


def login(username: str, password: str, session) -> bool:
    """Full flow: verify, map/provision a native user, populate the Flask session.

    The caller must have cleared the session. Returns False on any failure."""
    import db
    import settings
    try:
        ident = authenticate(username, password)
        user = db.provision_external_user(PROVIDER, ident["server_id"], ident["subject"],
                                          ident["name"], PASSWORD_SENTINEL,
                                          (settings.get("AUTH_USERNAME", "admin") or "admin",))
    except JellyfinLoginError as exc:
        log.info("Jellyfin login refused (%s)", exc.code)
        return False
    except Exception as exc:
        log.warning("Jellyfin login: provisioning failed (%s)", type(exc).__name__)
        return False
    if not user or not user.get("enabled"):
        return False
    session["user"] = user["username"]
    session["user_id"] = user["id"]
    session["role"] = user["role"]
    session["auth_source"] = "jellyfin"
    db.touch_user_login(user["id"])
    return True
