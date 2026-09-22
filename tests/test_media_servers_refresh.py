"""Tests for targeted Jellyfin metadata refreshes."""
import media_servers


def test_sends_one_modified_update_per_library_folder(monkeypatch):
    sent = []
    monkeypatch.setattr(media_servers, "_scan_jellyfin", sent.append)
    monkeypatch.setattr(media_servers, "_enqueue",
                        lambda *a: (_ for _ in ()).throw(AssertionError("no Plex scan")))
    monkeypatch.setattr(media_servers, "MEDIA_PATH", "/data/media")
    monkeypatch.setattr(media_servers, "JF_LIBRARY_ROOT", "/mnt/library-mycelium")

    count = media_servers.refresh_jellyfin_folders([
        "/data/media/series/999 On the Front Line",
        "/elsewhere/series/Not Ours",
    ])

    assert count == 1
    assert sent == [[{"Path": "/mnt/library-mycelium/series/999 On the Front Line",
                      "UpdateType": "Modified"}]]


def test_nothing_to_send_makes_no_request(monkeypatch):
    sent = []
    monkeypatch.setattr(media_servers, "_scan_jellyfin", sent.append)
    assert media_servers.refresh_jellyfin_folders([]) == 0
    assert sent == []
