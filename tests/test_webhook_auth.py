"""
Webhook secret transport for POST /webhook (app._check_auth).

Seerr's webhook agent can send exactly one credential: its "Authorization
Header" setting, verbatim as the Authorization header. Accepting that header
(raw or as "Bearer <secret>") lets Seerr call the plain /webhook URL instead of
carrying the secret in ?secret=, where it lands in access logs and in Seerr's
saved settings. X-Webhook-Secret keeps working, and ?secret= keeps working with
a deprecation warning until every caller has moved to a header.
"""
import logging
import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("TORBOX_API_KEY", "test")
os.environ.setdefault("MEDIA_PATH", "/tmp/mycelium-test-media")
os.environ.setdefault("SPORE_MEDIA_PATH", "/tmp/mycelium-test-spore")
os.environ.setdefault("TORBOX_BASE_URL", "https://api.torbox.app/v1/api")
# app.py refuses to import with no auth method configured.
os.environ.setdefault("INSECURE_ALLOW_ANON", "true")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# apscheduler and flask_limiter are not installed in the test env. The limiter
# decorators must stay pass-through or the routes they wrap become MagicMocks
# and Flask's registration fails at import.
for _m in ("apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.background",
           "apscheduler.triggers", "apscheduler.triggers.cron",
           "apscheduler.triggers.interval"):
    sys.modules.setdefault(_m, MagicMock())
if "flask_limiter" not in sys.modules:
    _fl = MagicMock()
    _identity = lambda *a, **k: (lambda f: f)  # noqa: E731
    _fl.Limiter.return_value.limit = _identity
    _fl.Limiter.return_value.exempt = _identity
    sys.modules["flask_limiter"] = _fl
    sys.modules["flask_limiter.util"] = MagicMock()

import pytest  # noqa: E402
from werkzeug.exceptions import Unauthorized  # noqa: E402

import app as app_mod  # noqa: E402

SECRET = "test-webhook-secret"
DEPRECATION = "passed via ?secret="
NON_ASCII = "caf" + chr(0xE9)


@pytest.fixture(autouse=True)
def _webhook_secret(monkeypatch):
    monkeypatch.setattr(app_mod, "WEBHOOK_SECRET", SECRET)


@pytest.fixture
def warnings(caplog):
    caplog.set_level(logging.WARNING, logger="mycelium")
    return caplog


def _check(headers=None, query=None):
    with app_mod.app.test_request_context(
        "/webhook", method="POST", headers=headers or {}, query_string=query or {},
    ):
        app_mod._check_auth()


@pytest.mark.parametrize("value", [
    SECRET,
    f"Bearer {SECRET}",
    f"bearer {SECRET}",
    f"Bearer   {SECRET}",
])
def test_authorization_header_is_accepted(value, warnings):
    _check(headers={"Authorization": value})
    assert DEPRECATION not in warnings.text


@pytest.mark.parametrize("value", [
    "wrong",
    "Bearer wrong",
    f"Basic {SECRET}",
    f"Bearer {SECRET}x",
    f"{SECRET} extra",
    "Bearer",
    "Bearer ",
])
def test_wrong_authorization_header_is_rejected(value):
    with pytest.raises(Unauthorized):
        _check(headers={"Authorization": value})


def test_x_webhook_secret_header_is_still_accepted(warnings):
    _check(headers={"X-Webhook-Secret": SECRET})
    assert DEPRECATION not in warnings.text


def test_wrong_x_webhook_secret_is_rejected():
    with pytest.raises(Unauthorized):
        _check(headers={"X-Webhook-Secret": "wrong"})


def test_query_secret_is_still_accepted_with_a_deprecation_warning(warnings):
    _check(query={"secret": SECRET})
    assert DEPRECATION in warnings.text
    # The warning names the transport, never the value.
    assert SECRET not in warnings.text


def test_query_secret_next_to_a_header_still_warns(warnings):
    # The secret is in the URL either way, so it is in the access log too.
    _check(headers={"Authorization": SECRET}, query={"secret": SECRET})
    assert DEPRECATION in warnings.text


def test_wrong_query_secret_is_rejected():
    with pytest.raises(Unauthorized):
        _check(query={"secret": "wrong"})


def test_missing_credential_is_rejected():
    with pytest.raises(Unauthorized):
        _check()


@pytest.mark.parametrize("headers,query", [
    ({"Authorization": f"Bearer {NON_ASCII}"}, None),
    ({"X-Webhook-Secret": NON_ASCII}, None),
    (None, {"secret": NON_ASCII}),
])
def test_non_ascii_credential_is_a_401_not_a_500(headers, query):
    # hmac.compare_digest raises TypeError on a non-ASCII str.
    with pytest.raises(Unauthorized):
        _check(headers=headers, query=query)


def test_no_secret_configured_leaves_the_webhook_open(monkeypatch):
    monkeypatch.setattr(app_mod, "_effective_webhook_secret", lambda: "")
    _check()


def test_webhook_route_takes_the_secret_from_authorization():
    # Seerr's "Test" button sends TEST_NOTIFICATION, which the parser ignores
    # only after the request has been authenticated.
    client = app_mod.app.test_client()
    payload = {"notification_type": "TEST_NOTIFICATION"}

    ok = client.post("/webhook", json=payload, headers={"Authorization": f"Bearer {SECRET}"})
    assert ok.status_code == 200
    assert ok.get_json()["status"] == "ignored"

    bad = client.post("/webhook", json=payload, headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 401
