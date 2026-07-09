# spore-nfs hybrid: cached -> real, uncached -> stub (one library)

## Goal

One Plex library on the NFS mount. Per title, spore-nfs decides:

- **Cached on TorBox** -> serve the **real file** (real size + real header) -> Plex analyzes
  the true codec and **Direct Plays**, lazy-adding to TorBox only on real playback.
- **Not cached** -> serve a **tiny stub** (forces transcode, cheap to scan, lazy) -> the
  torrent is only touched if someone actually plays it.

Titles flip between the two automatically as TorBox's cache changes. No seek-preview
(BIF) thumbnails (they read the whole file). Poster/cast art still comes from TMDB (free).

## Why this is small

`/spore-nfs/size/<token>` already does a TorBox `checkcached` and returns the real size
when cached and `size: 0` when not. So mycelium already knows the answer per title; today
an uncached title just shows as a broken 0-byte file. The change is: **uncached -> serve a
stub instead of 0**, and the stub bytes already exist via `strm_generator.make_stub_mkv()`.

## Changes

### mycelium (app.py + helper)
1. **Cache-status cache.** Background job refreshes a per-token `{cached: bool, size: int}`
   map using **batched** `torbox.check_cached_files([...])` (not one call per file), stored
   in memory (+ optional `virtual_items` columns). `/spore-nfs/tree` and `/spore-nfs/size`
   answer instantly from this, so directory listing costs zero live TorBox/CDN calls.
2. **`/spore-nfs/size` returns `{size, cached}`** -> real size + `cached:true`, or stub size
   + `cached:false` (instead of `0`).
3. **New `/spore-nfs/stub/<token>`** -> returns `make_stub_mkv(...)` bytes for that token
   (reusing the arg-building in `_write_spore_stubs`). Small, local, no CDN, no materialize.

### spore-nfs/main.go
4. `cheapSize()` -> returns `(size, cached)` from the extended size endpoint.
5. `Stat`/`ReadDir`: uncached -> report **stub size**; cached -> real size (as today).
6. `Open`/`Read`: carry a `cached` flag on `sporeFile`. On `Read`:
   - cached -> `readRange()` via `/spore-stream` (today's path, CDN-backed).
   - uncached -> serve bytes from `/spore-nfs/stub/<token>` (tiny, cached in memory).
7. **mtime that flips with state.** Report `ModTime` derived from `(cached,size)` instead of
   the constant `0`, so Plex notices a stub<->real flip and re-analyzes. (Size already
   changes drastically stub<->real, which most scanners treat as changed; mtime makes it
   reliable.)

### Plex / ops
8. Real library: **BIF off, intro/credit markers off** (already set on the temp libraries).
9. Flip pickup: rely on Plex's scheduled scan to catch size/mtime changes. v1.1 nicety:
   mycelium pokes a Plex **partial scan** for a title when its cache-status flips (mycelium
   already does Jellyfin refreshes; add the Plex equivalent).

## Behavior and cost

- **Directory scan:** instant (served from the cache-status map; no per-file TorBox/CDN).
- **Uncached title:** Plex reads the tiny stub -> cheap, forces transcode, lazy-adds only if
  played. Identical to today's stub library.
- **Cached title:** Plex reads the real header from the CDN **once** to analyze (~seconds),
  then Direct Plays. This is the only real per-title cost, only for cached titles, one-time
  until the title flips. Run the initial pass as a slow background scan.
- **Flip cached->uncached** (torrent expires ~30d): title reverts to a stub on the next
  scan; playing it re-materializes lazily.

## Risks and handling

- **Wrong Direct Play decision:** avoided, because cached titles are analyzed from the REAL
  header (validated live with Ocean's Eleven: correct HEVC/AC3, clean Direct Play).
- **checkcached load:** batched + cached + background-refreshed, never per-request.
- **First play of an uncached stub:** forces transcode (safe) and lazy-materializes; if it
  later caches, it flips to Direct Play. This is exactly the requested model.
- **Cache churn -> re-analysis:** only on real flips; TorBox cache is stable for popular items.

## v2 optimization (not in v1)

For cached titles, synthesize a **real-codec** header from mycelium's probe cache (populated
on first play, or a cheap ffprobe of just the CDN moov) so Plex's analysis of cached titles
is also cheap - no full CDN header read. Would make even the cached-scan nearly free. Deferred.

## Rollout (same safe pattern already used this session)

Build on `integrate` -> push `:spore-nfs` staging tag -> deploy to mycelium -> test on the
`Movies-Temp` library: confirm (a) uncached shows as a cheap stub, (b) cached Direct Plays,
(c) a title flips stub<->real when its cache status changes. Once trusted, merge to
`hardened`, rebuild `:latest`, repoint. Keep the existing full stub library until then.
