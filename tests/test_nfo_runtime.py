import importlib.util
import json
import stat
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


_MODULE_PATH = Path(__file__).resolve().parents[1] / "nfo_generator.py"
_SPEC = importlib.util.spec_from_file_location("nfo_generator_runtime_test", _MODULE_PATH)
nfo_generator = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(nfo_generator)


@pytest.fixture(autouse=True)
def _private_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(nfo_generator, "DB_PATH", str(tmp_path / "requests.db"))


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


def test_routine_nfo_pass_skips_untracked_folders_without_tmdb_search(tmp_path, monkeypatch):
    movie = tmp_path / "movies" / "Unmatched Movie"
    series = tmp_path / "series" / "Unmatched Series"
    movie.mkdir(parents=True)
    series.mkdir(parents=True)

    monkeypatch.setattr(nfo_generator, "MEDIA_PATH", str(tmp_path))
    monkeypatch.setattr(nfo_generator.db, "get_media_items", lambda: [])
    monkeypatch.setattr(nfo_generator.db, "get_all_monitored_series", lambda: [])

    def unexpected_lookup(*_args, **_kwargs):
        raise AssertionError("routine NFO pass must not perform broad TMDB searches")

    monkeypatch.setattr(nfo_generator.tmdb, "search_movie", unexpected_lookup)
    monkeypatch.setattr(nfo_generator.tmdb, "search_tv", unexpected_lookup)

    result = nfo_generator.generate_all(lookup_missing=False)

    assert result == {"movies": 0, "series": 0}
    assert not (movie / "Unmatched Movie.nfo").exists()
    assert not (series / "tvshow.nfo").exists()


def _show(tmp_path, name, title, imdb):
    folder = tmp_path / "series" / name
    folder.mkdir(parents=True)
    (folder / "tvshow.nfo").write_text(
        f'<tvshow><title>{title}</title>'
        f'<uniqueid type="imdb" default="true">{imdb}</uniqueid></tvshow>',
        encoding="utf-8",
    )
    return folder


def test_title_repair_asks_jellyfin_to_reread_the_repaired_shows(tmp_path, monkeypatch):
    import sys
    import types

    bad = _show(tmp_path, "999 On the Front Line", "Season 14", "tt9020422")
    good = _show(tmp_path, "The Hack", "The Hack", "tt35615598")
    monkeypatch.setattr(nfo_generator, "MEDIA_PATH", str(tmp_path))
    monkeypatch.setattr(nfo_generator.db, "get_all_monitored_series", lambda: [
        {"imdb_id": "tt9020422", "title": "999: On the Front Line"}])
    sent = []
    monkeypatch.setitem(sys.modules, "media_servers", types.SimpleNamespace(
        refresh_jellyfin_folders=lambda folders: sent.append(list(folders))))

    result = nfo_generator.repair_tvshow_titles()

    assert result == {"fixed": 1, "skipped": 0, "unreadable": 0}
    assert ET.parse(bad / "tvshow.nfo").getroot().findtext("title") == "999: On the Front Line"
    assert ET.parse(good / "tvshow.nfo").getroot().findtext("title") == "The Hack"
    assert sent == [[bad]]


def test_title_repair_reports_nfos_it_cannot_read(tmp_path, monkeypatch):
    import sys
    import types

    _show(tmp_path, "Hitler's Last Stand", "Season 01", "tt8675140")
    monkeypatch.setattr(nfo_generator, "MEDIA_PATH", str(tmp_path))
    monkeypatch.setattr(nfo_generator.db, "get_all_monitored_series", lambda: [])
    sent = []
    monkeypatch.setitem(sys.modules, "media_servers", types.SimpleNamespace(
        refresh_jellyfin_folders=lambda folders: sent.append(list(folders))))

    def root_owned(path, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(nfo_generator.ET, "parse", root_owned)

    result = nfo_generator.repair_tvshow_titles()

    assert result["fixed"] == result["skipped"] == 0
    assert result["unreadable"] == 1
    assert result["unreadable_report"]["status"] == "written"
    assert sent == []


def test_all_unreadable_nfos_have_private_attribution_without_public_paths(
        tmp_path, monkeypatch, caplog):
    paths = [_show(tmp_path, f"Private Show {i}", "Season 01", f"tt{i}") / "tvshow.nfo"
             for i in range(23)]
    before = {path: (path.read_bytes(), path.stat()) for path in paths}
    monkeypatch.setattr(nfo_generator, "MEDIA_PATH", str(tmp_path))
    monkeypatch.setattr(nfo_generator.db, "get_all_monitored_series", lambda: [])
    reads = []

    def denied(path):
        reads.append(path)
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(nfo_generator.ET, "parse", denied)
    monkeypatch.setattr(nfo_generator, "atomic_write_text",
                        lambda *args: pytest.fail("Unreadable NFO must not be written"))
    result = nfo_generator.repair_tvshow_titles()
    report_dir = tmp_path / ".nfo-diagnostics"
    report_path = report_dir / result["unreadable_report"]["name"]
    details = json.loads(report_path.read_text())["unreadable"]

    assert result["unreadable"] == len(details) == len(reads) == 23
    assert {entry["path"] for entry in details} == {str(path) for path in paths}
    assert len({entry["path_sha256"] for entry in details}) == 23
    assert stat.S_IMODE(report_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    for entry in details:
        assert entry["error_class"] == "PermissionError" and entry["errno"] == 13
        assert entry["metadata"]["uid"] == before[Path(entry["path"])][1].st_uid
        assert entry["metadata"]["source"] == "lstat"
        assert entry["path_sha256"] in caplog.text
    assert "Private Show" not in caplog.text + json.dumps(result)
    assert str(tmp_path) not in caplog.text + json.dumps(result)
    for path, (content, metadata) in before.items():
        assert path.read_bytes() == content
        assert path.stat().st_ctime_ns == metadata.st_ctime_ns


def test_unreadable_metadata_denial_is_explicit_and_does_not_retry(tmp_path, monkeypatch):
    path = tmp_path / "private" / "tvshow.nfo"
    calls = []

    def denied_stat(self):
        calls.append(self)
        raise PermissionError(13, "private path", str(self))

    monkeypatch.setattr(Path, "lstat", denied_stat)
    detail = nfo_generator._unreadable_nfo(path, PermissionError(13, "private path"))
    assert calls == [path]
    assert "metadata" not in detail
    assert detail["metadata_error"] == {"error_class": "PermissionError", "errno": 13}


@pytest.mark.parametrize("unsafe", ["public", "symlink"])
def test_private_report_refuses_unsafe_directory_without_changing_it(tmp_path, unsafe):
    directory = tmp_path / ".nfo-diagnostics"
    target = tmp_path / "other"
    if unsafe == "public":
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)  # The runner's private umask must not hide this control.
        before = directory.stat()
    else:
        target.mkdir(mode=0o700)
        directory.symlink_to(target, target_is_directory=True)
        before = directory.lstat()

    result = nfo_generator._write_unreadable_nfo_report([{"path": "private"}])

    assert result["status"] == "unavailable"
    assert "private" not in json.dumps(result)
    assert list(directory.iterdir()) == []
    assert directory.lstat().st_ctime_ns == before.st_ctime_ns


def test_private_report_write_failure_is_sanitized(tmp_path, monkeypatch):
    real_open = nfo_generator.os.open

    def denied_write(path, flags, *args, **kwargs):
        if flags & nfo_generator.os.O_WRONLY:
            raise OSError(28, "Private target /library/Secret Show/tvshow.nfo")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(nfo_generator.os, "open", denied_write)
    assert nfo_generator._write_unreadable_nfo_report([{"path": "private"}]) == {
        "status": "unavailable", "error_class": "OSError", "errno": 28}
