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
    if not nxt.startswith("/") or nxt.startswith("//"):
        return default
    return nxt


def is_enabled() -> bool:
    return bool(cfg.OIDC_ENABLED and cfg.OIDC_ISSUER_URL and cfg.OIDC_CLIENT_ID)


def provider_name() -> str:
    return cfg.OIDC_PROVIDER_NAME or "SSO"


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
        session["_oidc_next"] = nxt
        redirect_uri = url_for("oidc_callback", _external=True)
        return _oauth.oidc.authorize_redirect(redirect_uri)

    @app.get("/oidc/callback")
    def oidc_callback():
        try:
            token = _oauth.oidc.authorize_access_token()
        except Exception as exc:
            log.warning("OIDC: token exchange failed: %s", exc)
            return redirect(url_for("login_view", error="oidc"))

        user_info = token.get("userinfo")
        if not user_info:
            try:
                user_info = _oauth.oidc.userinfo()
            except Exception as exc:
                log.warning("OIDC: userinfo fetch failed: %s", exc)
                return redirect(url_for("login_view", error="oidc"))

        claim = cfg.OIDC_USER_CLAIM or "preferred_username"
        username = (
            (user_info or {}).get(claim)
            or (user_info or {}).get("email")
            or (user_info or {}).get("sub")
        )
        if not username:
            log.warning("OIDC: no usable user claim in userinfo: %s", list((user_info or {}).keys()))
            return redirect(url_for("login_view", error="oidc"))

        # Map upstream groups claim to a Mycelium role. Previously any OIDC
        # caller was synthesized into a virtual admin record; now membership
        # in OIDC_ADMIN_GROUP (default "admins") is the only path to admin,
        # and the resulting users-table row is persisted so request quotas /
        # watchlists etc. attribute correctly.
        groups_raw = (user_info or {}).get(cfg.OIDC_GROUPS_CLAIM or "groups") or []
        if isinstance(groups_raw, str):
            groups = [g.strip() for g in groups_raw.split(",") if g.strip()]
        else:
            groups = [str(g) for g in groups_raw]
        role = "admin" if (cfg.OIDC_ADMIN_GROUP and cfg.OIDC_ADMIN_GROUP in groups) else "user"

        try:
            import db as _db
            uid = _db.upsert_oidc_user(username, role=role)
            _db.touch_user_login(uid)
        except Exception as exc:
            log.warning("OIDC: user provisioning failed for %s: %s", username, exc)
            return redirect(url_for("login_view", error="oidc"))

        session["user"] = username
        session["user_id"] = uid
        session["role"] = role
        session["auth_source"] = "oidc"
        nxt = _safe_next_path(session.pop("_oidc_next", "/ui"), default="/ui")
        log.info("OIDC: %s signed in (role=%s, groups=%s)", username, role, groups)
        return redirect(nxt)
