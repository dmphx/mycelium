#!/bin/sh
# Lock-guarded continuous drain for the HEVC probe-later backlog.
# Only one instance runs at a time. Stale locks (crashed daemon) are reclaimed.
# Usage: drain_daemon.sh [BATCH] [SLEEP_SEC]
HR=/data/hevc_remediation
LOCK=$HR/drain.lock
BATCH=${1:-100000}
SLEEP=${2:-30}

# Reclaim stale lock if its recorded pid is no longer alive
if [ -d "$LOCK" ]; then
  oldpid=$(cat "$LOCK/pid" 2>/dev/null)
  if [ -n "$oldpid" ] && [ -d "/proc/$oldpid" ]; then
    echo "drain already running pid=$oldpid; exiting"
    exit 3
  fi
  echo "removing stale lock (recorded pid='$oldpid' not alive)"
  rm -rf "$LOCK"
fi

# Atomic acquire
mkdir "$LOCK" 2>/dev/null || { echo "lock race lost; exiting"; exit 3; }
echo "$$" > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT INT TERM

echo "=== drain start uptime=$(cut -d" " -f1 /proc/uptime) batch=$BATCH sleep=$SLEEP ==="
python3 "$HR/probe_later.py" "$BATCH" "$SLEEP"
rc=$?
echo "=== drain done rc=$rc uptime=$(cut -d' ' -f1 /proc/uptime) ==="
