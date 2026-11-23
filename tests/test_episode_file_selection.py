"""Selection controls use synthetic listings and prohibit real transports."""
import subprocess
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
import json

import monitor
from torrentio import TorrentioStream


@pytest.fixture(autouse=True)
def no_transport(monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError("selection controls prohibit real child/provider transport")
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(requests.sessions.Session, "request", refuse)


def stream():
    return TorrentioStream(name="One Piece S21 Complete 1080p WEB-DL",
                          title="One Piece S21 Complete 1080p WEB-DL",
                          info_hash="a" * 40, quality="1080p", seeders=10,
                          size_gb=20, is_season_pack=True, source="test")


@pytest.mark.parametrize("case,final_status", [
    ("valid", "selected"), ("uncached", "notcached"),
    ("missing", "missing_listing"), ("wrong", "numbering_refusal"),
    ("ambiguous", "file_refusal"), ("writer", "writer_conflict"),
    ("provider", "provider_error"), ("sanity", "file_refusal"),
])
def test_verified_file_inside_generic_pack_reaches_writer(monkeypatch, case, final_status):
    import release_sanity
    import strm_generator
    import processor
    item = stream()
    expected = "Luffy is on the Move A Turning Point of a New Era"
    file = {"id": 7, "name": f"One Piece S21E1063 {expected}.mkv", "size": 900_000_000}
    monkeypatch.setattr(monitor._settings, "get", lambda key, default=None: key == "CATBOX_MODE")
    monkeypatch.setattr(processor, "_episode_search_override", lambda *a: {"episode_title": expected})
    def search(*a, **kw):
        if kw.get("trace") is not None:
            kw["trace"].update(run_id=41)
        return [item]
    monkeypatch.setattr(monitor.search_engine, "search_candidates", search)
    monkeypatch.setattr(monitor.search_engine, "cache_map", lambda *a: {"torbox": set() if case == "uncached" else {item.info_hash}})
    monkeypatch.setattr(release_sanity, "filter_cached", lambda items, **kw: [] if case == "sanity" else items)
    monkeypatch.setattr(release_sanity, "series_identity", lambda *a: SimpleNamespace(
        imdb_id="tt0388629", name="One Piece", year=1999, regions=frozenset({"JP"}),
        language="ja", title_tokens=frozenset({"one", "piece"}), namesakes=frozenset()))
    if case == "wrong": file["name"] = file["name"].replace("E1063", "E1062")
    files = [file, {**file, "id": 8}] if case == "ambiguous" else [file]
    def listing(*args, **kwargs):
        if case == "provider": raise RuntimeError("provider credential must not be persisted")
        return {} if case == "missing" else {item.info_hash: {"name": item.title, "files": files}}
    monkeypatch.setattr(monitor.torbox, "check_cached_files", listing)
    monkeypatch.setattr(release_sanity, "verify_entry", lambda *a, **kw: None)
    writer = Mock(return_value=case != "writer")
    outcomes, selected = Mock(), Mock()
    monkeypatch.setattr(monitor.db, "record_episode_selection", outcomes)
    monkeypatch.setattr(monitor.db, "get_virtual_item_by_episode", lambda *a: None)
    monkeypatch.setattr(monitor.search_engine, "mark_selected", selected)
    monkeypatch.setattr(strm_generator, "create_lazy_episode_strm", writer)
    result = monitor._retry_episode({"id": 1, "imdb_id": "tt0388629", "title": "One Piece",
                                     "season": 21, "episode": 1063, "attempt_count": 0})
    assert result is (case == "valid")
    assert outcomes.call_args.args == (41, "tt0388629:S21E1063", item.info_hash, "torrent", final_status)
    if case != "valid":
        selected.assert_not_called()
        if case != "writer": writer.assert_not_called()
        return
    assert writer.call_args.kwargs["episode"] == 1063
    proof = writer.call_args.kwargs["episode_file_proof"]
    assert proof["file_name"] == file["name"]
    assert proof["episode"] == 1063
    selected.assert_called_once_with("tt0388629:S21E1063", item, "torbox", run_id=41)


@pytest.mark.parametrize("argv", [["ssh", "unraid", "true"], ["docker", "ps"]])
def test_transport_refuses_subprocess(argv):
    with pytest.raises(PermissionError):
        subprocess.run(argv)


def test_transport_refuses_provider_http():
    with pytest.raises(PermissionError):
        requests.get("https://provider.invalid/cache")


@pytest.fixture
def proof_inputs(monkeypatch):
    import episode_selection
    import release_sanity
    identity = SimpleNamespace(imdb_id="tt0388629", name="One Piece", year=1999,
                               regions=frozenset({"JP"}), language="ja",
                               title_tokens=frozenset({"one", "piece"}), namesakes=frozenset())
    monkeypatch.setattr(release_sanity, "series_identity", lambda *a: identity)
    monkeypatch.setattr(release_sanity, "verify_entry", lambda *a, **kw: None)
    kwargs = dict(imdb_id="tt0388629", show_title="One Piece", season=21,
                  episode=1063, episode_title="Luffy Moves Forward")
    file = dict(id=7, name="One Piece S21E1063 Luffy Moves Forward.mkv", size=900000000)
    return episode_selection, kwargs, file


@pytest.mark.parametrize("number", [130, 208, 1063, 1314])
def test_full_number_file_proof_and_playback_binding(proof_inputs, number):
    module, kwargs, file = proof_inputs
    kwargs["episode"] = number
    file["name"] = f"One Piece S21E{number} Luffy Moves Forward.mkv"
    proof, reason = module.prove_file(stream(), {"files": [file]}, **kwargs)
    assert reason == "eligible" and proof["episode"] == number
    item = dict(info_hash="a" * 40, imdb_id=kwargs["imdb_id"], season=21, episode=number)
    assert module.match_proven_file(json.dumps(proof), {"files": [file]}, item) == file


@pytest.mark.parametrize("entry", [None, {}, {"name": "One Piece S21"}, {"files": []}])
def test_missing_listing_is_not_fail_open_proof(proof_inputs, entry):
    module, kwargs, _ = proof_inputs
    assert module.prove_file(stream(), entry, **kwargs) == (None, "missing_listing")


@pytest.mark.parametrize("change", ["wrong_number", "prefix_number", "wrong_title", "wrong_show",
                                   "ambiguous", "trailer", "executable", "zero_size", "url", "parent"])
def test_unproved_file_refuses(proof_inputs, change):
    module, kwargs, file = proof_inputs
    files = [file]
    if change == "wrong_number": file["name"] = file["name"].replace("E1063", "E1062")
    if change == "prefix_number": file["name"] = file["name"].replace("E1063", "E10630")
    if change == "wrong_title": file["name"] = file["name"].replace("Luffy Moves Forward", "A Different Story")
    if change == "wrong_show": file["name"] = file["name"].replace("One Piece", "Different Show")
    if change == "ambiguous": files.append({**file, "id": 9})
    if change == "trailer": file["name"] = file["name"].replace(".mkv", ".trailer.mkv")
    if change == "executable": file["name"] += ".exe"
    if change == "zero_size": file["size"] = 0
    if change == "url": file["name"] = "https://provider.invalid/" + file["name"]
    if change == "parent": file["name"] = "../" + file["name"]
    proof, reason = module.prove_file(stream(), {"files": files}, **kwargs)
    assert proof is None and reason.endswith("refusal")


def test_listing_sanity_and_series_identity_stay_enforced(proof_inputs, monkeypatch):
    import release_sanity
    module, kwargs, file = proof_inputs
    monkeypatch.setattr(release_sanity, "verify_entry", lambda *a, **kw: "wrong pack")
    assert module.prove_file(stream(), {"files": [file]}, **kwargs) == (None, "file_refusal")
    monkeypatch.setattr(release_sanity, "verify_entry", lambda *a, **kw: None)
    monkeypatch.setattr(release_sanity, "series_identity", lambda *a: None)
    assert module.prove_file(stream(), {"files": [file]}, **kwargs) == (None, "identity_refusal")


@pytest.mark.parametrize("change", ["hash", "episode", "show", "size", "name", "duplicate", "id_zero", "missing"])
def test_playback_cannot_replace_proved_file(proof_inputs, change):
    module, kwargs, file = proof_inputs
    proof, _ = module.prove_file(stream(), {"files": [file]}, **kwargs)
    item = dict(info_hash="a" * 40, imdb_id="tt0388629", season=21, episode=1063)
    files = [dict(file)]
    if change == "hash": item["info_hash"] = "b" * 40
    if change == "episode": item["episode"] = 106
    if change == "show": item["imdb_id"] = "tt9999999"
    if change == "size": files[0]["size"] += 1
    if change == "name": files[0]["name"] = "One Piece S21E1062 Luffy Moves Forward.mkv"
    if change == "duplicate": files.append(dict(file))
    if change == "id_zero": files[0]["id"] = 0
    if change == "missing": files = []
    assert module.match_proven_file(proof, {"files": files}, item) is None


def test_proof_cannot_carry_unknown_fields_urls_or_duplicate_keys(proof_inputs):
    module, kwargs, file = proof_inputs
    proof, _ = module.prove_file(stream(), {"files": [file]}, **kwargs)
    with pytest.raises(ValueError): module.validate_proof({**proof, "provider_token": "secret"})
    with pytest.raises(ValueError): module.validate_proof({**proof, "episode_title": "https://provider.invalid/?token=secret"})
    with pytest.raises(ValueError): module.validate_proof(json.dumps(proof)[:-1] + ',"episode":1063}')


def test_proved_playback_uses_exact_file_without_alternate(proof_inputs, monkeypatch):
    import catbox
    import strm_generator
    import release_sanity
    module, kwargs, file = proof_inputs
    monkeypatch.setattr(catbox, "db", Mock())
    monkeypatch.setattr(catbox, "_record_playback_touch", Mock())
    proof, _ = module.prove_file(stream(), {"files": [file]}, **kwargs)
    item = dict(info_hash="a" * 40, imdb_id="tt0388629", season=21, episode=1063,
                media_type="series", protocol="torrent", episode_file_proof=json.dumps(proof))
    live = dict(id=5, hash="a" * 40, files=[file])
    monkeypatch.setattr(catbox.torbox, "find_by_hash", lambda *a: live)
    monkeypatch.setattr(catbox.torbox, "_is_ready", lambda *a: True)
    monkeypatch.setattr(catbox.torbox, "RateLimited", type("RateLimited", (Exception,), {}))
    monkeypatch.setattr(release_sanity, "verify_live_torrent", lambda *a, **kw: None)
    urls = Mock(return_value="https://cdn.invalid/selected")
    monkeypatch.setattr(strm_generator, "_get_stream_url", urls)
    assert catbox._materialize_proven_episode("synthetic", item, False) == "https://cdn.invalid/selected"
    urls.assert_called_once_with(5, 7)
    live["files"] = [{**file, "name": "One Piece S21E1062.mkv"}]
    urls.reset_mock()
    assert catbox._materialize_proven_episode("synthetic", item, False) is None
    urls.assert_not_called()


def test_exact_search_run_outcomes_and_choice_are_bound(tmp_path, monkeypatch, proof_inputs):
    import importlib.util
    import sqlite3
    from contextlib import contextmanager
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("selection_real_db", Path(monitor.__file__).with_name("db.py"))
    db = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(db)
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    @contextmanager
    def connect():
        yield conn
    monkeypatch.setattr(db, "_connect", connect)
    db.init()
    db.init()  # The migration must also be safe on an existing database.
    key = "tt0388629:S21E1063"
    run = db.start_search_run(key, "One Piece", "series", 21, 1063, "test")
    later = db.start_search_run(key, "One Piece", "series", 21, 1063, "test")
    module, kwargs, file = proof_inputs
    proof, _ = module.prove_file(stream(), {"files": [file]}, **kwargs)
    db.record_episode_selection(run, key, "a" * 40, "torrent", "eligible", proof)
    db.mark_latest_search_choice(key, "a" * 40, "test", run_id=run)
    assert conn.execute("SELECT chosen_hash FROM search_runs WHERE id=?", (run,)).fetchone()[0] == "a" * 40
    assert conn.execute("SELECT chosen_hash FROM search_runs WHERE id=?", (later,)).fetchone()[0] is None
    assert conn.execute("SELECT outcome FROM episode_selection_outcomes").fetchone()[0] == "eligible"
    with pytest.raises(ValueError): db.record_episode_selection(run, "wrong-content", "a" * 40, "torrent", "selected")
    with pytest.raises(ValueError): db.record_episode_selection(run, key, "a" * 40, "torrent", "https://provider.invalid/token")
    db.insert_virtual_item("test", "a" * 40, "synthetic", "One Piece", "series", imdb_id="tt0388629",
                           season=21, episode=1063, episode_file_proof=proof)
    assert json.loads(conn.execute("SELECT episode_file_proof FROM virtual_items").fetchone()[0]) == proof
    trace = db.get_search_trace(key)
    assert trace["run"]["id"] == later and trace["selection"] == []
    conn.close()


def test_cached_url_requires_same_proof_and_item(proof_inputs, monkeypatch):
    import catbox
    module, kwargs, file = proof_inputs
    proof, _ = module.prove_file(stream(), {"files": [file]}, **kwargs)
    item = dict(info_hash="a" * 40, imdb_id="tt0388629", season=21, episode=1063,
                media_type="series", episode_file_proof=json.dumps(proof))
    binding = module.item_binding(item)
    catbox._cache_put("proof-token", "https://cdn.invalid/selected", binding)
    assert catbox._cache_get("proof-token", binding) == "https://cdn.invalid/selected"
    assert catbox._cache_get("proof-token", "wrong-binding") is None
    catbox._cache_put("proof-token", "https://cdn.invalid/unproved")
    assert catbox._cache_get("proof-token", binding) is None
    item["episode"] = 106
    with pytest.raises(ValueError): module.item_binding(item)


def test_actual_materialize_dispatch_cannot_use_legacy_fallback_for_proof(proof_inputs, monkeypatch):
    import catbox
    module, kwargs, file = proof_inputs
    proof, _ = module.prove_file(stream(), {"files": [file]}, **kwargs)
    item = dict(info_hash="a" * 40, imdb_id="tt0388629", season=21, episode=1063,
                media_type="series", episode_file_proof=json.dumps(proof))
    monkeypatch.setattr(catbox.db, "get_virtual_item", lambda *a: item)
    route = Mock(return_value="https://cdn.invalid/selected")
    monkeypatch.setattr(catbox, "_materialize_proven_episode", route)
    assert catbox._materialize_locked("synthetic", allow_readd=False) == "https://cdn.invalid/selected"
    route.assert_called_once_with("synthetic", item, False)


def test_bad_proof_refuses_before_cached_url_or_provider(proof_inputs, monkeypatch):
    import catbox
    module, kwargs, file = proof_inputs
    proof, _ = module.prove_file(stream(), {"files": [file]}, **kwargs)
    item = dict(info_hash="b" * 40, imdb_id="tt0388629", season=21, episode=1063,
                media_type="series", episode_file_proof=json.dumps(proof))
    monkeypatch.setattr(catbox.db, "get_virtual_item", lambda *a: item)
    cached, locked = Mock(return_value="https://cdn.invalid/unproved"), Mock()
    monkeypatch.setattr(catbox, "_cache_get", cached)
    monkeypatch.setattr(catbox, "_materialize_locked", locked)
    assert catbox._materialize_inner("synthetic") is None
    cached.assert_not_called()
    locked.assert_not_called()


def test_directory_title_cannot_supply_episode_identity(proof_inputs):
    module, kwargs, file = proof_inputs
    file["name"] = "Luffy Moves Forward/One Piece S21E1063.mkv"
    assert module.prove_file(stream(), {"files": [file]}, **kwargs)[0] is None

@pytest.mark.parametrize('tag', ['S21E1063E1064', 'S21E1063-E1064', 'S21E1063-1064',
                                  'S21E1063 E1064', 'S21E1063+1064', 'S21E1063 to 1064'])
def test_multi_episode_file_is_not_single_episode_proof(proof_inputs, tag):
    module, kwargs, file = proof_inputs
    file['name'] = file['name'].replace('S21E1063', tag)
    assert module.prove_file(stream(), {'files': [file]}, **kwargs)[0] is None
