# Offline destination and reader/writer fence foundation

The pinned path inventory records actual dispatch, processor, monitor, import,
retry, repair, upgrade, enrichment, metadata, cache and webplayer writers, plus
bearer, NFS, server/indexer, generation and range/cache reader domains. Static
source coverage is not runtime completeness. Every listed legacy group remains
unfenced by this new model. Excluded runtime assets, dynamic/manual writers and
external readers remain unknown. In particular timestamps, idle player cleanup,
zero Plex sessions or an empty observed list never prove all readers absent.

`destination_authority.py` is explicitly initialized on a disposable owned SQLite
connection only. It models an opaque synthetic destination, not a filesystem path.
It is not imported by startup, dispatch, db.init, import, retry, repair or scheduler.
The closed fixture manifest pins a finite set of synthetic gateways. Every writer
and reader must explicitly register the exact pinned gateway before closure. A
missing member, wrong gateway or unknown external domain prevents closure, even
with zero recorded active readers. This is a controlled model assumption, not a
live authority issuer: pinning or registering a caller dictionary cannot enroll
real legacy processes or certify real reader completeness.

All synthetic reader and writer entries use the same SQLite admission gate.
Durable generations advance on entry and explicit completion. Unknown/abandoned
entries remain blocking, with no expiry, clock, idle or process-restart inference.
Membership cannot change after closure. Claiming a destination fence requires a
validated current owned plan, exact metadata generation/hash and unchanged native
owner/scope/body/future cutoff. It compares actual model preimage bytes, destination
revision and reader/writer generations with the pinned plan and expected snapshot.
One cross-process CAS winner blocks every new synthetic reader and writer. Checking
revalidates all of this, including a changed plan head or revoked owner. Interrupted
or checked fences cannot be reclaimed, replayed or automatically retried.

Only model bookkeeping is written. The destination preimage remains unchanged.
Even a checked winner reports qualified_for_effects false; the effect gate always
refuses before any fake or actual destination, provider or shared writer callback.
There is no real publication, rollback, deletion or activation adapter. The tests
qualify mismatch/refusal, source bindings and cross-process gate mechanics, not
real all-writer or all-reader authority. Real deployment still needs complete
source/destination/reader authority and every legacy effect boundary wired and
qualified together under a separately scoped implementation.

Participant event identity and history are also guarded in SQLite. Entries cannot
be deleted or reassigned, and only active entries can become explicitly closed or
unknown. An unknown entry cannot be rewritten as completed. These guards preserve
the evidence needed for model completeness even if bookkeeping SQL is mistaken.
