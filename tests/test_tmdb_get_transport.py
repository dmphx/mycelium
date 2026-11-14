"""tmdb._get against a local fake transport.

conftest stubs tmdb._get for the whole offline suite; these tests opt out
with @pytest.mark.real_tmdb_get and replace only the HTTP layer, so the real
auth, error and redaction logic in _get still runs.
"""
import logging
import types

import pytest
import requests

import tmdb

V3_KEY = "0123456789abcdef0123456789abcdef"
V4_KEY = "eyJsynthetic.ci.token"


class _Response:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} for url: "
                                     f"https://api.themoviedb.org/3/x?api_key={V3_KEY}")

    def json(self):
        return self._payload


@pytest.fixture
def transport(monkeypatch):
    calls = []
    state = {"response": _Response(200, {"ok": True}), "key": V3_KEY}

    def get(url, headers=None, params=None, timeout=None):
        calls.append({"url": url, "headers": dict(headers or {}), "params": dict(params or {})})
        return state["response"]

    monkeypatch.setattr(tmdb, "req_lib", types.SimpleNamespace(
        get=get, RequestException=requests.RequestException))
    monkeypatch.setattr(tmdb, "_settings", types.SimpleNamespace(
        get=lambda key, default=None: state["key"] if key == "TMDB_API_KEY" else default))
    return calls, state


@pytest.mark.real_tmdb_get
def test_v3_key_goes_in_the_query_not_a_bearer_header(transport):
    calls, _ = transport
    assert tmdb._get("/find/tt0111161", params={"external_source": "imdb_id"}) == {"ok": True}
    assert calls[0]["url"] == "https://api.themoviedb.org/3/find/tt0111161"
    assert calls[0]["params"] == {"external_source": "imdb_id", "api_key": V3_KEY}
    assert "Authorization" not in calls[0]["headers"]


@pytest.mark.real_tmdb_get
def test_v4_token_goes_in_a_bearer_header_not_the_query(transport):
    calls, state = transport
    state["key"] = V4_KEY
    tmdb._get("/tv/1")
    assert calls[0]["headers"]["Authorization"] == f"Bearer {V4_KEY}"
    assert "api_key" not in calls[0]["params"]


@pytest.mark.real_tmdb_get
def test_http_error_returns_none_and_logs_without_the_key(transport, caplog):
    _, state = transport
    state["response"] = _Response(401, {})
    with caplog.at_level(logging.WARNING, logger="tmdb"):
        assert tmdb._get("/movie/1") is None
    assert "TMDB request failed" in caplog.text
    assert V3_KEY not in caplog.text


@pytest.mark.real_tmdb_get
def test_missing_key_skips_the_transport(transport):
    calls, state = transport
    state["key"] = ""
    assert tmdb._get("/movie/1") is None
    assert calls == []


def test_suite_default_stub_never_reaches_the_transport(transport):
    calls, _ = transport
    assert tmdb._get("/movie/1") is None
    assert calls == []
