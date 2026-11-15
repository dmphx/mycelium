"""Episode numbers past 99 keep their identity through every consumer.

Before this, the writer read S67E192 as S67E19, and cleanup's duplicate pass
keyed E10 and E100-E109 as one episode: between 2026-06-06 and 2026-10-26 it
deleted 5,090 distinct .strm files as "duplicates". All episode-tag regexes now
share one contract: up to four episode digits, never followed by another digit.
"""
import ast
import re
import types
from pathlib import Path

import pytest

import cleanup
import placeholder_cleanup
import strm_generator as sg
from test_cleanup_series_identity import FakeDb, _configure, _folder

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("episode", [192, 1001])
def test_merge_keeps_newly_generated_episode_number(monkeypatch, tmp_path, episode):
    canonical = _folder(tmp_path / "series", "Soap", "tt1234567")
    _folder(tmp_path / "series", "Soap Release", "tt1234567")
    monkeypatch.setattr(sg, "MEDIA_PATH", str(tmp_path))
    info = sg._parse_info(f"Soap.Release.S01E{episode}", f"Soap.Release.S01E{episode}.mkv")
    path = sg._strm_path(info)
    path.parent.mkdir()
    path.write_text("/stream/synthetic", encoding="utf-8")
    _configure(monkeypatch, tmp_path, FakeDb(monitored=[{"imdb_id": "tt1234567", "title": "Soap"}]))

    assert cleanup.merge_series_duplicates() == 1
    assert (canonical / "Season 01" / f"Soap S01E{episode}.strm").is_file()
    assert sorted(p.name for p in canonical.rglob("*.strm")) == [f"Soap S01E{episode}.strm"]


def test_duplicate_pass_keeps_episodes_that_share_leading_digits(monkeypatch, tmp_path):
    season = tmp_path / "series" / "Bleach" / "Season 01"
    season.mkdir(parents=True)
    names = ["Bleach S01E10.strm", "Bleach S01E100.strm", "Bleach S01E104.strm", "Bleach S01E1001.strm"]
    paths = [season / n for n in names]
    for p in paths:
        p.write_text("/stream/synthetic", encoding="utf-8")
    deleted = []
    db = types.SimpleNamespace(delete_virtual_item_by_strm_path=deleted.append,
                               insert_repair_item=lambda *a: deleted.append(a))
    monkeypatch.setattr(cleanup, "MEDIA_PATH", str(tmp_path))
    monkeypatch.setattr(cleanup, "db", db)
    monkeypatch.setattr(sg, "_delete_spore_stubs", lambda path: None)

    removed, survivors = cleanup._remove_duplicates(paths, run_id=1)

    assert removed == 0 and deleted == []
    assert sorted(survivors) == sorted(paths)
    assert all(p.is_file() for p in paths)


def test_duplicate_pass_still_removes_a_real_duplicate(monkeypatch, tmp_path):
    first = tmp_path / "series" / "Bleach" / "Season 01"
    second = tmp_path / "series" / "bleach" / "Season 01"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    a = first / "Bleach S01E104 1080p.strm"
    b = second / "Bleach S01E104 720p.strm"
    for p in (a, b):
        p.write_text("/stream/synthetic", encoding="utf-8")
    db = types.SimpleNamespace(delete_virtual_item_by_strm_path=lambda p: None,
                               insert_repair_item=lambda *args: None)
    monkeypatch.setattr(cleanup, "MEDIA_PATH", str(tmp_path))
    monkeypatch.setattr(cleanup, "db", db)
    monkeypatch.setattr(sg, "_delete_spore_stubs", lambda path: None)

    removed, survivors = cleanup._remove_duplicates([a, b], run_id=1)

    assert removed == 1 and survivors == [a] and not b.exists()


def test_placeholder_destination_keeps_four_digit_episode(tmp_path):
    source = tmp_path / "Season 01" / "Soap S01E1001.strm"
    dest = placeholder_cleanup._episode_destination(source, tmp_path / "Soap", "Soap", ".strm")
    assert dest.name == "Soap S01E1001.strm"


def test_nfo_metadata_reader_uses_the_whole_episode_number(tmp_path):
    """Exercise the real _write_episode_meta with a synthetic TMDB season."""
    source = ROOT / "nfo_generator.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "_write_episode_meta")
    pattern = next(n for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "_EP_RE" for t in n.targets))
    calls = []
    namespace = {
        "Path": Path, "re": re,
        "time": types.SimpleNamespace(sleep=lambda _: None),
        "tmdb": types.SimpleNamespace(get_season_episodes=lambda *a: [
            {"episode_number": 100, "name": "Wrong episode"},
            {"episode_number": 1001, "name": "Correct episode"}]),
        "_episode_nfo_needs_metadata": lambda _: True,
        "_merge_episode_metadata": lambda *args: calls.append(args) or True,
        "log": types.SimpleNamespace(debug=lambda *a: None),
    }
    exec(compile(ast.Module(body=[pattern, function], type_ignores=[]), str(source), "exec"), namespace)
    season = tmp_path / "Season 01"
    season.mkdir()
    (season / "Soap S01E1001.strm").write_text("synthetic", encoding="utf-8")

    namespace["_write_episode_meta"](tmp_path, 1)

    assert calls and calls[0][1:4] == ("Correct episode", 1, 1001)


def _episode_tag_patterns():
    """Every SxxEyy regex literal in non-test source, with its location."""
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in ("tests", ".claude") or any(p.startswith(".") for p in rel.parts[:-1]):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "compile"
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and re.search(r"[Ss]\]?\(\\d\{1,\d\}\)\[?[Ee]", node.args[0].value)):
                flags = re.IGNORECASE if "IGNORECASE" in ast.unparse(node) else 0
                found.append((f"{rel}:{node.lineno}", re.compile(node.args[0].value, flags)))
    return found


def test_every_episode_tag_regex_reads_four_digit_episodes_whole():
    patterns = _episode_tag_patterns()
    sites = {site.split(":")[0] for site, _ in patterns}
    assert {"strm_generator.py", "cleanup.py", "placeholder_cleanup.py",
            "nfo_generator.py", "app.py"} <= sites
    for site, pattern in patterns:
        # Stems: some patterns are anchored at the end of a file stem.
        for name, episode in [("Show S01E10", 10), ("Show S01E104", 104), ("Show S01E1001", 1001)]:
            m = pattern.search(name)
            assert m and int(m.group(2)) == episode, f"{site} reads {name} as {m and m.group(2)}"
