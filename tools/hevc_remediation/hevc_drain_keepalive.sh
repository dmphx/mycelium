#!/bin/sh
# HEVC remediation drain daemon keep-alive guard (Onyx 2026-07-28).
#
# The drain daemon runs INSIDE mycelium as a `docker exec` child (source:
# mycelium repo tools/hevc_remediation) and is NOT part of the container start
# command, so any mycelium recreate/restart silently kills it and it never comes
# back on its own. This idempotently re-launches it, but ONLY when it is not
# already alive (checks drain_daemon.sh's lock pid first, so a healthy daemon is
# a clean no-op with no log spam; drain_daemon.sh's atomic lock is the final
# race guard). Gate v2 in probe_later.py stops it competing with live playback,
# so running this frequently is safe.
#
# Wired via onyx.cron every 15 min. Worst case after a recreate: ~15 min of
# drain downtime instead of the daemon staying dead forever.

# mycelium not up yet (mid-recreate / stopped): nothing to do, next tick retries.
docker ps --format '{{.Names}}' | grep -qx mycelium || exit 0

# Daemon already alive (lock pid present and its /proc entry exists)? no-op.
docker exec mycelium sh -c 'p=$(cat /data/hevc_remediation/drain.lock/pid 2>/dev/null); [ -n "$p" ] && [ -d "/proc/$p" ]' && exit 0

# Dead: relaunch detached (lock-guarded, gate-protected).
docker exec -d mycelium sh -c '/data/hevc_remediation/drain_daemon.sh 100000 30 >> /data/hevc_remediation/drain_run.log 2>&1'
