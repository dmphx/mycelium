"""No Mycelium code path may call Plex's library refresh API directly.

Plex folder scans go through the host consumer, which guards each folder before
scanning it (Onyx #473): a scan can trash attached NFS parts or merge a live item
into a trashed duplicate. A direct `/library/sections/<id>/refresh` call skips
that guard, so this source check fails if one is reintroduced anywhere outside
the tests.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {"tests", "node_modules", ".git", "static", "dist", "__pycache__"}
SOURCE_SUFFIXES = {".py", ".sh", ".go", ".ts", ".tsx", ".js"}
# The endpoint may be built in pieces, so match the sections path and the
# refresh verb within one statement-sized window.
REFRESH = re.compile(r"library/sections.{0,120}?refresh", re.IGNORECASE | re.DOTALL)


def _sources():
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        if path.is_file() and path.suffix in SOURCE_SUFFIXES:
            yield rel, path.read_text(encoding="utf-8", errors="replace")


def test_pattern_catches_the_removed_call_shapes():
    assert REFRESH.search('requests.get("%s/library/sections/%s/refresh" % (u, s))')
    assert REFRESH.search('_rq.get(f"{plex_url}/library/sections/{section}/refresh",')
    assert not REFRESH.search('f"{base}/Library/Refresh"  # Jellyfin')


def test_no_source_file_calls_plex_refresh_directly():
    offenders = [str(rel) for rel, text in _sources() if REFRESH.search(text)]
    assert offenders == []
