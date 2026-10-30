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

`migrate`, `sanitize-fit`, `verify-fit`, and `--legacy-store` were removed in this
version. They return explicit errors and are omitted from help. Legacy JSON store
access/migration and derived FIT archive creation/verification have no replacement.
Existing SQLite stores work directly without `--legacy-store`. For other handoffs,
use `export analysis` for a non-location SQLite analysis artifact, or
`snapshot export|verify|restore` for private SQLite backups.

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
garmin-bridge sync --since-last --dry-run
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

After upgrading, `sync --since-last` does **not** reparse old rides outside its
recent date range. Run sync once with an explicit date range covering existing
activities (for example, `sync --start 2025-01-01 --end 2025-12-31`) to reparse them
with the new normalizer. Choose dates covering your own history; originals are
required. Subsequent `sync --since-last` runs can resume the usual recent updates.

`sync --since-last --dry-run` prints only `{"start":"YYYY-MM-DD","end":"YYYY-MM-DD"}`
and exits successfully. It uses the same watermark and host-date calculation as a
real run, without constructing a network client, logging in, taking the bridge
lock, or changing the store, state, or watermark. A missing watermark still returns
`no_watermark`; `--dry-run` without `--since-last` is an error. The preview reads
committed SQLite state and refuses journal/WAL sidecars or WAL-mode databases with
`busy` instead of performing recovery or creating sidecars. It also returns `busy`
if the database file or header changes during the read. Coverage may change before
a later real run. A genuine `privacy_failed` error can write the private diagnostic
report described below; a concurrent change reported as `busy` does not write it.

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
Windows expose only documented measurement columns. Undocumented raw fields are
available in the private analysis export, not in model-facing query output.

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

`privacy_failed` errors additionally include `diagnostic` with `rule`, `file`,
`table`, and `field`, plus `report: "reports/privacy-failure.json"`. The latest
failure is saved under the effective data directory, including for snapshot
verification; it is never added to a snapshot or analysis artifact. Reports
contain only those labels and a report version, never offending values, activity
IDs, row counts, hashes, absolute paths, or exception text. `file` identifies
`activities.sqlite`, `manifest.json`, the logical `analysis.sqlite` artifact,
`reports/fit-blocked-fields.json`, or the logical `stdout.json` output. Nested
fields use `[]`/`*` instead of row indices or mapping IDs; unreviewed free-text
keys become `[unreviewed-key]`. Rule IDs are explicit at the validation sites.
Arbitrary numeric mapping keys are unreviewed too; raw message/field numbers are
reported only after validating their FIT field identifiers.

Report replacement is atomic with a `0600` temporary file and `0700` report
directory, with file and directory flushes. Concurrent failures leave the latest
completed report; each error JSON retains its own diagnostic. If the effective
data directory is unsafe, a snapshot, or unwritable, `report` is null and the
original diagnostic still reaches stderr; there is no alternate write location.
A failed restore can create a report-only data directory; use a new destination
for a subsequent restore. Other committed store contents remain unchanged.
These diagnostic members extend the previous `privacy_failed` error shape.
Snapshot sidecars and detected concurrent changes now return `busy` instead of
`privacy_failed`; both remain safe errors with exit code `2`. Consumers matching
error names or enforcing exact error keys must account for these changes.
Nested snapshot destinations now return `unsafe_store` (also exit `2`), rejecting
the path before diagnostic writes can modify the input store.

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
| `records` | `activity_id`, zero-based `ordinal`, `timestamp`, typed measurement columns including `fractional_cadence`, and `unknown_json` with raw `unknown_N` keys. Duplicates and input order are preserved. |
| `laps` | Base `start`, `end`, `duration`, `distance`; detailed `fit_*` and `garmin_reported_*` columns, including `fit_event` and `fit_event_type`; raw fields in `unknown_json`. Two-element FIT arrays use `_0` and `_1` in original order. |
| `events` | Detailed `fit_*` columns, including `fit_timestamp`. Historical streams without detailed events retain their timer rows in `timer_type`; unavailable numeric event codes remain NULL. |
| `messages` | The detailed session at ordinal zero, followed by auxiliary messages in source order, including `segment_lap` and `timestamp_correlation`; `message` and validated `detail_json` retain units, `garmin_reported` labels and separate raw `unknown` objects. Historical streams without details have no message rows. |
| `raw_messages` | Undocumented messages in source order: `activity_id TEXT`, `ordinal INTEGER`, `mesg_num INTEGER`, `fields_json TEXT` containing decimal field-number keys and raw integers/arrays/nulls. No timestamp alignment or semantic names are inferred. |
| `meta` | One row: export format version, generated UTC time, CLI version, current activity UTC range, contiguous successful coverage dates, all covered/pending ranges, gap count, per-table counts, and `units_json`. No content hashes determine these counts. |

Column units follow the existing normalized contract and are recorded explicitly
as `meta.units_json[table][column]`; JSON detail quantities carry their own units.
Numeric measurements use SQLite REAL, counts and ordinals INTEGER, and identifiers
and ISO timestamps TEXT. Tables use SQLite STRICT typing (SQLite 3.37+).
IDs remain text to avoid integer overflow. SQL NULL means
missing or invalid; no values are interpolated. `garmin_reported_*` values remain
Garmin estimates. The existing approved rider-posture fields such as
`avg_power_position` describe riding posture, not geographic position.

Analysis format **2** adds the raw-message table and JSON columns. All child tables
have primary key `(activity_id, ordinal)` and a foreign key to `activities`.
`records.fractional_cadence` uses REAL; `laps.fit_event` and `laps.fit_event_type`
use INTEGER for FIT enum codes. `unknown_json`,
`detail_json` and `fields_json` are validated JSON stored as TEXT; these map directly
to PostgreSQL JSONB alongside the typed keys. Raw integers retain their JSON integer
representation, including values outside SQLite's signed INTEGER range. Consumers
must avoid conversion to floating point if exact large integers matter. Missing
unknown fields are absent keys; invalid FIT values are JSON null. Raw quantities
have no inferred units, scale or offset and are excluded from measurement coverage.

Identity-enabled **current** revisions are refused with `analysis_identity`, even
if no serial/profile value was present. Reimport/recheck with identity retention
off before exporting. Older identity-enabled revisions stay private in the source
store and are not exported. No location fields, identity fields, raw references,
revision hashes, journals, or segment caches are copied. Measurements and timestamps
remain personal data; keep the analysis file private.

Export builds a fresh temporary SQLite file in the destination directory, validates
the full current source contracts, then checks `PRAGMA integrity_check`, foreign
keys, the exact table/column whitelist, JSON contracts with identity disabled, and
per-activity row counts and ordinals before atomic rename. An independent denylist
also checks every column name and every nested `detail_json`, `unknown_json`, and
`fields_json` key for location and
identity names, including prefixed/suffixed tokens, camelCase and separator
variants; approved power/cadence posture and sensor-position fields remain allowed.
The file and destination directory are flushed before success; a directory-flush failure restores the prior
destination. Failed construction or validation preserves the old destination and
removes staging files. A hard kill can leave private `.analysis-*` staging or
recovery links; remove those only after all exporters have exited. It never
publishes a partial target. Readers with the previous file open can finish reading
that version.

Snapshot and analysis exports pin all source reads to one SQLite read transaction
under the bridge lock. This also prevents mixed catalog/journal/artifact versions
when an independent SQLite connection commits in WAL mode. Rollback-mode writers
may be blocked during source acquisition, and WAL checkpoints may be unable to
truncate the WAL while that transaction is open. The source transaction closes
before independent destination validation and publication, including on errors.
An absent source database remains an empty snapshot if a first writer creates it
during acquisition. Filesystem replacement or writes that bypass SQLite locking
remain unsupported. The transaction fixes a reproducible mixed-commit race; it
does not establish the cause of an unobserved failure in a private store.

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
counts/versions, control documents, summaries, revision links/counts, and contracts.
Streams with raw fields (FIT field version 1.1) are validated in full, because
the identity checks need all rows to distinguish constants from varying counters.
Older streams use first/middle/last record and event spot checks, with lap/message
context retained.
It does not rebuild the database or require a canonical physical byte layout.
Snapshot verification refuses SQLite journal/WAL/SHM sidecars with `busy`, including
sidecars appearing during verification or restore; it never recovers them. Changes
to the database or manifest detected during a read also return `busy`, even if
checksum or contract validation failed. Such superseded failures do not replace
the privacy report. Snapshot files must remain immutable while being read; these
checks cannot protect against arbitrary same-user filesystem replacement or
writes that evade the before/after checks. Unexpected temporary
or recovery files fail the exact snapshot-member rule. Source-store staging files
are not scanned or copied. Directory iteration order does not select members or
samples.
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
schema **1.0**, normalizer **1.4.0**. Existing SQLite stores remain directly usable
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

The documented field contract is [fit_fields.py](src/garmin_bridge/fit_fields.py),
checked against the public [Garmin FIT Profile 21.218.0](https://github.com/garmin/fit-python-sdk/blob/main/garmin_fit_sdk/profile.py).
Summary metrics, units, sources, and fallback behavior are fixed. Detailed session,
lap, event, and auxiliary fields have `fit_fields_version: "1.1"`. Absent fields
stay absent; FIT invalid values become null; arrays retain order and null slots.
There is no resampling, interpolation, clamping, or invented sensor data. Garmin
estimates and historical settings remain labelled `garmin_reported`, not independent
measurements. Cadence consistency flags do not correct or discard observations.

Local time uses only unambiguous historical file evidence when API time is absent.
No IANA timezone is inferred; `utc_offset` alone is not a timezone. Compressed FIT
timestamps require an approved standard anchor. Unknown/developer fields cannot
supply it. Standard developer-field payloads and documented fields outside the
explicit contract remain excluded; undocumented numeric data follows the separate
interception rules below.

Version 1.1 adds all documented non-coordinate `segment_lap` fields, including
names, UUIDs, time, distance, power, HR, cadence, elevation, work, balance and status;
split-referenced `time_in_zone` rows with unique matching indices; generic event
codes; session `first_lap_index`, `trigger`, `event`, `event_type`; lap event headers;
split `message_index`; `device_info.ant_network`; remaining documented device
settings; and `timestamp_correlation`. Unknown event codes retain headers; opaque
event payloads still require the existing event-specific permission. Sport/profile
names and segment names/UUIDs retain printable free text up to 254 characters.
`record.fractional_cadence` is also stored separately in rpm; the existing combined
`cadence` calculation is unchanged. Garmin estimates retain `garmin_reported` labels.
`timestamp_correlation.system_timestamp` preserves device-clock seconds, including
relative values; it does not assert an independently known UTC time.

Undocumented physical fields retain raw integer values or arrays, with invalid
slots represented by null. Record keys are `unknown_N`; session, lap, split,
split_summary, sport, training_settings, device_settings, zones_target, device_info,
segment_lap and timestamp_correlation use a separate `unknown` object. Documented
fields absent from fitdecode's older profile still use the explicit contract when
available; they are never duplicated as raw fields. Entire undocumented messages
use `raw_messages` rows with `mesg_num` and decimal field-number keys. Their raw
timestamp fields, if present, stay integers and never seed the compressed clock.
Rows with no fields remaining after interception are omitted from `raw_messages`;
surviving rows retain their relative order and receive contiguous ordinals.

The primary store reuses `samples(name TEXT, kind TEXT, ordinal INTEGER, payload
BLOB)` with primary key `(name, kind, ordinal)`; payload is canonical JSON. The new
`kind='raw_messages'` partition separates high-frequency rows from ordinary detail
messages. Analysis exports give it a dedicated table. This avoids sparse dynamic
columns and preserves duplicate/missing timestamps without an inferred record
join. Documented messages remain in the existing `messages` partition. No physical
table migration is necessary. New detail shapes are identified by FIT field version
1.1; field version 1.0 and pre-detail streams remain readable. The snapshot manifest
stays at format 2, with an additional `fit_counts.raw_messages` count only when 1.1
streams exist. Analysis format changes to 2 because its SQL schema changes.

Re-sync API activities or re-import offline activities to obtain normalizer 1.4.0
revisions. Original FIT inputs are required. Old immutable revisions remain intact;
no watermark, authentication or segment-matching algorithm change is involved.
As before, offline import of identical API-derived content preserves its API
revision; use sync to upgrade that revision. Older software cannot read 1.1 streams.
Prior revisions remain immutable; reparsing does not erase historical data.
A privacy-processing discriminator in the input digest prevents reuse of earlier
1.4.0 artifacts while retaining normalizer version 1.4.0. Rechecks and resumed jobs
apply the current rules. Stored text-like raw arrays and suspected numeric
identifiers fail the strengthened export/snapshot validation rather than being
silently copied; a snapshot containing unsafe historical revisions is also refused.

### Undocumented-field interception and report

[fit_unknown.py](src/garmin_bridge/fit_unknown.py) applies conservative rules before
raw values enter structured storage:

- The entire `user_profile` message, including unknown fields, is excluded by
  default. Message 79 is always excluded. Identity opt-in still allows only the
  existing reviewed serial/profile subset; it never allows message 79 or unknown
  profile fields.
- Every undocumented `sint32` is blocked as `possible_semicircle`. All valid signed
  32-bit magnitudes can encode semicircle coordinates, even zero/small values.
  Blocking the entire type also catches fields correlated with positions, including
  short or constant streams; no position samples or correlation statistics are saved.
  This deliberately withholds some harmless counters/measurements.
- Across all integer types, raw fields copying observed coordinate or identity
  values are blocked (`position_match` / `identity_match`), including unsigned
  representations of semicircle coordinates. Identity matching collects only
  named identifiers (`serial_number`, `ant_device_number`, `device_number`,
  `device_id`, `local_id`, `global_id`) and undocumented integers from the excluded
  profile/messages 3 and 79, and only
  when their absolute value is at least **2^16 (65,536)**. This excludes the entire
  16-bit unsigned range and common small settings/enums from file-wide matching,
  even for short identifiers; reviewed identity fields themselves remain excluded
  by default. Known profile settings, including wake/sleep times, and transmission
  categories do not supply identifier matches. Coordinate
  matching has no magnitude threshold. Record fields with at least three
  paired coordinate observations and squared linear correlation of at least
  0.9999 are blocked as `position_correlated`. Each array slot is checked. A match
  withholds the entire message/field family, including earlier and unpaired rows.
  These conservative checks can also withhold coincidentally matching measurements.
  Only transient sets and integer statistics are used; none enter stored artifacts.
- An undocumented field is blocked as `suspected_identity` when any integer's
  absolute magnitude is at least **2^24 (16,777,216)**, is not timestamp-like, and
  its field/array slot has
  **at most three distinct non-null integers** across that message type's rows in
  the file. Single-row fields qualify; nulls and missing fields provide no evidence
  of variation. Each array slot is checked independently, and a match blocks the
  entire message/field family. Four or more distinct values exempt that slot from
  this heuristic, preserving varying counters. No message or field numbers are
  special-cased. Distinct sets stop growing after four values.
  Timestamp-like requires at least **three distinct values**, all exactly matching
  reviewed UTC timestamps retained in the same activity (including validated
  compressed timestamps), within the FIT absolute `date_time` range
  `[0x10000000, 0xFFFFFFFF)`. Proximity to an activity time is insufficient: a
  constant ID can have the same magnitude as a timestamp. Constant/sparse unknown
  times and unconfirmed local-clock offsets are withheld as ambiguous. Documented
  timestamps are unaffected. Unknown/developer fields and relative clocks cannot
  supply evidence; without reviewed time evidence there is no timestamp exemption.
  Exact coordinate/identity matches and position correlations still take priority.
  The same suspicion check runs across all rows of each activity during structured
  validation and independently on the constructed analysis export. It never pools
  variation or timestamp evidence across activities.
- Strings and byte blobs are blocked as `possible_identity`, as are arrays of any
  integer type containing at least four printable ASCII characters with zero/null
  padding, or at least four printable UTF-8 characters. All-null arrays remain null.
  This avoids retaining possible device names or serial strings. Other non-integer
  types, including floats, are blocked as `non_integer_raw`; no scaling is guessed.
- Unknown profile fields remain excluded and audited even with identity opt-in.
  Duplicate physical field numbers are rejected instead of silently overwriting
  values or bypassing an interception rule. Unreviewed compressed headers cannot
  advance the clock used for documented timestamps.

The local private report is
`$GARMIN_BRIDGE_DATA_DIR/reports/fit-blocked-fields.json` (under the effective
`--data-dir` when supplied). Inspect it offline with:

```sh
garmin-bridge fit-blocked
```

Each report row contains **only** `field` (`mesg_num.field_num`), a fixed `reason`,
and `row_count`. It contains no blocked values, timestamps, activity IDs, source
hashes, names or paths. Counts accumulate across successfully normalized attempts,
once per physical message row, not per array element. Cache hits do not increment
them; retries after publication failure or skipped publication can increment them.
They are audit counts, not current-revision totals. Writes are atomic and private
(`0600` file, `0700` directory). A malformed report fails closed. Reports are local
sidecars, excluded from analysis exports and snapshots; restore does not recreate
them. The command supports review only; it does not enable any blocked fields.

These heuristics cannot identify every numeric encoding of identity or geography.
Constant large counters can be withheld; changing identifiers with four or more
distinct values, short identifiers below the matching threshold, and varying
identifiers exactly copying reviewed timestamps can escape these checks.
The timestamp exemption is a
privacy-filtering heuristic, not a claim about an unknown field's meaning or units.
Correlation checks require co-observed record positions; no temporal joins are
inferred for undocumented messages. Encodings without that evidence remain opaque.
Explicitly retained names/UUIDs may themselves contain personal or place text.
The export denylist is a key-name backstop, not a semantic anonymizer; keep all
outputs private. No live files were used to tune the heuristics.

Size scales with field and row counts. An entirely synthetic 3,600-record example,
adding one raw integer per record plus one four-integer message 534 per record,
and populating fractional cadence, added **696,320 bytes** to the primary SQLite
store and **376,832 bytes** to the analysis export (about 0.66/0.36 MiB). This compares
two current-version stores and excludes originals. A four-field raw-message JSON
row was 51–63 bytes before SQLite keys/page overhead (values 0 through 3,602).
Arrays and longer text increase
this; these are synthetic estimates, not measurements of personal activity data.

Identity retention is default-off. `sync` and `import-fit` accept
`--include-fit-identity` for reviewed serial/profile fields only. Positions remain
excluded. Sport/profile labels now allow bounded free text; opt-in friendly names
remain bounded.
Changing this option creates/reuses an immutable revision and requeues affected
work on resume. Turning it off does not erase older revisions; snapshots explicitly
declare identity retained anywhere in their history. Such data must remain private.

Original archives are read in memory without extracting supplied paths. Ambiguous
archives, unsafe paths, bad FIT framing/CRC, multi-session/non-cycling inputs,
inputs above 64 MiB, and excessive expansion are rejected. Parser limits are
500,000 records, 10,000 laps, 100,000 events, 100,000 auxiliary messages, and 500,000
undocumented message rows. SQLite permits up to 512 KiB per stream row to accommodate
physical FIT arrays after JSON expansion, enforced before writes as well as on reads.
Privacy checks retain unique observed coordinate/identity/reviewed timestamp values
in memory, bounded distinct sets per raw field/array slot, and fixed-size correlation
statistics per record field/array slot, not a second copy of the raw rows.
These limits do not guarantee bounded
memory suitable for every device/file combination.

After raw deduplication, new originals are stored as the decompressed FIT file at
`raw/<sha256>.bin`, named by the SHA-256 of those FIT bytes. Garmin's ZIP wrapper
is not retained for new objects; different wrappers and activities with identical
FIT content share one object. Existing raw references still verify against their
stored byte hash and accept either FIT or ZIP content, so existing ZIP references
remain readable. There is no migration or deletion of old objects;
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
  offline queries, and continued use of storage version 1 with normalizer 1.4.0.
- Snapshot checksum/integrity/schema checks, contract spot checks, full validation
  on export/restore, revision/progress preservation, and incremental sync after restore.
- Current-only typed analysis export, identity/location exclusion, atomic overwrite
  and failure preservation, SQL units/provenance, and legacy stream support.
- Segment-lap location stripping, split zone references, generic event headers,
  raw undocumented fields/messages, conservative coordinate/identity interception,
  value-free blocked-field reports, and version 1.0/1.1 detail compatibility.
- `--since-last` overlap and no-watermark errors, a dry-run without clients, bridge
  locks or writes, successful watermark preservation on failure, decompressed raw
  deduplication, and existing ZIP reference readability.
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
