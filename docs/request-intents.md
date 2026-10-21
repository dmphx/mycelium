# Request intents (foundation)

`request_intents.py` is the persistence and service layer for per-user request
intents. It has no routes, no metadata resolver and no executor; those are a later
step. Public request ids are the existing `user_requests.id`.

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
