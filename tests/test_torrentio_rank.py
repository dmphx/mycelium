import importlib.util
import os
import sys

os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


def _load_real(name, extra_deps=None):
    """Load a fresh real module from source, isolated from the MagicMocks that
    conftest.py installs in sys.modules for the strm_generator test group.

    torrentio.rank_streams() does a lazy `import settings` at call time, and
    conftest left a MagicMock there, so settings.get() returned truthy garbage
    instead of the passed defaults and the undersize fallback dropped a
    candidate. We load the REAL settings (wired to the REAL db) here, and the
    fixture below installs it for the duration of each test, then restores the
    mock so a later-collected strm test is unaffected.
    """
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(os.path.dirname(__file__), "..", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    inject = {name: mod, **(extra_deps or {})}
    saved = {k: sys.modules.get(k) for k in inject}
    sys.modules.update(inject)
    try:
        spec.loader.exec_module(mod)
    finally:
        for k, prev in saved.items():
            if prev is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = prev
    return mod


_db = _load_real("db")
_settings = _load_real("settings", extra_deps={"db": _db})

import torrentio
from torrentio import TorrentioStream


@pytest.fixture(autouse=True)
def _real_settings(tmp_path, monkeypatch):
    # Back the real settings with a fresh empty SQLite db so every get() returns
    # its config default, then expose it under sys.modules["settings"] for
    # torrentio's lazy import. Restored to conftest's mock on teardown.
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "torrentio-test.db"))
    _db.init()
    monkeypatch.setitem(sys.modules, "settings", _settings)
    yield


def _stream(name, quality, size_gb, seeders=10):
    return TorrentioStream(
        name=name, title=name, info_hash="a" * 40, quality=quality,
        seeders=seeders, size_gb=size_gb, is_season_pack=False,
    )


def test_undersized_fake_quality_release_is_rejected():
    # A 90-minute movie claiming 2160p but only 500MB  -  physically impossible,
    # almost certainly a mislabeled cam or trailer.
    fake = _stream("Movie.2024.2160p.WEB-DL", "2160p", size_gb=0.5)
    real = _stream("Movie.2024.1080p.WEB-DL", "1080p", size_gb=2.0)
    ranked = torrentio.rank_streams([fake, real], override={"runtime_minutes": 90})
    hashes_kept = [s.name for s in ranked]
    assert "Movie.2024.2160p.WEB-DL" not in hashes_kept
    assert "Movie.2024.1080p.WEB-DL" in hashes_kept


def test_undersized_filter_scales_with_runtime():
    # A short (40-minute) title needs proportionally less data than a 90-minute
    # baseline  -  the same 1080p size that would fail for a 90-min film should
    # pass for something this short.
    short_ok = _stream("Special.2024.1080p.WEB-DL", "1080p", size_gb=0.6)
    ranked = torrentio.rank_streams([short_ok], override={"runtime_minutes": 40})
    assert len(ranked) == 1


def test_unknown_size_is_never_penalized():
    unknown = _stream("Movie.2024.2160p.WEB-DL", "2160p", size_gb=0.0)
    ranked = torrentio.rank_streams([unknown], override={"runtime_minutes": 90})
    assert len(ranked) == 1


def test_no_runtime_known_skips_the_filter():
    fake = _stream("Movie.2024.2160p.WEB-DL", "2160p", size_gb=0.1)
    ranked = torrentio.rank_streams([fake], override={})
    assert len(ranked) == 1


def test_all_candidates_undersized_falls_back_to_allowing_them():
    fake1 = _stream("Movie.2024.2160p.WEB-DL", "2160p", size_gb=0.1)
    fake2 = _stream("Movie.2024.1080p.WEB-DL", "1080p", size_gb=0.05)
    ranked = torrentio.rank_streams([fake1, fake2], override={"runtime_minutes": 90})
    assert len(ranked) == 2


def test_configured_url_only_results_fall_back_to_plain_endpoint(monkeypatch, caplog):
    class Response:
        status_code = 200
        headers = {}

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return Response({"streams": [{"url": "https://debrid.example/video"}]})
        return Response({"streams": [{
            "infoHash": "b" * 40,
            "name": "Torrentio 1080p",
            "title": "Show.S01E01.1080p.WEB-DL",
        }]})

    monkeypatch.setattr(torrentio, "TORRENTIO_OPTS", "private-option")
    monkeypatch.setattr(torrentio.requests, "get", fake_get)

    streams = torrentio.fetch_streams("series", "tt1234567", season=1, episode=1)

    assert [stream.info_hash for stream in streams] == ["b" * 40]
    assert len(calls) == 2
    assert "private-option" in calls[0]
    assert "private-option" not in calls[1]
    assert "private-option" not in caplog.text


def test_web_playback_profile_prefers_h264_aac_over_hevc_truehd():
    _settings.set("PLAYBACK_PROFILE", "web")
    hevc = _stream("Show.1080p.WEB-DL.HEVC.TrueHD", "1080p", 2.0, seeders=50)
    hevc.info_hash = "a" * 40
    h264 = _stream("Show.1080p.WEB-DL.H264.AAC.MP4", "1080p", 2.0, seeders=10)
    h264.info_hash = "b" * 40

    ranked = torrentio.rank_streams([hevc, h264])

    assert ranked[0].info_hash == "b" * 40
