# Integration catalog reads (C2b2)

Signed metadata reads are wired through `integration_catalog_service.py` to the
existing pure builders and checked transport. Integration remains off by default.
These reads do not enrich library presence, entitlement or playability and do not
change any existing UI consumer or mutation route.

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
season, which the service maps to 404 before handling CatalogUnavailable.

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

## Orchestration and inputs

The HTTP boundary authenticates the server-owned `RAW_URI`, exact scope and route query
registry before invoking the service. The service percent-decodes the verified target
components once as strict UTF-8. It never uses Flask query arguments, browser cookies,
forwarded identity or region headers. `%2541` becomes literal `%41`; Unicode normalization
and a second decode are forbidden.

Page defaults to 1 and must be canonical decimal 1..500. Search requires trimmed nonempty
`q`, at most 100 characters without controls; type is `all` (default), `movie` or `tv`.
Genres and providers require explicit `type=movie|tv`. IDs are canonical decimal
1..2147483647. The service checks each rail's applicable keys in addition to the signed
registry's union; inapplicable keys are 400, not ignored.

| Rail | Applicable keys | Defaults and restrictions |
|---|---|---|
| trending | type, window, page | all; week; window day or week |
| popular | type, region, page | movie; type movie or tv |
| top-rated | type, page | movie; type movie or tv |
| now-playing, upcoming | type, region, page | movie only |
| on-the-air | type, page | tv only |
| by-provider | type, provider_id, region, page | movie; positive provider_id required; flatrate only |
| by-genre | type, genre_id, year_from, year_to, page | movie; positive genre_id required; optional ordered years 1800..3000 |

Regions must already be uppercase two-letter strings; there is no trimming, case conversion,
alias or invented fallback. Details have no query keys and use only the verified native
user's region. Invalid native details preference is `503 preference_invalid`, retryable
false. Routes accepting region use a valid explicit override before the native region;
invalid explicit input is 400 and no usable region is `400 region_required`.

## Raw fetch matrix

Counts below describe successful calls. Invalid input performs zero provider fetches;
upstream failures stop early. No legacy TMDB helper or season alias is used.

| Read | Raw TMDB calls | Parameters |
|---|---|---|
| search | `/search/{multi\|movie\|tv}` (1) | query, page, include_adult=false |
| trending | `/trending/{all\|movie\|tv}/{day\|week}` (1) | page |
| popular | `/{movie\|tv}/popular` (1) | region, page |
| top-rated | `/{movie\|tv}/top_rated` (1) | page |
| now-playing, upcoming | `/movie/{now_playing\|upcoming}` (1) | region, page |
| on-the-air | `/tv/on_the_air` (1) | page |
| by-provider | `/discover/{movie\|tv}` (1) | with_watch_providers, watch_region, with_watch_monetization_types=flatrate, sort_by=popularity.desc, include_adult=false, page |
| by-genre | `/discover/{movie\|tv}` (1) | with_genres, optional date bounds, sort_by=popularity.desc, include_adult=false, page |
| detail | `/{movie\|tv}/{id}` (1) | append_to_response=credits,videos,watch/providers,recommendations |
| season | `/tv/{id}` then `/tv/{id}/season/{n}` (2) | none; raw numbering including specials 0 |
| genres | `/genre/{movie\|tv}/list` (1) | none |
| providers | `/watch/providers/{movie\|tv}` (1) | watch_region |
| genre tabs | local configuration (0) | none |
| person | `/person/{id}` (1) | append_to_response=combined_credits |

By-genre years map to `primary_release_date.gte/lte` for movies and
`first_air_date.gte/lte` for TV, using January 1 and December 31 respectively.
Builders preserve complete valid empty data; missing arrays, contradictory identities,
incomplete seasons and oversize output cannot become successful empty answers.

## Strict local configuration

The service reads persisted settings directly with `db.get_setting`, falling back to
static configuration only when the persisted value is genuinely absent. It does not use
the legacy `settings.get` database-error fallback. Read failures return safe retryable
503 errors. Missing or locally malformed TMDB keys return nonretryable 503.

`DISCOVER_GENRE_TABS` is a strict JSON array; a genuinely missing setting means `[]`.
Every row, including disabled rows, must have media_type, genre_id, genre_name and boolean
enabled, with optional year_from/year_to. Unknown fields, duplicate JSON keys, nonfinite
numbers, invalid types or year bounds, over 200 rows, and duplicate
(media_type, genre_id, year_from, year_to) identities fail closed with nonretryable 503.
The builder validates all rows before filtering enabled rows and maps genre_name to name.
Malformed configuration or database failure never fabricates an empty answer.

`/me` and `/capabilities` use the same operation calculation. Base operations remain
me.read and requests.read. Seven implemented provider reads are advertised only with
locally valid TMDB configuration; details additionally requires a valid native region.
Genre-tabs.read qualifies independently with valid local tab configuration. Configuration
read failures leave base reads available and affected operations incomplete. Provider
outages do not redefine implementation support. No provider probe is made for capabilities.

## Errors and qualification

Catch CatalogNotFound before CatalogUnavailable. Genuine missing detail, person or season
is 404; upstream 404 on a list, genre or provider endpoint is nonretryable 503. Malformed
metadata/JSON, invalid local configuration, missing blocks, incomplete seasons and output
over 900000 bytes are nonretryable 503. The service size-checks data using serialize and
the HTTP layer then uses the existing ok envelope with meta and no-store headers.

Only checked-transport reasons busy, deadline, upstream_unreachable, upstream_error,
session_error and upstream_status produce retryable catalog 503. Unknown reasons fail
closed as nonretryable. The transport combines redirects, upstream auth failures, rate
limits and server failures under upstream_status, so this batch conservatively treats
that entire reason as retryable; it cannot claim finer status discrimination. All error
messages are fixed safe text.

Deterministic service and signed HTTP tests cover exact fetches, zero-fetch validation,
raw query binding, region isolation, auth/replay, strict tabs, size, concurrency and
empty/missing/outage distinctions. Live provider behavior, real edge framing and final
browser acceptance remain later release gates. Paging for oversize data remains unimplemented.
