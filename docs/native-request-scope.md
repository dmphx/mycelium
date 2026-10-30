# Native request scope snapshots

New owned native requests record an immutable scope with the existing request row
in one SQLite transaction. History exposes native request_scope and its state.
The scope hash and original body hash are local integrity evidence, not a signed
integration grant. Native rows never become Media signed receipts by wrapping.

Selected seasons are a sorted exact set, including Specials only when explicitly
selected. All seasons means regular seasons, including later regular seasons.
Future starts_on is fixed once by the receiver UTC date, not approval or retry
time and not a caller date. Approval validates stored owner and typed media
identity, then passes the stored selection/date into dispatch. A pending review
can be won only once. Missing or corrupt old TV scope stays unavailable; no
backfill, reconstruction from null or automatic approval is performed.

The existing legacy shared monitor/executor still requires all-writer/reader,
destination and exact-scope qualification before activation. Persisting scope and
capturing dispatch arguments does not qualify provider execution, episode-pair
execution, signed admission, cross-account grants or final-source release. Only
fresh synthetic schemas/databases were used here; no existing database migration
or production/provider/identity activation is performed.
