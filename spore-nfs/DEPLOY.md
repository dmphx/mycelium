# spore-nfs deployment (Plex on Synology)

spore-nfs exposes the virtual library as a real read-only NFSv3 export so Plex
gets true file sizes and real bytes, and Direct Play becomes the correct outcome
on every client (Shield / Android TV included) instead of the black-screen the
341-byte stub hits there. It carries no media itself: every Stat/Read proxies to
mycelium's existing `/spore-stream/<token>` endpoint.

This is additive. Bring it up as a SECOND Plex library next to the stub library
and validate before retiring anything.

## Facts that drive the mount

- spore-nfs runs INSIDE the mycelium container (merged image), listening on
  `:2049`, backing store `http://127.0.0.1:8088`.
- It is a single-port `willscott/go-nfs` server: MOUNT and NFS both on TCP 2049,
  no rpcbind/portmapper, no lock manager (NLM), AUTH_NULL, read-only.
- Export root is `/`. Mount it as `127.0.0.1:/`.
- `docker-compose.yml` publishes it as `127.0.0.1:2049:2049` (host loopback only).
- The `/spore-nfs/tree` and `/spore-stream/` routes are auth-exempt
  (auth.py `_enforce`), so the fail-closed auth gate does not block polling.

Because there is no portmapper and no NLM, the client MUST pass
`vers=3,tcp,port=2049,mountport=2049,nolock`. Leaving any of those off makes the
mount hang or fail.

## Stage 0 - build and deploy the merged image

```bash
ssh corveck@10.0.0.10 "cd /volume1/docker/mycelium && docker compose up -d --build"
# watch spore-nfs come up alongside gunicorn:
ssh corveck@10.0.0.10 "docker logs mycelium 2>&1 | grep -i 'spore-nfs listening'"
```

Expect a line like `spore-nfs listening on :2049, backing store = http://127.0.0.1:8088`.

## Stage 1 - validate the export (no Plex yet)

First confirm mycelium is serving the tree (fast, proves the backing side):

```bash
ssh corveck@10.0.0.10 "curl -s http://127.0.0.1:8088/spore-nfs/tree | head -c 400"
# should be JSON: {"entries":[{"token":"...","path":"movies/.../....mkv"}, ...]}
```

Then mount the export read-only on the NAS host and list it:

```bash
ssh corveck@10.0.0.10
sudo mkdir -p /volume1/docker/mycelium/data/spore-nfs-mnt
sudo mount -t nfs -o vers=3,tcp,port=2049,mountport=2049,nolock,ro,soft,timeo=30,retrans=2 \
    127.0.0.1:/ /volume1/docker/mycelium/data/spore-nfs-mnt
ls -R /volume1/docker/mycelium/data/spore-nfs-mnt | head
# a real file should report its true size:
stat /volume1/docker/mycelium/data/spore-nfs-mnt/movies/*/*.mkv | head
```

If `ls` shows the movies/series tree with real sizes, the export is good.
Unmount before wiring Plex so Plex owns the mount lifecycle:

```bash
sudo umount /volume1/docker/mycelium/data/spore-nfs-mnt
```

If the mount hangs: you dropped one of `port=2049,mountport=2049,nolock`. If it is
empty: check `docker logs mycelium | grep 'tree refreshed'` and the curl above.

## Stage 2 - wire into Plex as a Docker NFS volume

Cleanest for the Plex-in-compose setup: a named NFS volume mounted by the Docker
daemon (addr `127.0.0.1` resolves to the host loopback where 2049 is published).
`soft,ro` so a spore-nfs hiccup never hard-hangs Plex I/O.

In `/volume1/docker/plex/docker-compose.yml`:

```yaml
services:
  plex:
    # ...existing config...
    volumes:
      # ...existing volumes...
      - spore-nfs-media:/spore-nfs-media:ro

volumes:
  spore-nfs-media:
    driver: local
    driver_opts:
      type: nfs
      o: "addr=127.0.0.1,vers=3,tcp,port=2049,mountport=2049,nolock,ro,soft,timeo=30,retrans=2"
      device: ":/"
```

Start order matters (separate compose projects, no cross-project depends_on):
bring mycelium up first, then Plex.

```bash
ssh corveck@10.0.0.10 "cd /volume1/docker/mycelium && docker compose up -d"
ssh corveck@10.0.0.10 "cd /volume1/docker/plex && docker compose up -d"
# confirm Plex sees the tree:
ssh corveck@10.0.0.10 "docker exec plex ls /spore-nfs-media/movies | head"
```

## Stage 3 - add the Plex library and test

1. Plex > Settings > Libraries > Add Library (Movies, and/or TV Shows).
2. Point it at `/spore-nfs-media/movies` (and `/spore-nfs-media/series`).
3. Advanced: disable "Perform extensive media analysis during maintenance" and
   intro/credit detection for this library while testing. Every deep analysis
   reads bytes through spore-nfs, which pulls from the CDN.
4. Scan. Then play a title that Direct-Plays badly on Shield/Android TV today.
   Expect Direct Play with a correct picture, no transcode, no black screen.

Watch the read path while testing:

```bash
ssh corveck@10.0.0.10 "docker logs -f mycelium 2>&1 | grep -Ei 'spore-nfs|spore-stream'"
```

## Stage 4 - retire the stub per title (only once trusted)

Keep both libraries up until you trust spore-nfs. Retiring is per title: remove a
title from the stub library's scope (or the stub library entirely) once its
spore-nfs equivalent plays clean everywhere. No rush, no big-bang cutover.

## Gotchas

- Mount hangs -> missing `port=2049` / `mountport=2049` / `nolock`.
- Empty library -> tree not refreshing; check `curl .../spore-nfs/tree` and
  `docker logs mycelium | grep 'tree refreshed'`.
- Plex container will not start -> NFS volume mounts at start and mycelium was
  down; start mycelium first, then Plex.
- Permission denied reading files -> go-nfs is AUTH_NULL and reports a
  world-readable mode; if Plex still cannot read, check the file mode go-nfs
  returns and the `ro` option is present (not a uid mismatch, NullAuth ignores it).
- Never widen `127.0.0.1:2049:2049` to the LAN: the NFS protocol here is
  unauthenticated. Loopback + Plex on the same host is the security boundary.
