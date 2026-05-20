# Mycelium — Session Handoff

Carry this into the next chat so context isn't lost. Last updated: 2026-05-20.

## What this project is

Self-hosted media pipeline (Flask + SQLite + React SPA) that turns watchlist
clicks into Jellyfin-ready `.strm` files streaming from TorBox. Runs as one
Docker container on a Synology NAS.

- **Live deployment (source of truth):** NAS at
  `/volume1/docker/jelly-stack/webhook` — this directory IS the git repo,
  on branch `main`, remote `github.com/corveck79/mycelium`.
- **Update flow on NAS:** `git pull origin main && docker compose up -d --build`.
  Working tree is clean; data lives in `./data` (DB, `.strm`, settings) and
  survives rebuilds.
- **App URLs:** dashboard `http://10.0.0.10:8088/ui`, SPA `http://10.0.0.10:8088/app/`.
  Jellyfin at `http://10.0.0.10:8096`.

---

## Current state (end of session 2026-05-20)

Everything is on `main`. The NAS needs a `git pull + rebuild` to pick up recent changes.

### What to do right now on the NAS

```bash
git pull origin main
docker compose up -d --build
```

After rebuild:
1. **Admin → Maintenance → Migrate to canonical names** — renames all movie folders to
   TMDB canonical names, merges duplicates. Run once. *(Already ran once this session —
   only run again if new duplicate folders appear.)*
2. Jellyfin: **remove and re-add the Movies library** to clear stale DB entries from
   renamed folders (normal scan adds new entries but doesn't remove old ones).
3. Jellyfin **Scan All Libraries**

---

## Architecture summary

```
User → SPA (/app/) or Seerr webhook → processor.py
  → Zilean (local) + Torrentio (fallback, with browser User-Agent)
  → debrid.check_cached_multi() → pick best CACHED release only
  → catbox.register() → write .strm + .nfo + poster.jpg + fanart.jpg
  → Jellyfin refresh

On play (catbox mode):
  Jellyfin reads .strm → opens http://10.0.0.10:8088/stream/<token>
  → catbox.materialize(token):
      1. torbox_id in DB still live? → requestdl → 302 redirect (fast path)
      2. Not live? → fresh Torrentio search → pick best cached release
         → add_magnet → wait_until_ready → requestdl → 302 redirect
      3. Nothing found? → remove .strm → film verdwijnt uit Jellyfin
```

**Core invariant (imdb_id is leading):**
- 1 imdb_id = 1 movie folder = 1 .strm — structurally enforced
- Folder name = TMDB canonical title + year (not torrent name)
- No imdb_id → not added to library

**Catbox mode** (`CATBOX_MODE=true` + `CATBOX_LAZY_ADD=true`):
- `.strm` contains `http://10.0.0.10:8088/stream/<token>`
- Token maps to `virtual_items` row (imdb_id + last known torbox_id as shortcut)
- On play: TorBox shortcut first, else fresh Torrentio search — always finds a playable
  release or removes the .strm
- Stored magnet is no longer re-added blindly; Torrentio is always the fallback

---

## Key design decisions made this session

| Decision | Reason |
|----------|--------|
| **No stored-magnet replay** | Dead magnets caused 45s waits → "Playback Failed". Fresh Torrentio search always finds a cached release or removes the film. |
| **imdb_id as primary key** | Folder names from torrent titles caused Cyrillic duplicates, fuzzy dedup failures. TMDB canonical name is deterministic. |
| **_SEARCH_UNAVAILABLE sentinel** | Distinguishes "searched and found nothing" (→ remove .strm) from "couldn't search" (→ keep .strm, retry later). |
| **Shared maintenance lock** | `migrate_to_canonical_names` and `repair_expired_strms` cannot run simultaneously — rename + repair would conflict. |
| **Auto repair every 6h** | Scheduled job recreates missing .strm files automatically; no manual repair needed. |
| **Dead .strm removal** | If materialize fails definitively, .strm is deleted so Jellyfin stops showing an unplayable film. |
| **Jellyfin NFO saver = OFF** | Mycelium writes .nfo with imdb_id. Jellyfin NFO saver would overwrite them. |

---

## All changes shipped this session (all on `main`)

| Commit | Change |
|--------|--------|
| `a709735` | Maintenance lock: migrate + repair cannot run simultaneously |
| `b11a71e` | **imdb_id as primary key**: `_canonical_movie_folder()`, `_find_movie_folder_by_imdb()`, update `create_lazy_movie_strm()`, `migrate_to_canonical_names()`, `db.update_virtual_strm_path_prefix()`, Admin "Migrate to canonical names" button |
| `a4ecf65` | Fix aggressive .strm removal (keep .strm when search unavailable); repair Pass 1 dedup skips Cyrillic sibling that already has .strm |
| `45559e8` | Resolve missing imdb_id via TMDB before Torrentio search; `db.update_virtual_item_imdb()` |
| `47b7b7a` | **Rebuild materialize**: live Torrentio search replaces stored-magnet replay; `_search_best_cached_release()` replaces `_find_fresh_cached_release()` |
| `b61b998` | Fix dedup: only skip folder if sibling already has .strm (fixes Cyrillic duplicate getting .strm instead of English folder) |
| `c515215` | Schedule automatic .strm repair every 6h in catbox mode |
| `8bd90cb` | Remove dead .strm from library when no playable release found |
| `ebc346a` | Auto-fallback to fresh Torrentio release when stored magnet is dead |
| `b6b731a` | Failure cooldown in catbox materialize (30s standard / 120s for 429) stops burst retries |
| `bc71df7` | Repair missing .strm files (folders with NFO but no .strm) |

---

## Key files

| File | Purpose |
|------|---------|
| `processor.py` | Request → search → cache-check → catbox lazy register |
| `strm_generator.py` | Write `.strm`/`.nfo`/images; `_canonical_movie_folder()`; `migrate_to_canonical_names()`; `repair_expired_strms()` |
| `catbox.py` | Lazy materialization: TorBox shortcut → Torrentio search → redirect or remove .strm |
| `cleanup.py` | Dedup `.strm`, merge series folders, rename messy names |
| `upgrader.py` | Auto-upgrade quality + season-pack consolidation |
| `torrentio.py` | Torrent candidate fetch + ranking + language filtering |
| `arr_import.py` | Radarr/Sonarr bulk import |
| `auth.py` | Session login, proxy-auth trust, multi-user roles |
| `db.py` | SQLite access: requests, virtual_items, monitored_series, retry_queue |
| `tmdb.py` | TMDB API: search, images, episode stills, IMDb↔TMDB ID mapping |
| `settings.py` | Runtime-editable settings (reads DB first, `.env` fallback) |
| `nfo_generator.py` | Write `.nfo` sidecars + fetch local images |
| `app.py` | Flask app, scheduler, all UI/API endpoints |
| `retry_queue.py` | Exponential backoff retry scheduler |

---

## virtual_items table (catbox mode source of truth)

| Column | Role |
|--------|------|
| `token` | Primary key — goes into .strm URL |
| `imdb_id` | **Leading key** — used for Torrentio search on play |
| `torbox_id` | Cache/shortcut — checked first on play, skips Torrentio if still live |
| `info_hash` | Last known hash — secondary shortcut via `find_by_hash` |
| `magnet` | Stored but no longer blindly re-added (only used if torbox_id/hash shortcut works) |
| `strm_path` | Path on disk — updated by `update_virtual_strm_path_prefix()` on rename |
| `last_played` | Used by idle GC (`release_idle()`) |

---

## Known remaining issues / next steps

- **Jellyfin duplicates after migration**: user needs to remove + re-add Movies library
  in Jellyfin to clear stale DB entries from renamed folders.
- **The Amateur (2025)**: has no imdb_id in virtual_items (`tt14961434` is the correct
  one). Set manually: `UPDATE virtual_items SET imdb_id='tt14961434' WHERE token='227a6d344f04441c'`
  Then run Admin → Repair broken strm files.
- **Highlander folder**: has imdb_id `tt1235529` in .nfo but that may be wrong. The 1986
  film is `tt0091203`. Folder is named "Highlander" (no year) as a result.
- **Series in Mycelium**: Sonarr import added 31 series to `monitored_series` DB. Episodes
  appear in Wanted → Episodes tab when found. Series folders appear in Jellyfin once
  episodes are found via Torrentio and .strm files are written.
- **Missing episodes**: "hoe vullen we gemiste afleveringen aan?" — not yet implemented.
- **CATBOX_IDLE_MINUTES**: currently aggressive (60 min default). Recommend setting to
  720 or 1440 in Settings to reduce Torrentio search frequency on play.

---

## Workflow notes / gotchas

- Work directly on `main` (user's preference).
- `data/` is gitignored — can't inspect DB or media from a cloud session.
  Ask user to run `find`/`ls`/`sqlite3` on the NAS when needed.
- POST endpoints are CSRF-protected by default → trigger via dashboard buttons, not curl.
  CSRF-exempt: `/ui/api/repair-strms`, `/ui/api/migrate-canonical`,
  `/ui/api/requests/<id>/retry`, `/ui/api/arr-import/*`.
- Single gunicorn worker, 8 threads → in-process state is shared and safe.
- `settings.get("KEY", default)` reads settings DB first, then falls back to env/config.py.
  Always use `settings.get()` in endpoints — never `config.KEY` directly.
- Jellyfin compose: `/volume1/docker/jellyfin/docker-compose.yml` (separate from app
  compose at `/volume1/docker/jelly-stack/webhook/`).
- CATBOX_HOST must be the externally reachable URL Jellyfin can reach
  (currently `http://10.0.0.10:8088`). This goes into the .strm file itself.
- Jellyfin NFO metadata saver must stay **OFF** — Mycelium owns the .nfo files.
- Media path inside container: `/data/media/movies` and `/data/media/series`.
  On NAS: `/volume1/docker/jelly-stack/webhook/data/media/`.
