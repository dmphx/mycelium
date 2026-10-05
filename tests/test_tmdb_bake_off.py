import tmdb


def test_bake_off_combines_runs_in_uk_order(monkeypatch):
    bbc = {"number_of_seasons": 7, "seasons": [{"season_number": 1}], "name": "Bake Off"}
    c4 = {"number_of_seasons": 10, "seasons": [{"season_number": 0}, {"season_number": 10}],
          "last_episode_to_air": {"season_number": 10, "episode_number": 2},
          "next_episode_to_air": {"season_number": 10, "episode_number": 3}}
    monkeypatch.setattr(tmdb, "_get", lambda path: {"/tv/34549": bbc, "/tv/87012": c4}[path])
    result = tmdb.get_show_info(34549)
    assert result["number_of_seasons"] == 17
    assert [s["season_number"] for s in result["seasons"]] == [1, 17]
    assert result["next_episode_to_air"]["season_number"] == 17
    assert c4["next_episode_to_air"]["season_number"] == 10


def test_bake_off_episode_consumers_share_mapping(monkeypatch):
    calls = []
    def get(path, **kwargs):
        calls.append(path)
        if path.startswith("/find/"):
            return {"tv_results": [{"id": 34549}]}
        if path.endswith("/episode/2"):
            return {"name": "Biscuit Week", "runtime": 68, "still_path": "/b.jpg"}
        return {"episodes": [{"season_number": 10, "episode_number": 2}]}
    monkeypatch.setattr(tmdb, "_get", get)
    assert tmdb.get_season_episodes(34549, 17)[0]["season_number"] == 17
    assert tmdb.get_episode_details(34549, 17, 2)["title"] == "Biscuit Week"
    assert tmdb.get_episode_still(34549, 17, 2) == "/b.jpg"
    assert tmdb.get_episode_runtime_sec("tt1877368", 17, 2) == 4080
    assert calls.count("/tv/87012/season/10/episode/2") == 3


def test_original_seasons_and_other_shows_keep_identity(monkeypatch):
    calls = []
    monkeypatch.setattr(tmdb, "_get", lambda path: calls.append(path) or {"episodes": []})
    tmdb.get_season_episodes(34549, 7)
    tmdb.get_season_episodes(123, 17)
    assert calls == ["/tv/34549/season/7", "/tv/123/season/17"]


def test_continuation_outage_does_not_invent_seasons(monkeypatch):
    original = {"number_of_seasons": 7}
    monkeypatch.setattr(tmdb, "_get", lambda path: original if path == "/tv/34549" else None)
    assert tmdb.get_show_info(34549) == original
