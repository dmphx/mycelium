# Native review and dispatch state

Approval is a review decision. It does not mean the processor completed, an exact
scope is available, or playback works. Newly approved native requests record a
ready dispatch identity in the same SQLite transaction as approval, including
owner, typed media, title, immutable scope and original body digest. Old movie
requests retain explicit legacy_movie provenance and no original body proof.
Old TV requests without original scope stay unavailable.

History and native mutation receipts expose request_dispatch separately from
review status. Each handoff uses an incrementing attempt and a database CAS.
Pure local preparation can fail before entering any adapter. Only that recorded
pre_effect_failed state, with handoff_not_entered proof, allows an explicit admin
retry supplying the exact previous attempt. Three total attempts are allowed.
Current owner enablement, approval status and original identity are checked again.
A ready record at attempt zero also proves no dispatcher claim occurred. An
explicit retry supplying zero can recover a crash after approval commit but
before claim. Only one initial or explicit claimant wins. The normal native
session, admin role and CSRF gates apply to retry-dispatch.
There is no automatic retry, new request creation, or scope/cutoff recomputation.

Unknown is committed before the dispatcher is invoked. Every exception inside
the dispatcher stays unknown, even one that appears early, because monitor or
other effects may already have occurred. A normal dispatcher return records only
accepted handoff, not processor success. Interrupted preparing or unknown states
are never reclaimed by elapsed time. A stale completion cannot convert unknown
into a retryable failure. Database-write or process failures after handoff can
leave conservative unknown state requiring separately qualified reconciliation.

No worker, retry scheduler, provider, shared monitor or signed API activation is
added here. Tests capture or raise at the dispatcher. Existing shared executor,
all-writer/reader fences, delivery durability, final browser presentation and
production database rollout still need their own qualification. These native
states are not signed integration processing states or RequestReceipt objects.
