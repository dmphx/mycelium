"""Native OpenID Connect (OIDC) authentication, opt-in.

Works with any OIDC-compliant provider: Authelia, Authentik, Keycloak,
Google Workspace, Auth0, Okta, etc. The provider does the password / 2FA /
device dance; Mycelium accepts the ID token, extracts a username claim,
and stores it in the session.

Register a redirect URI of `<public-url>/oidc/callback` at the provider.
Then set OIDC_ENABLED=true, OIDC_ISSUER_URL, OIDC_CLIENT_ID and
OIDC_CLIENT_SECRET. Restart the container so the OAuth client picks up
the issuer metadata.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from urllib.parse import urlsplit

from flask import Flask, redirect, request, session, url_for

import config as cfg

log = logging.getLogger(__name__)

_oauth = None  # populated by install()


def _safe_next_path(nxt: str | None, default: str = "/ui") -> str:
    nxt = (nxt or "").strip()
    if not nxt:
        return default
    parts = urlsplit(nxt)
    if parts.scheme or parts.netloc:
        return default
    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
        return default
    return nxt


def is_enabled() -> bool:
    return bool(cfg.OIDC_ENABLED and cfg.OIDC_ISSUER_URL and cfg.OIDC_CLIENT_ID)


def provider_name() -> str:
    return cfg.OIDC_PROVIDER_NAME or "SSO"


class LoginRejected(Exception):
    """Sign-in refused. `code` is the login-page error key, never identity detail."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def validate_id_claims(claims, issuer: str, client_id: str) -> tuple[str, str]:
    """Return (issuer, subject) from verified ID-token claims or raise LoginRejected.

    Authlib has already checked signature, expiry and nonce. This re-checks the
    exact issuer string and that this client is an audience, so a mis-set
    claims_options cannot silently weaken the binding. Nothing is normalised."""
    if not isinstance(claims, Mapping) or not issuer or not client_id:
        raise LoginRejected("oidc")
    iss = claims.get("iss")
    if not isinstance(iss, str) or iss != issuer:
        raise LoginRejected("oidc")
    aud = claims.get("aud")
    if isinstance(aud, str):
        auds = [aud]
    elif isinstance(aud, (list, tuple)) and aud:
        auds = list(aud)
    else:
        raise LoginRejected("oidc")
    if not all(isinstance(a, str) and a for a in auds) or client_id not in auds:
        raise LoginRejected("oidc")
    azp = claims.get("azp")
    if azp is not None and azp != client_id:
        raise LoginRejected("oidc")
    if len(auds) > 1 and azp != client_id:
        raise LoginRejected("oidc")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub or len(sub) > 512:
        raise LoginRejected("oidc")
    return issuer, sub


def claims_options() -> dict:
    """Authlib claim checks: exact configured issuer, this client as audience."""
    return {
        "iss": {"essential": True, "value": cfg.OIDC_ISSUER_URL},
        "aud": {"essential": True, "value": cfg.OIDC_CLIENT_ID},
        "sub": {"essential": True},
    }


def _groups(claims) -> list[str]:
    raw = claims.get(cfg.OIDC_GROUPS_CLAIM or "groups") or []
    if isinstance(raw, str):
        return [g.strip() for g in raw.split(",") if g.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(g) for g in raw]
    return []


def resolve_login(claims, *, issuer: str, client_id: str,
                  admission_mode: str | None = None, role_policy: str | None = None,
                  admin_group: str | None = None) -> dict:
    """Admit a verified OIDC identity and return the native user row.

    linked_only: only an existing exact (issuer, sub) link signs in.
    provision: an unknown identity becomes a new ordinary user.
    preserve: the stored role is never rewritten. groups: the role is set
    exactly from admin-group membership, so removal from the group demotes.
    Unlinked and disabled both give the same rejection, and no timestamp or
    row is written before admission is decided."""
    import db as _db
    mode = admission_mode or cfg.OIDC_ADMISSION_MODE
    policy = role_policy or cfg.OIDC_ROLE_POLICY
    group = cfg.OIDC_ADMIN_GROUP if admin_group is None else admin_group
    if mode not in ("linked_only", "provision") or policy not in ("preserve", "groups"):
        raise LoginRejected("oidc")
    iss, sub = validate_id_claims(claims, issuer, client_id)
    display = str(claims.get(cfg.OIDC_USER_CLAIM or "preferred_username")
                  or claims.get("email") or sub)[:64]
    user = _db.resolve_oidc_identity(iss, sub)
    if user is None:
        if mode != "provision":
            raise LoginRejected("oidc_unlinked")
        user = _db.provision_external_user("oidc", iss, sub, display, "oidc$disabled",
                                           (_settings_admin_name(),))
    if not user or not user.get("enabled"):
        raise LoginRejected("oidc_unlinked")
    if policy == "groups":
        want = "admin" if (group and group in _groups(claims)) else "user"
        if user.get("role") != want:
            _db.update_user(user["id"], role=want)
    _db.touch_oidc_login(user["id"], iss, sub, display)
    return _db.get_user(user["id"])


def _settings_admin_name() -> str:
    try:
        import settings
        return settings.get("AUTH_USERNAME", "admin")
    except Exception:
        return "admin"


def install(app: Flask) -> None:
    """Register OAuth client and the OIDC login + callback routes."""
    if not is_enabled():
        log.debug("OIDC: disabled (set OIDC_ENABLED=true and required vars to enable)")
        return

    try:
        from authlib.integrations.flask_client import OAuth
    except ImportError:
        log.error("OIDC: Authlib not installed; pip install Authlib")
        return

    global _oauth
    _oauth = OAuth(app)
    _oauth.register(
        name="oidc",
        server_metadata_url=f"{cfg.OIDC_ISSUER_URL.rstrip('/')}/.well-known/openid-configuration",
        client_id=cfg.OIDC_CLIENT_ID,
        client_secret=cfg.OIDC_CLIENT_SECRET,
        client_kwargs={"scope": cfg.OIDC_SCOPES},
    )
    log.info("OIDC: registered with issuer %s", cfg.OIDC_ISSUER_URL)

    @app.get("/login/oidc")
    def oidc_login():
        nxt = _safe_next_path(request.args.get("next"), default="/ui")
        session.clear()
        session["_oidc_next"] = nxt
        redirect_uri = url_for("oidc_callback", _external=True)
        return _oauth.oidc.authorize_redirect(redirect_uri)

    @app.get("/oidc/callback")
    def oidc_callback():
        nxt = _safe_next_path(session.get("_oidc_next"), default="/ui")
        try:
            token = _oauth.oidc.authorize_access_token(claims_options=claims_options())
        except Exception as exc:
            log.warning("OIDC: token exchange failed: %s", type(exc).__name__)
            session.clear()
            return redirect(url_for("login_view", error="oidc"))

        # Only claims Authlib parsed out of the signed ID token count. Plain
        # userinfo JSON is never used for identity or groups.
        claims = token.get("userinfo") if "id_token" in token else None
        try:
            user = resolve_login(claims, issuer=cfg.OIDC_ISSUER_URL, client_id=cfg.OIDC_CLIENT_ID)
        except LoginRejected as exc:
            log.warning("OIDC: sign-in rejected (%s)", exc.code)
            session.clear()
            return redirect(url_for("login_view", error=exc.code))
        except Exception as exc:
            log.warning("OIDC: sign-in failed: %s", type(exc).__name__)
            session.clear()
            return redirect(url_for("login_view", error="oidc"))

        session.clear()
        session["user"] = user["username"]
        session["user_id"] = user["id"]
        session["role"] = user["role"]
        session["auth_source"] = "oidc"
        log.info("OIDC: user id %s signed in (role=%s)", user["id"], user["role"])
        return redirect(nxt)
