# Offline staged receipts and durable uncertainty

The identity foundation refuses a changed observation but does not remember that
refusal. A caller that restores authority could otherwise reuse the old model
handle. This separate, unwired journal closes that acceptance gap for its own
attempts: observed identity/preimage/backup or current-policy mismatch commits an
append-only invalidation before returning an error. Restoring a synthetic owner,
dispatch state or generation cannot revive the attempt. Older foundation APIs are
unchanged and must not be mistaken for a globally enforced fence.

Enrollment requires the exact checked identity and current original physical
observations. One transaction CAS stages a forward generation and hash-bound
inverse plan; the inverse must name the exact forward staged receipt and revision.
The physical destination remains at the original preimage in both states. These
are explicitly offline staged receipts, never observations of performed forward
or rollback effects. Stored status is historical, not a current authority check.

Explicit unknown forward/rollback outcome or unavailable preimage is retained
terminal. Matching original bytes, matching planned postimage bytes, restored
authority, elapsed time and process restart cannot establish an effect outcome or
permit retry. Unknown is never promoted to success/failure automatically. Durable
SQL guards retain bindings/events, prohibit resetting history, and serialize one
forward winner. All fields remain qualified_for_effects=false,
qualified_for_rollback=false and actual_forward_receipt_available=false.

Only owned SQLite records change. Application operations only read fixture files;
setup/adversarial input changes are declared separately. No callback, swap, rename,
copy, deletion, real rollback, provider, global monitor or route is wired. A live
adapter still needs complete namespace/mount/source and all-reader/writer authority,
a fence held across the effect boundary, durable actual outcome evidence and a
separately qualified inverse reconciliation. No live topology completeness follows.
