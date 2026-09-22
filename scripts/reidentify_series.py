"""Move a show's library content to the IMDb id it actually belongs to.

scripts/sweep_series_identity.py flags items whose release is another national
version of their show. scripts/repoint_series_identity.py fixes the mixed case:
a few foreign releases inside an otherwise correct show, replaced by releases
that name the show. This tool fixes the opposite case: a show whose items are
nearly all another version, which means the request resolved to the wrong imdb
and the content people actually watch sits under it.

Repointing those would replace what people watch. Re-identifying keeps the
release and moves the item to the show it is: the catbox token, and therefore
the .strm URL and the Spore stub's token, stay the same, so a moved episode
keeps playing. Only imdb_id, the title and the paths change.

The sweep found no case where a release's own SxxExx tag disagreed with the
slot it sits in, so season/episode are kept as they are. What differs between
two national versions is which episodes exist at all, and that decides each
item's disposition:

    move       the right show has this episode and no item for it yet
    duplicate  the right show already holds this episode (--on-duplicate)
    ghost      the episode does not exist in the right show, so the slot is a
               real slot of the WRONG show that got filled with foreign
               content: the item is removed and the slot requeued for search
    keep       nothing safe to do; reported and left alone

Action "empty" treats every item of a show as a ghost. That is for a show that
is wanted as itself (UK Hell's Kitchen) rather than re-identified: its items
are removed and its episodes requeued, so the monitor refills them, now with
release_sanity.classify_release refusing the other version at selection.

Only items the sweep flagged, and that still hold the flagged release, are
touched. An item repointed since the sweep is left alone.

Read-only by default: without --apply nothing is written, no search runs, and
TMDB and the media servers only get GET requests. --apply first saves every
row and file it will change to --backup-dir, then applies. --rollback DIR
undoes an applied run.

The running image does not ship /app/scripts, so copy the file in first:
    docker cp scripts/reidentify_series.py mycelium:/tmp/
    docker exec -w /app mycelium python3 /tmp/reidentify_series.py \\
        --plan /tmp/plan.json \\
        --findings /data/ops/series_identity_findings_<date>.json [--apply]

The plan is the list of shows the user approved, one entry per show:
    [{"wrong": "tt0412137", "right": "tt0437005", "action": "reidentify"},
     {"wrong": "tt0377260", "right": "tt1586680", "action": "empty"},
     {"wrong": "tt0283799", "action": "skip", "note": "no right show yet"}]
"""
import argparse
import json
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

MOVE = "move"
DUPLICATE = "duplicate"
GHOST = "ghost"
KEEP = "keep"

REIDENTIFY = "reidentify"
EMPTY = "empty"
SKIP = "skip"
ACTIONS = (REIDENTIFY, EMPTY, SKIP)

_IMDB_RE = re.compile(r"^tt\d{6,10}$")
_RECENT_PLAY = timedelta(minutes=90)
# virtual_items columns a move rewrites. Everything else (token, info_hash,
# magnet, quality, torbox_id, file_id, spore_tracks, play_count) belongs to the
# release and to what people watched, and survives the move untouched.
_MOVE_COLUMNS = ("imdb_id", "title", "strm_path")


# ── plan and dispositions (pure) ─────────────────────────────────────────────

def load_plan(payload) -> list[dict]:
    """Validate the approved per-show plan. Raises ValueError on bad input."""
    if isinstance(payload, dict):
        payload = payload.get("shows", payload.get("plan"))
    if not isinstance(payload, list):
        raise ValueError("plan must be a list of show entries")
    seen: set[str] = set()
    entries = []
    for raw in payload:
        if not isinstance(raw, dict):
            raise ValueError(f"plan entry is not an object: {raw!r}")
        wrong = str(raw.get("wrong") or "").strip()
        right = str(raw.get("right") or "").strip() or None
        action = str(raw.get("action") or REIDENTIFY).strip().lower()
        if not _IMDB_RE.match(wrong):
            raise ValueError(f"plan entry has no valid 'wrong' imdb id: {raw!r}")
        if wrong in seen:
            raise ValueError(f"plan lists {wrong} twice")
        if action not in ACTIONS:
            raise ValueError(f"{wrong}: action must be one of {ACTIONS}, got {action!r}")
        if right is not None and not _IMDB_RE.match(right):
            raise ValueError(f"{wrong}: 'right' is not an imdb id: {right!r}")
        if action == REIDENTIFY and not right:
            raise ValueError(f"{wrong}: action 'reidentify' needs a 'right' imdb id")
        if right == wrong:
            raise ValueError(f"{wrong}: 'right' and 'wrong' are the same show")
        seen.add(wrong)
        entries.append({"wrong": wrong, "right": right, "action": action,
                        "note": str(raw.get("note") or "")})
    return entries


def classify(season, episode, action: str, right_slots: set,
             right_episodes: set | None, on_duplicate: str) -> tuple[str, str]:
    """Decide what happens to one still-wrong item, with the reason.

    right_slots are the episodes the right show already holds an item for.
    right_episodes are the episodes its TMDB metadata has, or None when that
    metadata could not be read, which is never a reason to delete anything."""
    if action == EMPTY:
        return GHOST, "show is wanted as itself, not re-identified"
    slot = (int(season), int(episode))
    if right_episodes is None:
        return KEEP, "no episode metadata for the right show"
    if slot not in right_episodes:
        return GHOST, "the right show has no such episode"
    if slot in right_slots:
        if on_duplicate == "keep":
            return KEEP, "the right show already holds this episode"
        return DUPLICATE, "the right show already holds this episode"
    return MOVE, "the right show lacks this episode"


def episode_stem(folder: str, season, episode) -> str:
    """The base name mycelium gives an episode: the show folder plus SxxExx."""
    return f"{folder} S{int(season):02d}E{int(episode):02d}"


def episode_strm(media_root, folder: str, season, episode) -> Path:
    return (Path(media_root) / "series" / folder / f"Season {int(season):02d}"
            / (episode_stem(folder, season, episode) + ".strm"))


def planned_moves(old_strm: Path, new_strm: Path,
                  old_stub_dir: Path, new_stub_dir: Path) -> list[tuple[Path, Path]]:
    """Every (source, target) pair one moved episode needs: the .strm Jellyfin
    reads, its per-episode NFO, and the Spore stub pair Plex reads. The stub
    keeps its .minfo, so the token and therefore playback survive the move."""
    if old_strm == new_strm:
        return []
    pairs = [(old_strm, new_strm),
             (old_strm.with_suffix(".nfo"), new_strm.with_suffix(".nfo"))]
    for ext in (".mkv", ".minfo"):
        pairs.append((old_stub_dir / (old_strm.stem + ext),
                      new_stub_dir / (new_strm.stem + ext)))
    return pairs


def retag_episode_nfo(text: str, right_imdb: str, right_tmdb: int | None) -> str:
    """Point a per-episode NFO at the show its episode moved to. The ids are
    rewritten in place rather than the file regenerated, because a Spore probe
    may already have written real <streamdetails> into it and Plex reads those
    to decide whether it can direct play."""
    text = re.sub(r"(<uniqueid[^>]*type=['\"]imdb['\"][^>]*>)tt\d+(</uniqueid>)",
                  lambda m: m.group(1) + right_imdb + m.group(2), text,
                  flags=re.IGNORECASE)
    tmdb_tag = r"[ \t]*<uniqueid[^>]*type=['\"]tmdb['\"][^>]*>\d+</uniqueid>\n?"
    if right_tmdb:
        text = re.sub(r"(<uniqueid[^>]*type=['\"]tmdb['\"][^>]*>)\d+(</uniqueid>)",
                      lambda m: m.group(1) + str(right_tmdb) + m.group(2), text,
                      flags=re.IGNORECASE)
    else:
        # A tmdb id for the show it no longer is would keep pointing Plex at
        # the wrong show, and there is nothing to put in its place.
        text = re.sub(tmdb_tag, "", text, flags=re.IGNORECASE)
    return text


def seasons_union(existing: str | None, seasons) -> list[int]:
    """Merge a monitored_series.seasons string with the seasons being moved in."""
    values = set()
    for part in str(existing or "").split(","):
        part = part.strip()
        if part.isdigit():
            values.add(int(part))
    values.update(int(s) for s in seasons)
    return sorted(values)


def still_wrong(item: dict, finding: dict | None) -> bool:
    """True when the sweep flagged this item and it still holds that release."""
    if not finding:
        return False
    return (item.get("info_hash") or "").lower() == (finding.get("info_hash") or "").lower()


def plex_file_to_strm(plex_file: str | None, plex_tv_root: str, media_root) -> str | None:
    """The .strm behind a file Plex has open. Plex sees the Spore stub under
    its TV root (/mnt/library/shows/Show/Season 01/Show S01E01.mkv); mycelium
    keeps the matching .strm under MEDIA_PATH/series with the same relative
    path."""
    root = plex_tv_root.rstrip("/") + "/"
    if not plex_file or not plex_file.startswith(root):
        return None
    rel = plex_file[len(root):]
    if rel.lower().endswith(".mkv"):
        rel = rel[:-4] + ".strm"
    return str(Path(media_root) / "series" / rel)


def _recently_played(row: dict) -> bool:
    try:
        played = datetime.strptime(row.get("last_played") or "", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return datetime.now(timezone.utc).replace(tzinfo=None) - played < _RECENT_PLAY


# ── library reads ────────────────────────────────────────────────────────────

def _series_items(conn, imdb_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT * FROM virtual_items WHERE imdb_id=? AND media_type='series'
           ORDER BY season, episode""", (imdb_id,))]


def _show_context(conn, imdb_id: str | None) -> dict:
    """Everything about one show that a person needs to judge the plan."""
    if not imdb_id:
        return {}
    items = _series_items(conn, imdb_id)
    monitored = conn.execute("SELECT * FROM monitored_series WHERE imdb_id=?",
                             (imdb_id,)).fetchone()
    request = conn.execute("SELECT * FROM requests WHERE imdb_id=?", (imdb_id,)).fetchone()
    wanted = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM wanted_episodes WHERE imdb_id=? GROUP BY status",
        (imdb_id,))}
    folders = sorted({str(Path(i["strm_path"]).parent.parent) for i in items
                      if i.get("strm_path")})
    return {
        "imdb_id": imdb_id,
        "items": items,
        "slots": {(int(i["season"]), int(i["episode"])) for i in items
                  if i.get("season") is not None and i.get("episode") is not None},
        "monitored": dict(monitored) if monitored else None,
        "request": dict(request) if request else None,
        "wanted": wanted,
        "plays": sum(i.get("play_count") or 0 for i in items),
        "folders": folders,
        "nfo_imdb": {folder: _folder_nfo_imdb(Path(folder)) for folder in folders},
    }


def _folder_nfo_imdb(folder: Path) -> str | None:
    nfo = folder / "tvshow.nfo"
    try:
        text = nfo.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    found = re.search(r"<uniqueid[^>]*type=['\"]imdb['\"][^>]*>(tt\d+)</uniqueid>",
                      text, re.IGNORECASE)
    return found.group(1) if found else None


def _tmdb_episodes(tmdb_id: int | None, seasons) -> set | None:
    """The (season, episode) pairs the right show's TMDB metadata has, or None
    when it could not be read for every season we need to judge."""
    if not tmdb_id:
        return None
    import tmdb as _tmdb
    known = set()
    for season in sorted({int(s) for s in seasons}):
        try:
            episodes = _tmdb.get_season_episodes(tmdb_id, season) or []
        except Exception as exc:
            print(f"    TMDB season {season} lookup failed: {type(exc).__name__}: {exc}")
            return None
        known.update((season, int(ep.get("episode_number") or 0)) for ep in episodes)
        time.sleep(0.25)
    return known


def _right_show_facts(right_imdb: str, context: dict) -> tuple[int | None, str | None]:
    """The right show's TMDB id and canonical title, from the library first."""
    import tmdb as _tmdb
    monitored = context.get("monitored") or {}
    tmdb_id = monitored.get("tmdb_id")
    title = monitored.get("title")
    if not tmdb_id:
        try:
            tmdb_id = _tmdb.find_by_imdb(right_imdb, "tv")
        except Exception as exc:
            print(f"    TMDB id lookup failed for {right_imdb}: {type(exc).__name__}")
            tmdb_id = None
    if not title and tmdb_id:
        try:
            info = _tmdb.get_show_info(tmdb_id) or {}
            title = info.get("name")
        except Exception:
            title = None
    return tmdb_id, title


def _right_folder(right_imdb: str, title: str | None) -> str:
    """The folder the right show's episodes belong in. Reuses the folder it
    already owns, so a show whose wrong and right halves share one folder
    (Antiques Roadshow) needs no file moves at all."""
    import strm_generator
    folder = strm_generator._canonical_series_folder(right_imdb, fallback_title=title)
    return folder or strm_generator._safe(title or right_imdb)


# ── planning ─────────────────────────────────────────────────────────────────

def build_show_plan(conn, entry: dict, findings: dict, on_duplicate: str) -> dict:
    """Everything one show needs, read-only: context for both sides and a
    disposition for every still-wrong item."""
    import strm_generator
    wrong_ctx = _show_context(conn, entry["wrong"])
    right_ctx = _show_context(conn, entry["right"])
    plan = {"entry": entry, "wrong": wrong_ctx, "right": right_ctx,
            "items": [], "counts": Counter(), "warnings": []}
    flagged = findings.get(entry["wrong"], {})
    candidates = [item for item in wrong_ctx.get("items", [])
                  if still_wrong(item, flagged.get(item["token"]))]
    plan["flagged"] = len(flagged)
    plan["candidates"] = len(candidates)
    if entry["action"] == SKIP or not candidates:
        return plan

    right_episodes = None
    tmdb_id = title = None
    folder = None
    if entry["action"] == REIDENTIFY:
        tmdb_id, title = _right_show_facts(entry["right"], right_ctx)
        right_episodes = _tmdb_episodes(
            tmdb_id, {int(i["season"]) for i in candidates if i.get("season") is not None})
        folder = _right_folder(entry["right"], title)
        if not folder:
            plan["warnings"].append("could not work out a folder for the right show")
            return plan
    media_root = Path(strm_generator.MEDIA_PATH)
    plan["right_tmdb_id"] = tmdb_id
    plan["right_title"] = title
    plan["right_folder"] = folder
    if folder:
        plan["right_folder_nfo"] = _folder_nfo_imdb(media_root / "series" / folder)

    for item in candidates:
        season, episode = item.get("season"), item.get("episode")
        if season is None or episode is None:
            plan["items"].append({"item": item, "disposition": KEEP,
                                  "reason": "item has no season/episode"})
            plan["counts"][KEEP] += 1
            continue
        disposition, reason = classify(season, episode, entry["action"],
                                       right_ctx.get("slots", set()), right_episodes,
                                       on_duplicate)
        row = {"item": item, "disposition": disposition, "reason": reason}
        if disposition == MOVE:
            old_strm = Path(item["strm_path"])
            new_strm = episode_strm(media_root, folder, season, episode)
            if new_strm != old_strm and new_strm.exists():
                row["disposition"] = KEEP
                row["reason"] = f"a file already sits at {new_strm}"
            else:
                row["new_strm"] = str(new_strm)
                row["new_title"] = episode_stem(folder, season, episode)
                row["moves"] = [(str(src), str(dst)) for src, dst in planned_moves(
                    old_strm, new_strm,
                    strm_generator._spore_stub_dir(old_strm),
                    strm_generator._spore_stub_dir(new_strm))]
        plan["items"].append(row)
        plan["counts"][row["disposition"]] += 1
    return plan


# ── reporting ────────────────────────────────────────────────────────────────

def _side(label: str, ctx: dict, lines: list) -> None:
    if not ctx:
        lines.append(f"  {label}: none given")
        return
    monitored = ctx.get("monitored")
    request = ctx.get("request")
    wanted = ", ".join(f"{k}={v}" for k, v in sorted(ctx["wanted"].items())) or "none"
    lines.append(f"  {label} {ctx['imdb_id']}: {len(ctx['items'])} item(s), "
                 f"{ctx['plays']} play(s)")
    lines.append("    monitored: " + (
        f"{monitored['title']!r} tmdb={monitored['tmdb_id']} "
        f"status={monitored['status']} seasons={monitored['seasons']}"
        if monitored else "no"))
    lines.append(f"    wanted_episodes: {wanted}")
    lines.append("    request: " + (
        f"{request['title']!r} status={request['status']} seasons={request['seasons']}"
        if request else "none"))
    for folder in ctx["folders"]:
        lines.append(f"    folder {folder}  tvshow.nfo={ctx['nfo_imdb'][folder] or 'missing'}")


def report(plan: dict) -> str:
    entry = plan["entry"]
    wrong_name = ((plan["wrong"].get("monitored") or {}).get("title")
                  or entry["wrong"])
    lines = [f"===== {wrong_name} ({entry['wrong']}) -> {entry['right'] or '?'}"
             f"   action={entry['action']}"]
    if entry["note"]:
        lines.append(f"  note: {entry['note']}")
    _side("wrong ", plan["wrong"], lines)
    _side("right ", plan["right"], lines)
    lines.append(f"  flagged by the sweep: {plan['flagged']}, still holding that "
                 f"release: {plan['candidates']}")
    if plan.get("right_folder"):
        shared = plan["right_folder"] in {Path(f).name for f in plan["wrong"]["folders"]}
        lines.append(f"  right show folder: {plan['right_folder']} "
                     f"(tmdb {plan.get('right_tmdb_id')})"
                     + ("  [the same folder both sides already share: "
                        "no files move]" if shared else ""))
        if any(row["disposition"] == MOVE for row in plan["items"]):
            lines.append("    will " + ("extend" if plan["right"].get("monitored")
                                        else "create") + " its monitored_series row"
                         + ("" if plan["right"].get("request")
                            else " and add a requests row"))
            folder_nfo = plan.get("right_folder_nfo")
            if folder_nfo and folder_nfo != entry["right"]:
                lines.append(f"    its tvshow.nfo says {folder_nfo}: it is repointed to "
                             f"{entry['right']} once every item in the folder is that show")
    for warning in plan["warnings"]:
        lines.append(f"  WARNING: {warning}")
    by_disposition = defaultdict(list)
    for row in plan["items"]:
        by_disposition[row["disposition"]].append(row)
    for disposition in (MOVE, DUPLICATE, GHOST, KEEP):
        rows = by_disposition.get(disposition)
        if not rows:
            continue
        plays = sum(r["item"].get("play_count") or 0 for r in rows)
        slots = ", ".join(
            f"S{int(r['item']['season']):02d}E{int(r['item']['episode']):02d}"
            for r in rows[:30] if r["item"].get("season") is not None)
        if len(rows) > 30:
            slots += f", ... {len(rows) - 30} more"
        lines.append(f"  {disposition}: {len(rows)} item(s), {plays} play(s)"
                     f"  [{rows[0]['reason']}]")
        lines.append(f"    {slots}")
    return "\n".join(lines)


# ── applying ─────────────────────────────────────────────────────────────────

def _backup_file(path: Path, backup_dir: Path) -> str | None:
    """Copy a file we are about to delete, keyed by its absolute path."""
    if not path.exists():
        return None
    target = backup_dir / "files" / path.as_posix().lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return str(path)


def _requeue_slot(db, imdb_id: str, season, episode) -> dict | None:
    """Put one episode back in the search queue, whatever state it was in, and
    return the row as it was so a rollback can restore it."""
    with db._connect() as conn:
        before = conn.execute(
            "SELECT * FROM wanted_episodes WHERE imdb_id=? AND season=? AND episode=?",
            (imdb_id, int(season), int(episode))).fetchone()
        if not before:
            return None
        conn.execute(
            """UPDATE wanted_episodes SET status='wanted', attempt_count=0,
                   last_attempted=NULL
               WHERE imdb_id=? AND season=? AND episode=?""",
            (imdb_id, int(season), int(episode)))
        conn.commit()
    return dict(before)


def _mark_found(db, right_imdb: str, tmdb_id: int | None, title: str,
                season, episode) -> dict | None:
    """Record the moved episode as held by the right show. Returns the previous
    row, or None when there was none, so a rollback can undo either case."""
    with db._connect() as conn:
        before = conn.execute(
            "SELECT * FROM wanted_episodes WHERE imdb_id=? AND season=? AND episode=?",
            (right_imdb, int(season), int(episode))).fetchone()
    before = dict(before) if before else None
    db.upsert_wanted_episode(right_imdb, tmdb_id, title, int(season), int(episode), None)
    db.mark_episode_status(right_imdb, int(season), int(episode), "found")
    return before


def _move_files(moves: list) -> list:
    """Move an episode's files, creating target folders. Returns the pairs that
    actually moved, so a partial failure can still be undone."""
    done = []
    for src_str, dst_str in moves:
        src, dst = Path(src_str), Path(dst_str)
        if not src.exists() or src == dst:
            continue
        if dst.exists():
            print(f"    skip {src.name}: {dst} already exists")
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except OSError:
            _undo_moves(done)
            raise
        done.append([str(src), str(dst)])
    return done


def _retag_nfo(path: Path, right_imdb: str, right_tmdb: int | None) -> str | None:
    """Rewrite one episode NFO's ids. Returns the text it had, for rollback."""
    try:
        before = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    after = retag_episode_nfo(before, right_imdb, right_tmdb)
    if after == before:
        return None
    import io_utils
    io_utils.atomic_write_text(path, after)
    return before


def _undo_moves(moved: list) -> None:
    for src_str, dst_str in reversed(moved):
        source, target = Path(dst_str), Path(src_str)
        if source.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))


def _delete_item(db, strm_generator, item: dict, backup_dir: Path) -> list:
    """Remove one item and its files, after copying them into the backup."""
    saved = []
    strm = Path(item["strm_path"]) if item.get("strm_path") else None
    if strm:
        stub_dir = strm_generator._spore_stub_dir(strm)
        for path in (strm, strm.with_suffix(".nfo"),
                     stub_dir / (strm.stem + ".mkv"), stub_dir / (strm.stem + ".minfo")):
            if _backup_file(path, backup_dir):
                saved.append(str(path))
            path.unlink(missing_ok=True)
    db.delete_virtual_item(item["token"])
    return saved


def _ensure_right_show(db, strm_generator, plan: dict, args) -> dict:
    """Make the right show a real show in the library: a folder with a
    tvshow.nfo carrying its imdb id, a monitored_series row covering the
    seasons moving in, and a request row so the UI stops calling it missing.
    Returns what was there before, for rollback."""
    entry, folder = plan["entry"], plan["right_folder"]
    right_imdb, title = entry["right"], plan.get("right_title") or folder
    before = {"monitored": plan["right"].get("monitored"),
              "request": plan["right"].get("request"),
              "created": []}
    seasons = sorted({int(row["item"]["season"]) for row in plan["items"]
                      if row["disposition"] == MOVE})
    if not seasons:
        return before
    series_root = Path(strm_generator.MEDIA_PATH) / "series" / folder
    series_root.mkdir(parents=True, exist_ok=True)
    tvshow_nfo = series_root / "tvshow.nfo"
    if not tvshow_nfo.exists():
        # _write_nfo takes the show title from the folder the .strm sits in,
        # so any name inside series_root gives it the right one.
        strm_generator._write_nfo(series_root / "show.strm", right_imdb,
                                  tmdb_id=plan.get("right_tmdb_id"),
                                  media_type="series", nfo_path=tvshow_nfo,
                                  show_title=title)
        before["created"].append(str(tvshow_nfo))
        try:
            import nfo_generator
            nfo_generator.fetch_images_for_folder(series_root, right_imdb, "tv")
        except Exception as exc:
            print(f"    image fetch skipped for {folder}: {type(exc).__name__}")
    if args.monitor_right:
        current = before["monitored"]
        db.upsert_monitored_series(
            right_imdb, plan.get("right_tmdb_id"), title,
            seasons_union((current or {}).get("seasons"), seasons),
            (current or {}).get("monitor_mode") or "all")
        if not current:
            before["created"].append(f"monitored_series {right_imdb}")
    if args.request_row and not before["request"]:
        request_id = db.insert_request(title, right_imdb, "series", seasons,
                                       plan.get("right_tmdb_id"))
        db.update_request(request_id, "success", source="reidentify")
        before["created"].append(f"requests {right_imdb}")
    return before


def apply_show(plan: dict, args, backup_dir: Path | None) -> Counter:
    """Apply one show's plan. Without a backup_dir this only prints."""
    import db
    import media_servers
    import strm_generator

    entry = plan["entry"]
    outcome = Counter()
    dry = backup_dir is None
    actionable = [row for row in plan["items"] if row["disposition"] != KEEP]
    if not actionable:
        return outcome
    if not dry and any(row["disposition"] == MOVE for row in actionable):
        show_before = _ensure_right_show(db, strm_generator, plan, args)
        with open(backup_dir / "shows.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"wrong": entry["wrong"], "right": entry["right"],
                                 "before": show_before}, default=str) + "\n")

    for row in actionable:
        item = row["item"]
        season, episode = item.get("season"), item.get("episode")
        label = (f"{entry['wrong']} S{int(season):02d}E{int(episode):02d}"
                 if season is not None else f"{entry['wrong']} {item['token']}")
        open_paths: set = set()
        if not dry:
            # Wait first, then read the row, so a play that happened during
            # the wait still counts as recent below.
            if getattr(args, "allow_paused", False):
                open_paths = _wait_for_no_stream()
            else:
                _wait_for_idle_playback()
        current = db.get_virtual_item(item["token"])
        if not current or (current.get("info_hash") or "").lower() != (
                item.get("info_hash") or "").lower():
            outcome["changed since the plan"] += 1
            continue
        if _recently_played(current):
            outcome["played in the last 90 min"] += 1
            print(f"  keep   {label}: played in the last 90 minutes")
            continue
        if current.get("strm_path") in open_paths:
            outcome["open in a paused Plex session"] += 1
            print(f"  keep   {label}: open in a paused Plex session")
            continue

        if row["disposition"] == MOVE:
            if dry:
                print(f"  move   {label} -> {entry['right']} {row['new_strm']}")
                outcome["would move"] += 1
                continue
            record = {"kind": MOVE, "row": current, "right": entry["right"],
                      "new_strm": row["new_strm"], "new_title": row["new_title"],
                      "moved": []}
            record["moved"] = _move_files(row["moves"])
            try:
                with db._connect() as conn:
                    conn.execute(
                        "UPDATE virtual_items SET imdb_id=?, title=?, strm_path=? "
                        "WHERE token=?",
                        (entry["right"], row["new_title"], row["new_strm"],
                         item["token"]))
                    conn.commit()
            except Exception:
                # The files already moved but the row did not follow. Put them
                # back so the item keeps playing, then let the run fail loudly.
                _undo_moves(record["moved"])
                raise
            record["nfo_before"] = _retag_nfo(
                Path(row["new_strm"]).with_suffix(".nfo"), entry["right"],
                plan.get("right_tmdb_id"))
            record["wrong_wanted"] = _requeue_slot(db, entry["wrong"], season, episode)
            record["right_wanted"] = _mark_found(
                db, entry["right"], plan.get("right_tmdb_id"),
                plan.get("right_title") or plan["right_folder"], season, episode)
            _reject_for_wrong_show(entry["wrong"], item, season, episode)
            if item["strm_path"] != row["new_strm"]:
                # Nothing left the old season folder when both shows already
                # share it, and telling Plex otherwise only costs a scan.
                media_servers.mark_removed(Path(item["strm_path"]))
            media_servers.mark(Path(row["new_strm"]))
            _record(backup_dir, record)
            outcome["moved"] += 1
            print(f"  move   {label} -> {row['new_strm']}")
            continue

        # A duplicate and a ghost both end with the item gone and the wrong
        # show's own slot back in the search queue. They differ only in why:
        # a duplicate is redundant, a ghost never belonged here.
        verb = "drop  " if row["disposition"] == DUPLICATE else "empty "
        if dry:
            print(f"  {verb} {label}: {row['reason']}")
            outcome[f"would {row['disposition']}"] += 1
            continue
        record = {"kind": row["disposition"], "row": current}
        record["files"] = _delete_item(db, strm_generator, current, backup_dir)
        record["wrong_wanted"] = _requeue_slot(db, entry["wrong"], season, episode)
        _reject_for_wrong_show(entry["wrong"], item, season, episode)
        if item.get("strm_path"):
            media_servers.mark_removed(Path(item["strm_path"]))
        _record(backup_dir, record)
        outcome[row["disposition"]] += 1
        print(f"  {verb} {label}: {row['reason']}")

    if not dry and outcome["moved"] and plan.get("right_folder"):
        if _retitle_folder_nfo(db, strm_generator, plan, backup_dir):
            outcome["tvshow.nfo corrected"] += 1
            moved = next(row["new_strm"] for row in plan["items"]
                         if row["disposition"] == MOVE)
            media_servers.mark(Path(moved))
    return outcome


def _retitle_folder_nfo(db, strm_generator, plan: dict, backup_dir: Path) -> bool:
    """Point a folder's tvshow.nfo at the right show once every item under it
    belongs to that show. Several of these shows never had two folders: the
    wrong and the right imdb already share one, and its tvshow.nfo is what Plex
    and Jellyfin match the whole folder on. Left alone, the folder would keep
    claiming a show none of its episodes are."""
    right_imdb = plan["entry"]["right"]
    folder = Path(strm_generator.MEDIA_PATH) / "series" / plan["right_folder"]
    nfo = folder / "tvshow.nfo"
    current = _folder_nfo_imdb(folder)
    if not current or current == right_imdb:
        return False
    holders = db.get_virtual_item_imdb_ids_under_path(str(folder))
    if holders != {right_imdb}:
        print(f"    tvshow.nfo in {folder.name} still says {current}: the folder also "
              f"holds {sorted(holders - {right_imdb})}")
        return False
    _backup_file(nfo, backup_dir)
    _record(backup_dir, {"kind": "nfo", "row": {"token": None}, "files": [str(nfo)],
                         "path": str(nfo), "was": current, "now": right_imdb})
    nfo.unlink()
    strm_generator._write_nfo(folder / "show.strm", right_imdb,
                              tmdb_id=plan.get("right_tmdb_id"),
                              media_type="series", nfo_path=nfo,
                              show_title=plan.get("right_title"))
    print(f"    tvshow.nfo in {folder.name}: {current} -> {right_imdb}")
    return True


def _reject_for_wrong_show(wrong_imdb: str, item: dict, season, episode) -> None:
    """Teach the search history that this release does not belong to the wrong
    show, so the refill of the slot we just vacated cannot pick it again."""
    info_hash = (item.get("info_hash") or "").strip()
    if not info_hash:
        return
    try:
        import search_engine
        search_engine.reject(
            search_engine.content_key(wrong_imdb, int(season), int(episode)),
            info_hash, "WRONG_SERIES_IDENTITY")
    except Exception as exc:
        print(f"    search history note skipped: {type(exc).__name__}: {exc}")


def _record(backup_dir: Path, record: dict) -> None:
    with open(backup_dir / "rows.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def _wait_for_idle_playback() -> None:
    import playback_guard
    while playback_guard.active(force=True):
        print("  playback active, waiting 60s", flush=True)
        time.sleep(60)


_sessions_cache: dict = {"at": 0.0, "value": None}


def _plex_sessions() -> tuple[bool, set] | None:
    """(is anything streaming, .strm paths open in any session), or None when
    Plex could not be asked. A paused session counts as open, not streaming.
    Cached for 30 seconds so a long run asks Plex a few times a minute."""
    now = time.monotonic()
    if _sessions_cache["value"] is not None and now - _sessions_cache["at"] < 30:
        return _sessions_cache["value"]
    import os
    import xml.etree.ElementTree as ET

    import media_servers
    import requests
    import settings
    import strm_generator
    url = (os.environ.get("PLEX_URL") or str(settings.get("PLEX_URL", "") or "")).rstrip("/")
    token = os.environ.get("PLEX_TOKEN") or str(settings.get("PLEX_TOKEN", "") or "")
    if not (url and token):
        return None
    headers = {"X-Plex-Token": token}
    try:
        root = ET.fromstring(requests.get(url + "/status/sessions", headers=headers,
                                          timeout=5).content)
        streaming, open_paths = False, set()
        for node in root:
            player = next((c for c in node if c.tag == "Player"), None)
            if (player is None or (player.get("state") or "").lower()
                    in {"playing", "buffering"}):
                streaming = True
            # /status/sessions leaves out file paths; the item's metadata has them.
            meta = ET.fromstring(requests.get(
                f"{url}/library/metadata/{node.get('ratingKey')}", headers=headers,
                timeout=5).content)
            for part in meta.iter("Part"):
                strm = plex_file_to_strm(part.get("file"), media_servers.PLEX_TV_ROOT,
                                         strm_generator.MEDIA_PATH)
                if strm:
                    open_paths.add(strm)
    except Exception as exc:
        print(f"  Plex session check failed: {type(exc).__name__}", flush=True)
        return None
    _sessions_cache.update(at=now, value=(streaming, open_paths))
    return streaming, open_paths


def _wait_for_no_stream() -> set:
    """--allow-paused: wait only while something is actually streaming, and
    return the .strm paths still open in a (paused) session so the caller can
    leave those items alone."""
    while True:
        state = _plex_sessions()
        if state is not None and not state[0]:
            return state[1]
        print("  " + ("playback active" if state else "Plex unreachable")
              + ", waiting 60s", flush=True)
        _sessions_cache["value"] = None
        time.sleep(60)


# ── rollback ─────────────────────────────────────────────────────────────────

def _restore_wanted(conn, before: dict | None, imdb_id: str, season, episode) -> None:
    if before is None:
        conn.execute("DELETE FROM wanted_episodes WHERE imdb_id=? AND season=? AND episode=?",
                     (imdb_id, int(season), int(episode)))
        return
    conn.execute(
        """UPDATE wanted_episodes SET status=?, attempt_count=?, last_attempted=?,
               first_attempted=?
           WHERE imdb_id=? AND season=? AND episode=?""",
        (before["status"], before["attempt_count"], before["last_attempted"],
         before["first_attempted"], imdb_id, int(season), int(episode)))


def _restore_files(backup_dir: Path, paths: list) -> None:
    for path_str in paths:
        saved = backup_dir / "files" / Path(path_str).as_posix().lstrip("/")
        if saved.exists():
            target = Path(path_str)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, target)


def rollback(args) -> int:
    import db
    import media_servers
    _flush_synchronously(media_servers)
    backup_dir = Path(args.rollback)
    restored = skipped = 0
    records = [json.loads(line) for line in
               open(backup_dir / "rows.jsonl", encoding="utf-8")]
    for record in reversed(records):
        row = record["row"]
        season, episode = row.get("season"), row.get("episode")
        if record["kind"] == "nfo":
            _restore_files(backup_dir, record.get("files") or [])
            restored += 1
            continue
        if record["kind"] == MOVE:
            current = db.get_virtual_item(row["token"])
            if not current or current.get("strm_path") != record["new_strm"]:
                skipped += 1
                continue
            if record.get("nfo_before"):
                nfo = Path(record["new_strm"]).with_suffix(".nfo")
                if nfo.exists():
                    nfo.write_text(record["nfo_before"], encoding="utf-8")
            _undo_moves(record.get("moved") or [])
            with db._connect() as conn:
                conn.execute(
                    f"UPDATE virtual_items SET {', '.join(c + '=?' for c in _MOVE_COLUMNS)} "
                    "WHERE token=?",
                    [row.get(c) for c in _MOVE_COLUMNS] + [row["token"]])
                _restore_wanted(conn, record.get("wrong_wanted"), row["imdb_id"],
                                season, episode)
                _restore_wanted(conn, record.get("right_wanted"), record["right"],
                                season, episode)
                conn.commit()
            media_servers.mark_removed(Path(record["new_strm"]))
            media_servers.mark(Path(row["strm_path"]))
            restored += 1
            continue

        if db.get_virtual_item(row["token"]):
            skipped += 1
            continue
        _restore_files(backup_dir, record.get("files") or [])
        columns = [c for c in row if c != "id"]
        with db._connect() as conn:
            conn.execute(
                f"INSERT INTO virtual_items ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [row[c] for c in columns])
            _restore_wanted(conn, record.get("wrong_wanted"), row["imdb_id"],
                            season, episode)
            conn.commit()
        if row.get("strm_path"):
            media_servers.mark(Path(row["strm_path"]))
        restored += 1
    media_servers._flush()
    print(f"Restored {restored} item(s); skipped {skipped} changed since the run")
    print("monitored_series and requests rows created by the run are listed in "
          f"{backup_dir / 'shows.jsonl'} and are left in place on purpose: they "
          "describe a real show and removing them would drop its episodes again")
    return 0


# ── media server notifications ───────────────────────────────────────────────

def _flush_synchronously(media_servers) -> None:
    """Make every scan request wait for the one _flush() this process runs at
    the end. media_servers normally flushes on a debounce timer in a daemon
    thread, and a short-lived script can exit while that thread is still
    inside its Jellyfin POST: the batch it holds never reaches the Plex spool.
    The 2026-09-20 run lost the scans for four shows that way."""
    media_servers._DEBOUNCE = 10 ** 9


def rescan_ops(records: list[dict]) -> list[tuple[str, str]]:
    """Every (mode, path) scan an applied run implies: the old season folder of
    anything that left it, the new season folder of anything moved in, and the
    show folder of a corrected tvshow.nfo. Rerunning these is harmless."""
    ops = []
    for record in records:
        if record["kind"] == "nfo":
            ops.append(("scan", record["path"]))
            continue
        old = (record.get("row") or {}).get("strm_path")
        new = record.get("new_strm")
        if old and old != new:
            ops.append(("remove", old))
        if new:
            ops.append(("scan", new))
    return list(dict.fromkeys(ops))


def rescan(args) -> int:
    import media_servers
    _flush_synchronously(media_servers)
    backup_dir = Path(args.rescan)
    records = [json.loads(line) for line in
               open(backup_dir / "rows.jsonl", encoding="utf-8")]
    ops = rescan_ops(records)
    for mode, path in ops:
        (media_servers.mark_removed if mode == "remove" else media_servers.mark)(Path(path))
    media_servers._flush()
    print(f"Queued scans for {len(ops)} path(s) from {len(records)} record(s)")
    return 0


# ── entry point ──────────────────────────────────────────────────────────────

def run(args) -> int:
    import db
    import media_servers

    _flush_synchronously(media_servers)

    entries = load_plan(json.load(open(args.plan, encoding="utf-8")))
    if args.imdb:
        entries = [e for e in entries if e["wrong"] in set(args.imdb)]
    findings: dict = defaultdict(dict)
    for finding in json.load(open(args.findings, encoding="utf-8")):
        findings[finding["imdb_id"]][finding["token"]] = finding

    backup_dir = None
    if args.apply:
        backup_dir = Path(args.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.plan, backup_dir / "plan.json")

    totals = Counter()
    with db._connect() as conn:
        plans = [build_show_plan(conn, entry, findings, args.on_duplicate)
                 for entry in entries]
    for plan in plans:
        print(report(plan))
        totals.update(apply_show(plan, args, backup_dir))
        print()
    media_servers._flush()
    print("Outcome:", dict(totals) or "nothing to do")
    if backup_dir:
        print(f"Backup: {backup_dir}  (undo with --rollback {backup_dir})")
    else:
        print("Read-only run. Nothing was changed; add --apply to apply this plan.")
    return 0


def main(argv=None) -> int:
    # Resolve app modules from the image when run in the container. Done here,
    # not at import time, so importing this file (tests) never shadows the tree
    # under test with the deployed /app.
    for path in ("/app", "."):
        if path not in sys.path:
            sys.path.insert(0, path)
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--plan", help="JSON list of approved per-show entries")
    ap.add_argument("--findings", help="series_identity_findings JSON from the sweep")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup-dir",
                    default=f"/data/ops/series_reidentify_{datetime.now():%Y%m%d-%H%M%S}")
    ap.add_argument("--rollback")
    ap.add_argument("--rescan", metavar="DIR",
                    help="queue again every Plex/Jellyfin scan an applied run implies")
    ap.add_argument("--imdb", action="append", default=[],
                    help="only this wrong-side show (repeatable)")
    ap.add_argument("--on-duplicate", choices=("drop", "keep"), default="drop",
                    help="what to do with an item whose episode the right show "
                         "already holds (default: drop the redundant copy)")
    ap.add_argument("--no-monitor-right", dest="monitor_right", action="store_false",
                    help="do not add or extend the right show's monitored_series row")
    ap.add_argument("--no-request-row", dest="request_row", action="store_false",
                    help="do not create a requests row for the right show")
    ap.add_argument("--allow-paused", action="store_true",
                    help="wait only while Plex is streaming, not for paused sessions or "
                         "the 10 minute after-play window; items open in a paused "
                         "session are still left alone")
    args = ap.parse_args(argv)
    if args.rollback:
        return rollback(args)
    if args.rescan:
        return rescan(args)
    if not args.plan or not args.findings:
        ap.error("--plan and --findings are required")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
