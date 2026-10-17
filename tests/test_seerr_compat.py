"""Seerr-compat blueprint: real sqlite db, fake tmdb, no network."""
import sys

import pytest


class FakeTmdb:
    def __init__(self):
        self.imdb = {603: "tt0133093", 1399: "tt0944947", 99: ""}

    def multi_search(self, q, page=1):
        return [{"tmdb_id": 603, "media_type": "movie", "title": "The Matrix",
                 "release_date": "1999-03-30", "overview": "o", "poster_path": None},
                {"tmdb_id": 1399, "media_type": "tv", "title": "GoT",
                 "release_date": "2011-04-17", "overview": "", "poster_path": None}]

    def details(self, kind, tmdb_id, region="NL"):
        if tmdb_id not in self.imdb:
            return None
        return {"title": "T%d" % tmdb_id, "release_date": "2000-01-01", "overview": "",
                "imdb_id": self.imdb[tmdb_id] or None, "number_of_seasons": 8}

    def tmdb_to_imdb(self, tmdb_id, media_type="movie"):
        return self.imdb.get(tmdb_id) or None


@pytest.fixture()
def env(monkeypatch, tmp_path):
    saved = {m: sys.modules.get(m) for m in ("db", "settings", "config", "seerr_compat")}
    for m in saved:
        sys.modules.pop(m, None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("SEERR_COMPAT_ENABLED", "true")
    monkeypatch.setenv("SEERR_API_KEY", "k" * 24)
    import config
    config.DB_PATH = str(tmp_path / "t.db")
    import db
    db.init()
    admin = db.create_user("root", "scrypt$x$y", role="admin")
    monkeypatch.setattr(config, "SEERR_COMPAT_USER_ID", str(admin))
    import flask
    import seerr_compat
    kicks = []
    app = flask.Flask(__name__)
    app.register_blueprint(seerr_compat.create_blueprint(
        db, FakeTmdb(), lambda *a: kicks.append(a), "9.9"))
    yield app.test_client(), db, admin, kicks, config
    for m, v in saved.items():
        sys.modules.pop(m, None)
        if v is not None:
            sys.modules[m] = v


H = {"X-Api-Key": "k" * 24}


def test_status_public_no_secrets(env):
    c, *_ = env
    r = c.get("/api/v1/status")
    assert r.status_code == 200 and r.json["version"] == "9.9"
    assert "k" * 24 not in r.get_data(as_text=True)


def test_missing_or_wrong_key_rejected(env):
    c, *_ = env
    for h in ({}, {"X-Api-Key": "wrong"}):
        assert c.get("/api/v1/request", headers=h).status_code == 401
        assert c.post("/api/v1/request", headers=h, json={"mediaType": "movie", "mediaId": 603}).status_code == 401
        assert c.get("/api/v1/search?query=x", headers=h).status_code == 401


def test_disabled_and_bad_config(env):
    c, db, admin, kicks, config = env
    config.SEERR_COMPAT_USER_ID = "999"
    assert c.get("/api/v1/request", headers=H).status_code == 503
    assert c.get("/api/v1/status").status_code == 503
    config.SEERR_COMPAT_USER_ID = str(admin)
    db.update_user(admin, role="user")
    assert c.get("/api/v1/request", headers=H).status_code == 503
    db.update_user(admin, role="admin")
    config.SEERR_COMPAT_ENABLED = False
    assert c.get("/api/v1/status").status_code == 404


def test_post_creates_native_request_once_and_is_idempotent(env):
    c, db, admin, kicks, _ = env
    r = c.post("/api/v1/request", headers=H, json={"mediaType": "tv", "mediaId": 1399, "seasons": [1]})
    assert r.status_code == 201 and r.json["status"] == 2
    assert r.json["media"]["tmdbId"] == 1399
    r2 = c.post("/api/v1/request", headers=H, json={"mediaType": "tv", "mediaId": 1399, "seasons": [1, 2]})
    assert r2.status_code == 200 and r2.json["id"] == r.json["id"]
    assert len(kicks) == 1 and kicks[0][1] == "tt0944947"
    rows = db.get_user_requests(user_id=admin)
    assert len(rows) == 1 and rows[0]["status"] == "approved"


def test_post_validation_and_unresolved_imdb(env):
    c, db, admin, kicks, _ = env
    for body in ({}, {"mediaType": "music", "mediaId": 1}, {"mediaType": "movie", "mediaId": "x"},
                 {"mediaType": "movie", "mediaId": True},
                 {"mediaType": "tv", "mediaId": 1399, "seasons": "all"}):
        assert c.post("/api/v1/request", headers=H, json=body).status_code == 400
    r = c.post("/api/v1/request", headers=H, json={"mediaType": "movie", "mediaId": 99})
    assert r.status_code == 400 and "unresolved" in r.json["message"]
    assert kicks == [] and db.get_user_requests(user_id=admin) == []


def test_reads_contract_and_no_processing(env):
    c, db, admin, kicks, _ = env
    db.create_user_request(admin, "", 77, "movie", "Imported", status="completed")
    db.create_user_request(admin, "tt0133093", 603, "movie", "The Matrix", status="approved")
    r = c.get("/api/v1/request?take=10&skip=0&sort=added&filter=all", headers=H)
    assert r.status_code == 200 and r.json["pageInfo"]["results"] == 2
    first = r.json["results"][0]
    assert {"id", "status", "media"} <= set(first) and first["media"]["mediaType"] == "movie"
    assert r.json["results"][1]["status"] == 5 and r.json["results"][1]["media"]["tmdbId"] == 77
    s = c.get("/api/v1/search?query=matrix", headers=H).json["results"]
    assert s[0]["title"] == "The Matrix" and s[1]["name"] == "GoT" and s[1]["mediaType"] == "tv"
    assert c.get("/api/v1/movie/603", headers=H).json["title"] == "T603"
    assert c.get("/api/v1/tv/1399", headers=H).json["numberOfSeasons"] == 8
    assert c.get("/api/v1/movie/5", headers=H).status_code == 404
    assert c.get("/api/v1/request?sort=modified", headers=H).status_code == 400
    assert kicks == []
    assert c.get("/api/v1/settings", headers=H).status_code == 404
