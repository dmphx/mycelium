import stremio_addons


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "streams": [
                {
                    "infoHash": "a" * 40,
                    "name": "Comet 1080p",
                    "description": "Example.S01E02.1080p.WEB-DL 💾 2.4 GB 👤 18",
                    "behaviorHints": {"bingeGroup": "comet|hevc"},
                },
                {"url": "https://example.invalid/direct-only"},
            ]
        }


def test_generic_addon_keeps_hash_results_and_ignores_direct_urls(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _Response()

    monkeypatch.setattr(stremio_addons.requests, "get", fake_get)
    streams = stremio_addons.fetch_from(
        "http://comet:8000", "series", "tt1234567", season=1, episode=2)

    assert calls == ["http://comet:8000/stream/series/tt1234567:1:2.json"]
    assert len(streams) == 1
    assert streams[0].info_hash == "a" * 40
    assert streams[0].source == "stremio/comet"
    assert streams[0].quality == "1080p"
