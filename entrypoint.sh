#!/bin/sh
# Mycelium entrypoint: remap the mycelium user/group to PUID/PGID, fix
# ownership on /data, then drop privileges via gosu.
#
# PUID/PGID default to 99/100 (Unraid's nobody:users) so the bind-mounted
# /data is writable on Unraid out of the box. Synology, TrueNAS and other
# NASes use different defaults; override via the environment.
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"

current_uid="$(id -u mycelium 2>/dev/null || echo 99)"
current_gid="$(getent group mycgrp | cut -d: -f3 2>/dev/null || echo 100)"

if [ "$current_gid" != "$PGID" ]; then
    groupmod -g "$PGID" -o mycgrp
fi
if [ "$current_uid" != "$PUID" ]; then
    usermod -u "$PUID" -o mycelium >/dev/null
fi

# Only chown the writable data dir; /app is baked into the image.
chown -R mycelium:mycgrp /data 2>/dev/null || true

exec gosu mycelium "$@"
