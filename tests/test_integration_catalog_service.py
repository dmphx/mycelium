"""Deterministic catalog orchestration tests. No network or real credentials."""
import datetime
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

import integration_auth as ia
import integration_catalog as ic
import integration_catalog_service as service
import tmdb_checked as tc

USER = {"region": "NL"}
EMPTY = {"page": 1, "total_pages": 0, "results": []}
TAB = {"media_type": "movie", "genre_id": 28, "genre_name": "Action",
       "year_from": None, "year_to": None, "enabled": True}


def reader(key):
    return "synthetic-key" if key == "TMDB_API_KEY" else None


def detail(kind="movie", tid=603):
    raw = {"id": tid, "title" if kind == "movie" else "name": "Synthetic",
           "genres": [], "credits": {"cast": [], "crew": []},
           "videos": {"results": []}, "recommendations": {"results": []},
           "watch/providers": {"results": {
               "NL": {"flatrate": [{"provider_id": 8, "provider_name": "NL catalog"}]},
               "US": {"buy": [{"provider_id": 9, "provider_name": "US catalog"}]}}}}
    if kind == "tv":
        raw["seasons"] = []
    return raw


def run(operation, query="", identity=(), raw=None, **kwargs):
    calls = []

    def fetch(path, params=None):
        calls.append((path, params))
        return EMPTY.copy() if raw is None else raw

    result = service.execute(operation, "/integration/v1/test" + query, USER, identity,
                             fetch=fetch, read_setting=reader, **kwargs)
    return result, calls


@pytest.mark.parametrize("rail,query,path,args", [
    ("trending", "", "/trending/all/week", {"page": 1}),
    ("trending", "?page=500&type=tv&window=day", "/trending/tv/day", {"page": 500}),
    ("popular", "?region=US&type=tv", "/tv/popular", {"page": 1, "region": "US"}),
    ("popular", "", "/movie/popular", {"page": 1, "region": "NL"}),
    ("top-rated", "?type=tv", "/tv/top_rated", {"page": 1}),
    ("now-playing", "", "/movie/now_playing", {"page": 1, "region": "NL"}),
    ("upcoming", "?region=US", "/movie/upcoming", {"page": 1, "region": "US"}),
    ("on-the-air", "", "/tv/on_the_air", {"page": 1}),
    ("by-provider", "?provider_id=8&region=US&type=tv", "/discover/tv",
     {"page": 1, "sort_by": "popularity.desc", "include_adult": False,
      "with_watch_providers": 8, "watch_region": "US", "with_watch_monetization_types": "flatrate"}),
    ("by-genre", "?genre_id=28&year_from=1800&year_to=3000", "/discover/movie",
     {"page": 1, "sort_by": "popularity.desc", "include_adult": False,
      "with_genres": 28, "primary_release_date.gte": "1800-01-01", "primary_release_date.lte": "3000-12-31"}),
    ("by-genre", "?genre_id=18&type=tv&year_to=2020", "/discover/tv",
     {"page": 1, "sort_by": "popularity.desc", "include_adult": False,
      "with_genres": 18, "first_air_date.lte": "2020-12-31"}),
])
def test_rail_matrix(rail, query, path, args):
    result, calls = run("catalog.rails", query, (rail,), raw=dict(EMPTY, page=args["page"]))
    assert calls == [(path, args)]
    assert result == {"items": [], "page": args["page"], "total_pages": 0}
    tc._check_request(path, args)  # Exact matrix also obeys the checked transport grammar.


@pytest.mark.parametrize("kind,path", [("all", "multi"), ("movie", "movie"), ("tv", "tv")])
def test_search_types(kind, path):
    result, calls = run("catalog.search", "?page=1&q=%20caf%C3%A9%20&type=" + kind)
    assert result["items"] == []
    assert calls == [("/search/" + path, {"query": "café", "page": 1, "include_adult": False})]


@pytest.mark.parametrize("encoded,decoded", [("%2541", "%41"), ("a%26b%3Dc", "a&b=c"),
                                           ("%C3%A9", "é"), ("e%CC%81", "e\u0301")])
def test_query_decoded_once(encoded, decoded):
    _, calls = run("catalog.search", "?q=" + encoded)
    assert calls[0][1]["query"] == decoded


INVALID = [
    ("catalog.search", q, ()) for q in ("", "?q=", "?q=%20", "?q=" + "a" * 101,
        "?q=a&type=person", "?page=0&q=a", "?page=501&q=a", "?page=01&q=a", "?q=a&region=NL")
] + [
    ("catalog.rails", q, (rail,)) for rail, q in (
        ("trending", "?region=NL"), ("trending", "?window=month"), ("popular", "?type=all"),
        ("top-rated", "?region=NL"), ("now-playing", "?type=tv"), ("upcoming", "?type=tv"),
        ("on-the-air", "?type=movie"), ("on-the-air", "?region=NL"),
        ("by-provider", ""), ("by-provider", "?provider_id=0"),
        ("by-provider", "?provider_id=2147483648"), ("by-provider", "?provider_id=08"),
        ("by-genre", "?genre_id=28&year_from=2021&year_to=2020"),
        ("by-genre", "?genre_id=28&year_from=1799"), ("by-genre", "?genre_id=28&year_to=3001"),
        ("by-genre", "?genre_id=28&year_from=02000"), ("by-genre", "?genre_id=1&provider_id=8"),
        ("popular", "?region=nl"), ("popular", "?region=NL%0A"), ("popular", "?page=1.0"))
] + [
    ("genres.read", "", ()), ("providers.read", "", ()),
    ("genres.read", "?type=all", ()), ("providers.read", "?region=NLD&type=movie", ()),
    ("catalog.details", "?region=US", ("movie", "603")),
    ("catalog.details", "", ("movie", "2147483648")),
    ("catalog.seasons", "", ("1399", "00")), ("catalog.seasons", "", ("1399", "10000")),
    ("people.read", "", ("0",)), ("people.read", "?page=1", ("9",)),
]


@pytest.mark.parametrize("operation,query,identity", INVALID)
def test_invalid_inputs_make_zero_fetches(operation, query, identity):
    def forbidden(*args):
        pytest.fail("invalid input must not read configuration or fetch")
    with pytest.raises(ia.IntegrationError) as error:
        service.execute(operation, "/integration/v1/test" + query, USER, identity,
                        fetch=forbidden, read_setting=forbidden)
    assert error.value.status == 400


@pytest.mark.parametrize("rail", list(service._RAIL_KEYS))
def test_every_inapplicable_rail_key_rejected(rail):
    union = set().union(*service._RAIL_KEYS.values())
    for key in union - service._RAIL_KEYS[rail]:
        with pytest.raises(ia.IntegrationError) as error:
            run("catalog.rails", "?" + key + "=1", (rail,))
        assert error.value.status == 400


@pytest.mark.parametrize("operation,query,raw,path,args", [
    ("genres.read", "?type=movie", {"genres": []}, "/genre/movie/list", None),
    ("providers.read", "?type=tv", {"results": []}, "/watch/providers/tv", {"watch_region": "NL"}),
    ("providers.read", "?region=US&type=movie", {"results": []}, "/watch/providers/movie", {"watch_region": "US"}),
])
def test_genre_provider_matrix(operation, query, raw, path, args):
    data, calls = run(operation, query, raw=raw)
    assert data["items"] == [] and calls == [(path, args)]


@pytest.mark.parametrize("kind", ["movie", "tv"])
def test_detail_matrix(kind):
    data, calls = run("catalog.details", identity=(kind, "603"), raw=detail(kind))
    assert data["watch_providers"]["region"] == "NL"
    assert calls == [(f"/{kind}/603", {"append_to_response": "credits,videos,watch/providers,recommendations"})]
    assert not {"availability", "entitlement", "can_play", "in_library"} & data.keys()


def test_season_exact_raw_identity_and_air_states():
    calls = []
    show = {"id": 1399, "seasons": [{"season_number": 0, "episode_count": 3}]}
    season = {"id": 99, "season_number": 0, "episodes": [
        {"id": n, "season_number": 0, "episode_number": n, "name": "Episode",
         "air_date": date} for n, date in ((1, None), (2, "2026-10-21"), (3, "2026-10-22"))]}

    def fetch(path, params):
        calls.append((path, params))
        return show if len(calls) == 1 else season

    data = service.execute("catalog.seasons", "/integration/v1/test", USER, ("1399", "0"),
                           fetch=fetch, read_setting=reader, today=datetime.date(2026, 10, 21))
    assert calls == [("/tv/1399", None), ("/tv/1399/season/0", None)]
    assert data["season"] == 0 and data["complete"] is True and data["numbering"] == "tmdb"
    assert [e["air_state"] for e in data["episodes"]] == ["unknown", "aired", "unaired"]


def test_person_matrix():
    raw = {"id": 9, "name": "Person", "combined_credits": {"cast": [], "crew": []}}
    data, calls = run("people.read", identity=("9",), raw=raw)
    assert data["filmography"] == [] and data["filmography_complete"] is True
    assert calls == [("/person/9", {"append_to_response": "combined_credits"})]


@pytest.mark.parametrize("operation,identity,status", [
    ("catalog.search", (), 503), ("catalog.rails", ("trending",), 503),
    ("genres.read", (), 503), ("providers.read", (), 503),
    ("catalog.details", ("movie", "603"), 404), ("catalog.seasons", ("1399", "0"), 404),
    ("people.read", ("9",), 404),
])
def test_upstream_notfound_is_route_specific(operation, identity, status):
    def missing(*args):
        raise tc.TmdbNotFound("not_found")
    query = "?q=a" if operation == "catalog.search" else "?type=movie" if operation in (
        "genres.read", "providers.read") else ""
    with pytest.raises(ia.IntegrationError) as error:
        service.execute(operation, "/integration/v1/test" + query, USER, identity,
                        fetch=missing, read_setting=reader)
    assert error.value.status == status and not error.value.retryable


@pytest.mark.parametrize("reason,retryable", [(r, True) for r in service.RETRYABLE_REASONS] + [
    (r, False) for r in ("bad_json", "too_large", "length_mismatch", "encoded_body", "no_key",
                         "auth_error", "param_not_allowed", "path_not_allowed", "unknown")])
def test_fetch_retry_reason_allowlist(reason, retryable):
    def failure(*args):
        raise tc.TmdbUnavailable(reason)
    with pytest.raises(ia.IntegrationError) as error:
        service.execute("catalog.search", "/integration/v1/test?q=a", USER,
                        fetch=failure, read_setting=reader)
    assert error.value.status == 503 and error.value.retryable is retryable


def test_missing_season_caught_before_unavailable_and_no_partial_empty():
    for show, season, expected in (
        ({"id": 1399, "seasons": []}, {"season_number": 0, "episodes": []}, 404),
        ({"id": 1399, "seasons": [{"season_number": 0, "episode_count": 1}]},
         {"season_number": 0, "episodes": []}, 503),
        ({"id": 1399, "seasons": [{"season_number": 0, "episode_count": 0}]},
         {"season_number": 0, "episodes": []}, 200),
    ):
        def fetch(path, params):
            return season if "/season/" in path else show
        try:
            data = service.execute("catalog.seasons", "/integration/v1/test", USER, ("1399", "0"),
                                   fetch=fetch, read_setting=reader)
            assert expected == 200 and data["complete"] is True and data["episodes"] == []
        except ia.IntegrationError as error:
            assert error.status == expected and not error.retryable


def test_native_region_errors_override_and_no_cross_viewer_state():
    for value in (None, "", "nl", "NLD", "NL\n", True):
        for operation, query, identity, code, status in (
            ("catalog.details", "", ("movie", "603"), "preference_invalid", 503),
            ("providers.read", "?type=movie", (), "region_required", 400),
        ):
            with pytest.raises(ia.IntegrationError) as error:
                service.execute(operation, "/integration/v1/test" + query, {"region": value}, identity,
                                fetch=lambda *a: pytest.fail("fetch forbidden"), read_setting=reader)
            assert error.value.code == code and error.value.status == status
        data = service.execute("providers.read", "/integration/v1/test?region=US&type=movie",
                               {"region": value}, fetch=lambda *a: {"results": []}, read_setting=reader)
        assert data["region"] == "US"

    def lookup(region):
        return service.execute("catalog.details", "/integration/v1/test", {"region": region},
                               ("movie", "603"), fetch=lambda *a: detail(), read_setting=reader)
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lookup, ["NL", "US"] * 10))
    assert [v["watch_providers"]["region"] for v in values] == ["NL", "US"] * 10
    assert values[0]["watch_providers"]["items"][0]["provider_id"] == 8
    assert values[1]["watch_providers"]["items"][0]["provider_id"] == 9


def tabs(raw):
    return service.execute("genre-tabs.read", "/integration/v1/test", USER,
                           read_setting=lambda key: raw,
                           fetch=lambda *a: pytest.fail("tabs must not fetch"))


def test_tabs_mapping_absence_empty_disabled_and_distinct_ranges():
    assert tabs(None) == tabs("[]") == {"items": []}
    data = tabs(json.dumps([TAB, dict(TAB, genre_id=18, enabled=False), dict(TAB, year_from=2000)]))
    assert data == {"items": [
        {"media_type": "movie", "genre_id": 28, "name": "Action", "year_from": None, "year_to": None},
        {"media_type": "movie", "genre_id": 28, "name": "Action", "year_from": 2000, "year_to": None}]}


@pytest.mark.parametrize("raw", ["", "null", "{}", "[", "[NaN]", "[Infinity]", "[1e999]",
    '[{"enabled":true,"enabled":false}]', json.dumps([TAB, TAB]),
    json.dumps([dict(TAB, enabled="false")]), json.dumps([dict(TAB, extra=1)]),
    json.dumps([dict(TAB, genre_id=True)]), json.dumps([dict(TAB, genre_id=1.0)]),
    json.dumps([dict(TAB, genre_name="")]), json.dumps([dict(TAB, year_from=3001)]),
    json.dumps([dict(TAB, year_from=2021, year_to=2020)]),
    json.dumps([dict(TAB, enabled=False, media_type="all")]), json.dumps([TAB] * 201),
    json.dumps([dict(TAB, genre_id=10**30)]), "[" * 1100, [],
])
def test_tabs_fail_closed(raw):
    with pytest.raises(ia.IntegrationError) as error:
        tabs(raw)
    assert error.value.status == 503 and not error.value.retryable


def test_strict_read_failure_and_capabilities_preserve_base(monkeypatch):
    import db
    def broken(key):
        raise OSError("private value must not escape")
    monkeypatch.setattr(db, "get_setting", broken)
    with pytest.raises(ia.IntegrationError) as error:
        service.execute("genre-tabs.read", "/integration/v1/test", USER)
    assert error.value.status == 503 and error.value.retryable
    assert "private" not in error.value.message
    assert service.operations(USER) == set(service.BASE_OPERATIONS)
    assert service.operations(USER, read_setting=reader) == set(service.BASE_OPERATIONS + service.CATALOG_OPERATIONS)
    assert "catalog.details" not in service.operations({}, read_setting=reader)
    assert service.operations(USER, read_setting=lambda k: None) == set(service.BASE_OPERATIONS + ("genre-tabs.read",))
    assert "genre-tabs.read" not in service.operations(USER, read_setting=lambda k:
        "synthetic-key" if k == "TMDB_API_KEY" else "broken")


def test_configuration_failure_prevents_fetch():
    def failure(key):
        raise OSError("unavailable")
    for read, retryable in ((failure, True), (lambda k: "", False)):
        with pytest.raises(ia.IntegrationError) as error:
            service.execute("catalog.search", "/integration/v1/test?q=a", USER,
                            read_setting=read, fetch=lambda *a: pytest.fail("fetch forbidden"))
        assert error.value.status == 503 and error.value.retryable is retryable


def test_malformed_data_and_real_payload_size():
    for raw in ({}, {"page": 1, "total_pages": 0, "results": [{"id": 1}]},
                {"page": 1, "total_pages": 1, "results": [
                    {"id": n, "media_type": "movie", "title": "Title", "overview": "x" * 200000}
                    for n in range(1, 6)]}):
        with pytest.raises(ia.IntegrationError) as error:
            run("catalog.search", "?q=a", raw=raw)
        assert error.value.status == 503 and not error.value.retryable


def test_busy_transport_route_and_recovery(monkeypatch):
    held = [tc._slots.acquire(blocking=False) for _ in range(tc.MAX_CONCURRENT)]
    try:
        assert all(held)
        with pytest.raises(ia.IntegrationError) as error:
            service.execute("catalog.search", "/integration/v1/test?q=a", USER, read_setting=reader)
        assert error.value.status == 503 and error.value.retryable
    finally:
        for acquired in held:
            if acquired:
                tc._slots.release()
    monkeypatch.setattr(tc, "_fetch", lambda *a: EMPTY.copy())
    data = service.execute("catalog.search", "/integration/v1/test?q=a", USER, read_setting=reader)
    assert data["items"] == []
