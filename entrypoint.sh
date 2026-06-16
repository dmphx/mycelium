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

current_uid="$(id -u mycelium 2>/dev/null || echo 8088)"
current_gid="$(getent group mycgrp | cut -d: -f3 2>/dev/null || echo 8088)"

if [ "$current_gid" != "$PGID" ]; then
    groupmod -g "$PGID" -o mycgrp
fi
if [ "$current_uid" != "$PUID" ]; then
    usermod -u "$PUID" -o mycelium >/dev/null
fi

# Fix ownership of the writable state under /data so the dropped-privilege
# mycelium user can read/write it. Deliberately do NOT recurse the large
# bind-mounted media library (/data/media) or the Spore stub tree
# (/data/plex-media): mycelium creates those files as itself (already PUID:PGID),
# and walking 200k+ inodes on every start adds minutes to a cold restart and
# scales with library size. /app is baked into the image.
for entry in /data/* /data/.[!.]*; do
    [ -e "$entry" ] || continue
    case "$entry" in
        /data/media|/data/plex-media) continue ;;
    esac
    chown -R mycelium:mycgrp "$entry" 2>/dev/null || true
done
# The top-level dirs themselves (mountpoints), non-recursively.
chown mycelium:mycgrp /data 2>/dev/null || true
[ -d /data/media ] && chown mycelium:mycgrp /data/media 2>/dev/null || true
[ -d /data/plex-media ] && chown mycelium:mycgrp /data/plex-media 2>/dev/null || true

exec gosu mycelium "$@"
