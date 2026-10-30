# Private player session implementation

Each preparation has a native owner, explicit movie or episode target, and an
independent random playback token. A content hash is retained separately for
the existing provider heartbeat. It cannot select another user's session or
another episode in a pack. File selection requires a unique eligible movie file
or one explicitly tagged season/episode file; ambiguous and untagged packs fail.
These filename checks are selection evidence, not catalog or grant proof.

HLS conversion and seeks share one operation lock per playback. A seek reaps its
own previous transcode and writes a new random generation directory. Readiness
requires every selected audio/video output and its playlists within one bounded
wait. Failed output is reaped and kept as evidence; it is never published as
ready. Earlier generations are retained and served only to the native owner.
There are at most eight generation attempts per playback, including failures;
reaching the limit leaves the current output intact and requires reopening.

Ready and seek responses identify the generation and requested source offset.
The offset is a fast keyframe seek, not proof of frame-exact positioning. The
frontend must reload the fresh URL, avoid applying the source offset twice, and
save absolute source positions with the current generation. Stale generation
writes fail without changing the owned resume record. Resume records bind the
native owner, logical target, content hash and provider torrent/file identity.

This work does not qualify provider bytes, external CDN reader absence, catalog
grants, replacement or storage cleanup. The legacy idle cleanup still needs
separate reader-safety review; timestamps and redirects are not reader leases.
No cleanup or provider operation is executed by synthetic validation. Frontend
lifecycle, real media seek behavior and exact-build browser acceptance remain
separate unfinished work. Daniel removed the old workflow holds; final source
and artifact verification still precede any user-facing replacement.
