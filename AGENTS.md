# Development instructions

This is a public repository for a narrow, read-only Garmin Connect bridge.
Read README.md before changing implementation or scope.

## Work within the requested scope

- Follow the user's current request and preserve unrelated changes.
- Prefer a small adapter around a pinned upstream dependency over copying its
  authentication engine or expanding the Garmin API surface.
- Separate acquisition, normalization, persistence, and offline queries.
- Use `codex/` when creating task branches unless the user specifies otherwise.
- Commit, push, merge, release, deploy, or change CI only when the user has
  authorized the corresponding action. Do not rebase or rewrite shared history.
- Do not add account write operations, dashboards, health synchronization, or
  production services as incidental implementation choices.

## Public-data boundary

- Only code, documentation, mocks, and entirely synthetic fixtures belong here.
- Never include real activity/health data, original exports, credentials,
  account/browser state, live request recordings, private harness files, personal
  memory, or machine-specific configuration in commits, PR text, logs, or examples.
- Do not turn real records into fixtures through partial redaction or renaming.
- Keep private runtime data and authentication state outside the checkout.
- Never print or return credentials or raw upstream bodies. Sanitize exceptions
  and diagnostic output at the boundary; disable upstream payload debugging.
- Use explicit non-location output allowlists. Unreviewed nested fields and
  free text must not pass through to model-facing output.
- Original FIT bytes are private inputs. Do not expose coordinates, tracks,
  routes, maps, unique device identifiers, or position streams in derived
  artifacts or model context.
- Use synthetic negative cases to test privacy failures. Never access a real
  Garmin account merely because credentials happen to be available.
- Before a public commit or push, inspect the tracked file list and full diff.
  `.gitignore` is not a substitute for this review.

## Verification and reporting

- Default tests must be offline and require no personal credentials or data.
- Cover the README acceptance criteria, especially idempotence, partial failures,
  watermark consistency, missing data, and privacy filtering.
- Run checks appropriate to the change. Do not broaden or repeat them without
  a failure, new change, or unresolved concern.
- Preserve explicit units, time zones, provenance, and metric/schema versions.
  Garmin-reported estimates must not be presented as independent measurements.
- Keep live-account validation separate from mocks and static inspection.
- At bootstrap there is no implementation or test suite. State this honestly;
  do not claim success for commands or tests that do not exist.
- Report what changed, what was actually verified, and what remains unverified.
