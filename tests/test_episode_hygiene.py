"""Synthetic files and independent processes; no scheduler, DB or provider I/O."""
import ast
import fcntl
import logging
import os
import subprocess
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

import episode_hygiene as hygiene


LOCK_PROBE = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_WRONLY)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(75)
finally:
    os.close(fd)
"""


def probe(path):
    return subprocess.run([sys.executable, "-c", LOCK_PROBE, str(path)],
                          capture_output=True, timeout=5, check=False).returncode


@pytest.fixture
def fence(tmp_path, monkeypatch):
    locks = tmp_path / "host-locks"
    locks.mkdir()
    for name in hygiene._LOCK_NAMES:
        (locks / name).write_bytes(b"existing host lock\n")
    monkeypatch.setattr(hygiene.config, "EPISODE_HYGIENE_HOST_LOCK_DIR", str(locks))
    monkeypatch.setattr(hygiene.config, "EPISODE_HYGIENE_TIMEZONE", "America/Phoenix")
    monkeypatch.setattr(hygiene.config, "EPISODE_HYGIENE_NIGHTLY_START", "01:00")
    monkeypatch.setattr(hygiene.config, "EPISODE_HYGIENE_NIGHTLY_END", "06:00")
    monkeypatch.setattr(hygiene.config, "EPISODE_HYGIENE_LIMIT", 500)
    monkeypatch.setattr(hygiene.config, "EPISODE_HYGIENE_INTERVAL_HOURS", 168)
    set_time(monkeypatch, "2026-11-13T19:00:00+00:00")
    calls = []

    def phantom(**kwargs):
        calls.append(("phantom", kwargs))
        assert all(probe(locks / name) == 75 for name in hygiene._LOCK_NAMES)
        return {"status": "complete", "quarantined": []}

    def numbering(**kwargs):
        calls.append(("numbering", kwargs))
        assert all(probe(locks / name) == 75 for name in hygiene._LOCK_NAMES)
        return {"status": "complete", "duplicates_removed": 0, "renumbered": 0}

    monkeypatch.setitem(sys.modules, "phantom_cleanup", types.SimpleNamespace(cleanup=phantom))
    monkeypatch.setitem(sys.modules, "episode_scheme", types.SimpleNamespace(reconcile=numbering))
    monkeypatch.setitem(sys.modules, "playback_guard", types.SimpleNamespace(active=lambda force=False: False))
    yield locks, calls
    assert all(probe(locks / name) == 0 for name in hygiene._LOCK_NAMES
               if (locks / name).is_file() and not (locks / name).is_symlink())


def set_time(monkeypatch, value):
    instant = datetime.fromisoformat(value)
    class Clock:
        @staticmethod
        def now(zone):
            return instant.astimezone(zone)
    monkeypatch.setattr(hygiene, "datetime", Clock)


def test_both_passes_hold_real_host_style_locks_and_release(fence):
    locks, calls = fence
    before = {name: ((locks / name).stat().st_ino, (locks / name).read_bytes())
              for name in hygiene._LOCK_NAMES}
    assert hygiene.run()["status"] == "complete"
    assert calls == [("phantom", {"dry_run": False}),
                     ("numbering", {"dry_run": False, "limit": 500})]
    assert before == {name: ((locks / name).stat().st_ino, (locks / name).read_bytes())
                      for name in hygiene._LOCK_NAMES}


@pytest.mark.parametrize("name", hygiene._LOCK_NAMES)
def test_busy_host_lock_defers_without_cleanup_or_wait(fence, name, caplog):
    locks, calls = fence
    with (locks / name).open("w") as host:
        fcntl.flock(host, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with caplog.at_level(logging.INFO):
            assert hygiene.run() == {"status": "deferred", "reason": "host_lock_busy"}
        assert calls == []
    assert "next scheduled interval, no retry" in caplog.text


def test_read_only_handles_work_and_are_not_inherited(fence):
    with hygiene._host_fence():
        found = []
        for fd in range(3, 256):
            try:
                name = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                continue
            if name.endswith(hygiene._LOCK_NAMES):
                found.append(fd)
                assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
                assert not os.get_inheritable(fd)
        assert len(found) == 2


@pytest.mark.parametrize("instant,blocked", [
    ("2026-11-13T07:59:59+00:00", False),
    ("2026-11-13T08:00:00+00:00", True),
    ("2026-11-13T12:59:59+00:00", True),
    ("2026-11-13T13:00:00+00:00", False),
])
def test_phoenix_nightly_window_boundaries(fence, monkeypatch, instant, blocked):
    _, calls = fence
    set_time(monkeypatch, instant)
    result = hygiene.run()
    assert result["status"] == ("deferred" if blocked else "complete")
    if blocked:
        assert result["reason"] == "nightly_window" and calls == []


@pytest.mark.parametrize("key,value", [
    ("EPISODE_HYGIENE_TIMEZONE", "No/SuchZone"),
    ("EPISODE_HYGIENE_NIGHTLY_START", "1:00"),
    ("EPISODE_HYGIENE_NIGHTLY_END", "25:00"),
    ("EPISODE_HYGIENE_NIGHTLY_END", "01:00"),
])
def test_unknown_window_never_runs(fence, monkeypatch, key, value):
    _, calls = fence
    monkeypatch.setattr(hygiene.config, key, value)
    assert hygiene.run() == {"status": "deferred", "reason": "nightly_window_unknown"}
    assert calls == []


def test_window_can_span_midnight(fence, monkeypatch):
    _, calls = fence
    monkeypatch.setattr(hygiene.config, "EPISODE_HYGIENE_NIGHTLY_START", "22:00")
    monkeypatch.setattr(hygiene.config, "EPISODE_HYGIENE_NIGHTLY_END", "02:00")
    set_time(monkeypatch, "2026-11-13T08:00:00+00:00")
    assert hygiene.run()["reason"] == "nightly_window" and calls == []


def test_window_rechecked_after_lock_acquisition(fence, monkeypatch):
    _, calls = fence
    checks = []
    def clock():
        checks.append(True)
        if len(checks) == 2:
            raise hygiene.Deferred("nightly_window")
    monkeypatch.setattr(hygiene, "_outside_nightly_window", clock)
    assert hygiene.run()["reason"] == "nightly_window" and calls == []


@pytest.mark.parametrize("name", hygiene._LOCK_NAMES)
def test_missing_lock_is_not_created(fence, name):
    locks, calls = fence
    (locks / name).unlink()
    assert hygiene.run()["reason"] == "host_lock_unavailable"
    assert calls == [] and not (locks / name).exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_nonregular_or_aliased_lock_refuses(fence, kind):
    locks, calls = fence
    path = locks / hygiene._LOCK_NAMES[1]
    path.unlink()
    if kind == "symlink":
        path.symlink_to(locks / hygiene._LOCK_NAMES[0])
    elif kind == "hardlink":
        os.link(locks / hygiene._LOCK_NAMES[0], path)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    assert hygiene.run()["reason"] == "host_lock_identity_unknown"
    assert calls == []


def test_replaced_lock_is_detected(fence, monkeypatch):
    locks, calls = fence
    original = fcntl.flock
    replaced = False
    def flock(fd, operation):
        nonlocal replaced
        result = original(fd, operation)
        if not replaced:
            replaced = True
            new = locks / "replacement"
            new.write_bytes(b"other inode")
            new.replace(locks / hygiene._LOCK_NAMES[0])
        return result
    monkeypatch.setattr(hygiene.fcntl, "flock", flock)
    assert hygiene.run()["reason"] == "host_lock_identity_changed" and calls == []


def test_playback_gate_retained(fence, monkeypatch):
    _, calls = fence
    seen = []
    def active(force=False):
        seen.append(force)
        return True
    monkeypatch.setitem(sys.modules, "playback_guard", types.SimpleNamespace(active=active))
    assert hygiene.run() == {"status": "deferred", "reason": "playback"}
    assert seen == [True] and calls == []


@pytest.mark.parametrize("stage", ["phantom_cleanup", "episode_scheme"])
@pytest.mark.parametrize("error", [OSError, RuntimeError, KeyboardInterrupt])
def test_cleanup_errors_release_locks_and_do_not_hide_failure(fence, monkeypatch, stage, error):
    def fail(**_kwargs):
        raise error("synthetic failure")
    member = "cleanup" if stage == "phantom_cleanup" else "reconcile"
    monkeypatch.setitem(sys.modules, stage, types.SimpleNamespace(**{member: fail}))
    with pytest.raises(error):
        hygiene.run()


def test_real_scheduler_callback_is_fenced_without_reschedule(fence):
    locks, calls = fence
    source = ast.parse((Path(__file__).parents[1] / "app.py").read_text())
    block = next(node for node in ast.walk(source) if isinstance(node, ast.If)
                 and isinstance(node.test, ast.Compare)
                 and isinstance(node.test.left, ast.Attribute)
                 and node.test.left.attr == "EPISODE_HYGIENE_INTERVAL_HOURS")
    scheduled = []
    scheduler = types.SimpleNamespace(add_job=lambda callback, **kw: scheduled.append((callback, kw)))
    exec(compile(ast.Module(body=[block], type_ignores=[]), "scheduler-registration", "exec"),
         {"cfg": hygiene.config, "scheduler": scheduler, "log": logging.getLogger("test")})
    assert len(scheduled) == 1
    callback, spec = scheduled[0]
    assert spec == {"trigger": "interval", "hours": 168, "id": "episode_hygiene",
                    "next_run_time": None, "max_instances": 1}
    with (locks / hygiene._LOCK_NAMES[0]).open("w") as host:
        fcntl.flock(host, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert callback()["reason"] == "host_lock_busy"
    assert len(scheduled) == 1 and calls == []
