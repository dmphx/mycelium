import os
import stat
import sys

import pytest

from io_utils import atomic_write_bytes, atomic_write_text


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_atomic_write_is_world_readable(tmp_path):
    text = tmp_path / "Film (2024).minfo"
    blob = tmp_path / "Film (2024).mkv"
    atomic_write_text(text, "token=abc\n")
    atomic_write_bytes(blob, b"\x1a\x45\xdf\xa3")
    for path in (text, blob):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o644


def test_atomic_write_replaces_content(tmp_path):
    path = tmp_path / "item.strm"
    atomic_write_text(path, "old")
    atomic_write_text(path, "new")
    assert path.read_text() == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["item.strm"]
