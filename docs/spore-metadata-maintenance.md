# NFS metadata maintenance for an offline library repair

This protocol is a prerequisite for a separately admitted repair carrier. It
never stops Python or Plex, edits a database, scans a library, or establishes a
VI/file writer fence itself. A source/image approval alone grants no runtime
maintenance or repair authority.

The virtual tree cannot be recovered exactly from VI and local stub files.
`app._build_spore_tree_json` also consumes a provider-derived, memory-only
`_spore_status` map. A historical `.fsh` size never proves `cached=true`.

## Local control transport

On Linux the Go helper listens at the abstract Unix address
`\0spore-nfs-control-v1` inside its own network namespace. It exposes no TCP
control port, creates no filesystem socket to delete on restart, and requires
kernel `SO_PEERCRED` UID 0 before reading a request. The app user is not root.
Non-Linux peers always refuse. This replaces filesystem socket permissions
with kernel peer credentials; a carrier must verify the actual user namespace,
UID mapping, container and Go process identities before using it.

Send exactly one JSON object of at most 4096 bytes, then half-close the writing
side. Duplicate/unknown keys, case aliases (such as `Owner`), extra objects and invalid
command fields refuse. Only the exact lowercase protocol keys are accepted.
At most two root control sessions run concurrently, each bounded by 40 seconds.
Commands are:

* `status`: no other fields. Reports the helper instance, current owner, mode,
  denied operations and active permit count. This counts media/HTTP/refresh
  work, not all read-only metadata RPCs.
* `enter`: `owner` (16-96 lowercase letters, digits, `-` or `_`),
  `remaining_seconds` (1-2700) and RFC3339 `hard_end`. The carrier supplies the
  remaining duration from its original monotonic frame and original UTC end,
  conservatively reserving transport time. The helper checks both clocks.
  Entry closes admission before draining in-flight media reads, HTTP bodies
  and complete refresh installations. The drain has a 30-second ceiling.
  Its current serving tree must be nonempty and no older than the normal tree
  TTL at admission. Effective size/mtime for every serving entry is captured once
  after the drain, using the same FSH/availability predicate as normal NFS stat.
  That immutable map serves both snapshots and NFS metadata for the interval;
  expiring pre-entry FSH misses cannot change it. Refusal after closing admission
  leaves it fenced.
* `snapshot`: same owner and up to 256 exact unique relative `paths`. Returns
  the sorted actual serving entries, generation, fetch time, effective sizes
  and mtimes for requested paths, helper instance and a SHA256. The digest is
  over the response snapshot with its `sha256` field empty. Effective sizes use
  the entry-time `.fsh` and availability predicate captured for NFS stat. An unknown path or
  expired operation refuses; there are no synthetic cached defaults.
* `resume`: same owner only. The controller must have explicitly resumed the
  exact Python writers and proved their metadata API ready. The helper keeps
  media admission closed while it fetches a fresh tree from the exact configured
  tree endpoint, with no redirect to another URL. Resume checks the ORIGINAL
  monotonic and UTC deadlines before any request, bounds the request by that
  remaining budget, and rechecks before install and reopening. Expired owners
  cannot resume or renew; recovery then needs a separately ruled action.
  The response must carry `X-Spore-Tree-Built-Monotonic-Ns`: Python atomically
  publishes the body with its CLOCK_MONOTONIC build-start timestamp. Go requires
  that build to start after this resume request, so an unexpired 30-second Python
  cache is refused rather than misrepresented as a post-repair tree. There is no
  cache-bypass endpoint or automatic retry: the controller must account for the
  cache TTL before an explicitly admitted resume. The carrier must prove Python
  and Go share the container time namespace and CLOCK_MONOTONIC origin.
  A failed, stale, unproven or empty result
  retains the frozen generation and fence. Success stages the response first, then checks the original budget AFTER
  acquiring both admission and tree locks. Installation, frozen-size retirement
  and reopening occur together at that decision point. Lock-wait expiry leaves
  every original serving map and generation untouched; a later wall-clock tick
  does not retroactively report a committed resume as a refusal. Repeating enter never renews an owner.

Socket replies are structured `{ "ok": ..., "result": ..., "error": ... }`.
A lost reply means unknown state, not permission to repeat an effect. Inspect
read-only status and reconcile under the operation's failure policy.

## What is fenced

While active, the serving tree cannot refresh or be replaced. Metadata remains
usable past the normal 300-second refresh cadence without contacting Python.
Media READ is denied, including header reads. Sidecar-only `.minfo` reads remain
read-only. Both HTTP clients, including background range work, deny new network
requests; existing response bodies are counted until EOF/close. A failed drain,
controller loss or expired budget never automatically resumes service. A helper
restart changes its instance and is an operation failure to reconcile.

Python's normal tree/size HTTP API is intentionally unavailable while its writer
processes are stopped. Collection must instead use this actual Go serving
snapshot AFTER the OS-level writer stop, joined to a fresh frozen VI snapshot
and current stub/minfo/STRM/header proofs. It is evidence of what NFS advertises,
not a guarantee that the remote provider cannot expire an object. Read-only
metadata operations can continue during maintenance; they cannot make network
requests or replace the serving tree.

## Required runtime evidence, separately reviewed

Check zero Plex AND Jellyfin sessions playing or paused before entry. Gracefully
stop Plex; hold the exact edit then scanner lock inodes and the normal burst
lease. Freeze the identity-pinned Gunicorn master and complete writer thread
set; prove no unknown external writer or async I/O path exists. Maintain that
fence from before collection through native after-verification. Verify full
unchanged VI DB/WAL hashes and scoped file-set membership, hashes and metadata;
hashes alone do not prove that a writer did not write and revert.

The carrier must bind every snapshot/sample to the same Go process instance and
generation, enforce its ORIGINAL UTC and monotonic budget across all stages,
and refuse any process, namespace, ownership or source mismatch. Resume only
under its exact carded policy, qualify fresh normal metadata before starting the
same Plex container, and never infer cleanup from a success marker. Unknown or
possible SQL effect requires a coordinator ruling; rollback is never automatic.

The collector adapter and live carrier still need their own source/rehearsal,
CODE+SEC desk and fresh runtime GO. The edit uses a reserved evening after every
gate, with the automated-edit buffers respected. This feature does not enable
hygiene, release held scans, fetch provider media or authorize any SQL change.
