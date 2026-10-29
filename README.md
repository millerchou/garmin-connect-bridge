# Garmin Connect Bridge

A read-only Garmin cycling adapter with a private SQLite store, offline FIT import,
bounded JSON queries, and anonymous segment comparisons. Authentication uses
`garminconnect==0.3.17`; FIT parsing uses `fitdecode==0.11.0`. Garmin's unofficial
web services can change. Live authentication, renewal, and synchronization require
separate private validation.

## Usage

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required for development:

```sh
uv sync --frozen
uv run --frozen garmin-bridge --help
```

`python -m garmin_bridge` is equivalent to the installed `garmin-bridge` command.
`uv.lock` pins all dependencies; `requirements.lock` is the runtime export with
hashes. Normal installation does not update these pins.

Choose a persistent private directory outside every Git checkout:

```sh
umask 077
export GARMIN_BRIDGE_DATA_DIR="$HOME/.local/share/garmin-connect-bridge"
garmin-bridge auth
garmin-bridge status
garmin-bridge sync --full
```

The global `--data-dir PATH` option overrides the environment variable. Defaults
are `$XDG_DATA_HOME/garmin-connect-bridge` (or `~/.local/share/garmin-connect-bridge`)
on Linux and `~/Library/Application Support/garmin-connect-bridge` on macOS.
Use one store per account. Directories must be owner-only (`0700`), files `0600`;
symlinks, hardlinks, permissive stores, and stores inside Git are rejected.

`auth` requires a private interactive terminal with hidden email, password, and
MFA prompts. It saves only protected session state in `auth/session.json`.
`auth --refresh` explicitly checks/refreshes the saved session without prompting
for a password. Failed authentication preserves the prior session. Offline status
reports `saved_unverified` for an unchecked saved session; this is not proof of
present validity. Sync may refresh and save rotated tokens.

### Synchronization and recovery

```sh
garmin-bridge sync --resume
garmin-bridge sync
garmin-bridge sync --since-last
garmin-bridge sync --lookback-days 30
garmin-bridge sync --start 2025-01-01 --end 2025-01-31
garmin-bridge sync --full --new-scan
```

A new store needs `sync --full` before daily sync. Full import enumerates the
API-visible cycling catalog without a lower date bound. Every job freezes a UTC
cutoff; activities starting after it are explicitly skipped. Missing time is not
invented. Two complete catalog passes must agree on in-scope IDs and reviewed
metadata, with at most three passes per invocation. Every previously observed
required ID remains required. The default budget is 1,000 pages per pass;
`--max-pages` allows up to 10,000. Budget exhaustion or non-convergence is incomplete.

Completion requires both converged enumeration and successful processing of all
required activities. Unavailable/corrupt originals remain explicit gaps. Matching
passes cannot prove Garmin exposed all historical data: `server_snapshot` is
always false. Catalog absence never deletes local activities.

Resume retains the cutoff, page checkpoints, completed activities, and gaps.
`--full --new-scan` archives the prior journal and carries unresolved IDs forward.
Network operations have bounded retries. A failed scan preserves the last usable
results and successful watermark; disconnected date ranges cannot advance it over
a gap. Status and scan receipts expose bounded gaps and a logical full-report reference.

Daily sync scans the catalog for new IDs, including delayed uploads with old dates.
Downloads cover new/changed metadata, missing/damaged artifacts, changed processing
versions, and a recent recheck window (14 days by default). Unchanged decompressed
FIT content and reviewed metadata avoid redundant parsing. A changed ZIP wrapper
alone does not create a revision. Content corrections outside the recent window
with unchanged catalog metadata require an explicit date-range or full recheck.
Date ranges are inclusive activity-local calendar dates.

`sync --since-last` is a date-range shortcut: start at the end of the local
contiguous successful watermark minus **one day**, and end today (the host's
calendar date). It overlaps prior coverage to recheck recent uploads. Disconnected
later coverage does not move its start forward. Without a watermark it returns
`no_watermark`; it never starts a full import automatically. It cannot be combined
with `--start`, `--end`, `--full`, `--resume`, or `--new-scan`. The normal two-pass
enumeration and failure rules apply; an unfinished job may require `--resume`.
Use ordinary daily sync to discover delayed uploads outside this date range.

### Offline FIT import

```sh
garmin-bridge import-fit --file "$PRIVATE_FIT_OR_ZIP" --activity-id "$ACTIVITY_ID"
garmin-bridge list --limit 20
garmin-bridge activity "$ACTIVITY_ID"
```

Inputs must be private regular `.fit` or single-FIT `.zip` files outside Git and
authentication directories, with a `0700` parent and `0600` file permissions.
Import is entirely offline and never reads authentication state. Supply the Garmin
activity ID yourself; import cannot verify its association with the file/account.
Local provenance records that limitation. Missing API metadata remains missing.

Receipts report `created`, `updated`, or `unchanged`. Changed offline content creates
an immutable revision. Identical content for an API-derived activity preserves its
API provenance; differing content or damaged API artifacts returns `import_conflict`.
Import leaves API coverage, journals, gaps, and watermarks unchanged. An offline-only
store still needs authentication and `sync --full` before daily API acquisition.

### Offline queries and segments

```sh
garmin-bridge status
garmin-bridge list --limit 20 --offset 0
garmin-bridge activity "$ACTIVITY_ID"
garmin-bridge window "$ACTIVITY_ID" --start "$UTC_START" --end "$UTC_END" --limit 100
garmin-bridge segment define --reference "$ACTIVITY_ID" --start "$UTC_START" --end "$UTC_END"
garmin-bridge segment refresh "$SEGMENT_ID"
garmin-bridge segment history "$SEGMENT_ID" --limit 20 --offset 0
```

List pages have at most 50 summaries. Activity queries return summaries, coverage,
and logical artifact references. Windows are inclusive UTC ranges of at most 600
seconds, with at most 200 ordered samples per page. Unknown timestamps remain
unassigned. Queries never authenticate, download, or normalize. Missing stores
remain absent; missing activities return `not_found`.

Segment definitions contain an opaque ID and reference time window. Only explicit
`segment refresh` reads private original positions, temporarily in process memory.
Algorithm `1.0.1` requires exact observed boundaries, a directed whole-path match
within 20 m, adequate samples, and consistent length. Gaps, poor accuracy, reverse
travel, truncated boundaries, short paths, or exceeded work limits do not establish
a match. Missing originals return `unavailable`. Elapsed time includes stops;
HR/power means use observed samples, preserve zero, and report coverage. Results
are evidence within fixed tolerances, not geographic certification.

History reads cached non-location results. Activity/reference revisions and
segment definition/algorithm versions invalidate stale results. Redefining an ID
with the same values is idempotent. Older algorithm caches require explicit refresh.

Commands return bounded, closed-schema JSON. Exit codes are `0` for success, `1`
for an incomplete scan, `2` for safe errors, and `130` for interruption. Errors do
not echo arguments, credentials, upstream bodies, or raw input.

### SQLite analysis export

```sh
garmin-bridge export analysis --file "$PRIVATE_ANALYSIS_SQLITE"
```

This offline handoff for SQL analysis contains only each activity's **current**
revision. It needs no authentication or originals. The destination must be a
private file with an owner-only parent outside Git and outside the source store.
Close other writers first; destinations with SQLite journal/WAL sidecars are refused.
An existing destination is replaced atomically only after validation. Success
returns JSON with `sha256`, byte `size`, activity count, and `verified: true`.
This is a query artifact, not a restorable store or snapshot.

| Table | Contents |
| --- | --- |
| `activities` | One row per activity, flattened summary metrics and `garmin_reported_*` estimates, metric `*_source` columns, normalizer/schema/FIT-field versions, UTC/local activity time, timezone and UTC offset, provenance, counts, coverage and quality JSON. |
| `records` | `activity_id`, zero-based `ordinal`, `timestamp`, and one numeric/text column per existing record whitelist field. Duplicates and input order are preserved. |
| `laps` | Base `start`, `end`, `duration`, `distance`; detailed `fit_*` and `garmin_reported_*` columns. Two-element FIT arrays use `_0` and `_1` in original order. |
| `events` | Detailed `fit_*` columns, including `fit_timestamp`. Historical streams without detailed events retain their timer rows in `timer_type`; unavailable numeric event codes remain NULL. |
| `messages` | The detailed session at ordinal zero, followed by auxiliary messages in source order; `message` and validated `detail_json` retain field units and `garmin_reported` labels. Historical streams without details have no message rows. |
| `meta` | One row: export format version, generated UTC time, CLI version, current activity UTC range, contiguous successful coverage dates, all covered/pending ranges, gap count, per-table counts, and `units_json`. No content hashes determine these counts. |

Column units follow the existing normalized contract and are recorded explicitly
as `meta.units_json[table][column]`; JSON detail quantities carry their own units.
Numeric measurements use SQLite REAL, counts and ordinals INTEGER, and identifiers
and ISO timestamps TEXT. Tables use SQLite STRICT typing (SQLite 3.37+).
IDs remain text to avoid integer overflow. SQL NULL means
missing or invalid; no values are interpolated. `garmin_reported_*` values remain
Garmin estimates. The existing approved rider-posture fields such as
`avg_power_position` describe riding posture, not geographic position.

Identity-enabled **current** revisions are refused with `analysis_identity`, even
if no serial/profile value was present. Reimport/recheck with identity retention
off before exporting. Older identity-enabled revisions stay private in the source
store and are not exported. No location fields, identity fields, raw references,
revision hashes, journals, or segment caches are copied. Measurements and timestamps
remain personal data; keep the analysis file private.

Export builds a fresh temporary SQLite file in the destination directory, validates
the full current source contracts, then checks `PRAGMA integrity_check`, foreign
keys, the exact table/column whitelist, JSON contracts with identity disabled, and
per-activity row counts and ordinals before atomic rename. Failed construction or
validation preserves the old destination and removes staging files. A hard kill
can leave a private `.analysis-*` staging file; it never publishes a partial target.
Readers with the previous file open can finish reading that version.

### Private SQLite snapshots

```sh
garmin-bridge snapshot export --directory "$NEW_SNAPSHOT_DIRECTORY"
garmin-bridge snapshot verify --directory "$NEW_SNAPSHOT_DIRECTORY"
garmin-bridge --data-dir "$NEW_STORE" snapshot restore --directory "$NEW_SNAPSHOT_DIRECTORY"
```

Snapshot is a private backup tool. A snapshot contains exactly `activities.sqlite`
and `manifest.json` (manifest format `2`). It preserves every structured revision,
current/archived scan journal, gap state, segment definition, and cached effort.
It excludes authentication, original FIT/ZIP files, and raw-file references.
`backup export|verify|restore --file DIRECTORY` is an alias for the same SQLite path.
Destinations must be new and outside the source directory.

Export validates every document, stream row, and relation, builds one clean logical
copy, and uses SQLite's backup API to produce an independent file. Verification
checks SHA-256, size, `PRAGMA integrity_check`, the exact schema whitelist, manifest
counts/versions, control documents, summaries, and contract spot checks. It checks
first/middle/last record and event rows per stream, with lap/message context retained.
It does not rebuild the database or require a canonical physical byte layout.
Checksums detect corruption; these checks do not authenticate an untrusted sender
or certify unused pages/trailing bytes as free of private content. Keep backups private.

Publication uses an atomic no-replace directory rename. Failed export/restore never
replaces an existing destination. A hard kill can leave an unpublished private
staging directory; retry to the intended new destination after the writer exits.
Restore validates all documents while copying into a fresh store. It needs neither
authentication nor originals. Reimport/recheck can replenish missing originals;
new segment matching and reparsing require them. Damaged immutable SQLite revisions
require recovery from a known-good snapshot; sync does not overwrite historical rows. A verified backup can contain
incomplete API history: consult status and gaps.

## Data and privacy boundaries

The primary store is `activities.sqlite`, storage/user version **1**, activity
schema **1.0**, normalizer **1.3.0**. Existing SQLite stores remain directly usable
without migration or schema changes. Historical stream shapes and immutable
revisions remain readable. The four tables are `documents`, `samples`, `segments`,
and `efforts`; JSON payloads preserve numeric types, signed zero, nulls, and precision.
`(name, kind, ordinal)` preserves sample order and duplicates. Artifact references
such as `objects/.../streams.json` are logical SQLite identifiers, not files.

Stream rows, summaries, and current-index switches commit in one transaction.
SQLite uses FULL synchronous rollback journals and POSIX process locks; contention
returns `busy`. Normal reads trust stored documents, retaining row-order and size
checks. Writes and exports validate full contracts; CLI output remains independently
validated. The catalog and each requested stream are materialized in memory. The
database limit is 16 GiB; this is not an unbounded-history performance guarantee.

The normalized field allowlist is [fit_fields.py](src/garmin_bridge/fit_fields.py).
Summary metrics, units, sources, and fallback behavior are fixed. Detailed session,
lap, event, and auxiliary fields have `fit_fields_version: "1.0"`. Absent fields
stay absent; FIT invalid values become null; arrays retain order and null slots.
There is no resampling, interpolation, clamping, or invented sensor data. Garmin
estimates and historical settings remain labelled `garmin_reported`, not independent
measurements. Cadence consistency flags do not correct or discard observations.

Local time uses only unambiguous historical file evidence when API time is absent.
No IANA timezone is inferred; `utc_offset` alone is not a timezone. Compressed FIT
timestamps require an approved standard anchor. Unknown/developer fields cannot
supply it. Unreviewed fields and arbitrary nested/free-text payloads are excluded.

Identity retention is default-off. `sync` and `import-fit` accept
`--include-fit-identity` for reviewed serial/profile fields only. Positions remain
excluded. Sport labels use a closed vocabulary; opt-in friendly names are bounded.
Changing this option creates/reuses an immutable revision and requeues affected
work on resume. Turning it off does not erase older revisions; snapshots explicitly
declare identity retained anywhere in their history. Such data must remain private.

Original archives are read in memory without extracting supplied paths. Ambiguous
archives, unsafe paths, bad FIT framing/CRC, multi-session/non-cycling inputs,
inputs above 64 MiB, and excessive expansion are rejected. Parser limits are
500,000 records, 10,000 laps, 100,000 events, and 100,000 auxiliary messages.

New `raw/` objects store decompressed FIT bytes addressed by their SHA-256, so
different ZIP wrappers and activities with identical FIT content share one object.
Existing raw references still verify against their stored byte hash and accept
either FIT or ZIP content. There is no migration or deletion of old objects;
unchanged existing references and all immutable revisions remain readable.

This public repository contains only code, documentation, and fixtures generated
from invented constants. Never add real activity/health data, original exports,
credentials, account/browser state, or live request recordings. Partial redaction
is not a synthetic fixture. Keep authentication and location-bearing originals
outside the checkout; never archive the collection root for sharing. Physiological
measurements and timestamps remain personal data even without coordinates.

SQL consumers must use the reviewed structured SQLite data and keep its contents
private. No account writes, health synchronization, dashboard, hosted service,
scheduling, transfer connector, or deployment automation is included.

## Acceptance criteria

Default tests are offline and generate FIT/SQLite inputs in code. They must cover:

- Closed field contracts, explicit units/provenance, nulls/zero, and privacy filtering,
  including unknown, nested, developer, location, and identity fields.
- MFA/session failures and safe diagnostics without leaking input or credentials.
- Idempotent sync/import, ZIP-wrapper changes, immutable revisions, source reversion,
  and protection of API provenance from offline replacement.
- Two-pass catalog consistency, pagination, delayed uploads, partial failures,
  interruption/resume, gaps, and failure-does-not-advance-watermark.
- Atomic SQLite publication/recovery, locking, permissions, missing data, bounded
  offline queries, and continued use of storage version 1 with normalizer 1.3.0.
- Snapshot checksum/integrity/schema checks, contract spot checks, full validation
  on export/restore, revision/progress preservation, and incremental sync after restore.
- Current-only typed analysis export, identity/location exclusion, atomic overwrite
  and failure preservation, SQL units/provenance, and legacy stream support.
- `--since-last` overlap and no-watermark errors, successful watermark preservation
  on failure, decompressed raw deduplication, and existing ZIP reference readability.
- Anonymous segment matching, insufficient/unavailable evidence, cached-result
  invalidation, and exclusion of locations from persisted/output results.

```sh
uv run --frozen pytest
uv run --frozen ruff check
uv run --frozen ruff format --check
git diff --check
```

Report these synthetic checks separately from any explicitly authorized live
account, original-file, cross-platform, or external SQL/transfer validation.
