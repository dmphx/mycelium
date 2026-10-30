# Request UI recovery

The detail dialog uses the existing native `/ui/api/discover/add` response:
`pending` and `approved` require a positive request ID and an exact IMDb identity;
legacy `queued` requires the exact IMDb identity. Missing, malformed, contradictory
or lost responses leave the outcome unknown. They cannot produce an Added label.

A browser-memory attempt is retained by native actor and typed title across dialog
close/reopen. It is claimed synchronously before POST, including same-turn clicks.
There is no automatic repeat submission. The memory ledger admits at most 128
attempts per QueryClient and does not evict uncertain entries. It is not durable
server idempotency: a full page reload or new browser loses it. Request admission,
quota/scope enforcement and exact server-side idempotency remain separate work.

Pending approval is distinct from processing. An explicit accepted response enables
bounded sequential title-library-status reads. The existing status endpoint is
an IMDb lookup, not an owned exact-scope job receipt; UI labels describe the
reported title state and do not prove full requested scope, entitlement or playback. HTTP/JSON failures stop polling and
offer a read-only Refresh request status action. The overall poll deadline is three
minutes; each read is bounded to fifteen seconds. Closing, changing title or changing
actor aborts the read and prevents a late redirect or state update. An unconfirmed
POST offers owned request history, not another POST. History is bounded and finding
no matching row does not prove non-admission or authorize an automatic retry.

TV controls freeze once an attempt is submitted. The selected-season body is the
explicit sorted set at the click, with the existing `monitor_mode` grammar. This
does not qualify the legacy backend's scope executor, grants, library availability
policy or real provider behavior. The signed request admission route stays separate.

The browser attempt retains an immutable click-time TV selection alongside its
status. Closing and reopening a pending or uncertain attempt shows that submitted
selection, including season numbers missing from later metadata. It is labeled as
a submission record, not server acceptance or library coverage. Read-only refresh
retains it; it never changes the POST body or authorizes a second POST.

Owned request history uses the existing native account ID as its cache identity.
Only the existing mine=1 GET is used, with abort on navigation, a fifteen-second
read limit and explicit read-only refresh. Every navigation refreshes stale data.
Malformed containers, duplicate IDs, contradictory ownership and invalid media
kinds are errors, never an empty history or a guessed Movie. Unknown approval or
library states are labeled unknown. A failed refresh retains the previous rows
with an error message. The endpoint still returns at most 200 recent rows; absence
is not proof of non-admission. These UI checks do not replace backend authorization
or qualify exact-scope completion. Admin mutation behavior is unchanged.

Pending approval has a separate explicit read-only refresh. It matches the
accepted request ID, native owner, media kind, TMDB ID and IMDb ID against the
existing owned recent-history endpoint. A matching denied row is shown as denied;
a matching approved row enables the existing title-library-status read only.
Missing, contradictory, unfamiliar or timed-out history remains uncertain and
never enables another POST. Closing aborts the read; the accepted identity and
submitted selection survive. This does not reconcile unconfirmed POST outcomes,
prove exact-scope coverage or grant backend admission authority.
