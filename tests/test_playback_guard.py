import playback_guard as guard


def _reset():
    with guard._lock:
        guard._cached_until = 0.0
        guard._cached_active = False
        guard._last_defer_log.clear()


def test_recent_play_keeps_gate_closed_after_live_session_disappears(monkeypatch):
    _reset()
    monkeypatch.setattr(guard, "_plex_active", lambda: False)
    monkeypatch.setattr(guard, "_recent_play", lambda: True)
    assert guard.active(force=True) is True


def test_no_live_or_recent_play_leaves_gate_open(monkeypatch):
    _reset()
    monkeypatch.setattr(guard, "_plex_active", lambda: False)
    monkeypatch.setattr(guard, "_recent_play", lambda: False)
    assert guard.active(force=True) is False


def test_live_query_failure_uses_recent_play_fallback(monkeypatch):
    _reset()
    monkeypatch.setattr(guard, "_plex_active", lambda: None)
    monkeypatch.setattr(guard, "_recent_play", lambda: True)
    assert guard.active(force=True) is True
