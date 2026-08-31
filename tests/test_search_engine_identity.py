import debrid
import search_engine
from torrentio import TorrentioStream


def _stream(name: str, info_hash: str, source: str) -> TorrentioStream:
    return TorrentioStream(
        name=name,
        title=name,
        info_hash=info_hash,
        quality="1080p",
        seeders=10,
        size_gb=0.75,
        is_season_pack=False,
        source=source,
    )


def test_cached_generic_fast_result_still_runs_episode_identity_fallback(monkeypatch):
    generic = _stream(
        "Futurama.S11E06.1080p.WEB-DL",
        "a" * 40,
        "mediafusion",
    )
    exact = _stream(
        "Futurama.S11E06.Late.Bloomers.1080p.WEB-DL",
        "b" * 40,
        "prowlarr/drunkenslug",
    )
    unusable_exact = _stream(
        "Futurama.S11E06.Late.Bloomers.2160p.WEB-DL",
        "c" * 40,
        "mediafusion",
    )
    calls = []

    def source_jobs(_media_type, _imdb_id, _title, _season, _episode,
                    include_prowlarr):
        if include_prowlarr:
            return [("prowlarr", lambda: calls.append("prowlarr") or [exact])]
        return [("mediafusion", lambda: [generic, unusable_exact])]

    def rank_streams(rows, **_kwargs):
        acceptable = [row for row in rows if "2160p" not in row.name]
        matches = [
            row for row in acceptable
            if search_engine.torrentio._episode_title_match(row, "Late Bloomers")
        ]
        return matches or acceptable

    monkeypatch.setattr(search_engine, "_source_jobs", source_jobs)
    monkeypatch.setattr(debrid, "check_cached_multi",
                        lambda _hashes: {"torbox": {generic.info_hash}})
    monkeypatch.setattr(
        search_engine.torrentio,
        "rank_streams",
        rank_streams,
    )
    monkeypatch.setattr(search_engine.blacklist, "filter_candidates", lambda rows: rows)
    monkeypatch.setattr(search_engine.db, "start_search_run", lambda *args: 1)
    monkeypatch.setattr(search_engine.db, "record_source_query", lambda *args: None)
    monkeypatch.setattr(search_engine.db, "get_rejected_candidate_hashes", lambda _key: set())
    monkeypatch.setattr(search_engine.db, "upsert_release_candidates", lambda *args: None)
    monkeypatch.setattr(search_engine.db, "finish_search_run", lambda *args, **kwargs: None)

    ranked = search_engine.search_candidates(
        "series",
        "tt0149460",
        "Futurama",
        season=11,
        episode=6,
        override={"episode_title": "Late Bloomers"},
        prowlarr_on_cache_miss=True,
    )

    assert calls == ["prowlarr"]
    assert ranked == [exact]
