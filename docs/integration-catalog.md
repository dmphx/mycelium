# Integration catalog builders (C2b1)

Two new modules, not wired to any route or capability yet.

## integration_catalog.py (pure, no I/O)

Raw TMDB JSON in, frozen `data_contract` objects out. Any missing or malformed required
data raises `CatalogUnavailable` (route maps to 503). `today` is injected as a `datetime.date`.

| Function | Output |
|---|---|
| `title(raw, media_type)` | Title |
| `catalog_list(raw, type, page)`, `search(raw, page, type=None)`, `trending(raw, page, type)` | `{items, page, total_pages}` |
| `detail(raw, type, id, region)` | title fields plus genres, runtime, status, seasons (TV), credits, recommendations, trailers, watch_providers |
| `season(raw_season, show_raw, id, n, today)` | season object, `complete` true only for a full valid season |
| `genres(raw, type)`, `providers(raw, type, region)`, `genre_tabs(tabs)` | list objects |
| `person(raw, id)` | person, merged filmography |
| `serialize(data)` | size check only, see below |

`serialize` is a size-check representation, NOT the HTTP response: it lacks `meta`. Routes must
build the response with the existing `ok()` envelope and apply the 900000 byte check to the data
first; never emit the helper's bytes as the contract response. `CatalogNotFound` (subclass of
`CatalogUnavailable`) is raised only for a well-formed show that does not declare the requested
season, for a later 404 route.

Strictness: every parent/embedded id, `page`, season and episode number must be a real int (not
bool/float) and equal the expected value; the season object's own `id` is only checked as a
positive id, never against the show. All regexes are full matches (no trailing newline aliases).
Detail requires genres plus credits (cast, crew), recommendations, videos and watch/providers
results to be present (empty is valid); person requires combined_credits cast and crew. Missing
data is 503, never an empty answer. Short text max 300 (character/job 1000), prose max 200000.
Person filmography order: popularity descending (absent = 0), then (type, id).

Rules: ids are real ints 1..2147483647; titles and names reject all controls; overview and
biography allow LF and tab, normalize CRLF, reject other C0/C1 and surrogates; Unicode format
characters (ZWJ) are kept; bad optional images are omitted; `info_url` only
`https://www.themoviedb.org/`; only `person` is filtered from multi search; no library,
availability, entitlement or IMDb data; no aliases; regions are uppercase two letters with no
fallback; `total_pages` is echoed unclamped (0..1000000), requested page 1..500; credit lists
max 3000 each and list pages max 100 items, over that is 503; payload over 900000 bytes is 503
(future paging); person credits are deduplicated by (type, id) before the 100 cap, cast
character wins, `filmography_complete` false when truncated.

## tmdb_checked.py

`fetch(path, params=None)` returns a parsed JSON object, raises `TmdbNotFound` (404 only) or
`TmdbUnavailable` (fixed reason code). Fixed base and path allowlist, param allowlist, no
redirects, identity encoding, 4 MiB cap on declared and actual bytes, strict JSON (no duplicate
keys, NaN, overflowing floats such as 1e999, integers over 19 digits, non-object), fresh session per call with `trust_env` off, 8 non-blocking slots,
connect 3 s / read 10 s. The 15 s deadline is an elapsed budget checked after headers and between body reads; one blocking read can run up to 10 s past it, so it is not a hard wall-clock limit (real edge/slow-server behaviour is unqualified). Params are grammar-checked per key (page 1..500, uppercase regions, ids 1..2147483647, real ISO dates, append whitelist, include_adult false, sort popularity.desc, query max 100 without controls); cross-parameter rules such as date order belong to the orchestrator. Key read lazily via the `tmdb` auth helpers;
no URL, key, header or upstream text in errors or logs.

## Not done in this batch

Route and capability wiring, orchestration (which raw calls feed each builder), preference
and genre-tab sourcing, rail-specific query parameters, paging for oversize payloads, release.
