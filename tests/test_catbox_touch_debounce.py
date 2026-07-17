"""
Unit tests for catbox._touch_debounced(): collapsing the play-counter write on
materialize's cache-hit path.

That path runs on every byte-range request, so one viewer fires dozens of
touch_virtual_item() calls a minute, each a synchronous UPDATE + commit. SQLite
takes one writer at a time, so under concurrent playback they serialize against
every other session's writes to record something that only needs per-session
precision.
"""
import os
import sys

import pytest

os.environ.setdefault("TORBOX_API_KEY", "test")
os.environ.setdefault("MEDIA_PATH", "/tmp/mycelium-test-media")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import catbox  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_touch_cache():
    catbox._touch_cache.clear()
    yield
    catbox._touch_cache.clear()


@pytest.fixture
def writes(monkeypatch):
    seen = []
    monkeypatch.setattr(catbox.db, "touch_virtual_item", lambda t: seen.append(t))
    return seen


def test_writes_once_per_window(writes):
    for _ in range(5):
        catbox._touch_debounced("tok1")
    assert writes == ["tok1"]


def test_debounce_is_per_token(writes):
    catbox._touch_debounced("tok1")
    catbox._touch_debounced("tok2")
    catbox._touch_debounced("tok1")
    assert writes == ["tok1", "tok2"]


def test_writes_again_after_window(writes):
    catbox._touch_debounced("tok1")
    catbox._touch_cache.clear()  # stands in for the 60s TTL expiring
    catbox._touch_debounced("tok1")
    assert writes == ["tok1", "tok1"]
