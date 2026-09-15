"""Plex analyze must not be queued for Spore stubs by default.

Regression test for 2026-09-15: every corrected stub queued an analyze, the
consumer analyzed the whole season directory, and because the transcoder wrapper
rewrites stub reads to /spore-stream/<token> each analyzed episode was pulled
from the debrid CDN. That sweep rate limited TorBox until playback failed.
"""
import pytest

import media_servers


@pytest.fixture
def queued(monkeypatch):
    seen = []
    monkeypatch.setattr(media_servers, "_enqueue",
                        lambda path, mode: seen.append((path, mode)))
    return seen


def test_reanalyze_downgrades_to_scan_by_default(queued, monkeypatch):
    monkeypatch.setattr(media_servers, "PLEX_ANALYZE_ENABLED", False)
    media_servers.request_reanalyze("/data/media/series/American Dad!/Season 22/x.strm")
    assert queued == [("/data/media/series/American Dad!/Season 22/x.strm", "scan")]


def test_reanalyze_still_available_when_explicitly_enabled(queued, monkeypatch):
    monkeypatch.setattr(media_servers, "PLEX_ANALYZE_ENABLED", True)
    media_servers.request_reanalyze("/data/media/series/American Dad!/Season 22/x.strm")
    assert queued == [("/data/media/series/American Dad!/Season 22/x.strm", "analyze")]


def test_analyze_is_off_unless_configured():
    assert media_servers.PLEX_ANALYZE_ENABLED is False


def test_mark_and_mark_removed_are_unchanged(queued):
    media_servers.mark("/data/media/series/Show/Season 01/a.strm")
    media_servers.mark_removed("/data/media/series/Show/Season 01/a.strm")
    assert [m for _, m in queued] == ["scan", "remove"]
