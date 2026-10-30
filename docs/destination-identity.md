# Offline source/destination identity and rollback binding

This module extends the checked synthetic fence with exact owned file identities.
It is not imported by app startup, normal db.init, routes, imports, retries, repairs
or a shared writer. Schema initialization receives an owned disposable connection.
Fixtures contain synthetic text files, not media or a live namespace snapshot.

The controller supplies an owned directory descriptor. Observation traverses only
literal relative paths beneath it, with O_NOFOLLOW on every component, and bounded
namespace/file reads. Absolute, dot/traversal, encoded, noncanonical Unicode and
case-colliding names are refused rather than normalized. Ancestor and leaf symlinks,
nonregular files, multiply-linked leaves and device changes are refused. The binding
records actual descriptor-root identity, ancestor and leaf inode/device/metadata,
and SHA256 bytes. Equal bytes do not excuse a changed inode, root or ancestor.
Multiple source candidates or source/destination/rollback aliases are refused.
These observations do not prove the controller's parent namespace, mount topology,
source provenance, media correctness or all possible aliases/readers complete.

The exact typed episode must be in the validated owned plan. Original owner, body,
scope and future cutoff remain bound through its metadata and checked fence hashes.
Source bytes and destination preimage are pinned. A separate unaliased rollback file
must contain that same original preimage. The immutable record also binds the planned
forward source hash and next destination revision. It never records an actual forward
effect or fabricates an applied receipt. A matching rollback plan remains unqualified
for rollback and for effects; no copy, install, rename, delete or rollback API exists.

Preparing and claiming revalidate the actual observations and current model fence.
Cross-process CAS permits one identity claimant. Checking repeats source/fence,
generation, root, ancestor, bytes and rollback checks before advancing its revision.
Unknown readers, changed plan/owner/fence/preimage, stale generations or mutated
backup evidence fail with no effect. Records and identity are immutable and retained;
claims have no expiry or automatic reclaim. New fixture setup and adversarial input
mutations are separate from application effects, which remain zero in every case.

The model holds a SQLite transaction while it checks, but does not reserve a real
filesystem namespace or close the later filesystem commit race. An eventual real
adapter still requires complete live reader/writer authority, held descriptor/fence
identity across effects, an actual durable forward receipt and qualified rollback.
qualified_for_effects and qualified_for_rollback are always false. Existing bearer,
CDN, NFS and legacy writer unknowns remain unchanged; no completeness claim follows.
