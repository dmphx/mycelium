"""
Repoint series items that scripts/sweep_series_identity.py flagged as another
national version of their show to a cached release that names the right show.

Input is the sweep's --json findings file.

Only mixed shows are touched: shows where the flagged items are less than
--max-show-share (default 0.8) of the show's items. There the show's identity
is taken as right and the flagged releases as contamination. A show whose
items are nearly all another version is skipped and listed, because there the
show's imdb is the likely error, and repointing would replace the content
people watch with a different show.

A replacement must be cached on TorBox, pass release_sanity.filter_cached
(the episode file is present, and the identity check passes), and positively
name this show: a country or premiere-year qualifier that matches it, or the
TMDB episode title when that title is distinctive. An unqualified name is not
enough, since an unqualified name is what made the original pick ambiguous.

Dry run by default. A dry run still searches normally, which records search
history, but changes no item. --apply first saves every original row and
Plex stub to --backup-dir, then repoints (clearing TorBox ids and probed Spore
tracks), rewrites the stub and queues season-scoped Plex/Jellyfin scans.
--rollback DIR restores a backup.

The running image does not ship /app/scripts, so copy the file in first:
    docker cp scripts/repoint_series_identity.py mycelium:/tmp/
    docker exec -w /app mycelium python3 /tmp/repoint_series_identity.py \\
        --findings /data/ops/series_identity_findings_<date>.json [--apply]
"""
import argparse
import json
import re
import shutil
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

_GENERIC_TITLE_RE = re.compile(
    r"(?:(?:the |series |season |grand |live |semi )?(?:episode|ep|part|chapter|show|"
    r"programme|program|week|day|series|season|pilot|special|finale|final|premiere|"
    r"reunion|launch|heat|round|results?)s?)?(?: (?:\d+|one|two|three|four|five|six))*")
_RECENT_PLAY = timedelta(minutes=90)
_BACKUP_COLUMNS = ("info_hash", "magnet", "quality", "source", "size_gb", "torbox_id",
                   "file_id", "spore_tracks", "protocol", "debrid_provider", "rd_id",
                   "nzb_url", "usenet_id")


def distinctive_title(title: str | None) -> bool:
    """True for an episode title that can identify a release on its own:
    "Turtle Tracks" yes, "Episode 3" or "Part 2" no."""
    words = " ".join(re.findall(r"[a-z0-9]+", (title or "").lower()))
    return len(words) >= 5 and not _GENERIC_TITLE_RE.fullmatch(words)


def choose_replacement(candidates: list, identity, expected_title: str | None,
                       current_hash: str):
    """First candidate that positively names the show, with the reason, or
    (None, ""). Candidates arrive ranked, cached and sanity-filtered."""
    import release_sanity
    import torrentio
    for candidate in candidates:
        if candidate.info_hash.lower() == (current_hash or "").lower():
            continue
        languages = set(getattr(candidate, "languages", ()) or ())
        if (identity.language == "en" and languages
                and not languages & {"en", "multi"}):
            continue  # a dub ("Черепашки ниндзя / ...") is the right show, wrong audio
        verdict, _ = release_sanity.classify_release(
            release_sanity._stream_text(candidate), identity)
        if verdict == release_sanity.IDENTITY_MATCH:
            return candidate, "qualifier"
        if (verdict == release_sanity.IDENTITY_NEUTRAL
                and distinctive_title(expected_title)
                and torrentio._episode_title_match(candidate, expected_title)):
            return candidate, "episode title"
    return None, ""


def mixed_show_ids(findings: list[dict], totals: dict[str, int],
                   max_share: float) -> tuple[set[str], dict[str, float]]:
    """imdb ids whose flagged share of items is below max_share, plus every
    show's share."""
    flagged = Counter(row["imdb_id"] for row in findings)
    share = {imdb: flagged[imdb] / max(1, totals.get(imdb, 0)) for imdb in flagged}
    return {imdb for imdb, value in share.items() if value < max_share}, share


def eligible(finding: dict, mixed: set[str], present: dict[str, set]) -> bool:
    """Mixed shows are eligible. A whole-show suspect only offers the items
    whose episode the right version (--duplicate-of) already holds, since those
    slots are duplicates: repointing them loses nothing anyone can watch."""
    imdb_id = finding["imdb_id"]
    if imdb_id in mixed:
        return True
    return (int(finding["season"]), int(finding["episode"])) in present.get(imdb_id, set())


def _stub_paths(strm_path: str) -> list[Path]:
    import strm_generator
    strm = Path(strm_path)
    stub_dir = strm_generator._spore_stub_dir(strm)
    return [stub_dir / (strm.stem + ext) for ext in (".mkv", ".minfo")]


def _backup_stubs(strm_path: str, backup_dir: Path) -> None:
    import config
    for path in _stub_paths(strm_path):
        if path.exists():
            rel = path.relative_to(Path(config.SPORE_MEDIA_PATH))
            target = backup_dir / "stubs" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _rewrite_stub(row: dict, quality: str | None, size_gb: float | None) -> None:
    import settings
    import strm_generator
    import config
    if not settings.get("SPORE_ENABLED", config.SPORE_ENABLED):
        return
    for path in _stub_paths(row["strm_path"]):
        path.unlink(missing_ok=True)
    strm_generator._write_spore_stubs(Path(row["strm_path"]), row["token"], row["title"],
                                      quality, size_gb)


def _recently_played(row: dict) -> bool:
    try:
        played = datetime.strptime(row.get("last_played") or "", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return datetime.now(timezone.utc).replace(tzinfo=None) - played < _RECENT_PLAY


def _wait_for_idle_playback() -> None:
    import playback_guard
    while playback_guard.active(force=True):
        print("  playback active, waiting 60s", flush=True)
        time.sleep(60)


def repoint(args) -> int:
    import db
    import media_servers
    import processor
    import release_sanity
    import search_engine

    findings = json.load(open(args.findings, encoding="utf-8"))
    duplicate_of = dict(pair.split("=", 1) for pair in args.duplicate_of)
    with db._connect() as conn:
        totals = dict(conn.execute(
            "SELECT imdb_id, COUNT(*) FROM virtual_items WHERE media_type='series' "
            "GROUP BY imdb_id").fetchall())
        titles = dict(conn.execute("SELECT imdb_id, title FROM monitored_series").fetchall())
        present = {
            wrong: {(int(s), int(e)) for s, e in conn.execute(
                "SELECT season, episode FROM virtual_items WHERE imdb_id=? "
                "AND media_type='series' AND season IS NOT NULL AND episode IS NOT NULL",
                (right,))}
            for wrong, right in duplicate_of.items()
        }
    mixed, share = mixed_show_ids(findings, totals, args.max_show_share)
    suspects = sorted({(r["show"], r["imdb_id"]) for r in findings
                       if r["imdb_id"] not in mixed})
    print(f"Findings: {len(findings)} items. Mixed shows: {len(mixed)}. "
          f"Whole-show identity suspects: {len(suspects)}")
    findings = [r for r in findings if eligible(r, mixed, present)]
    print(f"Eligible items: {len(findings)} (mixed shows, plus duplicates that the right "
          f"version of {len(duplicate_of)} suspect show(s) already holds)")
    for show, imdb in suspects:
        print(f"  whole-show suspect {show} ({imdb}): {share[imdb]:.0%} of its items are "
              f"another version; only its duplicates are eligible")

    backup_dir = Path(args.backup_dir) if args.apply else None
    if backup_dir:
        backup_dir.mkdir(parents=True, exist_ok=True)
    outcome = Counter()
    for finding in findings:
        if args.imdb and finding["imdb_id"] not in args.imdb:
            continue
        row = db.get_virtual_item(finding["token"])
        label = f"{finding['show']} S{int(finding['season']):02d}E{int(finding['episode']):02d}"
        if not row or (row.get("info_hash") or "").lower() != finding["info_hash"].lower():
            outcome["changed since sweep"] += 1
            continue
        if _recently_played(row):
            outcome["played in the last 90 min"] += 1
            continue
        _wait_for_idle_playback()
        imdb_id, season, episode = row["imdb_id"], int(row["season"]), int(row["episode"])
        identity = release_sanity.series_identity(imdb_id, titles.get(imdb_id))
        if identity is None:
            outcome["no identity"] += 1
            continue
        override = processor._episode_search_override(imdb_id, season, episode)
        ranked = search_engine.search_candidates(
            "series", imdb_id, titles.get(imdb_id) or finding["show"], season=season,
            episode=episode, override=override, trigger="identity_repoint",
            prowlarr_on_cache_miss=True)
        ckey = search_engine.content_key(imdb_id, season, episode)
        cached_hashes = search_engine.cache_map(ckey, ranked).get("torbox", set())
        cached = [c for c in ranked if not c.is_usenet and c.info_hash in cached_hashes]
        cached = release_sanity.filter_cached(
            cached, kind="episode", season=season, episode=episode, imdb_id=imdb_id,
            label=label)
        choice, why = choose_replacement(cached, identity, override.get("episode_title"),
                                         row["info_hash"])
        if not choice:
            outcome["no cached release names this show"] += 1
            print(f"  keep   {label}: no cached release names this show "
                  f"({len(cached)} cached candidate(s))", flush=True)
            time.sleep(args.delay)
            continue
        name = release_sanity._release_label(choice)[:80]
        if not args.apply:
            outcome["would repoint"] += 1
            print(f"  plan   {label}: {name} [{choice.source}, {why}]", flush=True)
            time.sleep(args.delay)
            continue
        with open(backup_dir / "rows.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"row": row, "new_hash": choice.info_hash.lower(),
                                 "release": name}) + "\n")
        _backup_stubs(row["strm_path"], backup_dir)
        if not db.repoint_virtual_item(row["token"], row["info_hash"], choice.info_hash,
                                       choice.magnet, choice.quality,
                                       f"identity/{choice.source}", choice.size_gb):
            outcome["changed during repoint"] += 1
            continue
        search_engine.reject(ckey, row["info_hash"], "IDENTITY_MISMATCH")
        search_engine.mark_selected(ckey, choice, "torbox")
        _rewrite_stub(row, choice.quality, choice.size_gb)
        outcome["repointed"] += 1
        print(f"  done   {label}: {name} [{choice.source}, {why}]", flush=True)
        time.sleep(args.delay)
    media_servers._flush()
    print("Outcome:", dict(outcome))
    if backup_dir:
        print(f"Backup: {backup_dir}  (undo with --rollback {backup_dir})")
    return 0


def rollback(args) -> int:
    import config
    import db
    import media_servers
    backup_dir = Path(args.rollback)
    restored = skipped = 0
    for line in open(backup_dir / "rows.jsonl", encoding="utf-8"):
        entry = json.loads(line)
        row = entry["row"]
        current = db.get_virtual_item(row["token"])
        if not current or (current.get("info_hash") or "").lower() != entry["new_hash"]:
            skipped += 1
            continue
        with db._connect() as conn:
            conn.execute(
                f"UPDATE virtual_items SET {', '.join(c + '=?' for c in _BACKUP_COLUMNS)} "
                "WHERE token=?",
                [row.get(c) for c in _BACKUP_COLUMNS] + [row["token"]])
            conn.commit()
        for path in _stub_paths(row["strm_path"]):
            saved = backup_dir / "stubs" / path.relative_to(Path(config.SPORE_MEDIA_PATH))
            if saved.exists():
                shutil.copy2(saved, path)
        media_servers.mark(row["strm_path"])
        restored += 1
    media_servers._flush()
    print(f"Rolled back {restored} item(s); skipped {skipped} changed since the repoint")
    return 0


def main(argv=None) -> int:
    # Resolve app modules from the image when run in the container. Done here,
    # not at import time, so importing this file (tests) never shadows the tree
    # under test with the deployed /app.
    for path in ("/app", "."):
        if path not in sys.path:
            sys.path.insert(0, path)
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--findings")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup-dir",
                    default=f"/data/ops/identity_repoint_{datetime.now():%Y%m%d-%H%M%S}")
    ap.add_argument("--rollback")
    ap.add_argument("--imdb", action="append", default=[])
    ap.add_argument("--duplicate-of", action="append", default=[], metavar="WRONG=RIGHT",
                    help="whole-show suspect WRONG whose content is show RIGHT; its items "
                         "whose episode RIGHT already holds become eligible")
    ap.add_argument("--max-show-share", type=float, default=0.8)
    ap.add_argument("--delay", type=float, default=2.0)
    args = ap.parse_args(argv)
    if args.rollback:
        return rollback(args)
    if not args.findings:
        ap.error("--findings is required")
    return repoint(args)


if __name__ == "__main__":
    sys.exit(main())
