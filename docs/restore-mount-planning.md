# Restore mount planning

A restore helper must see the application's namespace, including nested binds.
If `/data` comes from one source and `/data/media` from another, copying a file
under the first source's `media` directory does not restore the application's
file. The nested bind hides that directory.

`RestoreMountPlan.from_inspect` consumes the actual Docker mount inventory and
explicit physical roots for each source and pool. It requires bind mounts for
the complete data subtree. `worker_mounts` includes every nested bind in parent
order, as read-only logical views; writable access is limited to explicit
physical roots. `place` selects the deepest application bind before translating
the relative path to its admitted physical host and helper roots. It refuses a
missing pool mapping instead of falling back to the data root.

A carrier must use all three checks before any effect:

1. Re-read the application mounts and call `validate_live_mounts` against the
   original plan. Its separate identity gate must also bind the container,
   image, configuration, process and unrelated mounts.
2. Start its isolated helper with `worker_mounts`, inspect the actual helper,
   and call `validate_helper_mounts`. Extra, missing, duplicated, writable
   logical views, volumes and unexpected tmpfs mounts refuse. Explicit tmpfs
   allowances cannot shadow planned binds.
3. Call `validate_placement` for every source and target. Prove physical root,
   ancestor and file identities, pool/device ownership, target absence and
   logical/physical readback using the carrier's own bounded runtime checks.

This module performs no I/O and grants no write or runtime authority. It does
not discover which physical pool owns a shfs file, follow symlinks, stop writers
or establish a filesystem snapshot. Those facts must be earned and pinned by a
reviewed carrier. Callers must not retain the former data-root-relative
placement algorithm alongside this planner. Existing operation packets remain
immutable; future operations must bind the merged module and pass their own
review and admission gates.
