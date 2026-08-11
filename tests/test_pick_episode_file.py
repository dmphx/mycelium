"""
Regressietests voor de season-pack episode matcher.

Borgt dat _pick_episode_file NOOIT een verkeerde aflevering serveert:
geen blinde largest-file gok, en geen bestand dat als een ANDERE aflevering
getagd is. Achtergrond: een E01 die de generieke pack-naam droeg (geen SxxExx)
viel terug op het grootste bestand en speelde zo de verkeerde aflevering af;
NNxNN-packs (The Bill, Forensic Files) matchten de SxxExx-regex nooit.

Geen live DB of netwerk nodig; zware imports worden gemockt.
"""
import strm_generator as sg


def _f(i, name, size=300_000_000):
    return {"id": i, "name": name, "size": size}


def _pid(files, s, e):
    f = sg._pick_episode_file(files, s, e)
    return f["id"] if f else None


def _pid_abs(files, s, e, absolute):
    f = sg._pick_episode_file(files, s, e, absolute=absolute)
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


class TestCrossSchemeAbsolute:
    """The 'absolute' arg lets a TVDB request match an absolute-numbered file."""

    NARUTO = "[Koten_Gars] Naruto Shippuden - 446 [iTunes][h.264][1080p][AC3] [33CB660A].mkv"

    def test_has_absolute_token_matches(self):
        assert sg._file_has_absolute(self.NARUTO, 446) is True
        assert sg._file_has_absolute("Bleach - 047.mkv", 47) is True          # zero-padded
        assert sg._file_has_absolute("Show E0154 1080p.mkv", 154) is True

    def test_has_absolute_rejects_embedded_digits(self):
        assert sg._file_has_absolute("Show.x264.1080p.mkv", 264) is False     # codec
        assert sg._file_has_absolute("Show 1920x1080.mkv", 1080) is False     # resolution
        assert sg._file_has_absolute(self.NARUTO, 264) is False               # codec inside name

    def test_pick_matches_by_absolute_when_provided(self):
        files = [_f(i, "[Grp] Naruto Shippuden - %d [1080p].mkv" % a)
                 for i, a in enumerate([444, 445, 446, 447])]
        # request S20E15 -> TheXEM absolute 446 (computed by caller)
        assert _pid_abs(files, 20, 15, 446) == 2          # the "- 446" file
        assert _pid_abs(files, 20, 15, 999) is None       # absolute not present -> fail closed
        assert _pid(files, 20, 15) is None                # without absolute -> ambiguous, fail closed

    def test_absolute_ignored_for_normal_seasonal_pack(self):
        files = [_f(0, "Show.S01E05.mkv"), _f(1, "Show.S01E06.mkv")]
        assert _pid_abs(files, 1, 5, 305) == 0            # seasonal match wins; absolute unused
