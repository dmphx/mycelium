import threading
import time

import spore_readthrough as cache


def _reset(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_root", tmp_path)
    monkeypatch.setattr(cache, "_enabled", True)
    monkeypatch.setattr(cache, "BLOCK_SIZE", 16)
    monkeypatch.setattr(cache, "BUDGET", 1024)
    with cache._state_lock:
        cache._flights.clear()
        cache._title_locks.clear()
    cache.init(tmp_path)


def test_second_reader_reuses_completed_block(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    calls = []

    def fetch(start, end):
        calls.append((start, end))
        return bytes(range(start, end + 1))

    first = cache.read_range("title-a", 3, 10, 32, fetch)
    second = cache.read_range("title-a", 4, 9, 32, fetch)

    assert first == bytes(range(3, 11))
    assert second == bytes(range(4, 10))
    assert calls == [(0, 15)]


def test_concurrent_same_block_has_one_upstream_fetch(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    calls = []
    results = []

    def fetch(start, end):
        calls.append((start, end))
        entered.set()
        assert release.wait(2)
        return b"X" * (end - start + 1)

    def reader():
        results.append(cache.read_range("title-b", 2, 7, 32, fetch))

    one = threading.Thread(target=reader)
    two = threading.Thread(target=reader)
    one.start()
    assert entered.wait(1)
    two.start()
    time.sleep(0.05)
    release.set()
    one.join(2)
    two.join(2)

    assert results == [b"X" * 6, b"X" * 6]
    assert calls == [(0, 15)]


def test_different_blocks_for_one_title_are_serialized(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    state_lock = threading.Lock()
    active = 0
    peak = 0

    def fetch(start, end):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with state_lock:
            active -= 1
        return b"Y" * (end - start + 1)

    threads = [
        threading.Thread(target=cache.read_range,
                         args=("title-c", start, start + 3, 48, fetch))
        for start in (0, 16, 32)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)

    assert peak == 1


def test_wait_capacity_returns_retryable_error_instead_of_starving_threads(
        tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr(cache, "_request_slots", threading.BoundedSemaphore(1))
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def fetch(start, end):
        entered.set()
        assert release.wait(2)
        return b"Z" * (end - start + 1)

    leader = threading.Thread(
        target=cache.read_range,
        args=("title-d", 0, 3, 32, fetch),
    )
    leader.start()
    assert entered.wait(1)
    try:
        cache.read_range("title-d", 0, 3, 32, fetch)
    except Exception as exc:
        errors.append(exc)
    release.set()
    leader.join(2)

    assert len(errors) == 1
    assert isinstance(errors[0], cache.CacheWaitError)
    assert errors[0].status == 503
