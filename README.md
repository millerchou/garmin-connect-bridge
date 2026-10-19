# Garmin Connect Bridge

A small, read-only Garmin Connect adapter for deterministic data pipelines.

**Status: installable CLI MVP with offline FIT import, derived FIT archives,
synthetic tests and portable backups.**
The bridge commands, `import-fit`, `sanitize-fit`, `verify-fit` and offline
`backup` commands below are implemented.
The first verified runtime is Linux x86_64, Python 3.12.14.
The code also targets macOS and Python 3.12+; macOS has not been exercised on a
real runner. Live Garmin authentication, renewal and synchronization remain
unverified. There is no CI workflow, release or deployed service.

## Design

Use a pinned, inspected release of
[python-garminconnect](https://github.com/cyberjunky/python-garminconnect) as a
dependency rather than maintaining a fork of its authentication engine.
That library accesses unofficial Garmin web services. Authentication and
endpoints can change; successful website sign-in does not prove programmatic
authentication or unattended synchronization works.

This is a **new, independent data store**, populated directly from the current
Garmin international account. There is no import or migration from old local
projects, CSV files, Strava IDs, metrics, Coach records or directory layouts.
Activity identity is `(garmin, activity ID)`. Use one private store per account.
The offline path accepts a single original exported from Garmin's normal website,
with an explicitly supplied activity ID; it does not import an old local system.

Acquisition (`upstream.py`), FIT projection (`normalize.py`), private storage
(`storage.py`), resumable scans (`jobs.py`, `sync.py`) and offline queries
(`cli.py`) are separate. The offline file adapter (`import_fit.py`) shares the
same validation/publication function as API downloads in `sync.py`, and the same
FIT parser and Store. Portable snapshots (`backup.py`, `persistence.py`) reuse
the reviewed data contract and durable journal. The CLI writes local files and
JSON. Polaris runs it in a private cloud environment and uses its existing Drive
tools to transfer the reviewed package. Bridge has no Google OAuth, Drive SDK,
Space integration, hosted service or deployment configuration.

The API/import dependencies remain `garminconnect==0.3.17` and `fitdecode==0.11.0`; the former is the
PyPI distribution of python-garminconnect. On 2026-10-14, their non-yanked PyPI
metadata, wheel hashes, installed versions and relevant source/API signatures
were checked. Authentication uses the upstream `login`, `resume_login` and
`client.dumps` interfaces; no authentication engine is copied into this repo.
Catalog GET requests use the upstream activity-search endpoint and original
downloads use `ActivityDownloadFormat.ORIGINAL`.

Derived FIT verification additionally pins **`garmin-fit-sdk==21.218.0`** (official
Garmin Python SDK, Profile **21.218.0Release**), checked against non-yanked PyPI
metadata and the wheel on 2026-10-18. Its license is
**`LicenseRef-FIT-Protocol-License-Agreement`**, a custom FIT Protocol License,
not MIT. See the dependency's `licenses/LICENSE.txt` and
[official SDK repository](https://github.com/garmin/fit-python-sdk). It is installed
as a dependency; its code, whole Profile and upstream test data are not vendored.
The existing parser and authentication dependency versions were not upgraded.

The SDK's general encoder drops `None` fields and can reverse-scale/round values;
its decoder does not support compressed timestamp messages. Therefore it is used
as the **second independent output reader**, alongside fitdecode. A small bounded
physical-field transcriber preserves approved source bytes and expands supported
compressed timestamp headers before either reader sees the output. Using only
fitdecode would not provide independent re-reading; re-encoding normalized JSON
or SDK dictionaries would lose physical presence/precision information.

Verified wheel SHA-256 values:

```text
garminconnect 0.3.17: 624d8c2468d865d286c9fb7ae8133633bb1d52a2b019c8beb5ff9ae10dd43264
fitdecode 0.11.0:    a1bdb9b4d1e9ebac0001fc58c22fe07a3f9967ca4e13258c6ef99cdb8d697a78
garmin-fit-sdk 21.218.0: 7b912cde3b0b8f4acd7b543c202d15ac94f6b37b58eab5bad2186ca787b9ab37
```

This verification establishes package identity and the inspected adapter
contract, not successful authentication against Garmin or a security audit of
all upstream code. `uv.lock` pins development and transitive dependencies across
platforms. `requirements.lock` is its hash-locked runtime export.

## Install

With Python 3.12+ and [uv](https://docs.astral.sh/uv/):

```sh
uv sync --frozen
uv run --frozen garmin-bridge --help
uv run --frozen python -m pytest
```

The installed console entry point is `garmin-bridge`; `python -m garmin_bridge`
is equivalent. For Polaris, **uv is optional**. From this repository's root,
Python 3.12, pip and venv suffice. Install runtime dependencies from
`requirements.lock` and extract the build backend's complete pinned dependency
set from `uv.lock`; pip build isolation is disabled so it cannot resolve newer
build dependencies. These commands use public package registries, not Garmin:

```sh
python3.12 -m venv .venv
.venv/bin/python - <<'PY'
from pathlib import Path
import tomllib

names = {"hatchling", "packaging", "pathspec", "pluggy", "tomlkit", "trove-classifiers"}
packages = tomllib.loads(Path("uv.lock").read_text())["package"]
lines = []
found = set()
for package in packages:
    if package["name"] in names:
        found.add(package["name"])
        artifacts = [package.get("sdist"), *package.get("wheels", [])]
        hashes = sorted({item["hash"] for item in artifacts if item})
        assert hashes
        lines.append(package["name"] + "==" + package["version"] + " "
                     + " ".join("--hash=" + value for value in hashes))
assert found == names
Path(".venv/bridge-build.lock").write_text("\n".join(sorted(lines)) + "\n")
PY
.venv/bin/python -m pip install --no-cache-dir --only-binary=:all: --require-hashes \
  -r requirements.lock -r .venv/bridge-build.lock
.venv/bin/python -m pip install --no-cache-dir --no-index --no-deps --no-build-isolation .
.venv/bin/python -m pip check
.venv/bin/garmin-bridge --help
export PATH="$PWD/.venv/bin:$PATH"
```

`uv sync --frozen` is the development setup, including pytest, Ruff and the
pinned build backend. Build with `uv build --no-build-isolation` from that
environment. Dependency updates are intentional changes to the pins and lock
files, not something a normal sync command performs.

Alternatively, after installing uv separately, this runtime-only recipe is
available; do not assume uv already exists in Polaris:

```sh
uv sync --frozen --no-dev --python 3.12
export PATH="$PWD/.venv/bin:$PATH"
garmin-bridge --help
```

Keep the checkout and virtual environment available; the uv project installation
is editable. This is an installation recipe, not a deployment or scheduled job.

## MVP

| Command | Responsibility |
| --- | --- |
| `auth` | Explicit private sign-in, MFA continuation, and protected token persistence. |
| `status` | Authentication state, synchronization watermarks, freshness, and actionable failures; never credentials. |
| `sync` | Full historical import, resume, daily discovery/rechecks, and explicit historical rescans. |
| `import-fit` | Import one private original FIT or single-FIT ZIP entirely offline, with an explicit Garmin activity ID. |
| `sanitize-fit` | Create a new private derived training FIT archive directly from physical FIT fields, without changing the activity store. |
| `verify-fit` | Recheck all physical fields and both parsers; choose full comparison with `--source` or archive integrity only with `--archive-only`. |
| `list` | Read the local activity index without network access. |
| `activity` | Read an existing bounded activity summary and references to approved local files. |
| `backup export / verify / restore` | Create a non-location snapshot, validate it offline, or recover it into an empty store. |

All command results are JSON. Exit codes: `0` for successful imports/queries or
a complete scan, `1` for an incomplete scan, `2` for safe command/auth/storage errors, and
`130` for interruption. Invalid command arguments are not echoed back.

## Private store and authentication

The default store is outside the checkout:

- Linux: `$XDG_DATA_HOME/garmin-connect-bridge`, or
  `~/.local/share/garmin-connect-bridge` when XDG_DATA_HOME is unset.
- macOS: `~/Library/Application Support/garmin-connect-bridge`.

Override it with `GARMIN_BRIDGE_DATA_DIR` or the global `--data-dir PATH` option
**before** the command. Existing permissive directories, symlinks inside the
store, hardlinked files, and stores inside Git checkouts are rejected. System
path aliases such as macOS `/tmp` are resolved. The store is intended for a
single OS user on a local filesystem with POSIX locking and atomic rename.

For Polaris, choose a persistent private location outside every checkout. This
portable example uses the current user's home, without a machine-specific path:

```sh
umask 077
export GARMIN_BRIDGE_DATA_DIR="$HOME/.local/share/garmin-connect-bridge"
garmin-bridge auth
garmin-bridge auth --refresh
garmin-bridge status
```

The data root is configurable; the authentication subdirectory is always
`$GARMIN_BRIDGE_DATA_DIR/auth` (there is no separate auth-directory option).
Bridge creates private directories itself and refuses unsafe existing ones.

`auth` requires a private interactive terminal; email, password and MFA input
are hidden prompts. MFA resumes on the same upstream client, with up to three
code attempts. An interrupted MFA challenge is not serialized: rerun `auth`.
Passwords and codes are never accepted as arguments or saved. The session is
atomically saved as `auth/session.json` with mode `0600`, under directories with
mode `0700`. A failed sign-in leaves the previous session intact. Ambient
`GARMINTOKENS` is not imported. Upstream debugging and dynamic exception bodies
are suppressed.

`auth --refresh` explicitly checks/refreshes the saved session through the
upstream library; sync can also refresh it and saves rotated tokens. Neither
falls back to a password prompt. Rejected sessions report `reauth_required`.
Offline `status` reports `absent`, `saved_unverified` or `reauth_required` and a
last-verification timestamp; a saved session is not proof of present validity.

For a minimal live check in the separately authorized private environment,
replace this example day with a known cycling day in that account:

```sh
garmin-bridge sync --start 2025-01-02 --end 2025-01-02
garmin-bridge status
garmin-bridge list --start 2025-01-02 --end 2025-01-02 --limit 5
garmin-bridge activity 1001
```

Replace `1001` with a numeric ID returned by `list`. A one-day scan checks the
pipeline but does not initialize full history. Resolve an unfinished scan with
`sync --resume`, then start `sync --full`; alternatively `sync --full --new-scan`
retains unresolved gaps while starting the full scan. MFA is entered only at the
hidden `auth` prompt, with no separate MFA argument or command.

## First full historical import

After authentication, use the same configured store:

```sh
garmin-bridge sync --full
garmin-bridge status
```

A new store's plain `sync` refuses to substitute a recent-only import and asks
for `sync --full`. Full import has no lower date bound or server calendar-date
filter: it enumerates the visible cycling catalog, obtains originals, validates
FIT and publishes reviewed results. Current profile settings are never used to
fill historical fields. An explicitly requested range scan can populate a
subset, but does not mark the store as initialized for daily sync.

Each job freezes a UTC `cutoff_at`. It includes activities starting at or before
that instant, not a claimed snapshot of upload time. Activities after it are
recorded as skipped. Missing start times are not invented: the historical FIT
session time is used when present, otherwise time remains missing. Full catalog
queries avoid assuming that the Garmin account and host share a time zone.
The JSON filter, cutoff and `observed_range` describe the scan scope and earliest/
latest observed dates; the latter is not a promise that older records do not
exist on Garmin.

Offset pagination is deduplicated by source ID. Pages and activity states are
checkpointed before moving on. A second full catalog pass must match the first
pass's in-scope IDs and reviewed metadata; a changed catalog triggers another
pass, up to three passes per invocation. This catches shifts caused by uploads
during enumeration, including uploads with old activity dates. Observed required
IDs remain required even if a later pass omits them. Absence never deletes an
existing local activity.

`complete: true` requires both converged enumeration and successful processing
of every required activity. A successful HTTP request or an empty page alone
does not satisfy this. Page limits, repeated pages, malformed responses,
non-convergence, authentication failures and activity gaps report `incomplete`.
The default budget is 1,000 pages per pass (100 requested rows per page, including
the terminating empty page); `--max-pages` can increase it up to 10,000. Page
offsets advance by actual returned rows, handling smaller server page caps.

**Completeness is scoped to the API-visible cycling catalog.** The inspected API
does not provide a verified cycling count, snapshot token or historical extent
guarantee. Two matching passes cannot prove that Garmin silently hid or capped
history, or prevent a later upload. `server_snapshot` is always false and the
completion basis is explicit in the output. Known truncation/budget exhaustion
is incomplete; any suspected server history restriction must be checked against
the private account before calling a live import comprehensive.

## Resume and gaps

```sh
garmin-bridge sync --resume
garmin-bridge sync --resume --max-pages 2000
garmin-bridge sync --full --new-scan
```

`sync --resume`, another `sync --full` for an unfinished full import, or plain
`sync` with unfinished work resumes the existing job and its original cutoff.
The private journal contains the page offset, pass fingerprints, normalizer and
schema versions, per-ID metadata, attempts and processing state. Resume first
handles queued activities, then continues pagination. Reconciliation may revisit
catalog pages, but already completed unchanged activities are not parsed again.
Resumed completed items and old activities considered for a daily skip have
their derived file hashes checked; missing or damaged results are queued again.
There is no unsupported byte-range resume: an unfinished original is downloaded
again, never promoted from a temporary file.

Every job separately counts `discovered`, `completed`, `skipped`, `pending`,
`retry` and `unavailable`. Completed items include confirmed unchanged inputs;
`updated` and `unchanged` describe their recorded processing outcomes. Skips have
explicit journal reasons: outside scope, after cutoff, or outside the daily
recheck window. Unknown activity types, unsupported/corrupt FIT and unavailable
originals remain gaps rather than silently becoming successful skips.

The CLI returns at most 20 gaps plus `gap_count` and a private
`reports/<job-id>/gaps.json` reference containing the full reviewed gap list.
During a killed/in-progress job the journal remains authoritative; the report
reference is omitted until a matching report is written. `status` exposes the
current scan and bounded gaps without networking. Existing completed activities
remain queryable while gaps are unresolved.

Network and rate-limit errors have at most three bridge attempts per operation,
with 1- and 2-second backoff. Rate-limit exhaustion or rejected authentication
stops further acquisition; network failures on an individual activity can leave
other activities complete. A later resume retries transient and unavailable FIT
items. No automatic background retries run. `--full --new-scan` freezes a new
cutoff, archives the old journal and carries its unresolved IDs forward; it does
not discard gaps or cached results. This also supports discovery of new data
while an older original remains unavailable.

## Daily sync and historical review

After a complete full import:

```sh
garmin-bridge sync
garmin-bridge sync --lookback-days 30
garmin-bridge sync --start 2025-01-01 --end 2025-01-31
```

Daily sync scans the visible **catalog** to detect new IDs, including delayed
uploads with old start dates. The inspected API offers no reliable upload-time
cursor, so catalog enumeration is the intentional network cost; unchanged old
originals are not routinely downloaded or parsed. Downloads are limited to new
IDs, changed reviewed metadata, missing/damaged local artifacts, changed normalizer
versions, and the recent recheck window (default 14 calendar days relative to the
UTC cutoff date, compared with the activity's local date when available).

For a rechecked activity, `source_digest` hashes the provider/source ID, reviewed
metadata and decompressed FIT SHA-256. A different ZIP timestamp/container alone
does not cause parsing. Matching input digest, schema/normalizer version and
stored artifact hashes skip parsing and preserve the previous result. Revisions
are published under the same source identity, never as another activity.

There is no verified upstream original-content revision/ETag. Rechecks therefore
download the original before confirming its content hash. A FIT-only correction
outside the recent window whose reviewed catalog metadata does not change
**cannot be discovered reliably** by daily sync. Use an explicit date range to
force historical rechecks, or `sync --full` for a full audit. Date-range bounds
are inclusive Garmin local calendar dates; `--start` is required, and `--end`
defaults to the host's current date. Explicit ranges do not reset older coverage.

## Offline import of a website export

`import-fit` needs no authentication and does not read/write authentication
files, construct `GarminSource`, contact Garmin, or use Drive. Obtain the original
FIT or single-FIT ZIP using Garmin's normal website in a separate private
environment, then place it in an owner-only directory outside Git. The input
file must be a regular file with `0600` permissions and its immediate parent
must have `0700` permissions. Symlinks, hardlinks, paths containing `..`, files
inside this store's `auth` directory, and non-`.fit`/`.zip` suffixes are rejected.
ZIP member names never become filesystem paths. All existing archive, size,
ambiguity, cycling-session and CRC checks still apply.

After installing the current reviewed checkout, these are the Polaris acceptance
commands. `BRIDGE_FIT` must refer to the privately downloaded file and `BRIDGE_ID`
must be the activity ID from the Garmin activity page, not a device number or an
inferred ID. The prompts below keep actual private paths and IDs out of examples:

```sh
umask 077
export GARMIN_BRIDGE_DATA_DIR="$HOME/.local/share/garmin-connect-bridge-offline"
install -d -m 700 "$GARMIN_BRIDGE_DATA_DIR" "$HOME/.local/share/garmin-bridge-transfer"
read -r -p 'Private FIT or ZIP path: ' BRIDGE_FIT
read -r -p 'Garmin activity ID: ' BRIDGE_ID

garmin-bridge --data-dir "$GARMIN_BRIDGE_DATA_DIR" import-fit \
  --file "$BRIDGE_FIT" --activity-id "$BRIDGE_ID"
# Repeat unchanged input: status must be unchanged, with the same file references.
garmin-bridge --data-dir "$GARMIN_BRIDGE_DATA_DIR" import-fit \
  --file "$BRIDGE_FIT" --activity-id "$BRIDGE_ID"
garmin-bridge list --limit 20
garmin-bridge activity "$BRIDGE_ID"

# Keep this filename new; choose a new path for each later backup.
BRIDGE_BUNDLE="$HOME/.local/share/garmin-bridge-transfer/offline-import.zip"
garmin-bridge backup export --file "$BRIDGE_BUNDLE"
garmin-bridge backup verify --file "$BRIDGE_BUNDLE"
# This destination must be empty/new. Authentication and original FITs are absent.
BRIDGE_RESTORE="$HOME/.local/share/garmin-connect-bridge-offline-restored"
garmin-bridge --data-dir "$BRIDGE_RESTORE" backup restore --file "$BRIDGE_BUNDLE"
garmin-bridge --data-dir "$BRIDGE_RESTORE" list --limit 20
garmin-bridge --data-dir "$BRIDGE_RESTORE" activity "$BRIDGE_ID"
garmin-bridge --data-dir "$BRIDGE_RESTORE" import-fit \
  --file "$BRIDGE_FIT" --activity-id "$BRIDGE_ID"
```

The per-file receipt has `status: created|updated|unchanged`, a bounded activity
summary, approved file references, `api_state_changed: false`, and
`input: {kind: local_file, activity_id_origin: cli_argument,
garmin_api_verified: false}`. A newly saved offline summary records
`provenance.input: local_fit_file`, the same ID origin and verification flag,
and quality flags `local_file_import`, `missing_api_metadata`, and
`unverified_activity_id`. No original filename/path, raw bytes or sample arrays
are printed. The syntactically valid ID is accepted as supplied; offline import
cannot prove its association with the file or an account. Check it privately.

Missing API metadata stays missing. Only observed FIT session/record values are
retained; session start may supply UTC, but local time, time zone and offset
remain null. Missing start time is not guessed from records or the host. Garmin
estimates/settings remain separately labeled. The input digest includes source,
supplied ID, missing metadata, decompressed FIT SHA-256 and local provenance;
the parse cache also checks schema/parser version and saved artifact hashes.
Changing a ZIP name/comment/container does not trigger another parse or record.

For an existing **offline** ID, changed FIT content creates a new immutable
revision and atomically replaces its index entry. An existing **API-derived**
ID is handled conservatively: identical FIT content with intact derived files
returns `unchanged` and preserves its metadata, provenance, parser version and
revision. A content difference or damaged API artifacts returns `import_conflict`;
resolve it with an explicit API recheck when available, or inspect the ID privately.
No force-overwrite option is provided. A successful later API acquisition verifies
the source through its normal path and replaces the local provenance, even if
optional API metadata is absent. Existing originals after backup may be absent;
reimporting an unchanged offline record can replenish its raw file without parsing.

Offline import changes only the activity map and its referenced files. It leaves
`covered_ranges`, `pending_ranges`, the computed watermark, `history_initialized`,
`full_completed_at`, `last_success_at`, `last_attempt_at`, `last_failure`, scan
offsets, tasks and gaps unchanged. Importing a file for a pending API gap does
**not** resolve that gap. Its receipt never claims account history is complete.
An offline-only backup has `scan_status: none`; that also means no API scan was
completed. Failed imports return a safe error and retain the previous result;
they do not create or modify API gap reports. Retry the same command after fixing
the file, permissions or lock contention. Keep originals private for later
reparsing: portable backups intentionally omit them.

This entry point handles one activity at a time, not whole-account export bundles
or legacy migrations. When API access later works, an offline-only store still
requires `auth` followed by `sync --full`; an existing unfinished API job still
requires `sync --resume`. A previously initialized store may use normal `sync`.
None of those API commands are implicit in import, queries or restore. This
change neither diagnoses network restrictions nor integrates with Google Drive.

## Offline derived FIT archives

`sanitize-fit` produces a **“去除显式位置的派生训练FIT”**: a derived training FIT with
explicit location and identity fields removed. It is **not anonymous**. Original
timestamps, physiological/training measurements, altitude and historical thresholds
can still identify or characterize a person. Keep the complete archive private.
Removed fields cannot be recovered from it; this is not a lossless copy of the
whole input or a promise that every future training metric can be reconstructed.

These two commands work entirely offline. They do not construct a GarminSource,
read/write authentication files, open the activity index, change API coverage,
watermarks, scan/gap state, or change the `backup` format. `--data-dir` (or its
normal default) is used only to reject an archive destination inside that store
and an input under its authentication directory. Do not place archives in any
other account's store or authentication directory either.

Use the installed executable from the intended Python 3.12 environment. For
example, from the checkout containing that environment:

```sh
BRIDGE="$PWD/.venv/bin/garmin-bridge"
umask 077
FIT_PRIVATE="$HOME/.local/share/garmin-bridge-fit-archives"
mkdir -p "$FIT_PRIVATE/inbox" "$FIT_PRIVATE/archives"
chmod 700 "$FIT_PRIVATE" "$FIT_PRIVATE/inbox" "$FIT_PRIVATE/archives"

# Put a copy of the website export in this private inbox separately.
# Use its actual private path locally; do not paste it or the FIT into a model.
FIT_INPUT="$FIT_PRIVATE/inbox/input.fit"  # A single-FIT .zip is also accepted.
chmod 600 "$FIT_INPUT"
FIT_ARCHIVE="$FIT_PRIVATE/archives/derived-001"  # Must not already exist.

"$BRIDGE" sanitize-fit --file "$FIT_INPUT" --output-dir "$FIT_ARCHIVE"
"$BRIDGE" verify-fit --source "$FIT_INPUT" --directory "$FIT_ARCHIVE"
```

Successful commands exit 0 and return only a fixed description, counts, validation
status and the relative names `derived.fit`, `manifest.json`, `report.json`.
Errors are fixed codes/messages, exit 2; an interrupt exits 130. No original
filename/path, input/output hash, decoded value, raw frame or upstream exception
is printed. The private manifest contains the required SHA-256 values; do not
paste it into a public issue or model context.

`verify-fit --source` requires the **same original bytes**, including the ZIP wrapper when
one was supplied. It reads the original and archive without changing either,
recomputes the projection and manifest, traverses all output definitions/data,
and re-reads the actual output using both pinned parsers. A stored `verified`
flag or a matching checksum alone is insufficient. A missing/unreadable `--source`
fails; it never falls back to another mode. Its receipt has `mode: "with_source"`
and `source_comparison: {"status":"passed"}`.

If the original is no longer available, explicitly request an archive-only check:

```sh
"$BRIDGE" verify-fit --archive-only --directory "$FIT_ARCHIVE"
```

`--archive-only` and `--source` are mutually exclusive; one is required. The
archive-only mode reads **only** `derived.fit`, `manifest.json`, and `report.json`.
It rechecks the exact member set, private regular files, canonical closed JSON
structures and supported versions, output/report SHA-256, document/inventory
consistency, strict FIT CRC and complete byte consumption. Every physical definition
and data field is checked against the unchanged policy, including base types,
array bounds, context and cross-message references. Canonical framing excludes
unused/redundant definitions and extra payload. Both pinned parsers reread the
actual approved FIT. Reads are bounded to 64 MiB for FIT, 4 MiB for the manifest,
and 64 KiB for the report. Historical `validations` flags are required format
metadata, **not** evidence that the checks passed this time.

Both verification modes return current checks in `checks`. Archive-only success
returns `mode: "archive_only"`, only observed `output_messages`/`retained_fields`
counts, and exactly:

```json
{"source_comparison":{"status":"not_checked","reason":"source_not_provided"}}
```

Here `status: "verified"` means the selected checks passed. It does **not** claim
that original values, missing/invalid patterns, message order or retention
completeness were compared again. Source hashes, source definition/removal counts
and preservation flags in the historical documents cannot be independently
confirmed without the original. They are neither reissued as current results nor
rewritten. Matching unkeyed hashes cannot establish source authenticity: a change
to approved training values accompanied by consistent CRC/hash updates can pass
archive-only checks; a full comparison against the original rejects it. Preserve
the original privately if later preservation comparison is needed. No file is
automatically deleted or uploaded.

The archive has three fixed members:

| File | Private contents |
| --- | --- |
| `derived.fit` | Approved physical fields in original message/field order and byte order, including invalid sentinels. |
| `manifest.json` | Format/rule/SDK/Profile versions; original-container, extracted-FIT, output and report SHA-256; field-number inventory, counts, fixed reason codes and verification results. |
| `report.json` | Bounded aggregate counts, verification results and fixed limitations; no sample values. |

The manifest inventories every definition, including unused ones, and every
data-field occurrence. A definition marked `retained` means its shape is eligible;
only occurrences passing context/reference checks are actually retained. Summary
counts refer to data occurrences. Pending-review counts are a subset of removals,
not additional missing fields. Compressed timestamps supplied by a record header
are counted as retained logical timestamp occurrences, with the representation
change reported separately. Rebuilt definition records need not retain the original
local slot assignments or byte positions.
Preservation validation flags refer to the approved retained fields, not equality
of the complete original and derived files.

The complete closed policy, all 152 candidate decisions, physical base types,
array bounds, scale/offset facts and contextual rules are in
[FIT_SANITIZE_RULES.md](FIT_SANITIZE_RULES.md). Key limits:

- One activity FIT, one cycling session and at least one retained record are
  required. Multi-session/multisport files and whole-account exports are unsupported.
  The same original input ZIP path, member, CRC, size and ambiguity checks apply.
- 151 core candidates are eligible. `lap 19:40` is deferred because swimming-length
  context is unsupported. Unknown messages/fields and all developer fields are
  removed; future SDK recognition grants no new permission. Wrong base types or
  unsupported array lengths are removed and reported, never coerced.
- Additional individually reviewed fields retain file type, sport, session/lap
  indices and counts, activity timing, historical HR/power zone configurations,
  legacy speed/altitude and power-phase pairs. No current settings, timezone, FTP,
  interpolated data or independently recomputed metrics are added. Garmin estimates
  and configuration remain source-reported historical fields, not validated metrics.
- Timer, rider-position and gear-change event payloads have explicit conditions.
  Other opaque `event.data`/`data16` payloads are removed and reported. Zone records
  require unique surviving session/lap targets. Dangling indices/counts and ambiguous
  global zone configurations are removed and reported, without synthesizing replacements.
- Integers, bitfields, fractional cadence, left/right balance, array order, invalid
  patterns, signed zero, float bytes, original scale/offset encoding and timestamps
  are copied exactly. Parser-expanded components are never written as new fields.
  With `--source`, the verifier compares exact retained bytes, distinguishing absent fields
  from all-invalid fields even when a parser omits them from its dictionaries.
- Supported compressed timestamp records omit field 253 from their local definition
  and have a preceding valid timestamp anchor from an explicitly approved message
  and field. Unknown-message or mistyped timestamp fields invalidate the anchor;
  they cannot inject private bytes indirectly into later retained timestamps.
  The 5-bit offsets/rollovers become
  explicit timestamps. Missing anchors or definitions also containing timestamp 253
  are rejected as unsupported/ambiguous. No timestamp is guessed.
- FIT headers must be 12 or 14 bytes, protocol 1.0/2.0, with an exact data length and
  strict CRCs (a 14-byte header must have a valid header CRC). Chained FIT files,
  trailing bytes, malformed/reserved record headers and duplicate fields fail.
  Input/output FIT and original ZIP limits remain 64 MiB; framing is additionally
  bounded to 500,000 definition/data messages, 2,000,000 field occurrences including
  definitions, and 8,192 inventory rows. Oversized input fails without publication.

Inputs are read-only; symlinks, hardlinks, Git paths, input/output aliases, and
existing destinations are rejected. Archives are `0700`, files `0600`. Publication
uses private temporary files, fsync and an atomic **no-replace** directory rename
(`renameat2(RENAME_NOREPLACE)` on Linux; `renamex_np(RENAME_EXCL)` on macOS).
Concurrent creators cannot replace each other or an empty existing destination.
An unsupported filesystem fails safely; there is no replace-in-place fallback.
An ordinary failure cleans the staging directory. A hard process exit can leave
a private `.fit-pending-*` directory, which `verify-fit` explicitly rejects; it
may be removed privately after confirming no writer remains. Retry with a new
archive path. Once publication succeeds, the complete archive is immutable by
Bridge; verification never repairs or rewrites it.

This archive is separate from Bridge's non-location JSON backup. `backup export`
does not include original FIT/ZIP **or these derived archives**; `backup restore`
does not restore them. This task adds no Drive transport or integration.

### Polaris private acceptance (not performed during development)

First install this checkout using the locked Python 3.12 instructions above.
When offline, provide a private local wheelhouse containing the exact runtime
and build wheels from the current locks (including the new SDK). After generating
`.venv/bridge-build.lock` with the installation snippet above:

```sh
# Set this to a directory already populated with the locked wheels.
BRIDGE_WHEELHOUSE="$HOME/bridge-wheelhouse"
.venv/bin/python -m pip install --no-cache-dir --no-index --find-links "$BRIDGE_WHEELHOUSE" \
  --only-binary=:all: --require-hashes -r requirements.lock -r .venv/bridge-build.lock
.venv/bin/python -m pip install --no-index --no-deps --no-build-isolation --force-reinstall .
.venv/bin/python -m pip check
.venv/bin/garmin-bridge sanitize-fit --help
.venv/bin/garmin-bridge verify-fit --help
```

No uv is required for this pip path. A wheelhouse from the prior import-only
version lacks `garmin-fit-sdk==21.218.0`; offline installation cannot download it.
Obtain that hash-locked wheel through an approved code/dependency transfer first.
Always invoke `.venv/bin/garmin-bridge` explicitly to avoid an older CLI on PATH.

In a separate private environment, copy one real website FIT/ZIP into a `0700`
inbox, keep the source `0600` and run the two commands above with no credentials.
Locally check source hashes before/after, both exit codes, archive permissions,
and the manifest's per-field removal/review reasons. Repeat to a second new
directory and compare derived bytes privately; the same input/rules are
deterministic. Trying the first destination again must fail and preserve it.
Validate representative invalid arrays, event contexts and historical zone
references against the private source. Compare any existing activity index,
scan/gap files and authentication directory before/after; they must be unchanged.
Do not publish the real files, manifests, hashes, counts or reports. These real-file
checks, macOS atomic publication and future downstream-reader compatibility remain
unverified; this development task uses synthetic inputs only and performs no upload.

## Offline queries and persistence

```sh
garmin-bridge status
garmin-bridge list --limit 20
garmin-bridge list --start 2025-01-01 --end 2025-01-31 --offset 20
garmin-bridge activity 1001
```

These commands never log in, download or normalize data. An empty store returns
zero activities and absent authentication; a missing activity returns a safe
`not_found` error. List pages contain at most 50 compact summaries. `activity`
returns a bounded summary, field coverage, quality flags and approved file
references, not sample arrays. References are relative to the configured private
root, so output does not disclose the user's home-directory name.

Private storage contains:

```text
auth/session.json                    protected upstream session
index.json                           committed activity index and coverage
sync-job.json                         current page/activity journal
jobs/<job-id>.json                    superseded unfinished journal
raw/<sha256>.bin                      exact downloaded/imported ZIP or FIT bytes
objects/<activity-id>/<revision>/     summary.json and full-resolution streams.json
reports/<job-id>/gaps.json            reviewed gap report
.restore-pending.json                 interrupted restore gate, present only during restore
```

All of these files are private, including non-location streams and summaries.
New files/directories have `0600`/`0700` permissions even under a permissive umask.
One POSIX lock covers authentication, synchronization, offline imports and
publication; queries take a shared lock and report `busy` if a writer is active. Writes use sibling
temporary files, fsync, atomic replacement and directory fsync. Original bytes
and complete validated derived files are written before the index switches.
Crashes may leave unreferenced private objects, but cannot expose a partially
written revision as complete. Old revisions are retained; automatic cleanup is
not implemented.
Queries starting before the store exists keep an empty snapshot, even if a
concurrent first writer creates the root; they never read that writer's unlocked
intermediate state and do not create a directory just to query it.
Private files, including the atomic index and journal, are capped at 128 MiB.
An oversized write fails before replacement, preserving the last readable file.

Only a complete scan advances successful coverage. Disconnected explicit ranges
do not jump a continuous watermark over a gap. A failed rescan preserves the
last successfully completed watermark and timestamp, while the pending journal
and failure code expose current incompleteness. `history_initialized` means that
a full scan completed previously, not that the current scan is complete or the
account is currently fresh. Missing upstream records never trigger local deletion.

## Portable backups for Polaris and Drive

Do **not** archive the whole private root: it contains credentials and location-
bearing originals. Export a reviewed package into a separate owner-only directory:

```sh
umask 077
export BRIDGE_TRANSFER_DIR="$HOME/.local/share/garmin-bridge-transfer"
install -d -m 700 "$BRIDGE_TRANSFER_DIR"
export BRIDGE_PACKAGE="$BRIDGE_TRANSFER_DIR/snapshot-001.zip"
garmin-bridge backup export --file "$BRIDGE_PACKAGE"
garmin-bridge backup verify --file "$BRIDGE_PACKAGE"
```

Use a new filename for each snapshot; export refuses to overwrite any existing
file. Both commands return a bounded JSON receipt with `bundle_id`, the entire
package's `bundle_sha256`, file/activity/gap counts and `scan_status`. Keep the
export receipt for comparison after download. Absolute user paths and sample
arrays are not printed. A successful export/verification means the snapshot is
valid; `scan_status: incomplete` and nonzero gaps still mean incomplete history.

Polaris can upload this ZIP and the reviewed receipt using its existing Drive
tools, then download them to a private local transfer directory when needed.
Bridge never initiates that transfer. Full-resolution non-location data remains
private activity data; this format is not anonymization or encryption.

Format `garmin-bridge-nonlocation`, version `1`, contains only:

- `manifest.json`: format/schema/normalizer versions, creation time, bundle ID,
  exact member list, byte sizes and SHA-256 for every payload member.
- `index.json`: current activity catalog, hashes, prior coverage, pending ranges,
  initialization status and successful/attempted sync timestamps.
- Current `summary.json` and full-resolution `streams.json` for each indexed
  activity, including explicit units, missing values and source/quality markers.
- `sync-job.json`, when present: cutoff/filter, page progress, convergence
  fingerprints, per-activity outcomes and retry state; its full gap report is
  rebuilt from that authoritative journal.

Authentication, passwords, tokens, cookies, logs, originals under `raw/`, stale
revisions, superseded journals and arbitrary extra files/free-text analyses are
excluded. Summary/stream content, nested journal metadata, cross-file identities,
coverage counts and the gap list are validated against closed schemas, as well
as hashes. Original references become null; the FIT content hash and reviewed
source digest remain for future comparison. This preserves queryable results
and resumable work, not raw FIT for later offline reprocessing.

Recover a downloaded package **before authenticating in the new store**:

```sh
chmod 600 "$BRIDGE_PACKAGE"
garmin-bridge backup verify --file "$BRIDGE_PACKAGE"
export GARMIN_BRIDGE_DATA_DIR="$HOME/.local/share/garmin-connect-bridge-restored"
garmin-bridge backup restore --file "$BRIDGE_PACKAGE"
garmin-bridge status
garmin-bridge list --limit 5
garmin-bridge activity 1001
garmin-bridge auth
```

Again replace `1001` with an existing ID. Compare the verification receipt's
`bundle_sha256` with the saved export receipt. Checksums detect corruption; they
do not authenticate a package from an untrusted party. Restore requires an empty
target (an existing bridge lock file is allowed), never merges into or overwrites
an existing store, and leaves an invalid package's target untouched. Download
tools may not preserve permissions: both package and parent must be owner-only.

Restore validates all members in a private temporary directory beside the
package before touching the destination. It takes the target's exclusive lock,
writes a durable restore marker, publishes files atomically with the index last,
and removes the marker only after completion. While the marker exists all normal
commands fail safely with `restore_pending`. After interruption, rerun the same
restore command with the **identical package**. Other packages are refused until
that restore completes. Hard exits can leave private staging files, which are
never treated as completed inputs; automatic staging cleanup is not implemented.

Choose the next sync based on the recovered state:

| Recovered state | Command after `auth` |
| --- | --- |
| An unfinished scan exists | `garmin-bridge sync --resume` |
| A full import completed previously and no scan is unfinished | `garmin-bridge sync --lookback-days 14` |
| Only a completed range scan or an empty snapshot | `garmin-bridge sync --full` |

Restored authentication is `absent`; successful queries need no login. Old
unchanged activities can skip downloads using reviewed metadata/version and
derived-file hashes even though raw FIT is absent. A later content recheck
downloads an original, compares its source digest and replenishes the private
raw file without parsing when the normalized result is confirmed unchanged.
Changed inputs or parser versions still require a downloadable original; missing
originals become gaps and the previous reviewed result stays available.

Packages use uncompressed ZIP (`ZIP_STORED`) with strict allowlisted member
paths. Unknown files, path traversal, absolute/Windows paths, duplicate names or
JSON keys, links, encrypted/compressed members, unsupported versions, corrupt
hashes and inconsistent data are rejected. Limits are 128 MiB per member,
16 GiB per package and 200,004 members (at most 100,000 activities, subject to
the index size cap). Export/restore process one activity at a time, but keep the
catalog and journal in memory. Restore needs temporary space for the complete
validated snapshot plus its destination. No Drive transfer, quota, large real
history or cross-platform restore has been exercised against a real service.

## Data contract

Version the output schema. Include the provider, source activity ID, source
digest, acquisition time, activity time and time zone, units, field coverage,
quality flags, and normalizer version.

Schema `1.0`, normalizer `1.2.1` retain full-resolution standard FIT record time,
heart rate, power, cadence, speed, altitude, distance, temperature, enhanced
respiration with reviewed units, and balance percentage/side. Laps retain times,
duration and distance; timer events retain reviewed types/times. Device provenance
is deliberately limited to message count and input/parser attribution. Unique
identifiers, arbitrary developer fields and unreviewed message types are omitted.
The bridge selects a message-specific field allowlist **before reading field
values into its intermediate projection**; it never collects all standard fields
first. Final summaries, streams, receipts and portable packages still undergo
recursive closed-schema validation. The FIT dependency decodes private input
internally; its raw frames are not outputs. Version `1.1.0` adds a closed local
provenance variant and quality flags. Existing API records from `1.0.0` remain
queryable without recomputation; older clients cannot read the new local variant.
No resampling is performed. Missing samples remain JSON null, actual zero remains
zero, and timestamps retain their observed irregular spacing. Missing local zone
names/offsets remain missing; no current configuration fills historical gaps.

Normalizer `1.2.0` adds exactly two non-fatal cadence quality flags:

| Flag | Evidence required |
| --- | --- |
| `cadence_summary_max_below_avg` | Valid nonnegative average and maximum in rpm from the same FIT session or the same API activity summary, with maximum below average. Mixed FIT/API sources are not compared. |
| `cadence_record_max_exceeds_session` | A valid record inside the inclusive explicit FIT session `start_time`/`timestamp` interval has an integer cadence component above the FIT session maximum. The end must be later than the start. API maxima are not substituted. |

The record comparison uses the original integer component internally because the
existing session extraction retains integer precision. Thus record `80.5` versus
session `80` does not trigger a flag. Session fractional fields remain unextracted;
record fractions remain merged exactly as before. The session end timestamp is a
non-location internal comparison boundary, not a new output metric. Active
`total_timer_time` is never used to reconstruct elapsed end time.

Missing/invalid bounds, missing timestamps or cadence, out-of-range records, and
API-only maxima provide no deterministic record/session judgment. A missing or
partial stream cannot establish that a larger session maximum is wrong. Even
with partial coverage, one valid in-range integer exceedance supplies positive
evidence; no minimum sample count or fixed rpm threshold diagnoses sensor error.
A flag is a consistency hint, not a rejection or correction: all original values,
units, sources, nulls, zeros, coverage and other flags remain unchanged. Isolated
and consecutive high points are preserved without clamping, filtering, filling,
overflow guesses or adding 256.

Normalizer `1.2.1` excludes negative session maxima and negative record integer
components from comparison evidence, while retaining those source values and
coverage unchanged. Mixed negative/valid records still retain valid evidence.
This patch version also invalidates `1.2.0` offline parse caches.

The closed quality vocabulary accepts these two names, not arbitrary flags or
fields. Existing `1.0.0`/`1.1.0`/`1.2.0` summaries and backups remain readable. Reimporting
an old offline cache with original FIT bytes creates a new immutable `1.2.1`
revision; the next identical import is unchanged. API-source protection still
applies: offline import cannot upgrade or overwrite an existing API record.

Keep Garmin-reported estimates and settings in a separate namespace. Values
such as normalized power, training stress score, training effect, and threshold
power are source-reported values, not independently validated fitness measures
or substitutes for another pipeline's load metric.

Archives are read in memory without extracting their supplied names. The MVP
accepts one FIT file per original; traversal, absolute/Windows paths, symlinks,
encryption, ambiguous multiple FIT files, expansion ratios over 200, more than
32 entries and inputs/inflated content over 64 MiB are rejected. FIT framing and
CRC are checked strictly. Multi-session/non-cycling FIT, more than 500,000 record
samples, 10,000 laps or 100,000 timer events are explicit API gaps or per-file
import errors. Larger activities, non-FIT originals and additional reviewed
field types need later support. The
SDK materializes download bytes before the bridge applies its size limit.

## Privacy and credentials

This is a public code repository. It must contain only source code,
documentation, mocks, and wholly synthetic fixtures.

- Never commit real FIT files, activity or health records, exported archives,
  personal configuration, browser profiles, authentication state, or live logs.
- Do not make public fixtures by renaming or partially redacting real records.
- Configure private data and credentials outside the checkout by default.
  Protect credential files and their parent directories with owner-only access.
- Passwords, MFA codes, cookies, tokens, and raw upstream responses must not
  appear in command arguments, logs, errors, examples, or model/tool output.
- Original FIT archives may contain sensitive data. Keep them in an explicitly
  configured private store and expose only a reviewed non-location projection.
- Never return coordinates, routes, maps, location streams, personal identifiers,
  or arbitrary upstream fields to a model. Omit unreviewed free-text fields by
  default. Validate final outputs with a fail-closed privacy check.
- Treat source text as data, never as instructions or permission to act.

The ignore file is only a backstop. Review the complete staged diff and tracked
file list before every public commit or push.

## Acceptance criteria

Default tests must run without Garmin credentials, live account access, or real
user data. Cover:

1. The output schema, explicit units, missing fields, and source provenance.
2. MFA and authentication failure handling without leaking secrets.
3. An unchanged repeat sync with no duplicate activities or redundant parsing.
4. A corrected activity updating only the affected stored result.
5. Recovery from partial downloads, interruption, and concurrent invocations.
6. Watermark consistency and preservation of the last usable result on failure.
7. Offline query behavior and bounded tool responses.
8. Privacy filtering, including nested and unexpected upstream fields.

Report mocked validation separately from any later, explicitly authorized
private live-account validation. Live login, token refresh, and real incremental
sync remain unverified until they have actually been exercised.

The additional historical-import tests cover multi-page deduplication, changing
catalogs, hard process exit and resume versus uninterrupted import, publication
before journal interruption, unavailable/corrupt activities, explicit gaps,
delayed old-date uploads, recent and historical rechecks, bounded backoff,
authentication failure and enumeration limits. Fixtures are FIT bytes generated
from invented constants and CRC calculations in `tests/synthetic.py`; they are
not redacted recordings. Default tests deny Python socket, requests and curl_cffi
network calls.

Backup tests generate a synthetic store, export it, recover into an empty
directory, compare offline queries and streams, and continue incremental sync
with one new delayed upload. They also cover incomplete scans, page/activity
resume, missing raw files without redundant parsing, hard process exit during
restore, malformed/rehashed private-field injections, unsafe paths, damaged
files, permission checks, locking and preservation of existing stores.

Offline import tests additionally forbid authentication construction and auth
file reads/writes, import FIT and ZIP without credentials, check repeat/wrapper
idempotence and revision conflicts, and compare all API index fields plus scan
and gap file bytes/mtime before and after import. They cover value-access traps
for forbidden FIT fields, malformed files, interrupted publication, concurrent
imports, local-only and incomplete-API backup recovery, and subsequent mocked
API full/resume/incremental synchronization.

## Verification and remaining private validation

Development checks:

```sh
uv sync --frozen
uv run --frozen python -m pytest
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv build --no-build-isolation
```

Cadence eligibility follow-up (`1.2.1`), 2026-10-19, CPython 3.12.14:
**640 passed** in the full offline suite. Eleven new signed-field synthetic FIT
cases cover negative maxima/records, mixed records, valid exceedances, zeros and
summary eligibility; one additional historical-cache case covers `1.2.0`.
Before the fix, four signed-field cases reproduced the false flag. Source values,
coverage, other flags and streams are compared unchanged. Frozen Python 3.12
installation, Ruff, formatting (32 files), wheel/sdist build and diff checks all
passed again using command-local `UV_CACHE_DIR=/tmp/cadence-uv-cache`; builds
went to `/tmp/cadence-fix-dist`. No original tests were removed or skipped.

Actual cadence-quality verification on 2026-10-19, Linux x86_64, CPython 3.12.14:

- `UV_CACHE_DIR=/tmp/cadence-uv-cache uv sync --frozen --python 3.12`: passed.
- `uv run --frozen python -m pytest --tb=short`: **628 passed**, including 34
  new synthetic cadence cases and all 594 pre-existing tests; none skipped.
- `uv run --frozen ruff check src tests`, `uv run --frozen ruff format --check
  src tests`, and `git diff --check`: passed (32 Python files formatted).
- `uv build --no-build-isolation --out-dir /tmp/cadence-dist`: wheel and sdist
  built. All uv checks used a command-local temporary cache, no persistent change.
- Tests cover precision, absent/invalid fractions and integer components, zeros,
  mixed sources, missing/reversed/empty bounds, out-of-range records, partial
  coverage, high points, sanitization/import/backup recovery, historical versions,
  immutable cache replacement, API-source protection and privacy rejection.

Comparisons are intentionally skipped where no reliable temporal or source
comparison exists, as specified above; missing samples do not establish a full
observed maximum. This is synthetic validation, with no real activity or Garmin
account access. Existing sanitizer policy, dependencies and AGENTS.md are unchanged.

Actual derived-FIT verification on 2026-10-18, Linux x86_64, CPython 3.12.14:

| Check actually run | Result |
| --- | --- |
| `.venv/bin/python -m pytest --tb=short` | **594 passed**: 365 derived-FIT cases, 58 archive-only cases and all 171 existing API/auth/import/privacy/backup cases; offline and synthetic. |
| `.venv/bin/ruff check src tests`; `.venv/bin/ruff format --check src tests`; `git diff --check` | Passed; all 31 Python files formatted. |
| `uv lock --check --offline`; before/after lock comparison | Consistent; only `garmin-fit-sdk==21.218.0` added, all existing versions unchanged. |
| `.venv/bin/python -m hatchling build -t wheel -t sdist -d /tmp/garmin-bridge-archive-only-dist` | Built locally; 22 wheel members and 39 source-package members checked against explicit allowlists. No FIT, runtime data, credentials or vendored SDK fixtures. |
| Python 3.12 fresh pip venv; hash-locked local wheelhouse; `pip install --no-index --no-deps --no-build-isolation --force-reinstall .`; built-wheel installation; `pip check` | Source and wheel paths passed; installs used local locked wheels without uv/network. Public PyPI was used separately to obtain dependencies. |
| Installed `sanitize-fit`/`verify-fit` help and synthetic FIT/ZIP execution | Passed with network, authentication construction and activity-state access forbidden; source bytes unchanged. Reinstalled the archive-only wheel without network; both input forms passed full verification, then archive-only verification after deletion of the synthetic source. Missing `--source` failed; historical files stayed unchanged. |
| Installed root help and empty-store `status`/`list`/`activity` | Exit 0/0/2 for queries; no store created. |
| Physical policy, private output and recovery tests | All 152 candidates checked against the fixed Profile and in both byte orders; exact bytes/invalid arrays/zero/bitfields, conditional events, unknown-anchor rejection, references, unused definitions, CRC/trailing content, recursive JSON boundaries, concurrent no-replace publication and hard exit covered. |
| `.venv/bin/python -m pytest tests/test_verify_fit_archive.py --tb=short` | **58 passed**: original deleted; explicit modes/no fallback; strict files/JSON/versions/hashes; policy, arrays, context/references and extra definitions rejected despite recomputed CRC/hashes; both parsers reexecuted; historical files unchanged; safe errors. |
| Existing mocked API stores before/after sanitize and both verify modes | Index, watermarks, pagination/activity jobs, gaps and synthetic auth-marker bytes/mtime/modes unchanged for interrupted pagination, partial activity processing and completed history. |

Full verification uses one source byte snapshot and bounds archive reads to the
expected sizes; archive-only verification has independent fixed read limits.
Review also prevents unknown fields from seeding compressed
timestamps, and retains a real zero lap count without creating a dangling first-lap
reference. Source files and derived artifacts stay outside Git. AGENTS.md and the
existing activity/backup formats are unchanged. No real-file acceptance, upload,
account access, commit, push, PR, deployment or release is part of this work.

The baseline MVP previously passed 116 tests. Actual verification of the offline
import change on 2026-10-15, Linux x86_64, CPython 3.12.14:

| Check actually run | Result |
| --- | --- |
| `.venv/bin/python -m pytest --tb=short` | **171 passed**, including 55 new import cases; entirely offline and synthetic. |
| `.venv/bin/ruff check src tests` | Passed. |
| `.venv/bin/ruff format --check src tests` | All 23 Python files formatted. |
| `git diff --check` | Passed. |
| `uv lock --check --offline` | Lock matches the project. |
| `.venv/bin/python -m hatchling build -t wheel -t sdist -d /tmp/garmin-bridge-import-fit-dist` | Wheel and sdist built locally; explicit member allowlists checked, including the new module/test. |
| Pip-only environment: lock-derived build requirements, `pip install --no-index --no-deps --no-build-isolation .`, wheel installation, `pip check` | Both installation paths passed with the existing hash-locked dependencies; build dependency closure verified. No uv or network was needed for these installs. |
| Installed CLI root/import-fit/query/backup help pages | Exit 0; `--file` and required `--activity-id` present. |
| Installed CLI, empty private store: `status`, `list`, `activity 1001` | Exit 0/0/2; absent/empty/not-found; queries create no state. |
| Installed CLI: synthetic FIT/ZIP import with network and GarminSource construction forbidden | Created/unchanged results, wrapper idempotence, no auth files, no API progress changes. |
| Installed package: import → export/verify/restore → identical queries → reimport → mocked full/incremental sync | Queries identical after restore; unchanged reimport; later API initialization succeeds and incremental sync adds exactly one new activity. No Drive/Garmin access. |

Package contents were checked for private runtime artifacts; none were included.
The source distribution has an explicit code/documentation/lockfile allowlist.
Dependency acquisition used public package-registry access. Test execution and
the installed CLI smoke checks required no network or credentials. Local build
artifacts are ignored, and no distribution was published.

No real account, credentials, FIT record, browser state, Polaris file or request
recording was used. Actual website-export compatibility and the supplied ID/file
association still require private validation; offline import cannot verify that
association itself. A later, separately authorized private environment should
use a persistent owner-only store outside any checkout and validate the website
export/import/restore commands above, then, when API access is available:

1. First sign-in, MFA continuation/cancellation, saved session permissions,
   process restart, explicit refresh, expiry/revocation and safely reported failure.
2. Full visible history against the account's privately inspected history,
   including oldest available cycling, supported subtypes, missing originals,
   server page caps, date/time-zone behavior and any hidden history limits.
3. Interrupt/resume, rate-limit backoff and gap recovery without losing earlier
   results or advancing coverage over unresolved work.
4. Unchanged repeat, a new ride, a delayed old-date upload, a corrected recent
   ride and an explicit old-range recheck; privately compare source digests,
   counts, preserved missing values and unchanged parse outcomes.
5. Export, compare the receipt, transfer privately through Polaris's existing
   Drive tools, download and verify, restore to a fresh directory, authenticate
   again, and resume or incrementally sync. The current verification covers the
   local synthetic recovery chain only, with no Google Drive or Garmin access.

Keep all resulting data, tokens and diagnostic artifacts private; report only
reviewed aggregate results. macOS runtime behavior, unofficial API stability,
true account history visibility and unattended renewal remain live-validation
gaps. The JSON index/journal favors simple atomic recovery over very large-store
performance; disk encryption, keychain integration, retention/garbage collection,
deletion reconciliation and automatic scheduling are not implemented.

## Out of scope for the first version

- Garmin activity edits, deletions, uploads, or workout scheduling.
- Intervals.icu, health-data synchronization, medical interpretation, or AI coaching.
- A web dashboard, a hosted MCP server, or a permanently running public service.
- CI/release automation and production deployment.

Those can be considered separately after the acquisition and privacy contracts
have been validated. A development workspace is not the long-term private data store.
