"""
Report series virtual_items whose stored release belongs to another national
version of the show: The Traitors UK on The Traitors (US), a Hindi-only India
release on the UK show, "The.Traitors.2023" (the US premiere year) on the 2022
UK show. Uses release_sanity.classify_release, the same check that now guards
release selection, so the report shows what the guard would have refused.

Release names come from, in order: release_candidates (durable search history),
TorBox checkcached (cached torrent name plus file names) and Zilean
/dmm/filtered (raw titles). A show's identity (TMDB origin_country, first-air
year, same-named versions) is only fetched for shows with at least one release
name that carries a country, year or Indian-language word, which keeps TMDB
traffic small.

Strictly report-only: requests.db is opened read-only, TorBox, Zilean and TMDB
only get GET requests, and nothing is written except the optional --json file.

The running image does not ship /app/scripts, so copy the file in first:
    docker cp scripts/sweep_series_identity.py mycelium:/tmp/
    docker exec -w /app mycelium python3 /tmp/sweep_series_identity.py [options]

Options:
    --imdb ID          only this show (repeatable)
    --no-torbox        skip TorBox checkcached name lookups
    --no-zilean        skip Zilean name lookups
    --include-soft     also list soft findings (one-year premiere drift)
    --detail-limit N   max detail lines per show (default 40; 0 = all)
    --json PATH        also write every finding as JSON

Exit code is always 0 (a report is not an error).
"""
import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict

for _p in ("/app", "."):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import db  # noqa: E402


def _read_only_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True,
                           isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# Every db helper (settings.get included) now reads through a read-only handle.
db._raw_connect = _read_only_connect

import release_sanity  # noqa: E402
import requests  # noqa: E402
import settings  # noqa: E402
import torbox  # noqa: E402

_TORBOX_BATCH = 100
_TORBOX_PAUSE_SEC = 1.0
_TMDB_PAUSE_SEC = 0.3


def _load(conn: sqlite3.Connection, only: set[str]) -> tuple[list[dict], dict, dict, dict]:
    items = [dict(r) for r in conn.execute(
        """SELECT token, imdb_id, title, season, episode, info_hash, source, strm_path
           FROM virtual_items
           WHERE media_type='series' AND imdb_id IS NOT NULL AND info_hash IS NOT NULL""")]
    if only:
        items = [item for item in items if item["imdb_id"] in only]
    by_key: dict[tuple[str, str], str] = {}
    by_hash: dict[str, str] = {}
    for row in conn.execute("SELECT content_key, info_hash, title FROM release_candidates"):
        info_hash = (row["info_hash"] or "").lower()
        if not info_hash or not row["title"]:
            continue
        by_key[(row["content_key"], info_hash)] = row["title"]
        by_hash.setdefault(info_hash, row["title"])
    titles = {row["imdb_id"]: row["title"] for row in conn.execute(
        "SELECT imdb_id, title FROM monitored_series")}
    return items, by_key, by_hash, titles


def _torbox_names(hashes: list[str]) -> dict[str, str]:
    """hash -> torrent name plus file names, for hashes TorBox has cached. Uses
    a plain GET so no cache-status rows are written."""
    names: dict[str, str] = {}
    url = f"{torbox._base_url().rstrip('/')}/torrents/checkcached"
    for start in range(0, len(hashes), _TORBOX_BATCH):
        batch = hashes[start:start + _TORBOX_BATCH]
        try:
            resp = requests.get(url, headers=torbox._headers(), timeout=30,
                                params={"hash": ",".join(batch), "format": "object"})
            resp.raise_for_status()
            data = (resp.json() or {}).get("data") or {}
        except requests.RequestException as exc:
            print(f"  TorBox batch {start // _TORBOX_BATCH + 1} failed: "
                  f"{type(exc).__name__}", file=sys.stderr)
            data = {}
        for info_hash, entry in data.items():
            text = release_sanity._entry_names(entry or {})
            # Some cached torrents come back named after their own hash with no
            # file list; that says nothing, so leave them to the other sources.
            if text and text.strip().lower() != info_hash.lower():
                names[info_hash.lower()] = text
        time.sleep(_TORBOX_PAUSE_SEC)
    return names


def _zilean_names(groups: set[tuple[str, int]]) -> dict[str, str]:
    base = str(settings.get("ZILEAN_URL", "") or "").rstrip("/")
    if not base:
        return {}
    names: dict[str, str] = {}
    for imdb_id, season in sorted(groups):
        params = {"ImdbId": imdb_id}
        if season:
            params["Season"] = season
        try:
            resp = requests.get(f"{base}/dmm/filtered", params=params, timeout=20)
            resp.raise_for_status()
            rows = resp.json() or []
        except (requests.RequestException, ValueError):
            continue
        for row in rows:
            info_hash = (row.get("info_hash") or "").lower()
            if info_hash and row.get("raw_title"):
                names.setdefault(info_hash, row["raw_title"])
    return names


def _has_signal(text: str) -> bool:
    for words in release_sanity._qualifier_runs(text):
        if release_sanity._countries(words):
            return True
        if any(release_sanity._YEAR_WORD_RE.fullmatch(w) for w in words):
            return True
    return bool(set(release_sanity._words(text)) & release_sanity._INDIC_LANGUAGES)


def _display_name(text: str) -> str:
    """The first line that carries an episode tag, else the first line."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if release_sanity._EPISODE_TAG_RE.search(line):
            return line
    return lines[0] if lines else "?"


def _content_key(item: dict) -> str:
    return f"{item['imdb_id']}:S{int(item['season'] or 0):02d}E{int(item['episode'] or 0):02d}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--imdb", action="append", default=[])
    ap.add_argument("--no-torbox", action="store_true")
    ap.add_argument("--no-zilean", action="store_true")
    ap.add_argument("--include-soft", action="store_true")
    ap.add_argument("--detail-limit", type=int, default=40)
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    conn = _read_only_connect()
    items, by_key, by_hash, titles = _load(conn, set(args.imdb))
    conn.close()
    hashes = sorted({item["info_hash"].lower() for item in items})
    print(f"Series items: {len(items)}  distinct hashes: {len(hashes)}  "
          f"shows: {len({item['imdb_id'] for item in items})}")

    tb_names = {} if args.no_torbox else _torbox_names(hashes)
    # Zilean indexes the DMM hash lists most catalogs draw from; ask it about
    # every show/season that still holds an unnamed item.
    unnamed_groups = {
        (item["imdb_id"], int(item["season"] or 0)) for item in items
        if item["info_hash"].lower() not in tb_names
        and item["info_hash"].lower() not in by_hash
    }
    if not args.no_zilean:
        print(f"Zilean lookups for {len(unnamed_groups)} show/season group(s)")
    zl_names = {} if args.no_zilean else _zilean_names(unnamed_groups)

    texts: dict[str, str] = {}
    sources: dict[str, str] = {}
    unnamed = defaultdict(int)
    for item in items:
        info_hash = item["info_hash"].lower()
        parts, used = [], []
        rc_name = by_key.get((_content_key(item), info_hash)) or by_hash.get(info_hash)
        for label, value in (("history", rc_name), ("torbox", tb_names.get(info_hash)),
                             ("zilean", zl_names.get(info_hash))):
            if value:
                parts.append(value)
                used.append(label)
        if not parts:
            unnamed[item["imdb_id"]] += 1
            continue
        texts[item["token"]] = "\n".join(parts)
        sources[item["token"]] = "+".join(used)
    print(f"Named items: {len(texts)} (TorBox names {len(tb_names)}, Zilean names "
          f"{len(zl_names)}, history {len(by_hash)})  unnamed: {sum(unnamed.values())}")

    signal_cache: dict[str, bool] = {}
    signal_shows = set()
    for item in items:
        text = texts.get(item["token"])
        if text is None:
            continue
        if text not in signal_cache:
            signal_cache[text] = _has_signal(text)
        if signal_cache[text]:
            signal_shows.add(item["imdb_id"])
    signal_shows = sorted(signal_shows)
    print(f"Shows with a qualifier word in some release name: {len(signal_shows)}")
    release_sanity.identity_check_enabled = lambda: True
    identities = {}
    for imdb_id in signal_shows:
        identities[imdb_id] = release_sanity.series_identity(imdb_id, titles.get(imdb_id))
        time.sleep(_TMDB_PAUSE_SEC)

    findings = []
    verdicts: dict[tuple[str, str], tuple[str, str]] = {}
    for item in items:
        identity = identities.get(item["imdb_id"])
        text = texts.get(item["token"])
        if identity is None or not text:
            continue
        key = (item["imdb_id"], text)
        if key not in verdicts:
            verdicts[key] = release_sanity.classify_release(text, identity)
        verdict, reason = verdicts[key]
        if verdict == release_sanity.IDENTITY_REJECT or (
                args.include_soft and verdict == release_sanity.IDENTITY_SOFT):
            findings.append({
                "imdb_id": item["imdb_id"], "show": identity.name,
                "regions": sorted(identity.regions), "first_air_year": identity.year,
                "season": item["season"], "episode": item["episode"],
                "token": item["token"], "info_hash": item["info_hash"],
                "source": item["source"], "verdict": verdict, "reason": reason,
                "release": _display_name(text),
                "name_from": sources[item["token"]], "strm_path": item["strm_path"],
            })

    by_show = defaultdict(list)
    for finding in findings:
        by_show[(finding["show"], finding["imdb_id"])].append(finding)
    print(f"\nFlagged items: {len(findings)} across {len(by_show)} show(s)\n")
    for (show, imdb_id), rows in sorted(by_show.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        first = rows[0]
        print(f"== {show} ({imdb_id}, {'/'.join(first['regions']) or '?'} "
              f"{first['first_air_year'] or '?'}): {len(rows)} item(s)")
        rows.sort(key=lambda r: (int(r["season"] or 0), int(r["episode"] or 0)))
        shown = rows if args.detail_limit == 0 else rows[:args.detail_limit]
        for r in shown:
            print(f"   S{int(r['season'] or 0):02d}E{int(r['episode'] or 0):02d} "
                  f"{r['verdict']:<6} [{r['source'] or '?'} via {r['name_from']}] "
                  f"{r['release'][:90]}  ({r['reason']})")
        if len(shown) < len(rows):
            print(f"   ... {len(rows) - len(shown)} more")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(findings, fh, indent=1)
        print(f"\nJSON written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
