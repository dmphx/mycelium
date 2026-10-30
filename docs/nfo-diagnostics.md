# Unreadable show NFO diagnostics

Title repair skips an unreadable `tvshow.nfo` without retrying its contents or
changing its permissions. Each permission failure produces a structured log
entry with a path SHA256, exception class, errno, and lstat ownership/mode when
available. lstat describes the directory entry, including whether it is a
symlink; it does not assert the owner caused the failure.

The complete path allowlist is retained only in a private JSON report in
`.nfo-diagnostics/` next to the configured `DB_PATH`. The directory is created
0700, reports 0600. The repair result returns the report basename and status,
never its private paths. Existing diagnostics directories must already be
private and owned by the application user. A symlink, inaccessible directory,
or write error leaves report status `unavailable` with a sanitized type/errno.
It does not stop other title repairs or trigger permission changes.

Reports accumulate for operator review and are not a permission repair queue.
Review or archive them locally as private library metadata; do not attach them
to public issues. No automatic ownership change or report deletion is performed.
