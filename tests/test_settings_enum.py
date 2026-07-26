import importlib.util
import os
import sys

os.environ.setdefault("TORBOX_API_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


def _load_real(name, extra_deps=None):
    """Load a fresh real module from source, isolated from the MagicMocks that
    conftest.py installs in sys.modules for the strm_generator test group.

    conftest replaces db + settings (among others) with MagicMocks. These tests
    need the REAL pair (settings.get() must round-trip through SQLite), but must
    not leave the real modules in sys.modules where a later-collected strm test
    would bind to them. So the real deps are exposed in sys.modules only while
    `name`'s body executes, then the mocks are restored.
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


db = _load_real("db")
settings = _load_real("settings", extra_deps={"db": db})


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    yield


def test_zilean_mode_accepts_valid_values():
    settings.set("ZILEAN_MODE", "native")
    assert settings.get("ZILEAN_MODE") == "native"
    settings.set("ZILEAN_MODE", "external")
    assert settings.get("ZILEAN_MODE") == "external"


def test_zilean_mode_rejects_invalid_value():
    with pytest.raises(ValueError):
        settings.set("ZILEAN_MODE", "bogus")


def test_zilean_mode_get_falls_back_on_corrupt_stored_value():
    # Simulate a bad value having ended up in the DB some other way (e.g. a
    # stale row from before this enum existed) - get() should not surface it.
    db.set_setting("ZILEAN_MODE", "bogus")
    assert settings.get("ZILEAN_MODE") == settings._config.ZILEAN_MODE


def test_all_for_ui_reports_enum_kind_and_options():
    groups = settings.all_for_ui()
    group = next(g for g in groups if g["id"] == "zilean_native")
    item = next(i for i in group["items"] if i["key"] == "ZILEAN_MODE")
    assert item["kind"] == "enum"
    assert item["options"] == ["external", "native"]
