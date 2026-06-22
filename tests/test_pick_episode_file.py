"""
Regressietests voor de season-pack episode matcher.

Borgt dat _pick_episode_file NOOIT een verkeerde aflevering serveert:
geen blinde largest-file gok, en geen bestand dat als een ANDERE aflevering
getagd is. Achtergrond: een E01 die de generieke pack-naam droeg (geen SxxExx)
viel terug op het grootste bestand en speelde zo de verkeerde aflevering af;
NNxNN-packs (The Bill, Forensic Files) matchten de SxxExx-regex nooit.

Geen live DB of netwerk nodig; zware imports worden gemockt.
"""
import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("TORBOX_API_KEY", "test")
os.environ.setdefault("MEDIA_PATH", "/tmp/mycelium-test-media")
os.environ.setdefault("SPORE_MEDIA_PATH", "/tmp/mycelium-test-spore")
os.environ.setdefault("TORBOX_BASE_URL", "https://api.torbox.app/v1/api")

for _mod in ("db", "jellyfin", "settings", "torbox", "nfo_generator", "mp4_faststart"):
    sys.modules.setdefault(_mod, MagicMock())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import strm_generator as sg  # noqa: E402  (imports na sys.path setup)


def _f(i, name, size=300_000_000):
    return {"id": i, "name": name, "size": size}


def _pid(files, s, e):
    f = sg._pick_episode_file(files, s, e)
    return f["id"] if f else None


class TestPickEpisodeFile:
    def test_untagged_e01_is_not_the_largest_file(self):
        # E01 carries the generic pack name (no SxxExx); E05 is the largest file.
        files = [
            _f(2, "Solar.Opposites.S06E02.x265-iVy.mkv", 340_000_000),
            _f(4, "Solar Opposites-2020-S06 1080p WEBRip x265-iVy.mkv", 340_000_000),
            _f(7, "Solar.Opposites.S06E05.x265-iVy.mkv", 750_000_000),
        ]
        assert _pid(files, 6, 1) == 4   # untagged file, not largest (id7 = E05)
        assert _pid(files, 6, 5) == 7
        assert _pid(files, 6, 2) == 2

    def test_nnxnn_naming_matches_and_absent_is_none(self):
        files = [_f(374, "The Bill - 7x61 - The Corporal of Horse.mp4"),
                 _f(490, "The Bill - 12x89 - Target.mp4", 900_000_000)]
        assert _pid(files, 7, 61) == 374
        assert _pid(files, 7, 99) is None   # absent and all tagged: no guess

    def test_zero_padded_nnxnn(self):
        files = [_f(101, "Forensic Files - 02x10 - Sealed With A Kiss.avi"),
                 _f(81, "Forensic Files - 01x06 - Southside Strangler.avi", 900_000_000)]
        assert _pid(files, 2, 10) == 101

    def test_multiple_untagged_is_ambiguous(self):
        files = [_f(1, "Bleach - The Substitute.mkv"), _f(2, "Bleach - Work.mkv")]
        assert _pid(files, 1, 1) is None

    def test_single_untagged_file_resolves(self):
        assert _pid([_f(0, "Some.Show.Episode.mkv")], 3, 7) == 0

    def test_single_file_tagged_other_episode_is_rejected(self):
        assert _pid([_f(0, "Some.Show.S05E10.mkv")], 1, 16) is None

    def test_resolution_and_codec_not_parsed_as_episode(self):
        assert _pid([_f(0, "Show.S02E04.1920x1080.x265.mkv")], 2, 4) == 0

    def test_absolute_numbered_pack(self):
        # Anime-style pack: files numbered by absolute "E####", request stored
        # in the same absolute scheme (season 1). Should match by number.
        files = [_f(i, "E%04d.mkv" % i) for i in (1, 4, 5, 52, 92, 149)]
        assert _pid(files, 1, 52) == 52
        assert _pid(files, 1, 92) == 92
        assert _pid(files, 1, 2361) is None   # not present in this pack -> fail closed

    def test_absolute_does_not_hijack_seasonal_pack(self):
        # A normal seasonal pack must never be matched via the absolute path.
        files = [_f(0, "Show.S01E05.mkv"), _f(1, "Show.S01E06.mkv")]
        assert _pid(files, 1, 5) == 0
        # And a seasonal name must not be read as an absolute number.
        assert sg._file_absolute("Show.S01E05.mkv") is None
        assert sg._file_absolute("E0052.mkv") == 52


class TestFileEpisodeParser:
    def test_parses_sxxexx_and_nnxnn(self):
        assert sg._file_episode("Show.S03E07.mkv") == (3, 7)
        assert sg._file_episode("Show - 12x89 - Title.mp4") == (12, 89)

    def test_ignores_resolution_codec_and_generic_names(self):
        assert sg._file_episode("Show 1920x1080 x265.mkv") is None
        assert sg._file_episode("Generic Pack Name 2020.mkv") is None
