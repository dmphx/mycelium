from pathlib import Path
from types import SimpleNamespace

import cleanup


class FakeDb:
    def __init__(self, identities=None, monitored=None):
        self.identities = identities or {}
        self.monitored = monitored or []
        self.path_updates = []
        self.token_updates = []
        self.rows = {}
        self.path_update_result = 1

    def get_all_monitored_series(self):
        return self.monitored

    def get_series_strm_paths(self):
        return [
            (imdb_id, str(Path(folder) / "Season 01" / "Episode.strm"))
            for folder, imdb_ids in self.identities.items()
            for imdb_id in imdb_ids
        ]

    def update_virtual_item_strm_path(self, old_path, new_path):
        self.path_updates.append((old_path, new_path))
        return self.path_update_result

    def get_virtual_item(self, token):
        row = self.rows.get(token)
        return dict(row) if row else None

    def update_virtual_item_strm_path_if_unset(self, token, new_path):
        self.token_updates.append((token, new_path))
        row = self.rows.get(token)
        if not row or row.get("strm_path") is not None:
            return 0
        row["strm_path"] = new_path
        return 1

    def rename_virtual_item_paths(self, old_path, new_path):
        return 0


def _folder(series: Path, name: str, imdb_id: str | None) -> Path:
    folder = series / name
    folder.mkdir(parents=True)
    if imdb_id:
        (folder / "tvshow.nfo").write_text(
            f'<tvshow><uniqueid type="imdb">{imdb_id}</uniqueid></tvshow>',
            encoding="utf-8",
        )
    return folder


def _episode(folder: Path, name: str, content: str = "/stream/token") -> Path:
    season = folder / "Season 01"
    season.mkdir()
    path = season / name
    path.write_text(content, encoding="utf-8")
    return path


def _configure(monkeypatch, tmp_path, fake_db):
    monkeypatch.setattr(cleanup, "MEDIA_PATH", str(tmp_path))
    monkeypatch.setattr(cleanup, "db", fake_db)


def test_merge_requires_nfo_or_row_identity(monkeypatch, tmp_path):
    series = tmp_path / "series"
    canonical = _folder(series, "Doctor Who (2005)", "tt0436992")
    release_named = _folder(series, "Doctor Who 2023 S01", None)
    source = _episode(release_named, "Doctor Who 2023 S01E01.strm")
    fake_db = FakeDb(monitored=[{"imdb_id": "tt0436992", "title": canonical.name}])
    _configure(monkeypatch, tmp_path, fake_db)

    assert cleanup.merge_series_duplicates() == 0
    assert source.is_file()
    assert release_named.is_dir()
    assert fake_db.path_updates == []


def test_merge_skips_folder_with_conflicting_identity_evidence(monkeypatch, tmp_path):
    series = tmp_path / "series"
    canonical = _folder(series, "The Traitors", "tt15557874")
    conflicting = _folder(series, "The Traitors UK S03", "tt15557874")
    source = _episode(conflicting, "The Traitors UK S03E01.strm")
    fake_db = FakeDb(
        identities={str(conflicting): {"tt23743442"}},
        monitored=[{"imdb_id": "tt15557874", "title": canonical.name}],
    )
    _configure(monkeypatch, tmp_path, fake_db)

    assert cleanup.merge_series_duplicates() == 0
    assert source.is_file()
    assert conflicting.is_dir()


def test_row_identity_can_group_folders_without_nfo(monkeypatch, tmp_path):
    series = tmp_path / "series"
    canonical = _folder(series, "Coupling", None)
    duplicate = _folder(series, "Coupling 2000 S02", None)
    source = _episode(duplicate, "Coupling 2000 S01E01.strm")
    identities = {str(canonical): {"tt0237123"}, str(duplicate): {"tt0237123"}}
    fake_db = FakeDb(
        identities=identities,
        monitored=[{"imdb_id": "tt0237123", "title": canonical.name}],
    )
    _configure(monkeypatch, tmp_path, fake_db)

    assert cleanup.merge_series_duplicates() == 1
    assert not source.exists()
    assert (canonical / "Season 01" / "Coupling S01E01.strm").is_file()


def test_row_identity_scan_failure_disables_destructive_merge(monkeypatch, tmp_path):
    series = tmp_path / "series"
    canonical = _folder(series, "Coupling", "tt0237123")
    duplicate = _folder(series, "Coupling 2000 S02", "tt0237123")
    source = _episode(duplicate, "Coupling 2000 S01E01.strm")
    fake_db = FakeDb(monitored=[{"imdb_id": "tt0237123", "title": canonical.name}])
    fake_db.get_series_strm_paths = lambda: (_ for _ in ()).throw(OSError("database unavailable"))
    _configure(monkeypatch, tmp_path, fake_db)

    assert cleanup.merge_series_duplicates() == 0
    assert source.is_file()
    assert duplicate.is_dir()


def test_series_rename_does_not_query_tmdb_for_unmonitored_folder(monkeypatch, tmp_path):
    series = tmp_path / "series"
    folder = _folder(series, "Release Named Show S01", "tt1234567")
    fake_db = FakeDb()
    _configure(monkeypatch, tmp_path, fake_db)
    monkeypatch.setattr(
        cleanup.tmdb,
        "find_by_imdb",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("TMDB called")),
    )

    assert cleanup.rename_messy_series_folders() == 0
    assert folder.is_dir()


def test_merge_moves_a_row_backed_episode_with_matching_identity(monkeypatch, tmp_path):
    series = tmp_path / "series"
    canonical = _folder(series, "Coupling", "tt0237123")
    duplicate = _folder(series, "Coupling 2000 S02", "tt0237123")
    source = _episode(duplicate, "Coupling 2000 S01E01.strm")
    fake_db = FakeDb(monitored=[{"imdb_id": "tt0237123", "title": canonical.name}])
    _configure(monkeypatch, tmp_path, fake_db)

    assert cleanup.merge_series_duplicates() == 1
    destination = canonical / "Season 01" / "Coupling S01E01.strm"
    assert destination.read_text(encoding="utf-8") == "/stream/token"
    assert not duplicate.exists()
    assert fake_db.path_updates == [(str(source), str(destination))]


def test_unreadable_destination_isolated_to_its_folder(monkeypatch, tmp_path):
    series = tmp_path / "series"
    canonical = _folder(series, "Coupling", "tt0237123")
    duplicate = _folder(series, "Coupling 2000 S02", "tt0237123")
    destination = _episode(canonical, "Coupling S01E01.strm")
    source = _episode(duplicate, "Coupling 2000 S01E01.strm")
    fake_db = FakeDb(monitored=[{"imdb_id": "tt0237123", "title": canonical.name}])
    _configure(monkeypatch, tmp_path, fake_db)
    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if path == destination:
            raise PermissionError(13, "blocked", str(path))
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    assert cleanup.merge_series_duplicates() == 0
    assert source.is_file()
    assert duplicate.is_dir()
    assert fake_db.path_updates == []


def test_null_path_row_repoints_by_catbox_token(monkeypatch, tmp_path):
    series = tmp_path / "series"
    canonical = _folder(series, "Coupling", "tt0237123")
    duplicate = _folder(series, "Coupling 2000 S02", "tt0237123")
    source = _episode(duplicate, "Coupling 2000 S01E01.strm", "https://media/stream/abc123")
    fake_db = FakeDb(monitored=[{"imdb_id": "tt0237123", "title": canonical.name}])
    fake_db.path_update_result = 0
    fake_db.rows["abc123"] = {"strm_path": None}
    _configure(monkeypatch, tmp_path, fake_db)

    assert cleanup.merge_series_duplicates() == 1
    destination = canonical / "Season 01" / "Coupling S01E01.strm"
    assert fake_db.token_updates == [("abc123", str(destination))]
    assert fake_db.rows["abc123"]["strm_path"] == str(destination)
    assert destination.is_file()
    assert not duplicate.exists()


def test_conflicting_token_binding_rolls_back_new_destination(monkeypatch, tmp_path):
    series = tmp_path / "series"
    canonical = _folder(series, "Coupling", "tt0237123")
    duplicate = _folder(series, "Coupling 2000 S02", "tt0237123")
    source = _episode(duplicate, "Coupling 2000 S01E01.strm", "https://media/stream/abc123")
    fake_db = FakeDb(monitored=[{"imdb_id": "tt0237123", "title": canonical.name}])
    fake_db.path_update_result = 0
    fake_db.rows["abc123"] = {"strm_path": "/data/media/series/Elsewhere/Season 01/Episode.strm"}
    _configure(monkeypatch, tmp_path, fake_db)

    assert cleanup.merge_series_duplicates() == 0
    destination = canonical / "Season 01" / "Coupling S01E01.strm"
    assert source.is_file()
    assert not destination.exists()
    assert duplicate.is_dir()


def test_source_unlink_failure_does_not_abort_the_run(monkeypatch, tmp_path):
    series = tmp_path / "series"
    canonical = _folder(series, "Coupling", "tt0237123")
    duplicate = _folder(series, "Coupling 2000 S02", "tt0237123")
    source = _episode(duplicate, "Coupling 2000 S01E01.strm")
    fake_db = FakeDb(monitored=[{"imdb_id": "tt0237123", "title": canonical.name}])
    _configure(monkeypatch, tmp_path, fake_db)
    original_unlink = Path.unlink

    def guarded_unlink(path, *args, **kwargs):
        if path == source:
            raise PermissionError(13, "blocked", str(path))
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", guarded_unlink)

    assert cleanup.merge_series_duplicates() == 0
    assert source.is_file()
    assert duplicate.is_dir()
    assert (canonical / "Season 01" / "Coupling S01E01.strm").is_file()


def test_cleanup_sleeps_only_after_a_repair_attempt(monkeypatch, tmp_path):
    paths = [tmp_path / "healthy.strm", tmp_path / "repaired.strm"]
    results = iter(("ok", "repaired"))
    updates = []
    fake_db = SimpleNamespace(
        insert_cleanup_run=lambda: 42,
        update_cleanup_run=lambda *args: updates.append(args),
        get_recently_unfixable_paths=lambda hours: set(),
    )
    sleeps = []
    refreshes = []
    monkeypatch.setattr(cleanup, "db", fake_db)
    monkeypatch.setattr(cleanup, "rename_messy_series_folders", lambda: 0)
    monkeypatch.setattr(cleanup, "rename_messy_movie_folders", lambda: 0)
    monkeypatch.setattr(cleanup, "merge_movie_duplicates", lambda: 0)
    monkeypatch.setattr(cleanup, "merge_series_duplicates", lambda: 0)
    monkeypatch.setattr(cleanup, "remove_orphan_folders", lambda: 0)
    monkeypatch.setattr(cleanup, "_collect_strm_files", lambda: paths)
    monkeypatch.setattr(cleanup, "_remove_duplicates", lambda found, run_id: (0, found))
    monkeypatch.setattr(cleanup, "_regenerate_wrong_files", lambda found, mylist, run_id: 0)
    monkeypatch.setattr(cleanup, "_repair_strm", lambda path, run_id, mylist: next(results))
    monkeypatch.setattr(cleanup.torbox, "list_torrents", lambda: [])
    monkeypatch.setattr(cleanup.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(
        cleanup.strm_generator, "run_and_refresh",
        lambda maintenance_held=False: refreshes.append(("strm", maintenance_held)),
    )
    monkeypatch.setattr(cleanup.jellyfin, "refresh_library", lambda: refreshes.append("jellyfin"))

    cleanup._run_cleanup_locked()

    assert sleeps == [2]
    assert updates[-1] == (42, 2, 1, 0, 0)
    # Cleanup holds _maintenance_lock, so its own rebuild must say so or it
    # would defer itself.
    assert refreshes == [("strm", True), "jellyfin"]
