# Weekly episode hygiene

The `episode_hygiene` scheduler job runs phantom quarantine and episode-numbering
reconciliation. It keeps their existing playback and in-process maintenance
gates.

## Phantom classification

TMDB agreeing with itself is not enough evidence: on 2026-11-13 seventeen of
59 quarantined episodes were real (a merged two-part finale, spin-off arcs
TMDB dropped, specials and a restructured web series). Every never-played
episode above TMDB's self-consistent season boundary is now classified:

| Class | Rule | Applied |
| --- | --- | --- |
| `review` | TVmaze lists the episode number as a regular episode | never |
| `review` | its own `.nfo` has a real title (not `Episode N`) that matches no live canonical slot, or an air date | never |
| `duplicate` | its `.nfo` title equals exactly one live canonical slot in the same show, with no conflicting air date, whose `.strm` and Spore stub exist, rechecked at apply time | quarantined |
| `review` | its `.nfo` exists but can't be read | never |
| `phantom` | a readable or absent `.nfo` with no provenance, and either no recorded release (no hash, magnet or usenet id; an unknown size is not enough) or the legacy blind 1..24 registration on one pack | quarantined |
| `review` | anything else | never |

TVmaze is read without a key, at most once a second, with one bounded retry
after a 429, through a fresh cache for each discovery run. A season it can't confirm is skipped. Unnumbered specials never
veto. Review rows are reported in the result and never moved. A run that applies
anything, or finds any review row, writes `manifest.json` with every quarantined,
failed and review row, so review-only runs still leave a report.

| Setting | Default |
| --- | --- |
| `EPISODE_HYGIENE_MAX_QUARANTINE` | `10` per run |
| `EPISODE_HYGIENE_MAX_PER_SHOW` | `5` per show |

A run over either cap moves nothing and queues no scan. It writes
`proposal.json` in the quarantine stamp folder for a person to review. Each
manifest row keeps the complete `virtual_items` row, so a restore never needs a
database backup.

## Host fence

Before either pass, the job takes exclusive, nonblocking `flock` locks on the
host's existing `convert-cached.lock` and `plex-targeted-scan.lock`, in that
order. It holds both through discovery and both passes. Host conversion,
offline repair and the scanner must use those same files. An independent
lock file inside the container would not serialize host work.

Bind the **directory** containing the host locks at
`EPISODE_HYGIENE_HOST_LOCK_DIR` (default `/run/host-media-locks`). A read-only
directory bind is sufficient on Linux: the job opens existing files read-only
and locks their inodes. It never creates, truncates, replaces or unlinks a lock.
Do not bind individual files, since replacing a host file would leave a stale
container inode. Missing, unreadable, symlinked, hard-linked or replaced locks
defer the run. File descriptors are not inherited by child programs and close
on success, refusal and exceptions.

The job also refuses to start during the configured nightly window:

| Setting | Default |
| --- | --- |
| `EPISODE_HYGIENE_TIMEZONE` | `TZ`, or `UTC` when unset |
| `EPISODE_HYGIENE_NIGHTLY_START` | `01:00` |
| `EPISODE_HYGIENE_NIGHTLY_END` | `06:00` |
| `EPISODE_HYGIENE_INTERVAL_HOURS` | `168`; `0` disables the job |

Times are strict `HH:MM`, start inclusive and end exclusive. A window may cross
midnight; equal endpoints or an invalid timezone refuse work. The clock is
checked before opening locks and again after both are acquired. A run already
underway keeps the locks until it finishes, so host work cannot overlap it.

A busy lock, blocked window or active playback logs a fixed deferral reason.
There is no waiting, catch-up or immediate retry: the existing scheduler retains
its normal next interval. An interval that repeatedly lands inside the blocked
window will keep deferring; choose a safe phase when enabling the job.

Deployment must verify that the container sees the same lock inodes as the host.
Before re-enabling destructive hygiene, resolve any separate metadata-review
hold. Deploying this fence does not establish the correctness of a season
boundary, restore quarantined files, or release queued remove requests.
