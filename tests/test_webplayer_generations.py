"""HLS generation publication uses synthetic files and reaped fake processes."""
import hashlib
import subprocess
import threading
from types import SimpleNamespace

import pytest

from plugins.webplayer import web_player as wp


class Process:
    def __init__(self, command, **kwargs):
        self.command = command
        self.returncode = None
        self.terminations = self.kills = self.waits = 0
        # Model ffmpeg output; no media, executable, provider or network is used.
        for part in command:
            if isinstance(part, str) and part.endswith(".m3u8"):
                wp.Path(part).write_text("#EXTM3U\n")
            if isinstance(part, str) and "%05d" in part:
                for i in range(5):
                    wp.Path(part % i).write_bytes(b"synthetic segment")
    def poll(self):
        return self.returncode
    def terminate(self):
        self.terminations += 1
        self.returncode = -15
    def kill(self):
        self.kills += 1
        self.returncode = -9
    def wait(self, timeout):
        self.waits += 1
        assert self.returncode is not None
        return self.returncode


class ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, **ignored):
        self.target, self.args, self.kwargs = target, args, kwargs or {}
    def start(self):
        self.target(*self.args, **self.kwargs)


@pytest.fixture()
def playback(monkeypatch, tmp_path):
    monkeypatch.setattr(wp, "PLAYER_TMP_DIR", tmp_path / "player")
    monkeypatch.setattr(wp, "_sessions", {})
    monkeypatch.setattr(wp, "_generations", {})
    monkeypatch.setattr(wp, "_direct_sessions", {})
    monkeypatch.setattr(wp, "_owner_enabled", lambda uid: uid in (7, 8))
    monkeypatch.setattr(wp, "threading", SimpleNamespace(Thread=ImmediateThread))
    monkeypatch.setattr(wp.DirectSession, "start_heartbeat", lambda self: None)
    monkeypatch.setattr(wp.HLSSession, "start_heartbeat", lambda self: None)
    monkeypatch.setattr(wp.subprocess, "Popen", Process)
    monkeypatch.setattr(wp, "_extract_subtitles", lambda *args: None)
    def create(token="a"*32, owner=7, tracks=1):
        info = {"video_codec": "h264", "height": 720, "duration_s": 1000,
                "audio_tracks": [{"codec": "aac", "channels": 2, "language": "eng", "title": ""}
                                 for _ in range(tracks)], "subtitle_tracks": []}
        return wp._start_direct(token, info, "https://synthetic.invalid/not-requested",
                                torrent_id=10, file_id=0, owner_id=owner,
                                content_hash="c"*40, target=("tt1234567", "tv", 1, 2))
    return create


def digest(path):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.iterdir() if p.is_file()}


@pytest.mark.parametrize("tracks", [1, 2, 4])
def test_conversion_then_seek_publishes_fresh_ready_generation_without_overwriting(playback, tracks):
    direct = playback(tracks=tracks)
    wp._do_hls_conversion(direct.token, direct.file_info)
    first = wp.get_session(direct.token, owner_id=7)
    assert first is not None and first.seek_offset == 0
    before = digest(first.tmp_dir)
    assert len(first.generation) == 32 and first.proc.terminations == 0
    state = wp.seek_session(direct.token, 123.25, owner_id=7)
    second = wp.get_session(direct.token, owner_id=7)
    assert second is not first and second.tmp_dir != first.tmp_dir
    assert state["generation"] == second.generation and state["seek_offset_s"] == 123.25
    assert state["stream_url"].startswith(f"/stream/{direct.token}/hls/{second.generation}/")
    assert state["stream_url"].endswith("master.m3u8" if tracks > 1 else "playlist.m3u8")
    assert second.proc.command[second.proc.command.index("-ss")+1] == "123.250"
    assert first.proc.terminations == 1 and first.proc.waits == 1
    assert digest(first.tmp_dir) == before
    assert wp.get_generation(direct.token, first.generation, owner_id=7) is first
    assert wp.get_generation(direct.token, second.generation, owner_id=8) is None
    assert wp.get_generation(direct.token, "../outside", owner_id=7) is None
    assert direct.generation_attempts == 2 and not direct.operation_lock.locked()


def test_separate_tab_and_owner_keep_same_hash_playbacks_independent(playback):
    first, other = playback(), playback(token="b"*32, owner=8)
    for d in (first, other): wp._do_hls_conversion(d.token, d.file_info)
    foreign = wp.get_session(other.token, owner_id=8)
    before = digest(foreign.tmp_dir)
    assert wp.seek_session(first.token, 10, owner_id=8) is None
    assert wp.seek_session(first.token, 10, owner_id=7)
    assert digest(foreign.tmp_dir) == before and foreign.proc.terminations == 0
    assert wp.get_session(other.token, owner_id=8) is foreign


@pytest.mark.parametrize("position", [-1, True, "1", float("nan"), float("inf"), None, 604801])
def test_bad_seek_cannot_stop_process_or_allocate_generation(playback, position):
    d = playback(); wp._do_hls_conversion(d.token, d.file_info)
    old = wp.get_session(d.token, owner_id=7)
    with pytest.raises(ValueError): wp.seek_session(d.token, position, owner_id=7)
    assert old.proc.terminations == 0 and d.generation_attempts == 1


def test_duration_and_generation_cap_fail_before_stopping_current_output(playback, monkeypatch):
    d = playback(); wp._do_hls_conversion(d.token, d.file_info)
    old = wp.get_session(d.token, owner_id=7)
    with pytest.raises(ValueError): wp.seek_session(d.token, 1001, owner_id=7)
    monkeypatch.setattr(wp, "MAX_HLS_GENERATIONS", 1)
    assert wp.seek_session(d.token, 50, owner_id=7) is None
    assert old.proc.terminations == 0 and d.generation_attempts == 1
    assert not d.operation_lock.locked() and not old.seeking


def test_no_new_generation_is_visible_until_all_output_is_ready(playback, monkeypatch):
    d = playback(); wp._do_hls_conversion(d.token, d.file_info)
    old = wp.get_session(d.token, owner_id=7)
    entered, release = threading.Event(), threading.Event()
    real_start = wp._start_hls
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_start(*args, **kwargs)
    monkeypatch.setattr(wp, "_start_hls", delayed)
    result = []
    thread = threading.Thread(target=lambda: result.append(wp.seek_session(d.token, 70, owner_id=7)))
    thread.start()
    try:
        assert entered.wait(5)
        assert wp.get_session(d.token, owner_id=7) is old
        assert len(wp._generations) == 1
        assert wp.seek_session(d.token, 90, owner_id=7) is None
        assert d.generation_attempts == 2
    finally:
        release.set(); thread.join(5)
    assert not thread.is_alive() and result[0]["seek_offset_s"] == 70
    assert len(wp._generations) == 2 and not d.operation_lock.locked()


@pytest.mark.parametrize("failure", ["not-ready", "failed-after-segments", "missing-playlist", "audio-not-ready"])
def test_unusable_seek_output_is_reaped_not_published_and_old_evidence_retained(playback, monkeypatch, failure):
    d = playback(tracks=2 if failure == "audio-not-ready" else 1)
    wp._do_hls_conversion(d.token, d.file_info)
    old = wp.get_session(d.token, owner_id=7); before = digest(old.tmp_dir)
    processes = []
    def spawn(*args, **kwargs):
        proc = Process(*args, **kwargs); processes.append(proc)
        if failure == "failed-after-segments": proc.returncode = 1
        if failure == "missing-playlist":
            for arg in proc.command:
                if isinstance(arg, str) and arg.endswith("playlist.m3u8"): wp.Path(arg).unlink()
        return proc
    monkeypatch.setattr(wp.subprocess, "Popen", spawn)
    original_wait = wp._wait_segments_pattern
    def wait(path, pattern, count, timeout, proc):
        if failure == "not-ready" or (failure == "audio-not-ready" and pattern.startswith("seg_a1_")):
            return False
        return original_wait(path, pattern, count, timeout, proc)
    monkeypatch.setattr(wp, "_wait_segments_pattern", wait)
    assert wp.seek_session(d.token, 30, owner_id=7) is None
    assert len(processes) == 1 and processes[0].poll() is not None and processes[0].waits == 1
    assert wp.get_session(d.token, owner_id=7) is old and len(wp._generations) == 1
    assert digest(old.tmp_dir) == before and not old.seeking and not d.operation_lock.locked()


def test_native_policy_revocation_during_seek_reaps_unpublished_output(playback, monkeypatch):
    d = playback(); wp._do_hls_conversion(d.token, d.file_info)
    old = wp.get_session(d.token, owner_id=7); before = digest(old.tmp_dir)
    original = wp._start_hls; new = []
    def start(*args, **kwargs):
        result = original(*args, **kwargs); new.append(result)
        monkeypatch.setattr(wp, "_owner_enabled", lambda uid: False)
        return result
    monkeypatch.setattr(wp, "_start_hls", start)
    assert wp.seek_session(d.token, 20, owner_id=7) is None
    assert new[0].proc.terminations == 1 and new[0].proc.waits == 1
    assert wp.get_session(d.token, owner_id=7) is old and digest(old.tmp_dir) == before


def test_duplicate_conversion_does_not_start_second_process(playback, monkeypatch):
    d = playback(); calls = []
    def spawn(*args, **kwargs):
        calls.append(1); return Process(*args, **kwargs)
    monkeypatch.setattr(wp.subprocess, "Popen", spawn)
    assert wp.start_hls_conversion(d.token, owner_id=8) is False
    assert wp.start_hls_conversion(d.token, owner_id=7) is True
    assert wp.start_hls_conversion(d.token, owner_id=7) is True
    assert len(calls) == 1 and d.generation_attempts == 1 and not d.converting


def test_failed_initial_conversion_returns_safe_error_and_releases_operation(playback, monkeypatch):
    d = playback()
    monkeypatch.setattr(wp, "_wait_segments_pattern", lambda *a, **k: False)
    wp._do_hls_conversion(d.token, d.file_info)
    assert wp.get_session(d.token, owner_id=7) is None and not wp._generations
    assert d.conversion_error == "Conversion failed. Please reopen the player."
    assert not d.converting and not d.operation_lock.locked()
    assert wp.start_hls_conversion(d.token, owner_id=7) is False


def test_process_timeout_uses_kill_and_reap():
    class Slow(Process):
        def terminate(self): self.terminations += 1
        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1: raise subprocess.TimeoutExpired("synthetic", timeout)
            assert self.returncode == -9
    proc = Slow([])
    wp._stop_process(proc)
    assert (proc.terminations, proc.kills, proc.waits) == (1, 1, 2)
