# Cached episode selection

A successful search means ranking completed. It does not mean a release was
accepted or a virtual item was written. Wanted-episode searches now carry their
exact run id through selection and persist a separate outcome for each ranked
candidate in `episode_selection_outcomes`. `get_search_trace` returns these for
the current run, separately from the ranking result. Reasons are finite codes,
not raw provider exceptions, URLs, credentials, or tokens.

## Generic packs

The existing ranking, language, country, size, executable and cached-file sanity
guards run first. A cached generic pack can then qualify from its selected file
instead of requiring the episode title in the pack's display name. This version
supports only the strict episode-title identity route:

- A nonempty actual file listing is mandatory. A fail-open sanity result is not
  file proof.
- The show's provider identity must exist and match the requested IMDb id.
- Exactly one positive-size video file must name that exact show, the complete
  requested season/episode and the expected episode title. The basename itself
  must carry those identities, with episode-title evidence after the episode tag;
  directory and show names cannot supply a missing episode title.
- Foreign-version identity, wrong numbers, duplicate possible files, missing
  lists, untagged files and ambiguous numbering refuse. There is no largest-file
  or top-ranked fallback, and no new uncached acquisition fallback.

There is deliberately no new numbering-alias exception. A future implementation
of that route must prove its provider/numbering mapping rather than infer one
from the release name. Existing non-pack title selection and existing non-Catbox
acquisition behavior otherwise remain in place.

The proof contains only a version, torrent hash, show IMDb id and name, canonical
season/episode, expected episode title, and the selected relative file name and
size. The VI stores it atomically with registration. The writer refuses a proof
whose hash or canonical numbers no longer match after numbering normalization.

At playback, a proved token uses only that same torrent and the exact proved
name and size. Its current provider file id may change; the binding is not an
old provider id. Missing, changed or duplicated files refuse. Such tokens do not
enter legacy alternate-release, provider-switch or automatic-largest-file paths.
Cached URLs are bound to the same proof and virtual-item identity.

## Evidence and outcomes

Outcomes distinguish `notcached`, `missing_listing`, `numbering_refusal`,
`title_refusal`, `identity_refusal`, `file_refusal`, `provider_error`,
`writer_conflict`, `eligible`, `nzb_unusable`, `not_evaluated` and `selected`.
`eligible` may remain on a lower-ranked candidate when a different qualified
candidate wins. `not_evaluated` records that selection did not reach that
candidate, rather than manufacturing a rejection reason. The stored search
choice is still set only after actual acceptance, against that exact run.

Tests use synthetic files/listings. Their transport guard refuses SSH, Docker,
provider HTTP and socket connections and has explicit refusal controls. No test
initiates real acquisition, playback, a scan or a service operation. Source
approval is not deployment or playback acceptance.

Spore cache readers, both background preload/probe routes and advertised NFS
sizes also use this proof. Background work cannot publish an unbound URL for a
proved token or choose a different tagged file for its size. A stored torrent
id is checked before the bounded hash listing, with its hash and file rechecked.
Listing transport failures stay distinct from successful responses without a
file listing. Selection outcomes are removed when their originating run is pruned.
