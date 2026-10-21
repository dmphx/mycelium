# Integration API v1: signed receiver contract

Status: signed HTTP boundary, current actor/request reads, catalog metadata reads and
isolated non-automatic saved people are implemented. Watchlist and authority-bearing operations remain unqualified. The
feature is off unless `INTEGRATION_ENABLED=true`; this milestone does not qualify deployment.

## Configuration (environment only)

| Variable | Meaning |
|---|---|
| `INTEGRATION_ENABLED` | `false` by default |
| `INTEGRATION_ISSUER` | exact `https://` issuer string the signer uses |
| `INTEGRATION_SERVER_GUID` | pinned media-server GUID, 32 lowercase hex, non-zero |
| `INTEGRATION_VIEWER_JWKS_PATH` | local JWKS file for the viewer keyring |
| `INTEGRATION_SERVICE_JWKS_PATH` | local JWKS file for the service keyring |

JWKS are read from local files only. There is no URL fetch, `jku`, `x5u` or embedded key.

## Assertions

Transport is `Authorization: Bearer <compact JWT>`, at most 6144 bytes including the
`Bearer ` prefix. Only RS256 is accepted. The header holds exactly `alg`, `typ` (`JWT`) and
`kid`. Segments are canonical unpadded base64url and there are exactly three. Duplicate JSON
keys in the header or claims are refused.

The endpoint chooses the keyring (viewer or service) before the token's `kid` or `aud` is
read. There is no fallback and no union. Key material shared between the rings is refused
at load time, even under different `kid` values. Public RSA keys must be 2048 to 8192 bits
with an odd exponent of at least 65537, canonical JWK integers, and no private fields. A `kid`
matches `[A-Za-z0-9_-]{1,96}`, a ring holds 1 to 8 keys, and a JWKS file is read with a 1 MiB bound.
`Settings` rechecks ring roles and recomputes key overlap from the key objects themselves.
The issuer must be a strict `https://` authority with a valid port, no userinfo, query or
fragment, and at most 512 characters. JSON numbers must be representable as finite doubles.

| Role | `aud` | `sub` | Scopes |
|---|---|---|---|
| viewer | `mycelium.integration.v1` | linked media-server user GUID, never the server GUID | catalog:read, availability:read, watchlist:read, watchlist:write, requests:read, requests:write, issues:read, issues:write, playback-contexts:write |
| service | `mycelium.playback.v1` | the pinned server GUID | readers:write |

Claims: `iss` (exact), `aud` (single string), `sub`, `jf_sid` (must equal the pinned server
GUID), integer `iat`, `nbf` (equal to `iat`), `exp` (greater than `iat`, at most 60 s after),
`jti` (16 to 128 printable ASCII characters), `htm` (exact uppercase `GET`, `POST` or
`DELETE`), `target`, `body_sha256` (lowercase hex of the exact transmitted bytes), `scope`
(space separated, allowlisted for the role, no duplicates, must contain the route's required
scope), optional `library_grants`, and `idempotency_key` for writes. Clock skew is at most
5 seconds. Unknown claims are refused. Role, quota and enabled state are never read from a
token: the viewer's native user is re-read on each request and must be linked and enabled.
Unknown subjects are never provisioned.

`library_grants`, when present, must be an object with exactly `version`, `assertion_jti`,
`observed_at`, `expires_at`, `source_state` and `lookups` (a list of at most 4). The claims
`library_grants_count` (an integer equal to the number of lookups) and
`library_grants_sha256` (64 lowercase hex characters) are accepted only alongside
`library_grants`. The old list format, more than 4 lookups, a malformed count or hash, or
either extra claim without the object are refused. The object is returned as the dict
`grants_unqualified`. Version is the string `"1"`, assertion_jti equals the JWT jti,
observation is at most 5 seconds in the future, and expiry is strictly after the receiver
clock and no later than the JWT expiry or 30 seconds after observation. Both count and hash
are required. The hash covers compact JSON with ordinally sorted ASCII keys and strings,
integer numbers and booleans, without nulls, floats or control characters. Source state has
exactly `publisher_epoch`, `policy_revision` and `catalog_revision`, each a nonempty printable
ASCII token of at most 128 characters. These values are carried, not trusted as current.
The receiver checks envelope integrity but does not interpret lookups or derive availability,
entitlement or request authority. Semantic proof validation belongs to the route layer. Unknown
lookup fields must be rejected there, and until a qualified producer and
policy proof exist, new request writes must fail closed with 503.

## Target and body binding

The signed `target` is the canonical form of the raw request target. The receiver requires
the raw wire target to already equal that form: path matching
`/integration/v1/<segment>(/<segment>)*` with `[A-Za-z0-9_-]` segments, no dot, empty or
trailing segment, no `+ ; #` or controls, explicit `key=value` pairs, each component
percent-decoded once as strict UTF-8, re-encoded with uppercase escapes, pairs sorted by
encoded key then value, repeated decoded keys and keys outside the route's allowlist refused,
no Unicode normalization. `%2541` stays the literal text `%41`. Forwarded-URI headers are never
trusted; the route layer must pass the raw server request target.

Writes (`POST`, `DELETE`) require an `Idempotency-Key` header matching
`[A-Za-z0-9_-]{16,128}` and identical to the signed claim, a non-empty JSON object body (at
least `{}`) and a `Content-Type` that fully matches `application/json` with an optional
`; charset=utf-8` (single optional space, no other parameters, quoting or whitespace).
`GET` has no body, no `Content-Type`, no key header and no key claim. Bodies are at most
65536 bytes of UTF-8 JSON (no BOM). `Content-Length`, when present, must be a canonical
decimal of at most five digits, at most 65536, and equal to the body length. There is no
`Content-Encoding` or `Transfer-Encoding`. Duplicate keys, NaN or Infinity, negative zero, lone surrogates, depth
over 24 and non-object values are refused, and parsing happens after the signature and
binding checks.

## Replay

`iss` plus `jti` is stored durably in SQLite (`integration_replay`) until `exp` plus the skew.
Consumption happens last, after every other check, so a rejected request never burns a `jti`.
A reused `jti` is 401. A full store (100000 rows), a locked or failing database, or a wall
clock more than 5 seconds behind the persisted high-water mark fails closed with 503. A retry
needs a new assertion with the same `Idempotency-Key`.

## Response envelope (new `/integration/v1` and `/ui/api/v1` routes only)

Success: `{"api_version": "1", "data": <route object>, "meta": {"correlation_id": <opaque
UUID>, "generated_at": <UTC ISO 8601>}}`.

Error: `{"api_version": "1", "error": {"code": <snake_case>, "message": <safe text>,
"retryable": <bool>, "details": <optional allowlisted object>}, "meta": {...}}`. Errors never
contain tokens, issuers, identities, paths or logs.

Statuses: 400 malformed shape, 401 invalid signature, binding, time or replay, 403 unlinked,
disabled or missing scope, 404 unknown or foreign resource, 409 idempotency conflict or stale
generation, 422 unresolved metadata or invalid selection, 429 quota or rate limit, 503
dependency unavailable or fail-closed replay store.

## Data contract

Frozen route data v1. Endpoint availability is gated by capabilities and executed
qualification. The block below is the exact agreed contract; `future.starts_on` is the
admission-time cutoff date (`YYYY-MM-DD`) carried in a receipt's scope for the `future`
kind. Unknown availability is never inferred as missing.

```json
{
  "status": "Frozen route data v1 for peer consumer wiring; endpoint availability still gated by capabilities and executed qualification.",
  "envelope": "api_version is exactly string \"1\"; data is a route-specific object. Error envelope unchanged. Optional fields may be omitted; unknown availability is never inferred missing.",
  "title": {
    "tmdb_id": "int 1..2147483647",
    "media_type": "movie|tv",
    "title": "string",
    "overview": "string",
    "poster_url": "optional HTTPS TMDB image URL",
    "backdrop_url": "optional HTTPS TMDB image URL",
    "release_date": "optional YYYY-MM-DD for movie",
    "first_air_date": "optional YYYY-MM-DD for TV",
    "vote_average": "optional finite 0..10 number"
  },
  "capabilities": {
    "operations": "array of supported operation strings only",
    "limits": {
      "request_body_bytes": 65536,
      "authorization_bytes": 6144,
      "request_episodes": 500,
      "request_seasons": 100,
      "list_page_size": 100
    },
    "incomplete": "array of known unqualified/unsupported capability names or reasons"
  },
  "operation_names": [
    "me.read",
    "catalog.search",
    "catalog.rails",
    "catalog.details",
    "catalog.seasons",
    "genres.read",
    "providers.read",
    "genre-tabs.read",
    "people.read",
    "follows.read",
    "follows.write",
    "availability.read",
    "watchlist.read",
    "watchlist.write",
    "requests.read",
    "requests.write",
    "playback-contexts.write",
    "issues.read",
    "issues.write"
  ],
  "catalog_list": {
    "items": "Title[]",
    "page": "int >=1",
    "total_pages": "int >=0"
  },
  "catalog_detail": {
    "title_fields": "Title fields at data root",
    "genres": "[{genre_id:int,name:string}]",
    "runtime_minutes": "int|null",
    "status": "string (catalog production/release status, never library status)",
    "seasons": "TV only [{season_number:int>=0,name:string,episode_count:int>=0,air_date:YYYY-MM-DD|null,poster_url?:HTTPS}] in raw TMDB numbering",
    "credits": {
      "cast": "[{person_id:int,name:string,character:string,profile_url?:HTTPS}]",
      "crew": "[{person_id:int,name:string,job:string,profile_url?:HTTPS}]"
    },
    "recommendations": {
      "source": "tmdb",
      "personalized": false,
      "items": "Title[]"
    },
    "trailers": "[{site:\"YouTube\",key:string,name:string}]",
    "watch_providers": {
      "region": "ISO two-letter",
      "items": "[{provider_id:int,name:string,logo_url?:HTTPS,monetization:flatrate|rent|buy}]",
      "info_url": "optional HTTPS TMDB URL; catalog availability only"
    }
  },
  "season": {
    "media": {
      "type": "tv",
      "tmdb_id": "int"
    },
    "season": "int>=0",
    "numbering": "tmdb",
    "complete": true,
    "episodes": "[{episode_id:int,season:int>=0,episode:int>=1,title:string,overview:string,air_date:YYYY-MM-DD|null,runtime_minutes:int|null,still_url?:HTTPS,air_state:aired|unaired|unknown}]",
    "rules": "Metadata outage is 503, never complete empty. Dates describe catalog scheduling, not entitled availability/playability. Legacy continuous-order aliases never rewrite this identity."
  },
  "person": {
    "person_id": "int",
    "name": "string",
    "biography": "string",
    "profile_url": "optional HTTPS",
    "birth_date": "YYYY-MM-DD|null",
    "place_of_birth": "string|null",
    "filmography": "Title[] with optional character:string",
    "filmography_complete": "boolean; false when bounded"
  },
  "genres": {
    "items": "[{genre_id:int,name:string}]"
  },
  "providers": {
    "region": "ISO two-letter",
    "items": "[{provider_id:int,name:string,logo_url?:HTTPS}]"
  },
  "genre_tabs": {
    "items": "[{media_type:movie|tv,genre_id:int,name:string,year_from:int|null,year_to:int|null}]"
  },
  "follows_get": {
    "items": "[{person_id:int,name:string,profile_url?:HTTPS}]",
    "complete": "boolean; false whenever bounded/truncated; never implicit completeness",
    "automatic_requests": false
  },
  "follows_post_delete": {
    "person_id": "int",
    "following": "boolean",
    "automatic_requests": false
  },
  "watchlist_get": {
    "items": "Title[] with watchlist_id:int and added_at:UTC ISO8601",
    "next_before_id": "int|null"
  },
  "watchlist_post_delete": {
    "media": {
      "type": "movie|tv",
      "tmdb_id": "int"
    },
    "saved": "boolean"
  },
  "request_receipt": {
    "request_id": "positive int, stable existing native user_requests ID",
    "media": {
      "type": "movie|tv",
      "tmdb_id": "int",
      "title": "optional server-resolved title"
    },
    "scope": "same canonical scope kind/seasons/episodes as accepted; future receipt additionally includes starts_on:YYYY-MM-DD, fixed at admission (input may not choose it)",
    "request_status": "pending|approved|denied|failed",
    "processing": {
      "stage": "awaiting_approval|queued|dispatching|dispatched|awaiting_metadata|waiting_release|rate_limited|registered|watching|failed|denied",
      "episodes": "optional [{season:int,episode:int,state:awaiting_metadata|unaired|searching|registered|waiting_release|failed}]",
      "next_check_at": "optional UTC ISO8601"
    },
    "availability": {
      "state": "unknown|absent|partial|present",
      "entitlement": "unknown|denied|allowed",
      "lookup": "unknown|incomplete|complete",
      "playability": "unknown|unavailable|verified",
      "jellyfin_item_id": "optional exact GUID only with authoritative policy-checked join",
      "can_play": "optional bool only with actual entitled item",
      "episodes": "optional exact per-episode states"
    },
    "outcome": "created|existing_request|already_available (last only when authoritative full scope coverage proven; not implemented in B1)"
  },
  "requests_get": {
    "items": "RequestReceipt[]",
    "next_before_id": "positive int|null"
  },
  "request_get": "RequestReceipt object at data root; foreign and missing identical404",
  "me": {
    "viewer": {
      "id": "native positive int",
      "display_name": "string"
    },
    "policy": {
      "request_approval": "required|automatic",
      "monthly_quota": {
        "limit": "positive int|null (null=unlimited)",
        "used": "int>=0",
        "remaining": "int>=0|null"
      }
    },
    "operations": "current viewer-supported operations, no delegated admin APIs"
  },
  "rules": [
    "No metadata response includes global indexed presence. Plugin overlays separately signed exact grants.",
    "Play requires actual entitled item and can_play. Unknown playability never labeled verified.",
    "List query defaults page=1; requests limit=50 max100, before_id positive int cursor. Catalog page max500.",
    "Receipt processing registration is separate from availability and verified playability. Dispatch completion is not media completion."
  ]
}
```

## HTTP boundary (`integration_api.py`)

A single blueprint (`integration_v1`, endpoint `integration_v1.dispatch`) owns every
`/integration/v1` path. It is the only endpoint the global login gate lets through, by
exact endpoint name. CSRF protection is exempted for this blueprint only. The blueprint
never reads the browser session or cookies, never sets a cookie, and answers `404`
exactly like an unknown route while `INTEGRATION_ENABLED` is off.

The signed target is the server-owned WSGI variable `RAW_URI`. If it is missing or not an
origin-form `/integration/v1...` target the answer is `503 raw_target_unavailable`.
`X-Original-URI`, `X-Forwarded-*` headers and the decoded path are never used. Any alias
(`//`, `%2F`, dot segments, trailing slash, unsorted or lowercase-escaped queries) is a
`400`; there are no redirects (`merge_slashes` and `strict_slashes` are off). `HEAD`,
`OPTIONS`, `PUT` and `PATCH` answer `405` with `Allow: GET, POST, DELETE`; there is no CORS.
Request bodies are read with a hard bound of 65537 bytes (a larger `Content-Length` is
refused first) and never parsed by the framework. WSGI folds duplicate headers before this
code runs, so duplicate-header framing at the real edge is still to be qualified;
comma-folded security headers fail closed.

Order of operations: method, enabled, raw target, canonical form, route lookup (uniform
404), receiver verification (this consumes the `jti`), per-viewer rate limit, handler.
A refused or unqualified request therefore still consumes its assertion; retry with a new
one. The rate limit is an in-memory token bucket (burst 60, 20 per second, at most 2048
tracked viewers) and answers `429` with `Retry-After`.

### Route, scope and query map

All routes use the viewer keyring except `POST /readers` (service keyring, `readers:write`).
`list` keys are `before_id` (positive integer, at most 16 digits) and `limit` (1 to 100,
no leading zero).

| Method and path | Scope | Query keys | Status |
|---|---|---|---|
| GET `/me` | catalog:read | none | implemented |
| GET `/capabilities` | catalog:read | none | implemented |
| GET `/requests` | requests:read | list | implemented |
| GET `/requests/<id>` | requests:read | none | implemented (foreign and missing are the same 404) |
| GET `/catalog/search` | catalog:read | q, page, type | implemented |
| GET `/catalog/rails/<rail>` | catalog:read | type, region, window, provider_id, genre_id, year_from, year_to, page as applicable | implemented |
| GET `/catalog/(movie\|tv)/<id>`, `/catalog/tv/<id>/seasons/<n>` | catalog:read | none | implemented |
| GET `/genres` | catalog:read | type (required) | implemented |
| GET `/providers` | catalog:read | type (required), region | implemented |
| GET `/genre-tabs`, `/people/<id>` | catalog:read | none | implemented |
| GET `/follows` | watchlist:read | none | implemented; isolated saved people |
| POST, DELETE `/follows/<id>` | watchlist:write | none | implemented; exact body `{}` |
| GET `/watchlist` | watchlist:read | list | 503 |
| POST, DELETE `/watchlist` | watchlist:write | none | 503 |
| POST `/requests` | requests:write | none | 503 |
| POST `/availability` | availability:read | none | 503 |
| POST `/playback-contexts` | playback-contexts:write | none | 503 |
| GET `/issues`, `/issues/<id>` | issues:read | list, none | 503 |
| POST `/issues` | issues:write | none | 503 |
| POST `/readers` | readers:write (service) | none | 503 |

A `503 capability_unqualified` (`retryable: false`) is returned only after a valid signed
request; it performs no database write other than the replay store and makes no catalog,
provider or search call. `GET /capabilities` and `GET /me` share a dynamic supported-operation
list: base `me.read` and `requests.read`, plus locally configured implemented catalog reads.
Affected operations remain in sorted `incomplete` when configuration is invalid or cannot
be read. Genre tabs operate independently of the TMDB key; details require a valid native
region. Capabilities perform no provider probes, and provider outages do not remove support.
Follows use a separate non-automatic saved-person source. Read support requires a healthy
source; write support additionally requires locally valid metadata configuration and the
result store. GET, DELETE and historical retries can work without metadata configuration.
Personalization readiness failures do not disable catalog or base reads.

Catalog orchestration, exact upstream paths, strict query/region/tab validation, failure
mapping and retry policy are documented in [integration-catalog.md](integration-catalog.md).
Catalog queries are decoded once from the verified canonical target, never Flask arguments.
Catalog output carries metadata only, with no global presence, entitlement or availability
authority. The service applies the 900000 byte data size check before the HTTP ok envelope.

`GET /me` re-reads the native user row and returns the id, a display name (the username
when no safe display name exists), the approval policy (`automatic` for administrators and
auto-approve users) and the UTC calendar-month request count (denied requests excluded).
A quota of 0 or an administrator gives `limit` and `remaining` of `null`.

`GET /requests` fetches `limit + 1` rows and returns `next_before_id` only when more rows
exist. Receipts always carry `availability` as unknown in every field.

### Verification inputs

The receiver tests use generated synthetic RSA keys and temporary SQLite databases.
The application boundary test runs real startup, authentication, CSRF, the default
identity resolver and replay store in an isolated subprocess with network access
disabled. Browser cookies cannot replace or change the assertion actor.

Cross-language qualification additionally sets `MYCELIUM_WIRE_VECTORS` to the pinned
public-key fixture and `MYCELIUM_INTEGRATION_CONTRACT` to the reviewed route proposal.
These optional checks skip when their inputs are absent; skips are not release evidence.
Synthetic HTTP fixture results do not qualify raw production-edge framing, catalog
policy, library grants or repair activation.

## Saved people and historical personalization results (C2c1)

The signed follows facade uses only `integration_saved_people`. It never reads, copies,
writes or changes `favorite_actors`, whose existing automatic-request consumers remain
unchanged. Every follows response includes `automatic_requests: false`. Watchlist GET,
POST and DELETE remain unqualified and unadvertised; there is no legacy-row hydration,
metadata migration, identity-hint change or new processor/request/quota/outbox/grant path.

POST and DELETE `/follows/<person_id>` require body exactly `{}`. The canonical path
provides the strict integer identity 1..2147483647; all caller fields, including owner,
name, image, person_id and automatic flags, are rejected. Existing signed raw body,
Idempotency-Key, scope and native actor binding rules apply without new claims or routes.
Native admin status permits no cross-owner operation or request-policy override.

A new POST resolves checked raw `/person/<id>` with
`append_to_response=combined_credits` through the existing person builder. Only person_id,
name and optional validated profile_url are retained. Existing validated membership and
historical result retries avoid provider calls; DELETE never fetches. Actual missing
persons are 404; outages and malformed metadata use the catalog's safe 503 classification.
Capabilities perform no provider probes, and outages do not remove implementation support.

Metadata resolution happens outside a SQLite write transaction. `BEGIN IMMEDIATE` then
rechecks the native enabled owner, replay binding, current membership and capacity before
atomically changing membership and inserting the success receipt. Failures roll back both.
No legacy helper that commits or opens a nested transaction is used. Every list/replay
reads the enabled owner and owned data in one explicit read transaction.

The list returns at most 200 people ordered by person_id. Its same-transaction lookahead
of 201 proves `complete: true` only if no source rows are omitted. A genuine empty source
returns empty items with complete true; source failures return 503 rather than empty data.
Membership is capped at 1000 per owner. Existing-member operations and replays do not
consume another membership slot; DELETE remains available subject to durable result capacity.

`integration_personalization_results` reserves a shared namespace for follows and future
watchlist operations, separate from request-intent idempotency. The semantic hash binds
resource, action and normalized identity; the exact signed raw-body digest is stored
separately. Same-owner/key/same-operation retries return the original immutable success
data, even with differently spaced equivalent JSON and a fresh signed assertion. Changed
identity, resource or POST-versus-DELETE conflicts with 409. Transport assertion replay
still returns 401. Different owners have independent key namespaces.

Receipts describe historical committed operations, not current membership. Replaying an
old POST after a newer DELETE returns the original following=true receipt without adding
membership again. Clients must refetch the follows list after mutation receipts. Different
keys serialize by commit order; there is no client-time ordering promise.

Results survive membership deletion and process restart. They are immutable and retained
until native owner deletion, with guards against update, deletion and replacement that
still permit owner cascade. There is no TTL, automatic eviction or key recycling. Limits
are 100000 result records per owner and 1000000 globally. Capacity failures are safe
nonretryable 503 and do not mutate membership when a result cannot be persisted. Existing
receipt replays remain available at capacity. Only successful committed results are stored;
metadata or validation failures leave no membership/result pair. No JWT, token or private
key is stored in either table.

Deterministic temporary-SQLite and signed HTTP tests cover ownership, enabled rechecks,
historical replay, concurrency, rollback, retention/cascade, bounds, metadata failures,
source corruption and unchanged legacy consumers. These checks do not qualify deployment,
public edge behavior or final browser acceptance.
