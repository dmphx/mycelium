"""tmdb_checked with a fake requests session: no network, synthetic key."""
import sys
import threading
import types

import pytest
import requests

import tmdb_checked as tc

KEY = "k" * 32


@pytest.fixture(autouse=True)
def fake_tmdb(monkeypatch):
    calls = []
    mod = types.SimpleNamespace(_api_key=lambda: calls.append(1) or KEY)
    mod.calls = calls
    monkeypatch.setitem(sys.modules, "tmdb", mod)
    return mod


class Resp:
    def __init__(self, status=200, body=b"{}", headers=None, chunks=None, exc=None):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks if chunks is not None else [body]
        self._exc = exc
        self.closed = False

    def iter_content(self, chunk_size=0):
        for c in self._chunks:
            yield c
        if self._exc:
            raise self._exc

    def close(self):
        self.closed = True


class Sess:
    last = None

    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []
        self.trust_env = True
        self.closed = False
        Sess.last = self

    def get(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc
        return self.resp

    def close(self):
        self.closed = True


def run(resp=None, exc=None, path="/movie/603", params=None, clock=None):
    s = Sess(resp, exc)
    kw = {"clock": clock} if clock else {}
    return tc.fetch(path, params, session_factory=lambda: s, **kw), s


def test_identity_request_shape():
    data, s = run(Resp(body=b'{"a": 1}'), params={"page": 2})
    assert data == {"a": 1}
    url, kw = s.calls[0]
    assert url == "https://api.themoviedb.org/3/movie/603"
    assert kw["allow_redirects"] is False and kw["stream"] is True
    assert kw["headers"]["Accept-Encoding"] == "identity"
    assert kw["timeout"] == (3, 10)
    assert kw["params"] == {"page": "2", "api_key": KEY}
    assert s.closed and s.trust_env is False


def test_v4_token_uses_bearer(fake_tmdb):
    v4 = "eyJ" + "a" * 40
    fake_tmdb._api_key = lambda: v4
    _, s = run(Resp())
    kw = s.calls[0][1]
    assert kw["headers"]["Authorization"] == "Bearer " + v4
    assert "api_key" not in kw["params"]


@pytest.mark.parametrize("path", ["/movie/0", "/movie/01", "/movie/1/credits", "/../x",
                                  "/tv/5/season/10000", "/person/abc", "//movie/1", None])
def test_path_allowlist(path):
    with pytest.raises(tc.TmdbUnavailable, match="path_not_allowed"):
        run(Resp(), path=path)


@pytest.mark.parametrize("params", [{"api_key": "x"}, {"page": True}, {"page": 1.5}, {"bogus": "1"}])
def test_param_allowlist(params):
    with pytest.raises(tc.TmdbUnavailable, match="param_not_allowed"):
        run(Resp(), params=params)


def test_404_vs_outage():
    with pytest.raises(tc.TmdbNotFound):
        run(Resp(404))
    for status in (301, 302, 401, 429, 500, 503):
        with pytest.raises(tc.TmdbUnavailable, match="upstream_status"):
            run(Resp(status))
    with pytest.raises(tc.TmdbUnavailable, match="upstream_unreachable"):
        run(exc=requests.ConnectionError("https://x?api_key=" + KEY))
    with pytest.raises(tc.TmdbUnavailable, match="upstream_unreachable"):
        run(Resp(chunks=[b"{"], exc=requests.ReadTimeout("slow")))


def test_no_key(fake_tmdb):
    fake_tmdb._api_key = lambda: ""
    with pytest.raises(tc.TmdbUnavailable, match="no_key"):
        run(Resp())


def test_redaction(caplog):
    caplog.set_level("DEBUG")
    secret_url = "https://api.themoviedb.org/3/movie/1?api_key=" + KEY
    with pytest.raises(tc.TmdbUnavailable) as e:
        run(exc=requests.ConnectionError(secret_url))
    assert e.value.__cause__ is None and e.value.__suppress_context__
    text = str(e.value) + caplog.text + repr(e.value.args)
    assert KEY not in text and "themoviedb" not in text
    with pytest.raises(tc.TmdbNotFound) as e:
        run(Resp(404, body=b"upstream says " + KEY.encode()))
    assert KEY not in str(e.value) + caplog.text


def test_size_caps():
    big = tc.MAX_BYTES + 1
    with pytest.raises(tc.TmdbUnavailable, match="too_large"):
        run(Resp(headers={"Content-Length": str(big)}))
    with pytest.raises(tc.TmdbUnavailable, match="too_large"):
        run(Resp(headers={"Content-Length": "-1"}))
    # lying/missing Content-Length: actual bytes are counted
    with pytest.raises(tc.TmdbUnavailable, match="too_large"):
        run(Resp(chunks=[b" " * tc.MAX_BYTES, b" "]))
    with pytest.raises(tc.TmdbUnavailable, match="length_mismatch"):
        run(Resp(body=b"{}", headers={"Content-Length": "5"}))
    ok, _ = run(Resp(body=b"{}", headers={"Content-Length": "2"}))
    assert ok == {}


def test_encoded_body_rejected():
    with pytest.raises(tc.TmdbUnavailable, match="encoded_body"):
        run(Resp(headers={"Content-Encoding": "gzip"}))


@pytest.mark.parametrize("body", [b"[]", b'"x"', b"null", b'{"a":1,"a":2}', b'{"a":NaN}',
                                  b'{"a":Infinity}', b'{"a":-Infinity}', b"{", b"\xff", b""])
def test_strict_json(body):
    with pytest.raises(tc.TmdbUnavailable, match="bad_json"):
        run(Resp(body=body))


def test_legitimate_unicode_and_multiline():
    text = "line1\nline2\t‍\U0001F468‍\U0001F469 café"
    import json
    data, _ = run(Resp(body=json.dumps({"overview": text}).encode()))
    assert data["overview"] == text


def fake_clock(*values):
    it = iter(values)
    last = [0]

    def clock():
        last[0] = next(it, last[0])
        return last[0]
    return clock


def test_deadline_cases():
    # late chunk: start, after get, chunk1 ok, chunk2 late
    with pytest.raises(tc.TmdbUnavailable, match="deadline"):
        run(Resp(chunks=[b"{", b"}"]), clock=fake_clock(0, 1, 2, 100))
    # slow headers: already over budget when the response arrives
    with pytest.raises(tc.TmdbUnavailable, match="deadline"):
        run(Resp(), clock=fake_clock(0, 100))
    # empty body, over budget after the loop
    with pytest.raises(tc.TmdbUnavailable, match="deadline"):
        run(Resp(chunks=[]), clock=fake_clock(0, 1, 100))
    # empty body in budget is a JSON failure, not a deadline
    with pytest.raises(tc.TmdbUnavailable, match="bad_json"):
        run(Resp(chunks=[]), clock=fake_clock(0, 1, 2))
    assert "not a hard" in tc.__doc__.lower() or "NOT a hard" in tc.__doc__


def test_concurrency_nonblocking():
    held = [tc._slots.acquire(blocking=False) for _ in range(tc.MAX_CONCURRENT)]
    try:
        assert all(held)
        with pytest.raises(tc.TmdbUnavailable, match="busy"):
            run(Resp())
    finally:
        for _ in held:
            tc._slots.release()
    ok, _ = run(Resp())
    assert ok == {}


def test_slot_released_after_failure():
    for _ in range(tc.MAX_CONCURRENT + 2):
        with pytest.raises(tc.TmdbUnavailable):
            run(Resp(500))


def test_response_closed():
    r = Resp()
    run(r)
    assert r.closed
    r = Resp(404)
    with pytest.raises(tc.TmdbNotFound):
        run(r)
    assert r.closed


# --- review hardening -------------------------------------------------------

@pytest.mark.parametrize("params", [
    "page=1", [("page", 1)], {1: 2},
    {"page": 0}, {"page": 501}, {"page": "01"}, {"page": "1\n"}, {"page": " 1"}, {"page": "1.0"},
    {"region": "nl"}, {"region": "NLD"}, {"region": "NL\n"}, {"watch_region": "N"},
    {"with_genres": "0"}, {"with_genres": "2147483648"}, {"with_genres": "1,"}, {"with_genres": "a"},
    {"with_watch_providers": "1;2"}, {"with_watch_monetization_types": "rent"},
    {"append_to_response": "credits,bogus"}, {"append_to_response": "credits,credits"},
    {"append_to_response": "credits, videos"}, {"include_adult": "true"}, {"include_adult": True},
    {"sort_by": "vote_average.desc"}, {"language": "english"}, {"language": "EN"},
    {"first_air_date.gte": "2020-02-30"}, {"first_air_date.gte": "2020-1-1"},
    {"primary_release_date.lte": "2020-01-01\n"},
    {"query": ""}, {"query": "x" * 101}, {"query": "a\x00b"}, {"query": "a\nb"},
    {"query": "a\x85b"}, {"query": "a\ud800b"}, {"query": 5}, {"api_key": "x"}, {"bogus": "1"}])
def test_param_grammar_negatives(params):
    with pytest.raises(tc.TmdbUnavailable, match="param_not_allowed"):
        run(Resp(), path="/search/multi", params=params)


def test_param_grammar_positives():
    ok = {"page": 500, "region": "NL", "language": "en-US", "with_genres": "28,12",
          "with_watch_providers": "8|9", "with_watch_monetization_types": "flatrate",
          "append_to_response": "credits,videos,external_ids,watch/providers,recommendations",
          "include_adult": False, "sort_by": "popularity.desc", "watch_region": "US",
          "first_air_date.gte": "2020-01-01", "query": "caf\u00e9 \U0001F600 \u200d"}
    _, s = run(Resp(), path="/search/multi", params=ok)
    sent = s.calls[0][1]["params"]
    assert sent["page"] == "500" and sent["include_adult"] == "false" and sent["query"].startswith("caf")


@pytest.mark.parametrize("path", ["/movie/2147483648", "/movie/9999999999", "/movie/1\n",
                                  "/movie/1/", "/tv/1/season/00", "/person/1\n", "/trending/all/month",
                                  "/discover/person", " /movie/1"])
def test_path_id_bounds_and_fullmatch(path):
    with pytest.raises(tc.TmdbUnavailable, match="path_not_allowed"):
        run(Resp(), path=path)
    assert run(Resp(), path="/movie/2147483647")[0] == {}


@pytest.mark.parametrize("body", [b'{"a":1e999}', b'{"a":-1e999}', b'{"a":' + b"9" * 25 + b"}",
                                  b'{"a":' + b"9" * 5000 + b"}", b'{"a":' + b"[" * 100000 + b"]" * 100000 + b"}"], ids=["inf", "-inf", "int25", "int5000", "deep"])
def test_overflow_numbers_and_depth(body):
    with pytest.raises(tc.TmdbUnavailable, match="bad_json"):
        run(Resp(body=body))


def test_normal_numbers_ok():
    data, _ = run(Resp(body=b'{"a":1.5,"b":-2,"c":1e3,"d":2147483647}'))
    assert data == {"a": 1.5, "b": -2, "c": 1000.0, "d": 2147483647}


@pytest.mark.parametrize("length", ["9" * 5000, "1" * 13, "-5", "1e3", " 5", "5 ", "\u0665", ""])
def test_hostile_content_length_is_safe_error(length):
    with pytest.raises(tc.TmdbUnavailable, match="too_large") as e:
        run(Resp(headers={"Content-Length": length}))
    assert isinstance(e.value, tc.TmdbUnavailable)


def test_close_errors_are_swallowed(caplog):
    class BadResp(Resp):
        def close(self):
            raise RuntimeError("upstream text " + KEY)

    class BadSess(Sess):
        def close(self):
            raise RuntimeError("secret " + KEY)

    s = BadSess(BadResp(body=b"{}"))
    assert tc.fetch("/movie/1", session_factory=lambda: s) == {}
    assert KEY not in caplog.text
    s = BadSess(BadResp(404))
    with pytest.raises(tc.TmdbNotFound):
        tc.fetch("/movie/1", session_factory=lambda: s)


def test_session_factory_failure_and_generic_get_error():
    def boom():
        raise RuntimeError("secret " + KEY)
    with pytest.raises(tc.TmdbUnavailable, match="session_error") as e:
        tc.fetch("/movie/1", session_factory=boom)
    assert KEY not in str(e.value)
    with pytest.raises(tc.TmdbUnavailable, match="upstream_error"):
        run(exc=ValueError("secret " + KEY))


@pytest.mark.parametrize("key", [None, 5, "", " " + "k" * 31, "k" * 31 + "\n", "k" * 2049, "k\x00k", "k\u00e9" * 10])
def test_bad_key_values(fake_tmdb, key):
    fake_tmdb._api_key = lambda: key
    with pytest.raises(tc.TmdbUnavailable, match="no_key"):
        run(Resp())


def test_key_read_once_per_call(fake_tmdb):
    run(Resp())
    assert len(fake_tmdb.calls) == 1


def test_key_helper_failure_is_safe(fake_tmdb):
    def boom():
        raise RuntimeError("db path secret")
    fake_tmdb._api_key = boom
    with pytest.raises(tc.TmdbUnavailable, match="auth_error") as e:
        run(Resp())
    assert "secret" not in str(e.value)


def test_specials_zero_is_allowed_but_parent_zero_is_not():
    data, session = run(Resp(), path="/tv/34549/season/0")
    assert data == {} and session.calls[0][0].endswith("/tv/34549/season/0")
    with pytest.raises(tc.TmdbUnavailable, match="path_not_allowed"):
        run(Resp(), path="/tv/0/season/0")


@pytest.mark.parametrize("value", ["01", "1,02", "1|02"])
def test_provider_and_genre_identifiers_are_canonical(value):
    with pytest.raises(tc.TmdbUnavailable, match="param_not_allowed"):
        run(Resp(), params={"with_genres": value})


@pytest.mark.parametrize("imdb", ["tt123456", "tt0001234", "tt1234567890"])
def test_narrow_imdb_find_request(imdb):
    data, session = run(Resp(), path="/find/" + imdb, params={"external_source": "imdb_id"})
    url, sent = session.calls[0]
    assert data == {} and url.endswith("/find/" + imdb)
    assert sent["params"] == {"external_source": "imdb_id", "api_key": KEY}
    assert sent["allow_redirects"] is False and session.trust_env is False and session.closed


@pytest.mark.parametrize("path", ["/find/tt12345", "/find/tt12345678901", "/find/TT1234567",
    "/find/tt１２３４５６７", "/find/tt1234567/", "/find/tt1234567\n", "/find/%74t1234567",
    "/find/tt1234567?external_source=imdb_id", "//find/tt1234567", "/find/1234567"])
def test_find_path_aliases_rejected(path):
    with pytest.raises(tc.TmdbUnavailable, match="path_not_allowed"):
        run(Resp(), path=path, params={"external_source": "imdb_id"})


@pytest.mark.parametrize("params", [None, {}, {"external_source": "tvdb_id"}, {"external_source": True},
    {"external_source": "imdb_id\n"}, {"external_source": "imdb_id", "page": 1},
    {"external_source": "imdb_id", "language": "en-US"}, {"external_source": "imdb_id", "append_to_response": "external_ids"}])
def test_find_parameters_are_path_specific_and_exact(params):
    with pytest.raises(tc.TmdbUnavailable, match="param_not_allowed"):
        run(Resp(), path="/find/tt1234567", params=params)


@pytest.mark.parametrize("path", ["/movie/603", "/tv/1399", "/search/multi", "/person/9"])
def test_find_does_not_widen_existing_parameter_grammar(path):
    with pytest.raises(tc.TmdbUnavailable, match="param_not_allowed"):
        run(Resp(), path=path, params={"external_source": "imdb_id"})
