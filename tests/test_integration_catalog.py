"""Pure catalog builders against the frozen data_contract. Synthetic raw TMDB data."""
import copy
import datetime
import json
import os

import pytest

import integration_catalog as ic

TODAY = datetime.date(2026, 10, 21)
U = ic.CatalogUnavailable


def raw_movie(**kw):
    d = {"id": 603, "title": "The Matrix", "overview": "A hacker.\r\nWakes up.\tReally.",
         "poster_path": "/p.jpg", "backdrop_path": "/b.jpg", "release_date": "1999-03-30",
         "vote_average": 8.2}
    d.update(kw)
    return d


def raw_tv(**kw):
    d = {"id": 1396, "name": "Show", "overview": "", "first_air_date": "2008-01-20",
         "vote_average": 8}
    d.update(kw)
    return d


def test_title_movie_and_tv():
    t = ic.title(raw_movie(), "movie")
    assert t == {"tmdb_id": 603, "media_type": "movie", "title": "The Matrix",
                 "overview": "A hacker.\nWakes up.\tReally.",
                 "poster_url": "https://image.tmdb.org/t/p/w500/p.jpg",
                 "backdrop_url": "https://image.tmdb.org/t/p/w1280/b.jpg",
                 "release_date": "1999-03-30", "vote_average": 8.2}
    t = ic.title(raw_tv(), "tv")
    assert t["first_air_date"] == "2008-01-20" and "release_date" not in t
    assert "poster_url" not in t


@pytest.mark.parametrize("bad", [0, -1, 2147483648, True, 1.0, "5", None])
def test_bad_ids(bad):
    with pytest.raises(U):
        ic.title(raw_movie(id=bad), "movie")


@pytest.mark.parametrize("field,val", [
    ("title", ""), ("title", "  "), ("title", None), ("title", 5), ("title", "a\nb"),
    ("title", "a\x00b"), ("title", "a\x85b"), ("title", "a\ud800b"),
    ("overview", "a\x00b"), ("overview", "a\x1fb"), ("overview", "a\x9fb"),
    ("overview", "a\ud800b"), ("overview", 5),
    ("vote_average", True), ("vote_average", float("nan")), ("vote_average", float("inf")),
    ("vote_average", 10.1), ("vote_average", -0.1), ("vote_average", "8"),
    ("release_date", "1999-3-30"), ("release_date", "1999-02-30"), ("release_date", 5)])
def test_malformed_required_fields_503(field, val):
    with pytest.raises(U):
        ic.title(raw_movie(**{field: val}), "movie")


def test_optional_bad_images_omitted_and_empty_optionals():
    t = ic.title(raw_movie(poster_path="http://x/p.jpg", backdrop_path="/a/../b.jpg",
                           release_date="", vote_average=None, overview=None), "movie")
    assert "poster_url" not in t and "backdrop_url" not in t
    assert "release_date" not in t and "vote_average" not in t and t["overview"] == ""
    assert ic.title(raw_movie(vote_average=0), "movie")["vote_average"] == 0


def test_legitimate_unicode_kept():
    s = "Fam \U0001F468‍\U0001F469‍\U0001F467 ‮é ​"
    t = ic.title(raw_movie(title=s, overview="a\n\nb c"), "movie")
    assert t["title"] == s and t["overview"] == "a\n\nb c"


def test_list_page_echo_and_bounds():
    raw = {"page": 3, "total_pages": 90000, "results": [raw_movie()]}
    out = ic.catalog_list(raw, "movie", 3)
    assert out["page"] == 3 and out["total_pages"] == 90000  # unclamped
    for page in (0, 501, True, "1", 1.0):
        with pytest.raises(U):
            ic.catalog_list(raw, "movie", page)
    with pytest.raises(U):
        ic.catalog_list(raw, "movie", 2)  # raw page mismatch
    for tp in (-1, True, 1.5, float("inf"), "3", ic.MAX_TOTAL_PAGES + 1):
        with pytest.raises(U):
            ic.catalog_list({"page": 1, "total_pages": tp, "results": []}, "movie", 1)
    assert ic.catalog_list({"page": 1, "total_pages": 0, "results": []}, "tv", 1)["items"] == []


def test_list_malformed_item_503_and_size():
    with pytest.raises(U):
        ic.catalog_list({"page": 1, "total_pages": 1, "results": [raw_movie(), {"id": 1}]}, "movie", 1)
    with pytest.raises(U):
        ic.catalog_list({"page": 1, "total_pages": 1, "results": [raw_movie()] * 101}, "movie", 1)


def test_multi_search_filters_only_person():
    raw = {"page": 1, "total_pages": 1, "results": [
        dict(raw_movie(), media_type="movie"), dict(raw_tv(), media_type="tv"),
        {"id": 9, "media_type": "person", "name": "A\x00"}]}
    out = ic.search(raw, 1)
    assert [i["media_type"] for i in out["items"]] == ["movie", "tv"]
    raw["results"].append({"id": 10, "media_type": "collection", "name": "C"})
    with pytest.raises(U):
        ic.search(raw, 1)
    with pytest.raises(U):  # malformed movie is not silently dropped
        ic.search({"page": 1, "total_pages": 1, "results": [{"media_type": "movie", "id": 1}]}, 1)
    assert ic.trending({"page": 1, "total_pages": 1, "results": [raw_tv()]}, 1, "tv")["items"][0]["tmdb_id"] == 1396


def detail_raw(**kw):
    d = raw_movie(
        genres=[{"id": 28, "name": "Action"}], runtime=136, status="Released",
        credits={"id": 603, "cast": [{"id": 6384, "name": "Keanu", "character": "Neo",
                                      "profile_path": "/k.jpg"}],
                 "crew": [{"id": 1, "name": "Lana", "job": "Director"}]},
        recommendations={"results": [raw_movie(id=604, title="Reloaded")]},
        videos={"results": [{"site": "YouTube", "type": "Trailer", "key": "abc_-1", "name": "T"},
                            {"site": "Vimeo", "type": "Trailer", "key": "x", "name": "T"},
                            {"site": "YouTube", "type": "Trailer", "key": "bad key", "name": "T"}]},
        **{"watch/providers": {"id": 603, "results": {"NL": {
            "link": "https://www.themoviedb.org/movie/603/watch?locale=NL",
            "flatrate": [{"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.png"}],
            "rent": [{"provider_id": 8, "provider_name": "Netflix"}]}}}})
    d.update(kw)
    return d


def test_movie_detail():
    d = ic.detail(detail_raw(), "movie", 603, "NL")
    assert d["genres"] == [{"genre_id": 28, "name": "Action"}]
    assert d["runtime_minutes"] == 136 and d["status"] == "Released"
    assert "seasons" not in d
    assert d["credits"]["cast"][0] == {"person_id": 6384, "name": "Keanu", "character": "Neo",
                                       "profile_url": "https://image.tmdb.org/t/p/w185/k.jpg"}
    assert d["credits"]["crew"][0]["job"] == "Director"
    assert d["recommendations"] == {"source": "tmdb", "personalized": False,
                                    "items": [ic.title(raw_movie(id=604, title="Reloaded"), "movie")]}
    assert d["trailers"] == [{"site": "YouTube", "key": "abc_-1", "name": "T"}]
    wp = d["watch_providers"]
    assert wp["region"] == "NL" and wp["info_url"].startswith("https://www.themoviedb.org/")
    assert [(p["provider_id"], p["monetization"]) for p in wp["items"]] == [(8, "flatrate"), (8, "rent")]
    assert wp["items"][0]["logo_url"] == "https://image.tmdb.org/t/p/w92/n.png"
    for banned in ("imdb", "library", "entitlement", "available"):
        assert banned not in json.dumps(d).lower()


def test_detail_no_region_entry_no_fallback_and_bad_info_url():
    d = ic.detail(detail_raw(), "movie", 603, "US")
    assert d["watch_providers"] == {"region": "US", "items": []}
    raw = detail_raw()
    raw["watch/providers"]["results"]["NL"]["link"] = "https://evil.example/x"
    assert "info_url" not in ic.detail(raw, "movie", 603, "NL")["watch_providers"]
    for region in ("nl", "NLD", "N", "", None):
        with pytest.raises(U):
            ic.detail(detail_raw(), "movie", 603, region)


def test_detail_parent_and_full_arrays():
    with pytest.raises(U):
        ic.detail(detail_raw(), "movie", 604, "NL")
    raw = detail_raw()
    raw["credits"]["id"] = 5
    with pytest.raises(U):
        ic.detail(raw, "movie", 603, "NL")
    raw = detail_raw()
    raw["credits"]["cast"] = [{"id": i + 1, "name": "n", "character": ""} for i in range(2500)]
    raw["recommendations"]["results"] = [raw_movie(id=i + 1) for i in range(150)]
    d = ic.detail(raw, "movie", 603, "NL")
    assert len(d["credits"]["cast"]) == 2500 and len(d["recommendations"]["items"]) == 150
    raw["credits"]["cast"] = raw["credits"]["cast"] + raw["credits"]["cast"][:501]
    with pytest.raises(U):
        ic.detail(raw, "movie", 603, "NL")
    raw = detail_raw()
    raw["credits"]["cast"][0]["id"] = True
    with pytest.raises(U):
        ic.detail(raw, "movie", 603, "NL")


def tv_detail_raw():
    return raw_tv(genres=[], episode_run_time=[45], status="Ended",
                  credits={"cast": [], "crew": []}, recommendations={"results": []},
                  videos={"results": []}, **{"watch/providers": {"results": {}}},
                  seasons=[{"season_number": 0, "name": "Specials", "episode_count": 2,
                            "air_date": None},
                           {"season_number": 1, "name": "S1", "episode_count": 7,
                            "air_date": "2008-01-20", "poster_path": "/s.jpg"}])


def test_tv_detail_seasons_raw_numbering():
    d = ic.detail(tv_detail_raw(), "tv", 1396, "NL")
    assert d["runtime_minutes"] == 45
    assert [s["season_number"] for s in d["seasons"]] == [0, 1]
    assert d["seasons"][0]["air_date"] is None
    assert d["seasons"][1]["poster_url"].endswith("/w500/s.jpg")
    assert d["credits"] == {"cast": [], "crew": []} and d["trailers"] == []
    raw = tv_detail_raw()
    raw["seasons"].append(dict(raw["seasons"][0]))
    with pytest.raises(U):
        ic.detail(raw, "tv", 1396, "NL")


def show_with(count, num=1):
    return {"id": 1396, "seasons": [{"season_number": num, "episode_count": count}]}


def ep(n, eid=None, **kw):
    d = {"id": eid or 100 + n, "season_number": 1, "episode_number": n, "name": f"E{n}",
         "overview": "x\r\ny", "air_date": "2008-01-20", "runtime": 58, "show_id": 1396,
         "still_path": "/e.jpg"}
    d.update(kw)
    return d


def test_season_complete_and_air_states():
    raw = {"season_number": 1, "episodes": [ep(2, air_date="2099-01-01"), ep(1),
                                            ep(3, air_date=None, runtime=0)]}
    s = ic.season(raw, show_with(3), 1396, 1, TODAY)
    assert s["complete"] is True and s["numbering"] == "tmdb"
    assert s["media"] == {"type": "tv", "tmdb_id": 1396}
    assert [e["episode"] for e in s["episodes"]] == [1, 2, 3]
    assert [e["air_state"] for e in s["episodes"]] == ["aired", "unaired", "unknown"]
    assert s["episodes"][0]["overview"] == "x\ny"
    assert s["episodes"][2]["runtime_minutes"] is None and s["episodes"][2]["air_date"] is None
    assert s["episodes"][0]["still_url"].endswith("/w300/e.jpg")
    # same day counts as aired
    assert ic.season({"season_number": 1, "episodes": [ep(1, air_date="2026-10-21")]},
                     show_with(1), 1396, 1, TODAY)["episodes"][0]["air_state"] == "aired"


@pytest.mark.parametrize("mutate", [
    lambda r, s: r["episodes"].pop(),                      # fewer than episode_count
    lambda r, s: r["episodes"].append(ep(3)),              # more
    lambda r, s: r["episodes"][1].update(id=101),          # duplicate id
    lambda r, s: r["episodes"][1].update(episode_number=1),  # duplicate pair
    lambda r, s: r["episodes"][1].update(season_number=2),
    lambda r, s: r["episodes"][1].update(show_id=5),
    lambda r, s: r["episodes"][1].update(episode_number=0),
    lambda r, s: r["episodes"][1].update(id=True),
    lambda r, s: r.update(season_number=2),
    lambda r, s: r.update(episodes=None),
    lambda r, s: s.update(id=7),
])
def test_season_invalid_never_complete(mutate):
    raw = {"season_number": 1, "episodes": [ep(1), ep(2)]}
    show = show_with(2)
    mutate(raw, show)
    with pytest.raises(U):
        ic.season(raw, show, 1396, 1, TODAY)


def test_season_undeclared_is_not_found():
    show = show_with(2)
    show["seasons"].clear()
    with pytest.raises(ic.CatalogNotFound):
        ic.season({"season_number": 1, "episodes": []}, show, 1396, 1, TODAY)
    assert issubclass(ic.CatalogNotFound, U)


def test_season_zero_and_empty_declared():
    raw = {"season_number": 0, "episodes": [ep(1, season_number=0)]}
    assert ic.season(raw, show_with(1, 0), 1396, 0, TODAY)["season"] == 0
    with pytest.raises(ic.CatalogNotFound):
        ic.season(raw, show_with(1), 1396, 0, TODAY)


def test_genres_providers_tabs():
    assert ic.genres({"genres": [{"id": 1, "name": "A"}]}, "tv") == {"items": [{"genre_id": 1, "name": "A"}]}
    with pytest.raises(U):
        ic.genres({"genres": [{"id": 1, "name": "A"}]}, "all")
    with pytest.raises(U):
        ic.genres({"genres": [{"id": 0, "name": "A"}]}, "movie")
    raw = {"results": [{"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.png"}]}
    assert ic.providers(raw, "movie", "NL") == {"region": "NL", "items": [
        {"provider_id": 8, "name": "Netflix", "logo_url": "https://image.tmdb.org/t/p/w92/n.png"}]}
    for region in ("nl", None, "NLD"):
        with pytest.raises(U):
            ic.providers(raw, "movie", region)
    with pytest.raises(U):
        ic.providers({"results": raw["results"] * 2}, "movie", "NL")
    tabs = [{"media_type": "movie", "genre_id": 28, "name": "Action", "year_from": None, "year_to": 2000}]
    assert ic.genre_tabs(tabs) == {"items": tabs}
    with pytest.raises(U):
        ic.genre_tabs([dict(tabs[0], media_type="all")])


def raw_person(cast, crew, **kw):
    d = {"id": 6384, "name": "Keanu", "biography": "Line1\r\nLine2", "birthday": "1964-09-02",
         "place_of_birth": "Beirut", "profile_path": "/k.jpg",
         "combined_credits": {"id": 6384, "cast": cast, "crew": crew}}
    d.update(kw)
    return d


def test_person_dedup_character_wins_and_cap():
    m = dict(raw_movie(), media_type="movie")
    cast = [dict(m, character="Neo"), dict(raw_tv(), media_type="tv", character="")]
    crew = [dict(m, job="Producer"), dict(raw_movie(id=5, title="Z"), media_type="movie")]
    p = ic.person(raw_person(cast, crew), 6384)
    assert [(f["media_type"], f["tmdb_id"]) for f in p["filmography"]] == [
        ("movie", 5), ("movie", 603), ("tv", 1396)]
    assert p["filmography"][1]["character"] == "Neo" and "character" not in p["filmography"][0]
    assert p["filmography_complete"] is True and p["biography"] == "Line1\nLine2"
    assert p["birth_date"] == "1964-09-02" and p["profile_url"].endswith("/w185/k.jpg")
    # crew-only entry listed first in cast order? same key in crew then cast: cast character wins
    p = ic.person(raw_person([dict(m, character="Neo")], [dict(m, job="X")]), 6384)
    assert len(p["filmography"]) == 1


def test_person_cap_after_dedup_incomplete():
    many = [dict(raw_movie(id=i + 1), media_type="movie") for i in range(150)]
    p = ic.person(raw_person(many, many[:40]), 6384)  # 150 unique after dedup
    assert len(p["filmography"]) == 100 and p["filmography_complete"] is False
    assert [f["tmdb_id"] for f in p["filmography"]] == list(range(1, 101))  # tie: (type,id)
    exact = ic.person(raw_person(many[:100], many[:100]), 6384)
    assert len(exact["filmography"]) == 100 and exact["filmography_complete"] is True


def test_person_popularity_order_independent_of_input_order():
    rows = [dict(raw_movie(id=i), media_type="movie", popularity=float(i % 7)) for i in range(1, 131)]
    a = ic.person(raw_person(rows, []), 6384)
    b = ic.person(raw_person(list(reversed(rows)), []), 6384)
    assert a["filmography"] == b["filmography"] and a["filmography_complete"] is False
    pops = [rows[f["tmdb_id"] - 1]["popularity"] for f in a["filmography"]]
    assert pops == sorted(pops, reverse=True)
    for bad in (True, "5", float("inf"), float("nan")):
        with pytest.raises(U):
            ic.person(raw_person([dict(rows[0], popularity=bad)], []), 6384)
    assert ic.person(raw_person([{k: v for k, v in rows[0].items() if k != "popularity"}], []),
                     6384)["filmography"]


def test_person_nulls_and_strictness():
    p = ic.person(raw_person([], [], birthday=None, place_of_birth=None, biography=None,
                             profile_path="bad"), 6384)
    assert p["birth_date"] is None and p["place_of_birth"] is None and p["biography"] == ""
    assert "profile_url" not in p and p["filmography"] == []
    with pytest.raises(U):
        ic.person(raw_person([], []), 1)
    with pytest.raises(U):
        ic.person(raw_person([{"id": 1, "media_type": "person", "name": "x"}], []), 6384)
    with pytest.raises(U):
        ic.person(raw_person([], [], birthday="1964-13-01"), 6384)
    bad = raw_person([], [])
    bad["combined_credits"]["id"] = 9
    with pytest.raises(U):
        ic.person(bad, 6384)


def test_serialize_compact_ascii_and_cap():
    body = ic.serialize({"t": "café \U0001F600"})
    assert body == b'{"api_version":"1","data":{"t":"caf\\u00e9 \\ud83d\\ude00"}}'
    assert body.isascii()
    with pytest.raises(U, match="payload_too_large"):
        ic.serialize({"t": "x" * ic.MAX_PAYLOAD_BYTES})
    with pytest.raises(U):
        ic.serialize({"n": float("nan")})
    # boundary: exactly the cap is accepted
    overhead = len(ic.serialize({"t": ""}))
    assert len(ic.serialize({"t": "x" * (ic.MAX_PAYLOAD_BYTES - overhead)})) == ic.MAX_PAYLOAD_BYTES


def test_inputs_not_mutated():
    raw = detail_raw()
    snap = copy.deepcopy(raw)
    ic.detail(raw, "movie", 603, "NL")
    assert raw == snap


def test_contract_field_names_match_frozen_proposal():
    path = os.environ.get("MYCELIUM_INTEGRATION_CONTRACT")
    if not path or not os.path.exists(path):
        pytest.skip("contract file not provided")
    contract = json.load(open(path))["data_contract"]
    d = ic.detail(detail_raw(), "movie", 603, "NL")
    for key in contract["title"]:
        assert key in d or key in ("release_date", "first_air_date")
    for key in ("genres", "runtime_minutes", "status", "credits", "recommendations",
                "trailers", "watch_providers"):
        assert key in contract["catalog_detail"] and key in d
    s = ic.season({"season_number": 1, "episodes": [ep(1)]}, show_with(1), 1396, 1, TODAY)
    assert set(s) == set(contract["season"]) - {"rules"}
    assert set(s["episodes"][0]) <= set(
        x.split(":")[0].rstrip("?") for x in contract["season"]["episodes"].strip("[]{}").split(","))


# --- strictness review (items 1-5, 9) --------------------------------------

@pytest.mark.parametrize("bad", [True, 1.0, "1", None])
def test_raw_page_must_be_strict_int(bad):
    with pytest.raises(U):
        ic.catalog_list({"page": bad, "total_pages": 1, "results": []}, "movie", 1)
    with pytest.raises(U):
        ic.catalog_list({"total_pages": 1, "results": []}, "movie", 1)  # absent


def test_parent_ids_strict_in_detail():
    for bad in (True, 603.0, "603"):
        with pytest.raises(U):
            ic.detail(detail_raw(id=bad), "movie", 603, "NL")
    for key in ("credits", "recommendations", "videos", "watch/providers"):
        for bad in (604, True, 603.0):
            raw = detail_raw()
            raw[key]["id"] = bad
            with pytest.raises(U):
                ic.detail(raw, "movie", 603, "NL")
        raw = detail_raw()
        raw[key]["id"] = 603
        ic.detail(raw, "movie", 603, "NL")  # matching optional id is fine
    with pytest.raises(U):
        ic.detail(detail_raw(), "movie", True, "NL")


def test_season_strict_comparisons():
    base = {"season_number": 1, "episodes": [ep(1)]}
    for bad in (True, 1.0, "1"):
        r = copy.deepcopy(base); r["season_number"] = bad
        with pytest.raises(U):
            ic.season(r, show_with(1), 1396, 1, TODAY)
        r = copy.deepcopy(base); r["episodes"][0]["season_number"] = bad
        with pytest.raises(U):
            ic.season(r, show_with(1), 1396, 1, TODAY)
        r = copy.deepcopy(base); r["episodes"][0]["show_id"] = bad
        with pytest.raises(U):
            ic.season(r, show_with(1), 1396, 1, TODAY)
        show = show_with(1); show["seasons"][0]["season_number"] = bad
        with pytest.raises(U):
            ic.season(base, show, 1396, 1, TODAY)
        show = show_with(1); show["id"] = bad
        with pytest.raises(U):
            ic.season(base, show, 1396, 1, TODAY)
    for bad in (True, 1.0, -1, 10000, "1"):
        with pytest.raises(U):
            ic.season(base, show_with(1), 1396, bad, TODAY)


def test_season_own_id_is_not_show_id_and_top_show_id_strict():
    base = {"season_number": 1, "id": 999999, "episodes": [ep(1)]}
    assert ic.season(base, show_with(1), 1396, 1, TODAY)["complete"] is True
    for bad in (0, -5, True, 1.5, "9"):
        r = dict(base, id=bad)
        with pytest.raises(U):
            ic.season(r, show_with(1), 1396, 1, TODAY)
    for bad in (5, True, 1396.0):
        with pytest.raises(U):
            ic.season(dict(base, show_id=bad), show_with(1), 1396, 1, TODAY)
    assert ic.season(dict(base, show_id=1396), show_with(1), 1396, 1, TODAY)["complete"]


def test_episode_bounds_and_runtime_types():
    for bad in (True, 1.0, 0, 10000, -1):
        with pytest.raises(U):
            ic.season({"season_number": 1, "episodes": [ep(1, episode_number=bad)]},
                      show_with(1), 1396, 1, TODAY)
    for bad in (True, 58.0, -1, 1441, "58"):
        with pytest.raises(U):
            ic.season({"season_number": 1, "episodes": [ep(1, runtime=bad)]},
                      show_with(1), 1396, 1, TODAY)
    for ok, want in ((0, None), (None, None), (1440, 1440)):
        s = ic.season({"season_number": 1, "episodes": [ep(1, runtime=ok)]}, show_with(1), 1396, 1, TODAY)
        assert s["episodes"][0]["runtime_minutes"] == want
    with pytest.raises(U):
        ic.detail(detail_raw(runtime=True), "movie", 603, "NL")
    with pytest.raises(U):
        ic.detail(detail_raw(runtime=1441), "movie", 603, "NL")


def test_trailing_newline_aliases_rejected():
    assert ic.title(raw_movie(poster_path="/p.jpg\n"), "movie").get("poster_url") is None
    for bad in ("1999-03-30\n",):
        with pytest.raises(U):
            ic.title(raw_movie(release_date=bad), "movie")
    for region in ("NL\n", "NL "):
        with pytest.raises(U):
            ic.detail(detail_raw(), "movie", 603, region)
        with pytest.raises(U):
            ic.providers({"results": []}, "movie", region)
    raw = detail_raw()
    raw["videos"]["results"][0]["key"] = "abc\n"
    assert ic.detail(raw, "movie", 603, "NL")["trailers"] == []
    raw = detail_raw()
    raw["watch/providers"]["results"]["NL"]["link"] = "https://www.themoviedb.org/movie/603\n"
    assert "info_url" not in ic.detail(raw, "movie", 603, "NL")["watch_providers"]


def test_conflicting_explicit_media_type_rejected():
    with pytest.raises(U):
        ic.title(dict(raw_movie(), media_type="tv"), "movie")
    assert ic.title(dict(raw_movie(), media_type="movie"), "movie")["media_type"] == "movie"
    with pytest.raises(U):
        ic.catalog_list({"page": 1, "total_pages": 1, "results": [dict(raw_movie(), media_type="tv")]},
                        "movie", 1)
    with pytest.raises(U):  # multi needs its own type
        ic.search({"page": 1, "total_pages": 1, "results": [raw_movie()]}, 1)


def test_text_length_limits_and_unicode():
    with pytest.raises(U):
        ic.title(raw_movie(title="x" * 301), "movie")
    assert ic.title(raw_movie(title="x" * 300), "movie")["title"] == "x" * 300
    with pytest.raises(U):
        ic.title(raw_movie(overview="x" * (ic.MAX_PROSE + 1)), "movie")
    big = "caf\u00e9\n" * 1000
    assert ic.title(raw_movie(overview=big), "movie")["overview"] == big
    raw = detail_raw()
    raw["credits"]["cast"][0]["character"] = "c" * 1000
    assert ic.detail(raw, "movie", 603, "NL")
    raw["credits"]["cast"][0]["character"] = "c" * 1001
    with pytest.raises(U):
        ic.detail(raw, "movie", 603, "NL")
    raw = detail_raw()
    raw["credits"]["crew"][0]["job"] = "line\nbreak"
    with pytest.raises(U):
        ic.detail(raw, "movie", 603, "NL")


@pytest.mark.parametrize("key,path", [
    ("genres", None), ("credits", None), ("recommendations", None), ("videos", None),
    ("watch/providers", None), ("credits", "cast"), ("credits", "crew"),
    ("recommendations", "results"), ("videos", "results"), ("watch/providers", "results")])
def test_missing_appended_data_is_503_not_empty(key, path):
    raw = detail_raw()
    if path is None:
        del raw[key]
    else:
        del raw[key][path]
    with pytest.raises(U):
        ic.detail(raw, "movie", 603, "NL")
    raw = detail_raw()
    if path is None:
        raw[key] = None
        with pytest.raises(U):
            ic.detail(raw, "movie", 603, "NL")


def test_explicit_empty_blocks_accepted():
    d = ic.detail(tv_detail_raw(), "tv", 1396, "NL")
    assert d["credits"] == {"cast": [], "crew": []}
    assert d["watch_providers"] == {"region": "NL", "items": []}
    raw = tv_detail_raw()
    del raw["credits"]
    with pytest.raises(U):
        ic.detail(raw, "tv", 1396, "NL")


def test_person_requires_credit_arrays():
    for mutate in (lambda r: r.pop("combined_credits"),
                   lambda r: r["combined_credits"].pop("cast"),
                   lambda r: r["combined_credits"].pop("crew"),
                   lambda r: r.update(combined_credits=None),
                   lambda r: r["combined_credits"].update(cast=None)):
        raw = raw_person([], [])
        mutate(raw)
        with pytest.raises(U):
            ic.person(raw, 6384)
    for bad in (True, 6384.0):
        with pytest.raises(U):
            ic.person(raw_person([], [], id=bad), 6384)
        raw = raw_person([], [])
        raw["combined_credits"]["id"] = bad
        with pytest.raises(U):
            ic.person(raw, 6384)


def test_show_seasons_all_members_validated():
    raw = {"season_number": 1, "episodes": [ep(1)]}
    for bad_row in ({"season_number": 2}, {"season_number": 2, "episode_count": True},
                    {"season_number": 2, "episode_count": -1}, {"season_number": True, "episode_count": 1},
                    {"season_number": 1, "episode_count": 1}, "junk", None):
        show = show_with(1)
        show["seasons"].append(bad_row)
        with pytest.raises(U) as e:
            ic.season(raw, show, 1396, 1, TODAY)
        assert not isinstance(e.value, ic.CatalogNotFound)
    show = show_with(1)
    show["seasons"] = None
    with pytest.raises(U):
        ic.season(raw, show, 1396, 1, TODAY)
    # empty valid season is complete
    assert ic.season({"season_number": 1, "episodes": []}, show_with(0), 1396, 1, TODAY) == {
        "media": {"type": "tv", "tmdb_id": 1396}, "season": 1, "numbering": "tmdb",
        "complete": True, "episodes": []}


def test_genre_tab_year_order():
    t = {"media_type": "tv", "genre_id": 1, "name": "A"}
    assert ic.genre_tabs([dict(t, year_from=2000, year_to=2000)])
    assert ic.genre_tabs([dict(t, year_from=None, year_to=1999)])
    with pytest.raises(U):
        ic.genre_tabs([dict(t, year_from=2001, year_to=2000)])
    for bad in (True, 1999.0, 1799, 3001):
        with pytest.raises(U):
            ic.genre_tabs([dict(t, year_from=bad, year_to=None)])


def test_serialize_is_size_check_not_envelope():
    assert "meta" not in json.loads(ic.serialize({"a": 1}))
    assert "NOT the HTTP response" in ic.serialize.__doc__


@pytest.mark.parametrize("change", [
    {"total_pages": 0}, {"page": 2, "total_pages": 1},
    {"total_results": 0}, {"total_results": True},
])
def test_contradictory_paging_cannot_claim_empty_or_complete(change):
    raw = {"page": 1, "total_pages": 1, "results": [raw_movie()]}
    raw.update(change)
    with pytest.raises(U):
        ic.catalog_list(raw, "movie", raw["page"])
    with pytest.raises(U):
        ic.catalog_list({"page": 1, "total_pages": 0, "total_results": 5, "results": []}, "movie", 1)


@pytest.mark.parametrize("value", [False, 0, "", [45, False], [45, 1.0]])
def test_falsey_or_later_bad_tv_runtime_is_not_unknown(value):
    raw = tv_detail_raw()
    raw["episode_run_time"] = value
    with pytest.raises(U):
        ic.detail(raw, "tv", 1396, "NL")


@pytest.mark.parametrize("value", [False, 0, [], {}])
def test_falsey_credit_fields_are_not_silently_empty(value):
    raw = detail_raw()
    raw["credits"]["cast"][0]["character"] = value
    with pytest.raises(U):
        ic.detail(raw, "movie", 603, "NL")


def test_person_duplicate_validation_and_order_are_stable():
    first = dict(raw_movie(), media_type="movie", popularity=2, character="Z")
    second = dict(first, popularity=5, character="A")
    a = ic.person(raw_person([first, second], []), 6384)
    b = ic.person(raw_person([second, first], []), 6384)
    assert a == b and a["filmography"][0]["character"] == "A"
    with pytest.raises(U):
        ic.person(raw_person([first, dict(second, popularity=float("nan"))], []), 6384)
    with pytest.raises(U):
        ic.person(raw_person([first, dict(second, title="Conflicting title")], []), 6384)


def test_huge_injected_numbers_fail_with_catalog_error():
    with pytest.raises(U):
        ic.title(raw_movie(vote_average=10**400), "movie")
    credit = dict(raw_movie(), media_type="movie", popularity=10**400)
    with pytest.raises(U):
        ic.person(raw_person([credit], []), 6384)
