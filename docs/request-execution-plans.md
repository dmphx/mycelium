# Isolated request-scoped execution plans

The legacy IMDb-keyed monitor can lose seasons and mode through independent
writers. This ledger does not repair, replace or activate that monitor. It is
not imported by app startup, db.init, a route, a processor or a scheduler.
Explicit initialization and every ledger call receive an owned SQLite connection.

A plan binds its native request ID, owner, typed media/title, immutable native
scope/body hashes and proven pre-handoff dispatch attempt to pinned metadata.
The existing strict admission snapshot builder supplies exact episode identities,
its predicate and fixed receiver future cutoff. Empty TV metadata, missing selected
season metadata, out-of-scope pairs and changed pins fail without a plan. Snapshot
hashes prove input consistency, not metadata provenance or catalog completeness.
Only approved/enabled requests with ready attempt zero or a recorded pre-effect
failure can be planned. Accepted, preparing or unknown handoffs are refused.
No legacy missing TV or movie scope is reconstructed for this ledger.

Plan JSON and identity cannot be updated or deleted. An identical prepare can
return the original plan; changed metadata never overwrites it. Claiming requires
revision zero, exact plan hash and a worker identity. One SQLite transaction CAS
wins across processes and advances to revision one. Validation rechecks native
identity/owner/status/dispatch attempt, metadata, destination digest and reader
and writer generation pins, then advances the same claimant to revision two.
No elapsed-time reclaim, refresh merge or automatic retry exists. The original
v1 ledger remains immutable. Explicit offline successor metadata uses the separate
versioned policy below, never an in-place edit.

The destination and generation values are fixture-supplied preconditions. Their
comparison is not evidence of authoritative readers or all-writer fencing.
Validated means offline input validation only; qualified_for_effects is always
false. The effect gate refuses even the winning worker. There is no callback to
a provider, shared monitor, library, publication or replacement adapter.

The internal execution-plan-v1 fixture is synthetic and hash-pinned. It is not the
missing frozen integration contract or a signed Media receipt/grant. Tests use
fresh disposable databases, two independent CAS processes, immutable native scope
checks and a legacy-shaped sentinel that must remain unchanged. No existing
runtime database migration or provider/auth/credential activation is performed.

## Offline metadata refresh policy version 1

`request_plan_refresh.py` is explicitly initialized on an owned connection only.
No runtime startup, dispatcher, route or scheduler imports it. A validated v1 plan
is generation zero. Only a validated current generation at revision two can append
a successor, with exact expected generation and parent hash. Successors are immutable,
hash-linked version-two plan records, with their own planned/claimed/validated CAS
revisions zero/one/two. SQLite BEGIN IMMEDIATE serializes competing refreshes and
claims across processes. A stale generation or claimant cannot advance. There are
at most 64 explicit successors per request, no expiry, automatic retry or reclaim.

Every refresh rechecks the enabled owner, approval, original scope/body/identity,
pre-handoff dispatch attempt, destination digest and reader/writer generation pins.
The original accepted predicate and metadata cutoff remain fixed. The receiver's
future starts_on is never recomputed from the clock. Selected Specials remain
explicit; all/future scopes cannot add Specials or known back-catalog episodes.
The strict existing snapshot builder validates every new pinned offline snapshot.

This conservative policy permits eligible episode additions, descriptive revisions
and resolution of unknown air dates. It refuses removal of an observed episode,
remapping its TMDB identity or changing an already-known air date. An unknown date
resolving before a future cutoff fails closed rather than erasing or backfilling
that episode. Such corrections need a separately reviewed later policy. Unchanged
metadata does not allocate a new generation. Unknown dates remain unknown, never
playable or complete. Missing or malformed metadata leaves the current head intact.

Hash pins establish consistency of supplied synthetic metadata, not provider
provenance, complete catalog knowledge or authoritative reader coverage. Every
result still has qualified_for_effects false and the effect adapter always refuses.
Shared writers, real destinations, signed request admission and recurring runtime
refresh remain unwired and unqualified. No real database migration is included.
