import sys
import types

import strm_generator as sg


def test_probe_pending_stubs_reads_bounded_batch_from_environment(monkeypatch):
    seen = []
    monkeypatch.setattr(sg.settings, "get", lambda key, default=None: True)
    monkeypatch.setitem(sys.modules, "catbox", types.SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "playback_guard",
        types.SimpleNamespace(defer=lambda job: False),
    )
    monkeypatch.setattr(
        sg.db,
        "get_unprobed_spore_items",
        lambda limit: seen.append(limit) or [],
    )
    monkeypatch.setenv("SPORE_PROBE_BATCH", "17")

    result = sg.probe_pending_stubs()

    assert seen == [17]
    assert result == {"probed": 0, "skipped": 0, "queued_preload": 0, "errors": 0}
