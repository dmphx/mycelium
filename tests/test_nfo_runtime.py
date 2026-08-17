import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / "nfo_generator.py"
_SPEC = importlib.util.spec_from_file_location("nfo_generator_runtime_test", _MODULE_PATH)
nfo_generator = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(nfo_generator)


def test_movie_nfo_includes_runtime_minutes():
    root = ET.fromstring(nfo_generator._movie_nfo("Example", 2026, "tt123", 121))

    assert root.findtext("runtime") == "121"


def test_merge_episode_metadata_preserves_streamdetails(tmp_path):
    nfo = tmp_path / "Show S01E02.nfo"
    nfo.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<episodedetails>
  <title>Episode 2</title>
  <uniqueid type="imdb" default="true">tt123</uniqueid>
  <fileinfo><streamdetails><video><codec>hevc</codec></video></streamdetails></fileinfo>
</episodedetails>
""",
        encoding="utf-8",
    )

    changed = nfo_generator._merge_episode_metadata(
        nfo, "A Real Title", 1, 2, "Plot", "2026-08-16", 23
    )
    root = ET.parse(nfo).getroot()

    assert changed is True
    assert root.findtext("title") == "A Real Title"
    assert root.findtext("season") == "1"
    assert root.findtext("episode") == "2"
    assert root.findtext("runtime") == "23"
    assert root.findtext("uniqueid") == "tt123"
    assert root.findtext("fileinfo/streamdetails/video/codec") == "hevc"


def test_merge_episode_metadata_keeps_non_generic_title(tmp_path):
    nfo = tmp_path / "Show S01E03.nfo"
    nfo.write_text(
        "<episodedetails><title>Custom Cut</title></episodedetails>",
        encoding="utf-8",
    )

    nfo_generator._merge_episode_metadata(nfo, "TMDB Title", 1, 3, runtime_minutes=24)
    root = ET.parse(nfo).getroot()

    assert root.findtext("title") == "Custom Cut"
    assert root.findtext("runtime") == "24"


def test_episode_backfill_updates_existing_nfo_even_when_thumb_exists(tmp_path, monkeypatch):
    season = tmp_path / "Season 01"
    season.mkdir()
    strm = season / "Show S01E04.strm"
    strm.write_text("http://example.invalid/stream", encoding="utf-8")
    nfo = strm.with_suffix(".nfo")
    nfo.write_text(
        "<episodedetails><title>Episode 4</title>"
        "<fileinfo><streamdetails /></fileinfo></episodedetails>",
        encoding="utf-8",
    )
    strm.with_name("Show S01E04-thumb.jpg").write_bytes(b"existing")
    monkeypatch.setattr(
        nfo_generator.tmdb,
        "get_season_episodes",
        lambda _tmdb_id, _season: [{
            "episode_number": 4,
            "name": "Tree Trunks",
            "overview": "Plot",
            "air_date": "2010-04-19",
            "runtime": 11,
            "still_path": "/still.jpg",
        }],
    )
    monkeypatch.setattr(nfo_generator.time, "sleep", lambda _seconds: None)

    updated, stills = nfo_generator._write_episode_meta(tmp_path, 15260)
    root = ET.parse(nfo).getroot()

    assert (updated, stills) == (1, 0)
    assert root.findtext("title") == "Tree Trunks"
    assert root.findtext("runtime") == "11"

