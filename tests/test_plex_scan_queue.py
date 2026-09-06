from concurrent.futures import ThreadPoolExecutor
import json
from datetime import datetime, timedelta, timezone

import pytest

import media_servers
import plex_scan_queue as queue


def test_enqueue_deduplicates_pending_request_and_claim_increments_attempt(tmp_path):
    first = queue.enqueue(
        tmp_path, "analyze", "8", "/mnt/library/shows/Example/Season 01"
    )
    second = queue.enqueue(
        tmp_path, "analyze", "8", "/mnt/library/shows/Example/Season 01"
    )
    assert first == second
    assert queue.counts(tmp_path)["ready"] == 1

    payload = queue.claim(tmp_path, "worker-a", lease_seconds=60)

    assert payload["request_id"] == first
    assert payload["mode"] == "analyze"
    assert payload["attempts"] == 1
    assert payload["lease_id"] == "worker-a"
    assert queue.claim(tmp_path, "worker-b") is None


def test_concurrent_producers_create_one_pending_request(tmp_path):
    def enqueue(_):
        return queue.enqueue(
            tmp_path, "scan", "8", "/mnt/library/shows/Example"
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        request_ids = list(executor.map(enqueue, range(24)))

    assert len(set(request_ids)) == 1
    assert queue.counts(tmp_path)["ready"] == 1


def test_ack_requires_lease_owner_and_retains_done_evidence(tmp_path):
    request_id = queue.enqueue(
        tmp_path, "scan", "7", "/mnt/library/movies/Example"
    )
    queue.claim(tmp_path, "worker-a")

    assert queue.ack(tmp_path, request_id, "worker-b") is False
    assert queue.ack(tmp_path, request_id, "worker-a") is True
    assert queue.counts(tmp_path) == {
        "ready": 0, "working": 0, "done": 1, "dead": 0,
    }


def test_lease_can_be_renewed_only_by_current_owner(tmp_path):
    request_id = queue.enqueue(
        tmp_path, "scan", "7", "/mnt/library/movies/Example"
    )
    queue.claim(tmp_path, "worker-a", lease_seconds=60)

    assert queue.renew(tmp_path, request_id, "worker-b") is False
    assert queue.renew(tmp_path, request_id, "worker-a", lease_seconds=120) is True


def test_nack_retries_then_dead_letters_without_deleting_failure(tmp_path):
    request_id = queue.enqueue(
        tmp_path, "remove", "8", "/mnt/library/shows/Example/Season 02"
    )
    for attempt in range(1, 4):
        payload = queue.claim(
            tmp_path, "worker-a", max_attempts=3, retry_base_seconds=1
        )
        assert payload["attempts"] == attempt
        state = queue.nack(
            tmp_path, request_id, "worker-a", "scanner failed",
            max_attempts=3, retry_base_seconds=1,
        )
        if attempt < 3:
            assert state == "retry"
            ready = next((tmp_path / "ready").glob("*.json"))
            data = json.loads(ready.read_text())
            data["next_attempt_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat().replace("+00:00", "Z")
            ready.write_text(json.dumps(data) + "\n")
        else:
            assert state == "dead"
    assert queue.counts(tmp_path)["dead"] == 1
    dead = next((tmp_path / "dead").glob("*.json"))
    assert json.loads(dead.read_text())["last_error"] == "scanner failed"


def test_retry_limit_is_hard_capped_at_five(tmp_path):
    request_id = queue.enqueue(
        tmp_path, "scan", "8", "/mnt/library/shows/Bounded"
    )
    for attempt in range(1, 6):
        assert queue.claim(
            tmp_path, "worker-a", max_attempts=99, retry_base_seconds=1
        )["attempts"] == attempt
        outcome = queue.nack(
            tmp_path, request_id, "worker-a", "failed",
            max_attempts=99, retry_base_seconds=1,
        )
        if attempt < 5:
            ready = next((tmp_path / "ready").glob("*.json"))
            payload = json.loads(ready.read_text(encoding="utf-8"))
            payload["next_attempt_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat().replace("+00:00", "Z")
            ready.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            assert outcome == "retry"
        else:
            assert outcome == "dead"


def test_expired_worker_is_recovered_with_backoff(tmp_path):
    queue.enqueue(tmp_path, "scan", "8", "/mnt/library/shows/Example")
    payload = queue.claim(tmp_path, "lost-worker", lease_seconds=30)
    working = tmp_path / "working" / f"{payload['request_id']}.json"
    data = json.loads(working.read_text())
    data["lease_expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat().replace("+00:00", "Z")
    working.write_text(json.dumps(data) + "\n")

    assert queue.recover_expired(tmp_path) == 1
    assert queue.counts(tmp_path)["ready"] == 1


def test_claim_crash_state_keeps_consumed_attempt_before_retry(tmp_path):
    request_id = queue.enqueue(
        tmp_path, "scan", "8", "/mnt/library/shows/Example"
    )
    ready = tmp_path / "ready" / f"{request_id}.json"
    payload = json.loads(ready.read_text(encoding="utf-8"))
    payload["attempts"] = 1
    payload["lease_id"] = "crashed-worker"
    payload["lease_expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat().replace("+00:00", "Z")
    ready.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    assert queue.claim(tmp_path, "worker-a", retry_base_seconds=1) is None
    recovered = json.loads(ready.read_text(encoding="utf-8"))
    assert recovered["attempts"] == 1
    assert recovered["lease_id"] is None
    recovered["next_attempt_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat().replace("+00:00", "Z")
    ready.write_text(json.dumps(recovered) + "\n", encoding="utf-8")

    assert queue.claim(tmp_path, "worker-a")["attempts"] == 2


def test_future_retry_does_not_block_another_ready_request(tmp_path):
    queue.enqueue(tmp_path, "scan", "8", "/mnt/library/shows/First")
    queue.enqueue(tmp_path, "scan", "8", "/mnt/library/shows/Second")
    ready = sorted((tmp_path / "ready").glob("*.json"))
    delayed = json.loads(ready[0].read_text(encoding="utf-8"))
    delayed["next_attempt_at"] = (
        datetime.now(timezone.utc) + timedelta(hours=1)
    ).isoformat().replace("+00:00", "Z")
    ready[0].write_text(json.dumps(delayed) + "\n", encoding="utf-8")

    payload = queue.claim(tmp_path, "worker-a")

    assert payload is not None
    assert payload["request_id"] != delayed["request_id"]


def test_malformed_ready_request_is_preserved_in_dead_letter(tmp_path):
    queue.counts(tmp_path)
    invalid = tmp_path / "ready" / "000-invalid.json"
    invalid.write_text("not json\n", encoding="utf-8")

    assert queue.claim(tmp_path, "worker-a") is None
    assert not invalid.exists()
    assert queue.counts(tmp_path)["dead"] == 1
    assert next((tmp_path / "dead").glob("invalid.*.json")).read_text() == "not json\n"


def test_spool_mode_preserves_analyze_operation_from_producer(tmp_path, monkeypatch):
    monkeypatch.setattr(media_servers, "PLEX_QUEUE_MODE", "spool")
    monkeypatch.setattr(media_servers, "PLEX_SPOOL_DIR", str(tmp_path))

    media_servers._queue_plex([{
        "mode": "analyze",
        "section": "8",
        "path": "/mnt/library/shows/Example/Season 01",
    }])

    payload = queue.claim(tmp_path, "worker-a")
    assert payload["mode"] == "analyze"


def test_legacy_producer_format_remains_backward_compatible(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.tsv"
    monkeypatch.setattr(media_servers, "PLEX_QUEUE_MODE", "legacy")
    monkeypatch.setattr(media_servers, "PLEX_QUEUE", str(legacy))

    media_servers._queue_plex([{
        "mode": "scan",
        "section": "8",
        "path": "/mnt/library/shows/Example",
    }])

    assert legacy.read_text(encoding="utf-8") == (
        "8\t/mnt/library/shows/Example\n"
    )


def test_legacy_producer_holds_analyze_for_durable_consumer(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.tsv"
    spool = tmp_path / "spool"
    monkeypatch.setattr(media_servers, "PLEX_QUEUE_MODE", "legacy")
    monkeypatch.setattr(media_servers, "PLEX_QUEUE", str(legacy))
    monkeypatch.setattr(media_servers, "PLEX_SPOOL_DIR", str(spool))

    failed = media_servers._queue_plex([{
        "mode": "analyze",
        "section": "8",
        "path": "/mnt/library/shows/Example",
    }])

    assert not legacy.exists()
    assert failed == []
    assert queue.claim(spool, "worker-a")["mode"] == "analyze"


def test_flush_reschedules_spool_enqueue_failure(monkeypatch):
    class Timer:
        def __init__(self, delay, target):
            self.delay = delay
            self.target = target
            self.started = False
            self.daemon = False

        def start(self):
            self.started = True

    with media_servers._lock:
        media_servers._pending.clear()
        media_servers._pending.add(("series", "Example", "analyze"))
        media_servers._timer = None
    monkeypatch.setattr(media_servers, "_scan_jellyfin", lambda updates: None)
    monkeypatch.setattr(media_servers, "_plex_api_scan", lambda *a, **k: False)
    monkeypatch.setattr(media_servers, "_queue_plex", lambda requests: requests)
    monkeypatch.setattr(media_servers.threading, "Timer", Timer)

    media_servers._flush()

    with media_servers._lock:
        assert media_servers._pending == {("series", "Example", "analyze")}
        assert media_servers._timer.started is True
        media_servers._pending.clear()
        media_servers._timer = None


def test_flush_reschedules_unexpected_spool_exception(monkeypatch):
    class Timer:
        def __init__(self, delay, target):
            self.delay = delay
            self.target = target
            self.started = False
            self.daemon = False

        def start(self):
            self.started = True

    with media_servers._lock:
        media_servers._pending.clear()
        media_servers._pending.add(("movies", "Example", "scan"))
        media_servers._timer = None
    monkeypatch.setattr(media_servers, "_scan_jellyfin", lambda updates: None)
    monkeypatch.setattr(media_servers, "_plex_api_scan", lambda *a, **k: False)
    monkeypatch.setattr(
        media_servers, "_queue_plex",
        lambda requests: (_ for _ in ()).throw(OSError("spool unavailable")),
    )
    monkeypatch.setattr(media_servers.threading, "Timer", Timer)

    media_servers._flush()

    with media_servers._lock:
        assert media_servers._pending == {("movies", "Example", "scan")}
        assert media_servers._timer.started is True
        media_servers._pending.clear()
        media_servers._timer = None


def test_analyze_never_degrades_to_refresh_api(monkeypatch):
    called = []
    monkeypatch.setattr(media_servers.requests, "get", lambda *a, **k: called.append(1))

    assert media_servers._plex_api_scan(
        "8", "/mnt/library/shows/Example", "analyze"
    ) is False
    assert called == []


def test_spool_mode_never_bypasses_acknowledged_delivery(monkeypatch):
    called = []
    monkeypatch.setattr(media_servers, "PLEX_QUEUE_MODE", "spool")
    monkeypatch.setattr(media_servers.requests, "get", lambda *a, **k: called.append(1))

    assert media_servers._plex_api_scan(
        "8", "/mnt/library/shows/Example", "scan"
    ) is False
    assert called == []


@pytest.mark.parametrize(
    "mode,section,path",
    [
        ("wrong", "8", "/mnt/library/shows/Example"),
        ("scan", "tv", "/mnt/library/shows/Example"),
        ("scan", "8", "/tmp/not-library/Example"),
        ("scan", "8", "/mnt/library/../private/Example"),
        ("scan", "8", "/mnt/library/shows/Bad\nPath"),
    ],
)
def test_invalid_requests_are_rejected(tmp_path, mode, section, path):
    with pytest.raises(ValueError):
        queue.enqueue(tmp_path, mode, section, path)
