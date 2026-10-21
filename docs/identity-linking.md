# OIDC sign-in and identity linking

## Policy settings (environment only)

| Variable | Values | Default | Meaning |
|---|---|---|---|
| `OIDC_ADMISSION_MODE` | `linked_only`, `provision` | `linked_only` | `linked_only` admits only an identity that was linked beforehand. `provision` creates an ordinary user (role user, no auto approve, quota 0) for an unknown identity. |
| `OIDC_ROLE_POLICY` | `preserve`, `groups` | `preserve` | `preserve` never rewrites a stored role at sign-in. `groups` sets the role exactly from membership of `OIDC_ADMIN_GROUP`, so removal from the group demotes on the next sign-in, and a missing groups claim counts as no membership. |

An unrecognised value stops startup. Identity is the exact pair of issuer string and `sub`. Username and email are display only and never merge accounts. The ID token must pass Authlib validation with the configured issuer and client as audience; plain userinfo responses are not used for identity or groups. The issuer string is compared byte for byte, so a trailing slash is a different issuer.

Unlinked and disabled identities get the same generic message, and no row or timestamp is written before admission succeeds. The session role is the stored native role.

Upgrade note: the previous behaviour (provision unknown identities, rewrite role from groups on every sign-in) is `OIDC_ADMISSION_MODE=provision` with `OIDC_ROLE_POLICY=groups`.

## Linking existing users

A manifest is only as good as the proof behind it. Every row must be backed by verified cross-app evidence that the person who owns the existing native account and its media-server identity is the same person as the OIDC subject (for example an operator-checked export from the identity provider, matched to each application's own account record). A username or email match is never proof. Assuming that the subject is the same value in two different client registrations is never proof either; subjects can differ per client and must be read from the actual provider output for this client. The tool checks the database against the manifest, it cannot check the proof.

`scripts/link_oidc_identity.py` is an offline tool. It opens the SQLite file directly, does not import the application, and never creates users or moves rows. Take a database backup first.

1. Write a manifest (see the docstring in the script): per user the native id, the Jellyfin server and user GUIDs, the exact OIDC issuer and subject, the expected role, enabled, quota and auto approve values, and the exact set of existing links.
2. Check (read only, default): `link_oidc_identity.py --db <file> --manifest <file>` prints a plan fingerprint.
3. Review, then apply: `--apply --fingerprint <value>`. The whole batch runs in one transaction. Any drift, conflict or fingerprint mismatch rolls everything back.

Re-running an applied manifest is a no-op. To undo a link, make a reviewed single-row delete from `external_identities` and only while OIDC sign-in is disabled, so that no active access depends on the row being removed. Do not restore an old database over newer activity.

Revocation: this feature does not promise immediate revocation. Disabling a native user takes effect on that user's next request here. Removing access at the identity provider only affects later sign-ins and whatever the provider itself enforces. Existing sessions here stay valid until they expire or the user is disabled.

Manifest input is strict: duplicate JSON keys, NaN or Infinity, wrong types (booleans where integers are expected, numbers where strings are expected), roles other than `user` or `admin`, and malformed GUIDs are refused before the database is read. The check reads the database in one read transaction, and the database path may contain spaces or URI characters.

The check opens the database with `mode=ro` and never retries with write access. If a WAL snapshot cannot be opened read only, use a consistent SQLite backup for review; do not bypass the check with an immutable view of a live database or omit its WAL.
