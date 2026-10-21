"""Strict raw metadata resolver with an injected fake TMDB. No network, no database."""
import copy
import sys

import pytest

import intent_metadata as im


def ep(eid, s, n, name=None, air="2020-01-01", rt=45, **extra):
    d = {"id": eid, "season_number": s, "episode_number": n, "name": name or f"E{n}",
         "air_date": air, "runtime": rt}
    d.update(extra)
    return d


class Fake:
    """Path-keyed fake of the raw TMDB endpoints; records every request."""

    def __init__(self, tid=100, seasons=None, imdb="tt1234567"):
        seasons = seasons if seasons is not None else {
            1: [ep(11, 1, 1), ep(12, 1, 2), ep(13, 1, 3, air="2099-01-01")],
            2: [ep(21, 2, 1, air=None), ep(22, 2, 2, air="2030-06-01")],
            0: [ep(1, 0, 1, name="Special")],
        }
        self.tid, self.calls = tid, []
        self.data = {
            f"/tv/{tid}": {"id": tid, "name": " Show ", "first_air_date": "2019-05-05",
                           "seasons": [{"season_number": s, "episode_count": len(v), "id": 9000 + s}
                                       for s, v in seasons.items()]},
            f"/tv/{tid}/external_ids": {"id": tid, "imdb_id": imdb},
        }
        for s, v in seasons.items():
            self.data[f"/tv/{tid}/season/{s}"] = {"id": 7000 + s, "season_number": s, "episodes": v}

    def __call__(self, path):
        self.calls.append(path)
        return copy.deepcopy(self.data.get(path))


def TV(kind="all_seasons", tid=100, **scope):
    return {"media": {"type": "tv", "tmdb_id": tid}, "scope": dict(kind=kind, **scope)}


def run(payload, fake=None, today="2024-01-01"):
    return im.resolve(payload, today=today, fetch=fake or Fake())


def code(payload, fake, today="2024-01-01"):
    with pytest.raises(im.MetadataError) as e:
        im.resolve(payload, today=today, fetch=fake)
    return e.value.code, e.value.status


def test_all_seasons_skips_specials_and_shapes_output():
    out = run(TV())
    assert out["title"] == "Show" and out["imdb_id"] == "tt1234567" and out["year"] == 2019
    assert out["media"] == {"type": "tv", "tmdb_id": 100} and out["scope"] == {"kind": "all_seasons"}
    assert out["cutoff"] == "2024-01-01"
    assert [(e["season"], e["episode"]) for e in out["episodes"]] == [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2)]
    first = out["episodes"][0]
    assert set(first) == {"tmdb_episode_id", "season", "episode", "name", "air_date",
                          "runtime_min", "air_state"}
    assert first["tmdb_episode_id"] == 11 and first["air_state"] == "aired"
    assert {e["air_state"] for e in out["episodes"][2:]} == {"unaired", "unknown"}


def test_specials_only_when_explicit():
    f = Fake()
    out = run(TV("selected_seasons", seasons=[0]), f)
    assert [(e["season"], e["episode"]) for e in out["episodes"]] == [(0, 1)]
    out = run(TV("episodes", episodes=[{"season": 0, "episode": 1}]))
    assert out["episodes"][0]["name"] == "Special"
    f2 = Fake()
    run(TV(), f2)
    assert "/tv/100/season/0" not in f2.calls


def test_episodes_scope_fetches_only_named_seasons_and_pairs():
    f = Fake()
    out = run(TV("episodes", episodes=[{"season": 1, "episode": 2}, {"season": 1, "episode": 1}]), f)
    assert [e["episode"] for e in out["episodes"]] == [1, 2]
    assert "/tv/100/season/2" not in f.calls and "/tv/100/season/1" in f.calls


def test_selected_seasons_returns_named_seasons_only():
    out = run(TV("selected_seasons", seasons=[2]))
    assert {e["season"] for e in out["episodes"]} == {2}


def test_future_cutoff_same_day_included_unknown_kept():
    out = run(TV("future"), today="2030-06-01")
    got = {(e["season"], e["episode"]): e["air_state"] for e in out["episodes"]}
    assert got == {(1, 3): "unaired", (2, 1): "unknown", (2, 2): "aired"}  # same day counts, kept
    out = run(TV("future"), today="2030-06-02")
    assert {(e["season"], e["episode"]) for e in out["episodes"]} == {(1, 3), (2, 1)}
    out = run(TV("future"), today="2100-01-01")
    assert [(e["season"], e["episode"]) for e in out["episodes"]] == [(2, 1)]


def test_raw_34549_season_8_never_uses_87012():
    seasons = {8: [ep(801, 8, 1)]}
    f = Fake(tid=34549, seasons=seasons)
    out = run(TV("selected_seasons", tid=34549, seasons=[8]), f)
    assert out["episodes"][0]["tmdb_episode_id"] == 801
    assert "/tv/34549/season/8" in f.calls
    assert not any("87012" in c or "find" in c for c in f.calls)


def test_movie_returns_no_episodes_and_validates_identity():
    f = {"/movie/5": {"id": 5, "title": "Film", "release_date": "2001-02-03"},
         "/movie/5/external_ids": {"id": 5, "imdb_id": "tt0000123"}}
    pay = {"media": {"type": "movie", "tmdb_id": 5}, "scope": {"kind": "movie"}}
    out = im.resolve(pay, today="2024-01-01", fetch=f.get)
    assert out["episodes"] == [] and out["year"] == 2001 and out["imdb_id"] == "tt0000123"
    f["/movie/5"]["id"] = 6
    assert code(pay, f.get) == ("metadata_unavailable", 503)
    f["/movie/5"]["id"] = 5
    f["/movie/5/external_ids"]["id"] = 6
    assert code(pay, f.get) == ("metadata_unavailable", 503)
    f["/movie/5/external_ids"]["id"] = 5
    f["/movie/5"]["release_date"] = ""
    assert im.resolve(pay, fetch=f.get)["year"] is None


def test_caller_input_errors_are_b1_400():
    for bad in (None, {}, {"media": {"type": "movie", "tmdb_id": 1}, "scope": {"kind": "all_seasons"}},
                TV(tid=True), TV("selected_seasons")):
        # other test modules reload request_intents, so match by name rather than class identity
        with pytest.raises(Exception) as e:
            im.resolve(bad, fetch=Fake())
        assert type(e.value).__name__ == "IntentError" and e.value.status == 400


def test_valid_complete_response_missing_scope_is_404():
    f = Fake()
    assert code(TV("selected_seasons", seasons=[9]), f) == ("scope_not_found", 404)
    assert code(TV("episodes", episodes=[{"season": 1, "episode": 99}]), f) == ("scope_not_found", 404)
    assert code(TV("episodes", episodes=[{"season": 7, "episode": 1}]), f) == ("scope_not_found", 404)
    only_specials = Fake(seasons={0: [ep(1, 0, 1)]})
    assert run(TV(), only_specials)["episodes"] == []


def test_empty_season_is_valid():
    f = Fake(seasons={1: []})
    assert run(TV("selected_seasons", seasons=[1]), f)["episodes"] == []
    assert code(TV("episodes", episodes=[{"season": 1, "episode": 1}]), f) == ("scope_not_found", 404)


@pytest.mark.parametrize("path", ["/tv/100", "/tv/100/external_ids", "/tv/100/season/1"])
def test_outage_none_empty_and_exceptions_are_503(path):
    p = TV("selected_seasons", seasons=[1])
    for bad in (None, {}, [], "x"):
        f = Fake()
        f.data[path] = bad
        assert code(p, f) == ("metadata_unavailable", 503)

    def boom(_):
        raise RuntimeError("down")
    with pytest.raises(im.MetadataError) as e:
        im.resolve(p, fetch=boom)
    assert (e.value.code, e.value.status) == ("metadata_unavailable", 503)
    assert code(p, lambda _: None) == ("metadata_unavailable", 503)


def mutate(fn):
    f = Fake()
    fn(f.data)
    return f


S1 = "/tv/100/season/1"
BAD = {
    "show id mismatch": lambda d: d["/tv/100"].update(id=101),
    "show id bool": lambda d: d["/tv/100"].update(id=True),
    "show id str": lambda d: d["/tv/100"].update(id="100"),
    "no name": lambda d: d["/tv/100"].pop("name"),
    "blank name": lambda d: d["/tv/100"].update(name="  "),
    "long name": lambda d: d["/tv/100"].update(name="x" * 301),
    "bad year": lambda d: d["/tv/100"].update(first_air_date="2019-13-45"),
    "seasons missing": lambda d: d["/tv/100"].pop("seasons"),
    "seasons dup": lambda d: d["/tv/100"]["seasons"].append({"season_number": 1, "episode_count": 3}),
    "season bool": lambda d: d["/tv/100"]["seasons"].append({"season_number": True}),
    "ext id mismatch": lambda d: d["/tv/100/external_ids"].update(id=101),
    "ext imdb short": lambda d: d["/tv/100/external_ids"].update(imdb_id="tt12345"),
    "ext imdb newline": lambda d: d["/tv/100/external_ids"].update(imdb_id="tt1234567\n"),
    "ext imdb none": lambda d: d["/tv/100/external_ids"].update(imdb_id=None),
    "ext imdb unicode": lambda d: d["/tv/100/external_ids"].update(imdb_id="tt١٢٣٤٥٦٧"),
    "season number mismatch": lambda d: d[S1].update(season_number=2),
    "season episodes missing": lambda d: d[S1].pop("episodes"),
    "season episodes not list": lambda d: d[S1].update(episodes={}),
    "episode_count mismatch": lambda d: d["/tv/100"]["seasons"][0].update(episode_count=9),
    "ep dup pair": lambda d: d[S1]["episodes"].append(ep(99, 1, 1)),
    "ep dup id": lambda d: d[S1]["episodes"].append(ep(11, 1, 9)),
    "ep id bool": lambda d: d[S1]["episodes"][0].update(id=True),
    "ep id zero": lambda d: d[S1]["episodes"][0].update(id=0),
    "ep id missing": lambda d: d[S1]["episodes"][0].pop("id"),
    "ep season wrong": lambda d: d[S1]["episodes"][0].update(season_number=2),
    "ep season float": lambda d: d[S1]["episodes"][0].update(season_number=1.0),
    "ep number bool": lambda d: d[S1]["episodes"][0].update(episode_number=True),
    "ep number zero": lambda d: d[S1]["episodes"][0].update(episode_number=0),
    "ep number str": lambda d: d[S1]["episodes"][0].update(episode_number="1"),
    "ep name none": lambda d: d[S1]["episodes"][0].update(name=None),
    "ep name blank": lambda d: d[S1]["episodes"][0].update(name=""),
    "ep date invalid": lambda d: d[S1]["episodes"][0].update(air_date="2020-02-30"),
    "ep date empty": lambda d: d[S1]["episodes"][0].update(air_date=""),
    "ep date format": lambda d: d[S1]["episodes"][0].update(air_date="01/02/2020"),
    "ep date int": lambda d: d[S1]["episodes"][0].update(air_date=20200101),
    "ep runtime neg": lambda d: d[S1]["episodes"][0].update(runtime=-1),
    "ep runtime float": lambda d: d[S1]["episodes"][0].update(runtime=45.5),
    "ep runtime nan": lambda d: d[S1]["episodes"][0].update(runtime=float("nan")),
    "ep runtime huge": lambda d: d[S1]["episodes"][0].update(runtime=99999),
    "ep runtime bool": lambda d: d[S1]["episodes"][0].update(runtime=True),
    "ep show_id other": lambda d: d[S1]["episodes"][0].update(show_id=101),
    "ep not dict": lambda d: d[S1]["episodes"].append(5),
    "ep date key missing": lambda d: d[S1]["episodes"][0].pop("air_date"),
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_malformed_upstream_is_503_without_partial_success(name):
    f = mutate(BAD[name])
    assert code(TV("selected_seasons", seasons=[1]), f) == ("metadata_unavailable", 503), name


def test_missing_runtime_key_and_null_runtime_are_none():
    f = Fake()
    f.data[S1]["episodes"][0].pop("runtime")
    f.data[S1]["episodes"][1]["runtime"] = None
    out = run(TV("selected_seasons", seasons=[1]), f)
    assert out["episodes"][0]["runtime_min"] is None and out["episodes"][1]["runtime_min"] is None


def test_season_response_id_is_not_compared_to_show_id():
    f = Fake()
    f.data[S1]["id"] = 100  # equals the show id: still just a season id
    assert run(TV("selected_seasons", seasons=[1]), f)["episodes"]


def test_unrelated_malformed_episode_in_named_season_fails_closed():
    f = mutate(lambda d: d[S1]["episodes"][2].update(name=None))
    assert code(TV("episodes", episodes=[{"season": 1, "episode": 1}]), f) == ("metadata_unavailable", 503)


def test_bounds_over_limit_is_503_not_partial():
    many = {s: [ep(s * 10, s, 1)] for s in range(1, 102)}
    f = Fake(seasons=many)
    assert code(TV(), f) == ("metadata_unavailable", 503)
    big = {1: [ep(i, 1, i) for i in range(1, 2501)], 2: [ep(5000 + i, 2, i) for i in range(1, 2502)]}
    f = Fake(seasons=big)
    assert code(TV(), f) == ("metadata_unavailable", 503)
    ok = {1: [ep(i, 1, i) for i in range(1, 2501)], 2: [ep(5000 + i, 2, i) for i in range(1, 2501)]}
    assert len(run(TV(), Fake(seasons=ok))["episodes"]) == 5000


def test_today_argument_validation_and_default():
    out = im.resolve(TV(), fetch=Fake())
    assert len(out["cutoff"]) == 10
    from datetime import date
    assert im.resolve(TV(), today=date(2024, 2, 3), fetch=Fake())["cutoff"] == "2024-02-03"
    with pytest.raises(im.MetadataError):
        im.resolve(TV(), today="2024-13-01", fetch=Fake())
    with pytest.raises(TypeError):
        im.resolve(TV(), today=5, fetch=Fake())


def test_module_is_pure_no_database_or_tmdb_import():
    src = open(im.__file__).read()
    assert "import db" not in src and "find_by_imdb" not in src and "get_show_info" not in src
    assert "_season_location" not in src and "get_season_episodes" not in src


def test_duplicate_episode_id_across_seasons_rejected():
    f = Fake()
    f.data["/tv/100/season/2"]["episodes"][0]["id"] = 11
    assert code(TV(), f) == ("metadata_unavailable", 503)


def test_future_budget_counts_catalog_before_filtering():
    many = {1: [ep(i, 1, i) for i in range(1, 5002)]}
    assert code(TV("future"), Fake(seasons=many)) == ("metadata_unavailable", 503)


@pytest.mark.parametrize("kind", ["all_seasons", "future"])
def test_upcoming_show_with_no_regular_seasons_is_valid_empty(kind):
    result = run(TV(kind), Fake(seasons={}))
    assert result["episodes"] == [] and result["scope"] == {"kind": kind}


@pytest.mark.parametrize("value", ["bad\nname", "bad\x00name", "bad\ud800name"])
def test_invalid_title_characters_rejected(value):
    f = Fake()
    f.data["/tv/100"]["name"] = value
    assert code(TV(), f) == ("metadata_unavailable", 503)


def test_default_fetch_uses_tmdb_get_lazily(monkeypatch):
    import types
    fake_tmdb = types.SimpleNamespace(_get=lambda path: {"id": 5, "title": "F", "imdb_id": "tt0000123"}
                                      if path.endswith("external_ids") else {"id": 5, "title": "F"})
    monkeypatch.setitem(sys.modules, "tmdb", fake_tmdb)
    out = im.resolve({"media": {"type": "movie", "tmdb_id": 5}, "scope": {"kind": "movie"}})
    assert out["imdb_id"] == "tt0000123"
