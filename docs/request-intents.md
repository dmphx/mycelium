# Request intents (foundation)

`request_intents.py` is the persistence and service layer for per-user request
intents. It has no routes or executor. The separate metadata resolver is described below. Public request ids are the existing `user_requests.id`.

## API
- `normalize_payload(payload) -> {media, scope}`: strict, canonical, raises `IntentError(code, status)`.
- `semantic_hash(normalized)`, `raw_signature_hash(raw_body: bytes)`: replay decisions use only the semantic hash. The raw hash is SHA-256 of the exact body bytes; `admit(..., raw_body=None)` stores NULL when the caller has no verified raw body, and never derives one from the parsed payload.
- `replay_lookup(user_id, payload, idempotency_key) -> receipt | None`: call before any network metadata lookup. Checks the owner is enabled; a different body under the same key raises `idempotency_conflict` (409).
- `admit(user_id, payload, idempotency_key, metadata=None, raw_body=None) -> receipt`: `metadata` is trusted, server-resolved `{title, imdb_id}`, needed only when a new row is created.
- `review(request_id, admin_id, approve, note=None) -> receipt`, `get(user_id, request_id)`, `list_for_user(user_id, limit, before_id)`.
- Outbox: `claim_next(lease_seconds, now)`, `renew(outbox_id, token, lease_seconds, now)`, `finish(outbox_id, token, stage='dispatched', now)`, `defer(outbox_id, token, retry_after, stage, now)`, `fail(outbox_id, token, error, retry_after, max_attempts, now)`. `now` exists for tests only.

## Rules
- Idempotency keys are per user. A replay returns the original receipt; coalescing an active identical intent (same user, media and exact scope) returns `existing_request` and records the new key.
- Policy is read from the database inside `BEGIN IMMEDIATE`: enabled, role, auto_approve, quota. Admins bypass quota, 0 means unlimited. Quota counts this month's `user_requests` rows except denied (so legacy rows count and failed rows stay charged); replays and coalesced requests are free.
- Pending requests create no outbox row. Auto-approved or admin requests create exactly one ready job in the same transaction. Approval is a pending-only compare and swap; a denied request cannot be revived.
- Media, scope, scope hash and the UTC cutoff date are immutable (triggers). Receipts show the exact accepted scope and all availability fields as `unknown`.
- A future receipt includes `scope.starts_on`, fixed at admission; the request input cannot choose or override it. Receipt media includes the server-resolved title.
- `finish` means dispatch completed, with stage `dispatched`, `registered` or `watching`; it never claims media is available. `defer` (stages `awaiting_metadata`, `waiting_release`, `rate_limited`) requeues without spending the attempt budget. Finish, fail, defer and renew need the current unexpired lease, an enabled owner and a still-approved request, otherwise they change nothing. Reads (`get`, `list_for_user`) need an enabled native owner, and request ids must be positive integers (not booleans). Lease and retry seconds are finite and bounded.
- Workers lease jobs with a token and expiry. Expired leases are reclaimed with a new token. Jobs of disabled owners are not dispatched.

## Known gaps
- The legacy approve route calls processing with default scope; intents must be approved through `review()` once the routes exist.
- `scope.episodes` and `future` need executor support before they can be dispatched.
- `request_idempotency.raw_hash` is nullable in the schema; this module is unreleased so no migration of earlier test databases is provided.

## Metadata resolver (`intent_metadata.py`)

`resolve(payload, *, today=None, fetch=None)` is pure: no database, no quota, no writes. It validates the payload with `normalize_payload` (invalid input raises `IntentError`, 400) and reads raw TMDB endpoints through `tmdb._get` (imported lazily; `fetch(path)` can be injected for tests). It never uses the IMDb alias lookup, `get_show_info`, `get_season_episodes` or the season-offset helper, so raw show 34549 season 8 is requested as `/tv/34549/season/8`.

Result: `media`, `scope`, `title`, `imdb_id`, `year`, `cutoff` (the UTC date used) and `episodes[]` of `{tmdb_episode_id, season, episode, name, air_date, runtime_min, air_state}` with `air_state` one of `aired`, `unaired`, `unknown`. Movies return `episodes: []`. `episodes` scope returns only the listed pairs; `selected_seasons` returns the named seasons; `all_seasons` and `future` use the show's regular seasons (1 and up), and specials only when named. `future` keeps episodes whose air date is on or after the cutoff day, and keeps unknown air dates for later deferral. This describes metadata only, not registration or playability.

Errors (`MetadataError`): `metadata_unavailable` (503) for any outage, `None` or empty reply, malformed or inconsistent data (identity mismatches, duplicate pair or episode id, bad dates, runtimes, names, IMDb ids, season counts that disagree with the show's `episode_count`, more than 100 fetched seasons or 5000 observed episodes before filtering, including duplicate raw episode IDs across seasons); `scope_not_found` (404) only when a valid, complete response lacks a named season or episode. An upcoming show with no regular seasons is a valid empty snapshot, so an ongoing scope can wait for future metadata. `tmdb._get` returns `None` for both outages and TMDB 404s, so that is always 503, never 404. A named season's whole response is validated, so one malformed unrelated episode in it fails the request. An empty-string `air_date` is treated as malformed, not unknown.

Executors that use this must recheck the lease immediately before each side effect, not only between episodes, because a search can outlast a lease.

## Admission snapshots (internal foundation)

`intent_snapshots.py` (pure) validates a resolver result against the normalized input and builds an immutable snapshot, an internal accepted predicate and hashes. `request_intents.admit(..., resolved=None)` stores them in the same `BEGIN IMMEDIATE` transaction as the request, idempotency key and outbox row; any failure rolls everything back. Without `resolved` the legacy metadata-only behaviour is unchanged and no snapshot is stored. `admit_resolved(user_id, payload, key, resolved)` requires it. `find_active(user_id, payload)` returns the owner's active intent for the same canonical input so a route can skip the metadata network call; replays and coalesced requests never need `resolved` (a supplied one is ignored). `get_snapshot(user_id, request_id)` is an internal, owner-only, enabled-only getter and is not exposed over HTTP. The wire receipt is unchanged.

Validation runs after replay and coalescing and before quota or any insert: media and scope must equal the normalized input, title and IMDb pass the usual checks, at most 5000 episodes (`catalog_too_large`, 503), no duplicate season/episode pair or TMDB episode id, `air_state` consistent with `air_date` against the cutoff, specials only for explicit scopes, and `future` never includes a known date before the cutoff. Disagreement between `metadata` and `resolved` is `invalid_metadata` (400).

Tables (all internal): `request_metadata_snapshot` (canonical JSON, predicate JSON, snapshot/predicate/accepted/input hashes, cutoff; fully immutable), `request_episode_state` (raw TMDB episode id, name, runtime, air date frozen by trigger; only `state` and `updated_at` change; initial state `pending`, `unaired` or `waiting_airdate`) and `request_episode_observation` (append-only, one `admission` row per episode). Delete triggers refuse deletes while the parent intent exists, so owner or request cascade cleanup still works. `request_intents.future_cutoff` is the resolver's cutoff day, never re-read from the clock.

Accepted predicate (internal, pending the final wire contract): movie `{"kind":"movie"}`; TV `{input_scope, numbering:"tmdb", monitoring:"none"|"selected_seasons"|"all_regular_seasons", specials:"explicit_only"}` plus `starts_on` (the cutoff) for `future`.

Not in this slice: publication or the executor, metadata refresh, content registration across users, HTTP routes, or any freshness check on `resolved`. `resolved` must come from the server-side resolver only; a browser-supplied dict must never reach `admit`. Prewrite lease guards do not close the filesystem race, which is a separate gate.

## Receiver-owned preparation (private followup)

`request_preparation.prepare(native_user_id, exact_body_bytes, idempotency_key)` issues an
immutable in-process `PreparedRequest`. The existing receiver must authenticate the actor
and exact body first; this primitive does not authenticate callers. It parses strict JSON,
normalizes the exact media/scope input, captures the enabled native user's policy, fixes the
receiver UTC date, and resolves metadata outside SQLite transactions. Checked root detail
uses `append_to_response=external_ids`; the resolver's logical external-ID read comes from
that same owned response. It never uses legacy `tmdb._get`, find aliases, title matching or
global library caches. Exact typed root/IMDb and raw show/season/episode checks precede the
existing immutable snapshot/predicate builder. Source JSON hashes are provenance diagnostics,
not qualified publisher policy or library grants.

The issued value binds owner, a distinct preparation ID, idempotency key, exact raw body
SHA256, normalized input hash, native policy preimage/hash, receiver cutoff, resolved metadata,
snapshot/predicate/accepted hashes and checked metadata source hashes. Reports expose hashes,
not a reusable JSON-plan credential. There is no exported-plan loader or public constructor.
`check_prepared(..., user_id=..., raw_body=..., idempotency_key=...)` requires the original issued
object and exact bindings, rechecks current enabled native policy, and reports
`admission_qualified:false`. Equivalent JSON spacing preserves semantic/snapshot hashes but
does not preserve raw-body binding. Current policy equality is not proof against unobserved
intermediate changes; source policy revisions and all-writer coverage remain final-admission
requirements. An observed policy change retires that value.

Preparation/checks use a read-only SQLite URI and query-only connection. Missing databases
are not initialized, and the normal thread-local caller transaction must be absent before
metadata work. They never write schema, membership, request history, quota, token ledger or
outbox. The fixed limits are 128 live/resolving values, 60 monotonic seconds including
resolution, 101 checked fetches, 8 MiB aggregate response JSON and 4 MiB serialized preparation.
A blocking checked read can overrun the elapsed budget by its bounded transport timeout;
expiry is checked before issuing and using the value. UTC day rollover or monotonic rollback
expires it rather than silently changing an accepted cutoff. Unknown air dates remain waiting;
specials require explicit scope. Capacity failure releases a reserved slot; issued slots are
released on object collection, without recycling a live value.

`retire(prepared)` cancels the in-process value irreversibly. The value also records its
issuing process; a forked copy is refused before any inherited lock or SQLite access.
Checks revalidate the value after native policy and retirement-ledger reads so concurrent
cancellation or expiry cannot yield a fresh readiness report. The elapsed budget is checked
again after serialization and before issuance.

`request_preparation_tokens.cancel(prepared, user_id=..., raw_body=..., idempotency_key=...)`
adds an immutable durable cancellation bound to the genuine local value and exact context.
It can retire an expired value, but never make it usable. Cancellation replays are read-only
and work at capacity. The additive table has no native-user foreign key: native owner cleanup
cannot erase a tombstone or be blocked by it. Update/delete/replacement guards retain records.
Limits are 1000 per owner and 100000 globally; no TTL or eviction resets a retired token.
Storage failure retires the local value and returns failure rather than reporting durable
success. Preparation/checks require the ledger schema/guards, but never create them. The
existing fresh-schema initializer adds only CREATE statements; no live migration is performed.

Durable consumption tied to a real admitted request, qualified source-policy/destination
proof, atomic final admission and exact scoped execution remain separate required work.
Request write/availability/repair authority and live activation stay unqualified; no existing
request route is wired to this preparation or to the legacy dictionary admission path here.
