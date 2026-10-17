"""Library presence wins over stale attempts, without rewriting their history."""
import os
import subprocess
import sys

import pytest

import library_status


@pytest.fixture()
def database(monkeypatch, tmp_path):
    saved = {m: sys.modules.get(m) for m in ("db", "config")}
    for m in saved:
        sys.modules.pop(m, None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "library.db"))
    import db
    db.init()
    yield db
    db._thread_conn().close()
    for m, value in saved.items():
        sys.modules.pop(m, None)
        if value is not None:
            sys.modules[m] = value


def indexed(db, imdb="tt1234567", kind="movie", path="/library/Example.strm"):
    db.insert_virtual_item("token-" + imdb, "hash", "", "Example", kind,
                           strm_path=path, imdb_id=imdb)


@pytest.mark.parametrize("attempt", ["failed", "wanted", "pending", "upcoming"])
def test_indexed_entry_overrides_attempt_without_writes(database, attempt):
    db = database
    rid = db.insert_request("Example", "tt1234567", "movie", tmdb_id=123)
    db.update_request(rid, attempt, error="previous attempt")
    indexed(db)
    before = db.get_request_by_imdb("tt1234567")
    changes = db._thread_conn().total_changes
    items = [{"media_type": "movie", "tmdb_id": 123}]
    library_status.enrich(items)
    assert items[0]["library_status"] == "success"
    assert items[0]["imdb_id"] == "tt1234567"
    assert db.get_request_by_imdb("tt1234567") == before
    assert db._thread_conn().total_changes == changes


@pytest.mark.parametrize("path", [None, ""])
def test_unindexed_placeholder_is_not_available(database, path):
    db = database
    rid = db.insert_request("Example", "tt1234567", "movie", tmdb_id=123)
    db.update_request(rid, "failed", error="unresolved")
    indexed(db, path=path)
    items = [{"imdb_id": "tt1234567", "media_type": "movie"}]
    library_status.enrich(items)
    assert items[0]["library_status"] == "failed"


def test_typed_identities_and_legacy_links(database):
    db = database
    uid = db.create_user("member", "disabled")
    db.add_to_watchlist(uid, "tt1234567", 123, "movie", "Example")
    indexed(db)
    rid = db.insert_request("Other", "tt7654321", "series", tmdb_id=123)
    db.update_request(rid, "failed")
    items = [{"media_type": kind, "tmdb_id": 123} for kind in ("movie", "tv")]
    library_status.enrich(items)
    assert [(r["imdb_id"], r["library_status"]) for r in items] == [
        ("tt1234567", "success"), ("tt7654321", "failed")]
    # Explicit, different identities must never borrow this TMDB status.
    other = [{"imdb_id": "tt9999999", "tmdb_id": 123, "media_type": "movie"}]
    library_status.enrich(other)
    assert other[0]["library_status"] is None
    # A conflicting same-kind link is ambiguous, not evidence of availability.
    db.create_user_request(uid, "tt9999999", 123, "movie", "Conflict")
    assert ("movie", 123) not in library_status.by_tmdb([123])


def test_series_alias_and_virtual_only_entry(database):
    db = database
    indexed(db, kind="series")
    rows = [{"imdb_id": "tt1234567", "media_type": "tv"},
            {"imdb_id": "tt1234567", "media_type": "movie"},
            {"imdb_id": "", "media_type": "movie"}]
    library_status.enrich(rows)
    assert [r["library_status"] for r in rows] == ["success", None, None]


def test_real_routes_agree_without_processing(tmp_path):
    script = r'''
from unittest.mock import Mock
import app as m
import db, auth, settings
settings.set("SETUP_COMPLETE", True)
m._start_scheduler = Mock()
uid = db.create_user("owner", "disabled", role="admin")
rid = db.insert_request("Example", "tt1234567", "movie", tmdb_id=123)
db.update_request(rid, "failed", error="old attempt")
db.insert_virtual_item("token", "hash", "", "Example", "movie",
                       strm_path="/library/Example.strm", imdb_id="tt1234567")
db.create_user_request(uid, "tt1234567", 123, "movie", "Example", status="approved")
db.add_to_watchlist(uid, "tt1234567", 123, "movie", "Example")
missing = db.insert_request("Unresolved", "tt7654321", "movie", tmdb_id=456)
db.update_request(missing, "failed", error="no stream")
db.insert_virtual_item("placeholder", "hash", "", "Unresolved", "movie", imdb_id="tt7654321")
m.tmdb.multi_search = Mock(return_value=[{"tmdb_id": 123, "media_type": "movie"},
                                       {"tmdb_id": 456, "media_type": "movie"},
                                       {"tmdb_id": 999, "media_type": "movie"}])
m.tmdb.details = lambda kind, tid, **kw: {"imdb_id": "tt1234567" if tid == 123 else "tt7654321"}
m.processor.process = Mock(side_effect=AssertionError("a status read must not process"))
c = m.app.test_client()
with c.session_transaction() as s:
    s.update(user_id=uid, user="owner", role="admin")
before = db.get_request_by_imdb("tt1234567")
assert c.get("/ui/api/discover/search?q=Example").json["results"][0]["library_status"] == "success"
assert c.get("/ui/api/discover/details?type=movie&id=123").json["library_status"] == "success"
assert c.get("/ui/api/discover/details?type=movie&id=456").json["library_status"] == "failed"
movies = c.get("/ui/api/library/movies").json["items"]
assert next(r for r in movies if r["imdb_id"] == "tt1234567")["status"] == "success"
assert c.get("/ui/api/requests/status?imdb_id=tt1234567").json["status"] == "success"
assert c.get("/ui/api/library/status-map").json["123"] == "success"
assert c.get("/ui/api/user-requests?mine=1").json["items"][0]["library_status"] == "success"
assert c.get("/ui/api/watchlist").json["items"][0]["library_status"] == "success"
assert [r["id"] for r in c.get("/ui/api/requests/failed").json["items"]] == [missing]
assert db.get_request_by_imdb("tt1234567") == before
m.processor.process.assert_not_called()
print("OK")
'''
    env = dict(os.environ, DB_PATH=str(tmp_path / "routes.db"), AUTH_ENABLED="true",
               AUTH_SESSION_SECRET="s" * 40, TORBOX_API_KEY="test", MEDIA_PATH=str(tmp_path),
               SPORE_MEDIA_PATH=str(tmp_path), LITE_MODE="true", INSECURE_ALLOW_ANON="false")
    result = subprocess.run([sys.executable, "-c", script], cwd=os.path.dirname(os.path.dirname(__file__)),
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout[-500:] + result.stderr[-2000:]
