import importlib.util
import json
import os
from pathlib import Path
import sys
from unittest.mock import MagicMock
import xml.etree.ElementTree as ET

import pytest

_temporary_modules = {
    name: MagicMock() for name in ("catbox", "playback_guard", "strm_generator")
}
_saved_modules = {name: sys.modules.get(name) for name in _temporary_modules}
sys.modules.update(_temporary_modules)
_spec = importlib.util.spec_from_file_location(
    "enrichment_worker_under_test",
    os.path.join(os.path.dirname(__file__), "..", "enrichment.py"),
)
enrichment = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(enrichment)
for _name, _saved in _saved_modules.items():
    if _saved is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _saved


class Response:
    def __init__(self, content=b"<MediaContainer />", status=200, headers=None,
                 chunks=None):
        self.content = content
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks or []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_session_poll_queues_only_durable_real_session_start(monkeypatch):
    xml = b"""
    <MediaContainer size="2">
      <Video ratingKey="101" type="episode">
        <Media><Part file="/spore-nfs-media/series/Show/Season 01/Show S01E01.mkv" /></Media>
        <Player state="playing" machineIdentifier="player-a" />
        <Session id="session-a" />
      </Video>
      <Video ratingKey="102" type="episode">
        <Media><Part file="/spore-nfs-media/series/Show/Season 01/Show S01E02.mkv" /></Media>
      </Video>
    </MediaContainer>
    """
    monkeypatch.setattr(enrichment, "enabled", lambda: True)
    monkeypatch.setattr(enrichment, "_plex_request", lambda *a, **k: Response(xml))
    enrichment.db.find_virtual_item_by_plex_path.return_value = {"token": "token-a"}
    enrichment.db.record_plex_playback_event.return_value = True
    queued = MagicMock(return_value=12)
    monkeypatch.setattr(enrichment, "queue_from_playback", queued)

    result = enrichment.poll_plex_sessions()

    assert result == {
        "status": "ok", "sessions": 1, "queued": 1, "errors": 0,
    }
    queued.assert_called_once_with("token-a", reason="plex-session")
    enrichment.db.mark_plex_playback_event_queued.assert_called_once()


def test_bad_session_does_not_block_another_session(monkeypatch):
    xml = b"""
    <MediaContainer size="2">
      <Video ratingKey="101" type="episode">
        <Media><Part file="/spore-nfs-media/series/Show/Season 01/One.mkv" /></Media>
        <Player state="playing" machineIdentifier="player-a" />
        <Session id="session-a" />
      </Video>
      <Video ratingKey="102" type="episode">
        <Media><Part file="/spore-nfs-media/series/Show/Season 01/Two.mkv" /></Media>
        <Player state="playing" machineIdentifier="player-b" />
        <Session id="session-b" />
      </Video>
    </MediaContainer>
    """
    monkeypatch.setattr(enrichment, "enabled", lambda: True)
    monkeypatch.setattr(enrichment, "_plex_request", lambda *a, **k: Response(xml))
    enrichment.db.find_virtual_item_by_plex_path.side_effect = [
        {"token": "bad-token"}, {"token": "good-token"},
    ]
    enrichment.db.record_plex_playback_event.side_effect = [
        RuntimeError("database write failed"), True,
    ]
    queued = MagicMock(return_value=1)
    monkeypatch.setattr(enrichment, "queue_from_playback", queued)

    result = enrichment.poll_plex_sessions()

    assert result == {
        "status": "partial", "sessions": 2, "queued": 1, "errors": 1,
    }
    queued.assert_called_once_with("good-token", reason="plex-session")


def test_stable_imdb_match_handles_punctuation_difference(monkeypatch):
    search = b"""
    <MediaContainer>
      <Directory type="show" ratingKey="42" librarySectionID="8"
                 title="Killers: Caught on Camera" year="2023" />
    </MediaContainer>
    """
    metadata = b"""
    <MediaContainer>
      <Directory type="show" ratingKey="42" librarySectionID="8"
                 title="Killers: Caught on Camera" year="2023">
        <Guid id="imdb://tt26596455" />
        <Guid id="tmdb://225772" />
      </Directory>
    </MediaContainer>
    """

    def request(method, path, **kwargs):
        return Response(metadata if path.startswith("/library/metadata/") else search)

    monkeypatch.setattr(enrichment, "_plex_request", request)
    monkeypatch.setattr(enrichment, "_plex_section", lambda media_type: "8")

    node = enrichment._lookup_library_item({
        "title": "Killers Caught on Camera S03E05",
        "media_type": "series",
        "imdb_id": "tt26596455",
        "tmdb_id": 225772,
        "year": None,
    })

    assert node.get("ratingKey") == "42"


def test_stable_tmdb_match_is_used_when_imdb_is_unavailable(monkeypatch):
    search = b"""
    <MediaContainer>
      <Video type="movie" ratingKey="42" librarySectionID="7"
             title="Example Movie" year="2024" />
    </MediaContainer>
    """
    metadata = b"""
    <MediaContainer>
      <Video type="movie" ratingKey="42" librarySectionID="7"
             title="Example Movie" year="2024">
        <Guid id="tmdb://225772" />
      </Video>
    </MediaContainer>
    """

    def request(method, path, **kwargs):
        return Response(metadata if path.startswith("/library/metadata/") else search)

    monkeypatch.setattr(enrichment, "_plex_request", request)
    monkeypatch.setattr(enrichment, "_plex_section", lambda media_type: "7")

    node = enrichment._lookup_library_item({
        "title": "Example Movie (2024)",
        "media_type": "movie",
        "imdb_id": None,
        "tmdb_id": 225772,
        "year": 2024,
    })

    assert node.get("ratingKey") == "42"


def test_stable_id_search_considers_all_safe_title_queries(monkeypatch):
    wrong_search = b"""
    <MediaContainer>
      <Directory type="show" ratingKey="1" librarySectionID="8"
                 title="Example Show" year="2023" />
    </MediaContainer>
    """
    right_search = b"""
    <MediaContainer>
      <Directory type="show" ratingKey="2" librarySectionID="8"
                 title="Example: Show" year="2023" />
    </MediaContainer>
    """

    def request(method, path, **kwargs):
        if path == "/search":
            query = kwargs["params"]["query"]
            return Response(wrong_search if query == "Example: Show" else right_search)
        rating_key = path.rsplit("/", 1)[1]
        imdb_id = "tt0000001" if rating_key == "1" else "tt7654321"
        return Response(
            f'<MediaContainer><Directory type="show" ratingKey="{rating_key}" '
            f'librarySectionID="8" title="Example: Show" year="2023">'
            f'<Guid id="imdb://{imdb_id}" /></Directory></MediaContainer>'.encode()
        )

    monkeypatch.setattr(enrichment, "_plex_request", request)
    monkeypatch.setattr(enrichment, "_plex_section", lambda media_type: "8")

    node = enrichment._lookup_library_item({
        "title": "Example: Show S01E01",
        "media_type": "series",
        "imdb_id": "tt7654321",
        "tmdb_id": None,
        "year": 2023,
    })

    assert node.get("ratingKey") == "2"


def test_title_fallback_requires_unique_matching_year(monkeypatch):
    search = b"""
    <MediaContainer>
      <Video type="movie" ratingKey="1" librarySectionID="7"
             title="I, Robot" year="2004" />
      <Video type="movie" ratingKey="2" librarySectionID="7"
             title="I Robot" year="2004" />
    </MediaContainer>
    """

    def request(method, path, **kwargs):
        if path == "/search":
            return Response(search)
        key = path.rsplit("/", 1)[1]
        return Response(
            f'<MediaContainer><Video type="movie" ratingKey="{key}" '
            f'librarySectionID="7" title="I Robot" year="2004" />'
            f'</MediaContainer>'.encode()
        )

    monkeypatch.setattr(enrichment, "_plex_request", request)
    monkeypatch.setattr(enrichment, "_plex_section", lambda media_type: "7")
    with pytest.raises(RuntimeError, match="ambiguous normalized"):
        enrichment._lookup_library_item({
            "title": "I Robot (2004)", "media_type": "movie",
            "imdb_id": None, "tmdb_id": None, "year": 2004,
        })


def test_title_fallback_accepts_one_normalized_title_and_year(monkeypatch):
    search = b"""
    <MediaContainer>
      <Video type="movie" ratingKey="7" librarySectionID="7"
             title="Amelie" year="2001" />
    </MediaContainer>
    """
    metadata = b"""
    <MediaContainer>
      <Video type="movie" ratingKey="7" librarySectionID="7"
             title="Amelie" year="2001" />
    </MediaContainer>
    """

    def request(method, path, **kwargs):
        return Response(metadata if path.startswith("/library/metadata/") else search)

    monkeypatch.setattr(enrichment, "_plex_request", request)
    monkeypatch.setattr(enrichment, "_plex_section", lambda media_type: "7")

    node = enrichment._lookup_library_item({
        "title": "Amelie! (2001)",
        "media_type": "movie",
        "imdb_id": None,
        "tmdb_id": None,
        "year": 2001,
    })

    assert node.get("ratingKey") == "7"


def test_conflicting_plex_provider_ids_fail_closed(monkeypatch):
    search = b"""
    <MediaContainer>
      <Video type="movie" ratingKey="1" librarySectionID="7"
             title="Example" year="2024" />
    </MediaContainer>
    """
    metadata = b"""
    <MediaContainer>
      <Video type="movie" ratingKey="1" librarySectionID="7"
             title="Example" year="2024">
        <Guid id="imdb://tt7654321" />
        <Guid id="tmdb://999" />
      </Video>
    </MediaContainer>
    """

    def request(method, path, **kwargs):
        return Response(metadata if path.startswith("/library/metadata/") else search)

    monkeypatch.setattr(enrichment, "_plex_request", request)
    monkeypatch.setattr(enrichment, "_plex_section", lambda media_type: "7")

    with pytest.raises(RuntimeError, match="conflicting Plex IMDb/TMDB"):
        enrichment._lookup_library_item({
            "title": "Example (2024)",
            "media_type": "movie",
            "imdb_id": "tt7654321",
            "tmdb_id": 123,
            "year": 2024,
        })


def test_split_expected_provider_ids_fail_closed(monkeypatch):
    search = b"""
    <MediaContainer>
      <Video type="movie" ratingKey="1" librarySectionID="7"
             title="Example" year="2024" />
      <Video type="movie" ratingKey="2" librarySectionID="7"
             title="Example" year="2024" />
    </MediaContainer>
    """

    def request(method, path, **kwargs):
        if path == "/search":
            return Response(search)
        key = path.rsplit("/", 1)[1]
        guid = "imdb://tt7654321" if key == "1" else "tmdb://123"
        return Response(
            f'<MediaContainer><Video type="movie" ratingKey="{key}" '
            f'librarySectionID="7" title="Example" year="2024">'
            f'<Guid id="{guid}" /></Video></MediaContainer>'.encode()
        )

    monkeypatch.setattr(enrichment, "_plex_request", request)
    monkeypatch.setattr(enrichment, "_plex_section", lambda media_type: "7")

    with pytest.raises(RuntimeError, match="conflicting Plex IMDb/TMDB"):
        enrichment._lookup_library_item({
            "title": "Example (2024)", "media_type": "movie",
            "imdb_id": "tt7654321", "tmdb_id": 123, "year": 2024,
        })


def test_movie_resolution_is_supported(monkeypatch):
    node = ET.fromstring(
        '<Video type="movie" ratingKey="movie-9" librarySectionID="7" />'
    )
    monkeypatch.setattr(enrichment, "_lookup_library_item", lambda item: node)

    targets, failures = enrichment._resolve_batch([
        {"token": "movie-token", "title": "Movie", "media_type": "movie"}
    ])

    assert failures == {}
    assert targets == {
        "movie-token": {"rating_key": "movie-9", "season_key": None}
    }


def test_series_resolution_supports_specials_and_rejects_duplicate_episode(
        monkeypatch):
    show = ET.fromstring('<Directory type="show" ratingKey="show-1" />')
    leaves = b"""
    <MediaContainer>
      <Video parentIndex="0" index="1" ratingKey="special-1"
             parentRatingKey="season-0" />
      <Video parentIndex="1" index="1" ratingKey="episode-a"
             parentRatingKey="season-1" />
      <Video parentIndex="1" index="1" ratingKey="episode-b"
             parentRatingKey="season-1" />
    </MediaContainer>
    """
    monkeypatch.setattr(enrichment, "_lookup_library_item", lambda item: show)
    monkeypatch.setattr(enrichment, "_plex_request", lambda *a, **k: Response(leaves))

    targets, failures = enrichment._resolve_batch([
        {"token": "special", "title": "Show S00E01", "media_type": "series",
         "season": 0, "episode": 1},
        {"token": "regular", "title": "Show S01E01", "media_type": "series",
         "season": 1, "episode": 1},
    ])

    assert targets["special"] == {
        "rating_key": "special-1", "season_key": "season-0",
    }
    assert "regular" not in targets
    assert "ambiguous Plex episode" in failures["regular"]


def test_invalid_plex_activity_response_fails_closed(monkeypatch):
    monkeypatch.setattr(
        enrichment, "_plex_request", lambda *a, **k: Response(b"not xml")
    )
    with pytest.raises(RuntimeError, match="invalid XML"):
        enrichment._analysis_activities()


def test_invalid_interrupted_stub_backup_blocks_startup(tmp_path, monkeypatch):
    stub = tmp_path / "Episode.mkv"
    backup = tmp_path / ".Episode.mkv.enrichment-stub"
    stub.write_bytes(b"stub")
    backup.write_bytes(b"x" * (1024 * 1024 + 1))
    monkeypatch.setattr(
        enrichment.db, "get_enrichment_recovery_items",
        MagicMock(return_value=[{"token": "tok", "strm_path": "unused"}]),
    )
    monkeypatch.setattr(enrichment, "_stub_path", lambda item: stub)

    with pytest.raises(RuntimeError, match="failed to restore 1"):
        enrichment.recover_overlays()
    assert stub.read_bytes() == b"stub"
    assert backup.exists()


def test_safe_preferences_require_successful_readback_of_every_setting(monkeypatch):
    settings = "".join(
        f'<Setting id="{key}" value="never" />'
        for key in enrichment._ANALYSIS_SETTINGS
    ) + "".join(
        f'<Setting id="{key}" value="0" />'
        for key in enrichment._BUTLER_SETTINGS
    )
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, kwargs.get("params")))
        if method == "GET":
            return Response(f"<MediaContainer>{settings}</MediaContainer>".encode())
        return Response()

    monkeypatch.setattr(enrichment, "_plex_request", request)

    enrichment._set_analysis("never")

    assert len([call for call in calls if call[0] == "PUT"]) == 6
    assert enrichment.health_snapshot()["preferences_safe"] is True


def test_safe_preference_restore_attempts_every_setting_after_partial_failure(
        monkeypatch):
    settings = "".join(
        f'<Setting id="{key}" value="never" />'
        for key in enrichment._ANALYSIS_SETTINGS
    ) + "".join(
        f'<Setting id="{key}" value="0" />'
        for key in enrichment._BUTLER_SETTINGS
    )
    puts = []

    def request(method, path, **kwargs):
        if method == "GET":
            return Response(f"<MediaContainer>{settings}</MediaContainer>".encode())
        puts.append(kwargs["params"])
        return Response(status=500 if len(puts) == 1 else 200)

    monkeypatch.setattr(enrichment, "_plex_request", request)

    with pytest.raises(RuntimeError, match="preference verification failed"):
        enrichment._set_analysis("never")

    assert len(puts) == 6
    assert enrichment.health_snapshot()["preferences_safe"] is False


def test_missing_preference_readback_fails_closed(monkeypatch):
    monkeypatch.setattr(
        enrichment, "_plex_request",
        lambda method, path, **kwargs: Response(b"<MediaContainer />"),
    )

    with pytest.raises(RuntimeError, match="preference verification failed"):
        enrichment._set_analysis("never")
    assert enrichment.health_snapshot()["preferences_safe"] is False


def test_download_enforces_actual_bytes_without_content_length(tmp_path, monkeypatch):
    monkeypatch.setattr(enrichment.config, "ENRICHMENT_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(enrichment.config, "ENRICHMENT_MIN_FREE_GB", 1)
    monkeypatch.setattr(enrichment, "_local_media_valid", lambda path: False)
    monkeypatch.setattr(enrichment, "_renew_claim", lambda lease_id: None)
    enrichment.db.set_enrichment_claim_state.return_value = 1
    monkeypatch.setattr(enrichment.playback_guard, "active", lambda force=False: False)
    monkeypatch.setattr(
        enrichment.catbox, "materialize", lambda *a, **k: "https://media.invalid/file"
    )
    monkeypatch.setattr(
        enrichment.requests, "get",
        lambda *a, **k: Response(chunks=[b"1234", b"5678"]),
    )
    monkeypatch.setattr(
        enrichment.shutil, "disk_usage",
        lambda path: shutil_usage(total=10**13, used=0, free=10**13),
    )

    with pytest.raises(enrichment.EnrichmentItemTooLarge):
        enrichment._download(
            {"token": "tok", "title": "Movie"}, 0, 6, "lease-a"
        )
    assert not (tmp_path / "tok.part").exists()


def test_download_does_not_reuse_staged_bytes_from_old_release(
        tmp_path, monkeypatch):
    final = tmp_path / "tok.media"
    final.write_bytes(b"old-media")
    (tmp_path / "tok.media.json").write_text(
        json.dumps({
            "schema_version": 1, "token": "tok", "info_hash": "old-hash",
            "size": len(b"old-media"), "sha256": "unused",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(enrichment.config, "ENRICHMENT_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(enrichment.config, "ENRICHMENT_MIN_FREE_GB", 1)
    monkeypatch.setattr(enrichment, "_local_media_valid", lambda path: True)
    monkeypatch.setattr(enrichment, "_renew_claim", lambda lease_id: None)
    enrichment.db.set_enrichment_claim_state.return_value = 1
    monkeypatch.setattr(enrichment.playback_guard, "active", lambda force=False: False)
    materialize = MagicMock(return_value="https://media.invalid/file")
    monkeypatch.setattr(enrichment.catbox, "materialize", materialize)
    monkeypatch.setattr(
        enrichment.requests, "get",
        lambda *a, **k: Response(chunks=[b"new-media"]),
    )
    monkeypatch.setattr(
        enrichment.shutil, "disk_usage",
        lambda path: shutil_usage(total=10**13, used=0, free=10**13),
    )

    size = enrichment._download(
        {"token": "tok", "title": "Movie", "info_hash": "new-hash"},
        0, 100, "lease-a",
    )

    assert size == len(b"new-media")
    assert final.read_bytes() == b"new-media"
    metadata = json.loads((tmp_path / "tok.media.json").read_text())
    assert metadata["info_hash"] == "new-hash"
    materialize.assert_called_once()


def test_download_does_not_reuse_checksum_mismatched_staged_bytes(
        tmp_path, monkeypatch):
    final = tmp_path / "tok.media"
    final.write_bytes(b"tampered")
    (tmp_path / "tok.media.json").write_text(
        json.dumps({
            "schema_version": 1, "token": "tok", "info_hash": "same-hash",
            "size": len(b"tampered"),
            "sha256": enrichment.hashlib.sha256(b"original").hexdigest(),
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(enrichment.config, "ENRICHMENT_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(enrichment.config, "ENRICHMENT_MIN_FREE_GB", 1)
    monkeypatch.setattr(enrichment, "_local_media_valid", lambda path: True)
    monkeypatch.setattr(enrichment, "_renew_claim", lambda lease_id: None)
    enrichment.db.set_enrichment_claim_state.return_value = 1
    monkeypatch.setattr(enrichment.playback_guard, "active", lambda force=False: False)
    materialize = MagicMock(return_value="https://media.invalid/file")
    monkeypatch.setattr(enrichment.catbox, "materialize", materialize)
    monkeypatch.setattr(
        enrichment.requests, "get",
        lambda *a, **k: Response(chunks=[b"replacement"]),
    )
    monkeypatch.setattr(
        enrichment.shutil, "disk_usage",
        lambda path: shutil_usage(total=10**13, used=0, free=10**13),
    )

    size = enrichment._download(
        {"token": "tok", "title": "Movie", "info_hash": "same-hash"},
        0, 100, "lease-a",
    )

    assert size == len(b"replacement")
    assert final.read_bytes() == b"replacement"
    materialize.assert_called_once()


def shutil_usage(total, used, free):
    return type("usage", (), {"total": total, "used": used, "free": free})()


def test_analysis_requires_positive_activity_lifecycle(monkeypatch):
    states = iter([[], [ET.Element("Activity")], [], []])
    monkeypatch.setattr(enrichment, "_analysis_activities", lambda: next(states))
    monkeypatch.setattr(
        enrichment, "_metadata_evidence",
        MagicMock(side_effect=[
            {"digest": "before", "duration_ms": 1000,
             "part_count": 1, "stream_count": 2},
            {"digest": "after", "duration_ms": 1000,
             "part_count": 1, "stream_count": 2},
            {"digest": "after", "duration_ms": 1000,
             "part_count": 1, "stream_count": 2},
        ]),
    )
    monkeypatch.setattr(enrichment, "_plex_request", lambda *a, **k: Response())
    monkeypatch.setattr(enrichment.playback_guard, "active", lambda force=False: False)
    monkeypatch.setattr(enrichment.time, "sleep", lambda seconds: None)

    evidence = enrichment._analyze_target("55")

    assert evidence["activity_started"] is True
    assert evidence["activity_completed"] is True


def test_analysis_accepts_stable_idle_metadata_change_after_three_polls(monkeypatch):
    monkeypatch.setattr(enrichment, "_analysis_activities", lambda: [])
    evidence_calls = iter([
        {"digest": "before", "duration_ms": 1000, "part_count": 1,
         "stream_count": 2},
        {"digest": "after", "duration_ms": 1000, "part_count": 1,
         "stream_count": 2},
        {"digest": "after", "duration_ms": 1000, "part_count": 1,
         "stream_count": 2},
        {"digest": "after", "duration_ms": 1000, "part_count": 1,
         "stream_count": 2},
    ])
    monkeypatch.setattr(
        enrichment, "_metadata_evidence", lambda key: next(evidence_calls)
    )
    monkeypatch.setattr(enrichment, "_plex_request", lambda *a, **k: Response())
    monkeypatch.setattr(enrichment.playback_guard, "active", lambda force=False: False)
    monkeypatch.setattr(enrichment.time, "sleep", lambda seconds: None)

    evidence = enrichment._analyze_target("55")

    assert evidence["activity_started"] is False
    assert evidence["metadata_changed"] is True


def test_unrelated_activity_without_target_change_is_not_completion(monkeypatch):
    states = iter([[], [ET.Element("Activity")]])
    monkeypatch.setattr(enrichment, "_analysis_activities", lambda: next(states))
    monkeypatch.setattr(
        enrichment, "_metadata_evidence",
        lambda key: {"digest": "same", "duration_ms": 1000,
                     "part_count": 1, "stream_count": 2},
    )
    monkeypatch.setattr(enrichment, "_plex_request", lambda *a, **k: Response())
    monkeypatch.setattr(enrichment.playback_guard, "active", lambda force=False: False)
    monkeypatch.setattr(enrichment.config, "ENRICHMENT_ANALYZE_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(enrichment.time, "monotonic", MagicMock(side_effect=[0, 0, 1]))
    monkeypatch.setattr(enrichment.time, "sleep", lambda seconds: None)

    with pytest.raises(RuntimeError, match="no completion evidence"):
        enrichment._analyze_target("55")


def test_analysis_aborts_when_playback_starts_during_wait(monkeypatch):
    monkeypatch.setattr(enrichment, "_analysis_activities", lambda: [])
    monkeypatch.setattr(
        enrichment, "_metadata_evidence",
        lambda key: {"digest": "before", "duration_ms": 1000,
                     "part_count": 1, "stream_count": 2},
    )
    monkeypatch.setattr(enrichment, "_plex_request", lambda *a, **k: Response())
    monkeypatch.setattr(enrichment.playback_guard, "active", lambda force=False: True)

    with pytest.raises(enrichment.EnrichmentDeferred, match="during Plex analysis"):
        enrichment._analyze_target("55")


def test_season_pass_accepts_observed_lifecycle_without_media_parts(monkeypatch):
    states = iter([[], [ET.Element("Activity")], [], []])
    monkeypatch.setattr(enrichment, "_analysis_activities", lambda: next(states))
    monkeypatch.setattr(
        enrichment, "_metadata_evidence",
        lambda key: {"digest": "same", "duration_ms": 0,
                     "part_count": 0, "stream_count": 0},
    )
    monkeypatch.setattr(enrichment, "_plex_request", lambda *a, **k: Response())
    monkeypatch.setattr(enrichment.playback_guard, "active", lambda force=False: False)
    monkeypatch.setattr(enrichment.time, "sleep", lambda seconds: None)

    evidence = enrichment._analyze_target(
        "season-1", require_media_artifact=False
    )

    assert evidence["activity_completed"] is True


def test_one_item_analysis_failure_does_not_poison_healthy_sibling(monkeypatch):
    items = [
        {"token": "bad", "title": "Bad", "media_type": "movie"},
        {"token": "good", "title": "Good", "media_type": "movie"},
    ]
    targets = {
        "bad": {"rating_key": "1", "season_key": None},
        "good": {"rating_key": "2", "season_key": None},
    }
    monkeypatch.setattr(enrichment.playback_guard, "active", lambda force=False: False)

    def analyze(key, lease_id=None, require_media_artifact=True):
        if key == "1":
            raise RuntimeError("poison item")
        return {"activity_completed": True, "metadata_changed": True}

    monkeypatch.setattr(enrichment, "_analyze_target", analyze)

    evidence, failures = enrichment._analyze_batch(items, targets, "lease-a")

    assert set(evidence) == {"good"}
    assert failures == {"bad": "poison item"}


def test_dead_letter_cleanup_removes_media_and_release_binding(
        tmp_path, monkeypatch):
    monkeypatch.setattr(enrichment.config, "ENRICHMENT_CACHE_DIR", str(tmp_path))
    (tmp_path / "tok.media").write_bytes(b"staged")
    (tmp_path / "tok.media.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        enrichment.db, "get_enrichment_tokens_by_state",
        MagicMock(return_value=["tok"]),
    )

    assert enrichment._cleanup_dead_letter_staged_media() == 1

    assert not (tmp_path / "tok.media").exists()
    assert not (tmp_path / "tok.media.json").exists()


def test_preference_restore_failure_prevents_completion(tmp_path, monkeypatch):
    item = {
        "token": "tok", "title": "Movie", "media_type": "movie",
        "strm_path": "/data/media/movies/Movie/Movie.strm",
    }
    monkeypatch.setattr(enrichment, "enabled", lambda: True)
    monkeypatch.setattr(enrichment, "_runtime_ready", True)
    monkeypatch.setattr(enrichment, "_PROCESS_LOCK_PATH", str(tmp_path / "lock"))
    monkeypatch.setattr(enrichment, "recover_overlays", lambda: 0)
    enrichment.db.reset_interrupted_enrichment.return_value = 0
    enrichment.db.claim_enrichment_batch.return_value = [item]
    monkeypatch.setattr(enrichment.playback_guard, "defer", lambda name: False)
    monkeypatch.setattr(enrichment.playback_guard, "active", lambda force=False: False)
    monkeypatch.setattr(enrichment, "_download", lambda *a, **k: 100)
    monkeypatch.setattr(
        enrichment, "_resolve_batch",
        lambda batch: ({"tok": {"rating_key": "9", "season_key": None}}, {}),
    )
    monkeypatch.setattr(
        enrichment, "_overlay",
        lambda item: {"stub": Path("stub"), "backup": Path("backup"), "checksum": "x"},
    )
    monkeypatch.setattr(enrichment, "_restore_overlay", lambda overlay: None)
    monkeypatch.setattr(
        enrichment, "_analyze_batch",
        lambda batch, targets, lease_id: ({
            "tok": {"activity_completed": True, "metadata_changed": True}
        }, {}),
    )
    enrichment.db.set_enrichment_claim_state.return_value = 1
    monkeypatch.setattr(
        enrichment, "_set_analysis",
        MagicMock(side_effect=[None, None, RuntimeError("restore failed"), None]),
    )
    enrichment.db.fail_enrichment_claim.return_value = {
        "retry": 1, "dead_letter": 0,
    }

    result = enrichment.run_once()

    assert result["status"] == "failed"
    enrichment.db.complete_enrichment_claim.assert_not_called()
    enrichment.db.fail_enrichment_claim.assert_called()


def test_movie_worker_completes_only_with_evidence_and_restored_stub(
        tmp_path, monkeypatch):
    item = {
        "token": "movie-token", "title": "Movie (2024)",
        "media_type": "movie",
        "strm_path": "/data/media/movies/Movie (2024)/Movie (2024).strm",
    }
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "movie-token.media").write_bytes(b"verified-media")
    monkeypatch.setattr(enrichment.config, "ENRICHMENT_CACHE_DIR", str(cache))
    monkeypatch.setattr(enrichment, "enabled", lambda: True)
    monkeypatch.setattr(enrichment, "_runtime_ready", True)
    monkeypatch.setattr(enrichment, "_PROCESS_LOCK_PATH", str(tmp_path / "lock"))
    monkeypatch.setattr(enrichment, "recover_overlays", lambda: 0)
    enrichment.db.reset_interrupted_enrichment.return_value = 0
    enrichment.db.claim_enrichment_batch.return_value = [item]
    enrichment.db.set_enrichment_claim_state.return_value = 1
    enrichment.db.complete_enrichment_claim.return_value = True
    enrichment.db.fail_enrichment_claim.reset_mock()
    enrichment.db.complete_enrichment_claim.reset_mock()
    monkeypatch.setattr(enrichment.playback_guard, "defer", lambda name: False)
    monkeypatch.setattr(enrichment.playback_guard, "active", lambda force=False: False)
    monkeypatch.setattr(enrichment, "_download", lambda *a, **k: 14)
    monkeypatch.setattr(
        enrichment, "_resolve_batch",
        lambda batch: ({
            "movie-token": {"rating_key": "movie-9", "season_key": None}
        }, {}),
    )
    monkeypatch.setattr(
        enrichment, "_overlay",
        lambda row: {
            "stub": Path("stub"), "backup": Path("backup"), "checksum": "x"
        },
    )
    monkeypatch.setattr(enrichment, "_restore_overlay", lambda overlay: None)
    monkeypatch.setattr(
        enrichment, "_analyze_batch",
        lambda batch, targets, lease_id: ({
            "movie-token": {
                "activity_started": True,
                "activity_completed": True,
                "metadata_changed": True,
                "artifact": {"part_count": 1, "stream_count": 2},
            }
        }, {}),
    )
    monkeypatch.setattr(enrichment, "_set_analysis", lambda mode: None)

    result = enrichment.run_once()

    assert result == {
        "status": "complete", "items": 1, "failures": 0, "bytes": 14,
    }
    args = enrichment.db.complete_enrichment_claim.call_args.args
    assert len(args[0]) == 32
    assert args[1:3] == ("movie-token", "movie-9")
    assert args[3]["stub_restored"] is True
    assert args[3]["downloaded_bytes"] == 14
    assert not (cache / "movie-token.media").exists()
