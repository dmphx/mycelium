"""
Sweep existing virtual_items for mislabeled-pack / oversized / no-video stored
hashes and report them (optionally re-queue).

This is the retroactive companion to the grab-time release_sanity guard: the
guard stops NEW bad grabs, this finds items whose hash was stored BEFORE the
guard existed and now resolves (via TorBox check_cached_files) to a season/
complete-series pack, an oversized collection, or a torrent with no usable
single video file for the requested title/episode.

The running image does not ship /app/scripts, so copy this file into the
container first, then run it (it only needs db + torbox + release_sanity):
    docker cp scripts/sweep_mislabeled_releases.py mycelium:/app/
    docker exec -e PYTHONPATH=/app -w /app mycelium \
        python3 /app/sweep_mislabeled_releases.py [options]

Options:
    --category KIND  all (default) | movie | episode  -  which media kind to
                     report/requeue. 'movie' is the mislabeled-pack guard target.
    --requeue        re-enqueue each flagged item for reprocessing (nothing is
                     deleted; the normal pipeline re-resolves with the guard on)
    --token TOKEN    only inspect this single virtual_item token
    --limit N        only inspect the first N items with a stored hash
    --detail-limit N max detailed lines per category (default 20; 0 = all)
    --include-uncached
                     also list items whose hash is not currently cached on TorBox
                     (informational; these can't be verified either way)

Report-only by default. Exit code is always 0 (a report is not an error).
"""
import argparse
import sys

# Importable both in-container (/app on PYTHONPATH) and from the repo root.
for _p in ("/app", "."):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import db
import release_sanity
import torbox

_GB = 1024 ** 3


def _kind_for(item: dict) -> tuple[str, int | None, int | None]:
    if (item.get("media_type") or "").lower() == "movie":
        return "movie", None, None
    season, episode = item.get("season"), item.get("episode")
    if season and episode:
        return "episode", int(season), int(episode)
    # A series virtual_item with no specific episode: treat as a season pack
    # (only rejected when it has no playable video at all).
    return "season_pack", (int(season) if season else None), None


def _label(item: dict, kind: str) -> str:
    title = item.get("title") or "?"
    if kind == "episode":
        return f"{title} S{int(item['season']):02d}E{int(item['episode']):02d}"
    return title


def _category(kind: str, reason: str) -> str:
    """Bucket a finding for the summary. The movie buckets are the direct target
    of this guard; the episode buckets are a separate (larger) pre-existing data
    problem surfaced as a bonus."""
    if kind == "movie":
        if "over movie cap" in reason:
            return "movie:oversized"
        if "episode-tagged" in reason:
            return "movie:is-series-pack"
        if "collection" in reason:
            return "movie:collection"
        if "pack pattern" in reason:
            return "movie:pack-name"
        return "movie:no-video"
    if kind == "episode":
        if "single file tags" in reason:
            return "episode:wrong-episode-file"
        if "identifiable" in reason:
            return "episode:missing-in-pack"
        return "episode:other"
    return "season_pack:no-video"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--requeue", action="store_true",
                    help="re-enqueue flagged items for reprocessing (nothing deleted)")
    ap.add_argument("--token", help="only inspect this single virtual_item token")
    ap.add_argument("--limit", type=int, default=0, help="inspect only the first N items")
    ap.add_argument("--include-uncached", action="store_true",
                    help="also list items whose hash is not currently cached")
    ap.add_argument("--category", default="all",
                    help="only report/requeue this media kind: all | movie | episode")
    ap.add_argument("--detail-limit", type=int, default=20,
                    help="max detailed lines printed per category (0 = all)")
    args = ap.parse_args()
    want_kind = args.category.lower()

    items = db.get_all_virtual_items()
    if args.token:
        items = [i for i in items if i.get("token") == args.token]
    items = [i for i in items if (i.get("info_hash") or "").strip()]
    if args.limit:
        items = items[:args.limit]

    print(f"Sweeping {len(items)} virtual_item(s) with a stored hash "
          f"(verify enabled={release_sanity.enabled()})\n")
    if not items:
        print("Nothing to sweep.")
        return 0

    # Batch the TorBox cache-file lookups by unique hash to minimize API calls.
    uniq = sorted({(i["info_hash"] or "").lower() for i in items})
    entries: dict[str, dict] = {}
    for k in range(0, len(uniq), 100):
        chunk = uniq[k:k + 100]
        try:
            entries.update(torbox.check_cached_files(chunk))
        except Exception as exc:  # noqa: BLE001
            print(f"  ! check_cached_files failed for a chunk of {len(chunk)}: {exc}")
    print(f"Resolved TorBox listings for {len(entries)}/{len(uniq)} unique hash(es).\n")

    from collections import Counter
    flagged: list[tuple[dict, str, str, dict]] = []
    uncached: list[dict] = []
    cat_counts: Counter = Counter()
    for it in items:
        entry = entries.get((it["info_hash"] or "").lower())
        if not entry:
            uncached.append(it)
            continue
        kind, season, episode = _kind_for(it)
        if want_kind != "all" and kind != want_kind and not (
                want_kind == "episode" and kind == "season_pack"):
            continue
        reason = release_sanity.verify_entry(
            entry, kind, season=season, episode=episode, imdb_id=it.get("imdb_id"))
        if reason:
            flagged.append((it, kind, reason, entry))
            cat_counts[_category(kind, reason)] += 1

    if not flagged:
        print("No mislabeled/oversized/no-video stored hashes found among cached items.\n")
    else:
        print(f"==== SUMMARY: {len(flagged)} flagged item(s) ====")
        for cat, n in sorted(cat_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {n:6d}  {cat}")
        print("\n  movie:* = the mislabeled-pack guard's direct target.")
        print("  episode:wrong-episode-file = stored hash is a DIFFERENT episode "
              "(pre-existing data\n     bug; note anime/absolute-numbered shows may include false positives).\n")

        # Detailed listing grouped by category, capped per category.
        by_cat: dict[str, list] = {}
        for row in flagged:
            by_cat.setdefault(_category(row[1], row[2]), []).append(row)
        for cat in sorted(by_cat, key=lambda c: (-len(by_cat[c]), c)):
            rows = by_cat[cat]
            print(f"---- {cat}  ({len(rows)}) ----")
            shown = rows if args.detail_limit == 0 else rows[:args.detail_limit]
            for it, kind, reason, entry in shown:
                size_gb = (entry.get("size") or 0) / _GB
                print(f"  - {_label(it, kind)}")
                print(f"      token={it.get('token')}  imdb={it.get('imdb_id')}  "
                      f"hash={it.get('info_hash')}")
                print(f"      torbox_name={entry.get('name')!r}  ~{size_gb:.1f}GB  "
                      f"files={len(entry.get('files') or [])}")
                print(f"      REASON: {reason}")
            if len(rows) > len(shown):
                print(f"      ... and {len(rows) - len(shown)} more "
                      f"(raise --detail-limit to see all)")
            print()

    if args.include_uncached and uncached:
        print(f"---- {len(uncached)} item(s) not currently cached (unverifiable) ----")
        for it in uncached[:args.detail_limit or None]:
            kind, _, _ = _kind_for(it)
            print(f"  . {_label(it, kind)}  token={it.get('token')}  hash={it.get('info_hash')}")
        print()
    elif uncached:
        print(f"({len(uncached)} item(s) not currently cached were skipped; "
              f"pass --include-uncached to list them.)\n")

    if args.requeue and flagged:
        print(f"Re-queuing {len(flagged)} flagged item(s) (category={want_kind}, nothing deleted)...")
        done = 0
        for it, kind, reason, entry in flagged:
            imdb = it.get("imdb_id")
            try:
                if kind == "movie":
                    if not imdb:
                        print(f"  ! skip {it.get('token')}: no imdb_id to re-enqueue")
                        continue
                    db.enqueue_retry(imdb, it.get("title") or "?", "movie", None, 0, 0)
                else:
                    db.mark_episode_status(imdb, int(it["season"]), int(it["episode"]), "wanted")
                done += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  ! requeue failed for {it.get('token')}: {exc}")
        print(f"Re-queued {done}/{len(flagged)} item(s). The retry queue / series "
              f"monitor will re-resolve them with the grab-time guard active.")
    elif flagged:
        print("(report-only; re-run with --requeue [--category movie] to re-resolve them)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
