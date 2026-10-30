"""Closed schemas for the private catalog and resumable, non-location journal.

These are also the portable backup boundary: hashes alone cannot establish
privacy or consistency. Unknown keys and free text fail closed.
"""

import copy

from . import contract as c
from .errors import MESSAGES
from .privacy import require, require_schema, located


def mapping(key_check, value_check, maximum=1_000_000):
    def check(x):
        return (
            type(x) is dict
            and len(x) <= maximum
            and all(key_check(k) and value_check(v) for k, v in x.items())
        )

    check.mapped = value_check
    return check


def BOOLEAN(x):
    return type(x) is bool


def STAMP(x):
    return c.iso_time(x, True)


VERSION = c.ACTIVITY_FIELDS["normalizer_version"]
INTERVAL = c.obj({"start": c.nullable(c.day), "end": c.nullable(c.day)})
META = c.obj(
    {
        "source_activity_id": c.identifier,
        "activity_time": c.TIME,
        "metrics": c.obj({k: c.nullable(c.numeric) for k in c.METRIC_UNITS}),
        "garmin_reported": c.obj({k: c.nullable(c.numeric) for k in c.REPORTED_UNITS}),
    }
)
ENTRY = c.obj(
    {
        "summary": c.ACTIVITY,
        "file_hashes": c.obj({k: c.digest for k in ("summary", "streams")}),
        "raw_ref": c.nullable(
            lambda x: (
                type(x) is str and x.startswith("raw/") and x.endswith(".bin") and c.digest(x[4:-4])
            )
        ),
        "raw_sha256": c.nullable(c.digest),
        "metadata_digest": c.digest,
    }
)
STATE = c.obj(
    {
        "version": c.literal(1),
        "activities": mapping(c.identifier, ENTRY),
        "covered_ranges": c.sequence(c.RANGE, 1000),
        "pending_ranges": c.sequence(c.RANGE, 1000),
        "last_success_at": c.nullable(STAMP),
        "last_attempt_at": c.nullable(STAMP),
        "last_failure": c.FAILURE,
        "history_initialized": BOOLEAN,
        "full_completed_at": c.nullable(STAMP),
    }
)
ITEM = c.obj(
    {
        "metadata": c.nullable(META),
        "reason": c.nullable(
            c.literal("outside_scope", "after_cutoff", "outside_recheck_window", *MESSAGES)
        ),
        "fingerprint": c.digest,
        "state": c.literal("discovered", "completed", "skipped", "retry", "unavailable"),
        "attempts": c.count,
        "outcome": c.nullable(c.literal("updated", "unchanged")),
    }
)
LEGACY_JOB = c.obj(
    {
        "version": c.literal(1),
        "schema_version": c.literal(c.SCHEMA_VERSION),
        "normalizer_version": VERSION,
        "id": c.JOB_ID,
        "mode": c.literal("full", "incremental", "range"),
        "filter": INTERVAL,
        "cutoff_at": STAMP,
        "lookback": lambda x: type(x) is int and 1 <= x <= 365,
        "status": c.literal("complete", "incomplete"),
        "enumeration_complete": BOOLEAN,
        "passes": c.count,
        "offset": c.count,
        "pages_in_pass": c.count,
        "page_size": lambda x: type(x) is int and 1 <= x <= 100,
        "seen": mapping(c.identifier, c.digest),
        "page_signatures": c.sequence(c.digest, 10_000),
        "previous_fingerprint": c.nullable(c.digest),
        "pass_conflicted": BOOLEAN,
        "limit_reached": BOOLEAN,
        "items": mapping(c.identifier, ITEM),
        "report_valid": BOOLEAN,
        "last_failure": c.FAILURE,
    }
)


def JOB(value):
    if type(value) is not dict:
        return False
    if "include_fit_identity" not in value:
        return LEGACY_JOB(value)
    return BOOLEAN(value["include_fit_identity"]) and LEGACY_JOB(
        {k: v for k, v in value.items() if k != "include_fit_identity"}
    )


JOB.diagnose = lambda value: c.schema_field(LEGACY_JOB, value)


def identity_enabled(summary):
    return summary["provenance"]["unique_identifiers_retained"] == "explicit_opt_in"


@located("activities.sqlite", "documents")
def validate_summary_provenance(summary, metadata_digest=None):
    """Apply the same source guarantees to current and historical revisions."""
    from .normalize import LOCAL_FLAGS, input_digest_matches, local_metadata, sha256
    from .storage import canonical

    require_schema(c.ACTIVITY, summary, "summary.contract")
    if summary["provenance"]["input"] == "local_fit_file":
        meta = local_metadata(summary["source_activity_id"])
        if metadata_digest is not None:
            require(
                metadata_digest == sha256(canonical(meta)),
                "persistence.validate_summary_provenance.2",
                "metadata_digest",
            )
        require(
            input_digest_matches(
                summary["source_digest"],
                meta,
                summary["fit_sha256"],
                local_import=True,
                include_identity=identity_enabled(summary),
            ),
            "persistence.validate_summary_provenance.3",
            "source_digest,fit_sha256",
        )
        require(
            LOCAL_FLAGS <= set(summary["quality_flags"]),
            "persistence.validate_summary_provenance.4",
            "quality_flags",
        )
        require(
            summary["activity_time"]["timezone"] is None,
            "persistence.validate_summary_provenance.5",
            "timezone,activity_time",
        )
        if tuple(map(int, summary["normalizer_version"].split("."))) < (1, 3, 0):
            require(
                all(summary["activity_time"][k] is None for k in ("local", "utc_offset_seconds")),
                "persistence.validate_summary_provenance.6",
                "activity_time",
            )
        require(
            all(
                q["source"] != "garmin_activity"
                for group in ("metrics", "garmin_reported")
                for q in summary[group].values()
            ),
            "persistence.validate_summary_provenance.7",
            "source",
        )
    return summary


@located("activities.sqlite", "documents")
def validate_state(value, portable=False):
    require_schema(STATE, value, "state.contract")
    for interval in value["covered_ranges"] + value["pending_ranges"]:
        require(interval["start"] <= interval["end"], "persistence.validate_state.2", "start,end")
    require(
        value["history_initialized"] == (value["full_completed_at"] is not None),
        "persistence.validate_state.3",
        "history_initialized,full_completed_at",
    )
    for source_id, entry in value["activities"].items():
        summary = c.project_activity(entry["summary"])
        require(
            summary["source_activity_id"] == source_id,
            "persistence.validate_state.4",
            "source_activity_id",
        )
        validate_summary_provenance(summary, entry["metadata_digest"])
        if portable or entry["raw_ref"] is None:
            require(
                entry["raw_ref"] is None and entry["raw_sha256"] is None,
                "persistence.validate_state.5",
                "raw_ref,raw_sha256",
            )
        else:
            require(
                entry["raw_ref"] == f"raw/{entry['raw_sha256']}.bin",
                "persistence.validate_state.6",
                "raw_ref,raw_sha256",
            )
    return value


@located("activities.sqlite", "documents")
def validate_job(value):
    require_schema(JOB, value, "job.contract")
    interval = value["filter"]
    if value["mode"] == "range":
        require(
            interval["start"] is not None and interval["end"] is not None,
            "persistence.validate_job.2",
            "start,end",
        )
        require(interval["start"] <= interval["end"], "persistence.validate_job.3", "start,end")
    else:
        require(
            interval == {"start": None, "end": None}, "persistence.validate_job.4", "sync-job.json"
        )
    require(set(value["seen"]) <= set(value["items"]), "persistence.validate_job.5", "seen,items")
    for source_id, item in value["items"].items():
        if item["metadata"] is not None:
            require(
                item["metadata"]["source_activity_id"] == source_id,
                "persistence.validate_job.6",
                "source_activity_id,metadata",
            )
        if item["state"] == "completed":
            require(
                item["metadata"] is not None and item["reason"] is None,
                "persistence.validate_job.7",
                "metadata,reason",
            )
            require(
                item["outcome"] in {"updated", "unchanged"}, "persistence.validate_job.8", "outcome"
            )
        else:
            require(item["outcome"] is None, "persistence.validate_job.9", "outcome")
        if item["state"] == "skipped":
            require(
                item["reason"] in {"outside_scope", "after_cutoff", "outside_recheck_window"},
                "persistence.validate_job.10",
                "reason",
            )
        if item["state"] == "discovered":
            require(
                item["metadata"] is not None and item["reason"] is None,
                "persistence.validate_job.11",
                "metadata,reason",
            )
        if item["state"] in {"retry", "unavailable"}:
            require(item["reason"] in MESSAGES, "persistence.validate_job.12", "reason")
    if value["status"] == "complete":
        require(
            value["enumeration_complete"] and value["last_failure"] is None,
            "persistence.validate_job.13",
            "enumeration_complete,last_failure",
        )
        require(
            all(i["state"] in {"completed", "skipped"} for i in value["items"].values()),
            "persistence.validate_job.14",
            "state,items",
        )
    return value


@located("activities.sqlite", "documents")
def validate_pair(state, job):
    """A portable snapshot must not claim completed items whose results are absent."""
    from .normalize import input_digest_matches, sha256
    from .storage import canonical

    validate_state(state, portable=True)
    if job is None:
        from .storage import empty_state

        require(
            all(
                s["summary"]["provenance"]["input"] == "local_fit_file"
                for s in state["activities"].values()
            ),
            "persistence.validate_pair.1",
            "input,provenance,summary,activities",
        )
        require(
            {**state, "activities": {}} == empty_state(),
            "persistence.validate_pair.2",
            "index.json",
        )
        return
    validate_job(job)
    for source_id, item in job["items"].items():
        if item["state"] == "completed" or item["reason"] == "outside_recheck_window":
            require(source_id in state["activities"], "persistence.validate_pair.3", "activities")
            entry = state["activities"][source_id]
            require(item["metadata"] is not None, "persistence.validate_pair.4", "metadata")
            require(
                entry["metadata_digest"] == sha256(canonical(item["metadata"])),
                "persistence.validate_pair.5",
                "metadata_digest,metadata",
            )
            require(
                input_digest_matches(
                    entry["summary"]["source_digest"],
                    item["metadata"],
                    entry["summary"]["fit_sha256"],
                    include_identity=identity_enabled(entry["summary"]),
                ),
                "persistence.validate_pair.6",
                "source_digest,metadata,fit_sha256,summary",
            )
    if job["status"] == "complete":
        require(
            state["last_success_at"] is not None and state["last_failure"] is None,
            "persistence.validate_pair.7",
            "last_success_at,last_failure",
        )
        if job["mode"] == "full":
            require(
                state["history_initialized"], "persistence.validate_pair.8", "history_initialized"
            )


def snapshot(store):
    from .jobs import load_job

    state = copy.deepcopy(store.state())
    for entry in state["activities"].values():
        entry["raw_ref"] = entry["raw_sha256"] = None
    job = load_job(store)
    validate_pair(state, job)
    return state, job


def control_files(state, job):
    from .jobs import JOB_FILE, gaps

    values = {"index.json": state}
    if job is not None:
        values[JOB_FILE] = job
        # Rebuild from the journal, including after a hard kill. Do not copy
        # a stale report or arbitrary files from the reports directory.
        values[f"reports/{job['id']}/gaps.json"] = c.validate_gap_report(
            {
                "schema_version": c.SCHEMA_VERSION,
                "job_id": job["id"],
                "cutoff_at": job["cutoff_at"],
                "gaps": gaps(job),
            }
        )
    return values


@located("activities.sqlite", "documents")
def validate_artifact_paths(summary):
    from .normalize import sha256
    from .storage import canonical

    revision = sha256(
        canonical(
            [summary["source_digest"], summary["normalizer_version"], summary["schema_version"]]
        )
    )
    require(
        summary["files"]
        == {
            kind: f"objects/{summary['source_activity_id']}/{revision}/{kind}.json"
            for kind in ("summary", "streams")
        },
        "persistence.validate_artifact_paths.1",
        "files,source_activity_id",
    )


@located("activities.sqlite", "documents")
def validate_artifact_relation(summary, streams):
    """Cheap revision/metadata/count checks, independent of per-sample validation."""
    validate_artifact_paths(summary)
    from .fit_details import fill_local_time

    require(
        identity_enabled(summary) == streams.get("include_identity", False),
        "persistence.validate_artifact_relation.1",
        "include_identity",
    )
    require(
        summary["coverage"].keys() == streams["units"].keys(),
        "persistence.validate_artifact_relation.2",
        "coverage,units",
    )
    if summary["provenance"]["input"] == "local_fit_file" and "fit_fields_version" in streams:
        expected_time = {**summary["activity_time"], "local": None, "utc_offset_seconds": None}
        fill_local_time(expected_time, streams["messages"])
        require(
            summary["activity_time"] == expected_time,
            "persistence.validate_artifact_relation.3",
            "activity_time",
        )
    for key in (
        "schema_version",
        "normalizer_version",
        "provider",
        "source_activity_id",
        "source_digest",
    ):
        require(streams[key] == summary[key], "persistence.validate_artifact_relation.4", key)
    for array, field in (
        ("records", "sample_count"),
        ("laps", "lap_count"),
        ("timer_events", "timer_event_count"),
    ):
        require(
            len(streams[array]) == summary[field],
            "persistence.validate_artifact_relation.5",
            field,
        )


@located("activities.sqlite", "documents")
def artifact_files(entry, read):
    from .normalize import sha256
    from .storage import decode

    summary = entry["summary"]
    validate_artifact_paths(summary)
    contents = {}
    for kind, path in summary["files"].items():
        data = read(path)
        require(
            data is not None and sha256(data) == entry["file_hashes"][kind],
            "persistence.artifact_files.1",
            "file_hashes",
        )
        contents[kind] = (path, data, decode(data))
    require(contents["summary"][2] == summary, "persistence.artifact_files.2", "summary")
    c.project_activity(summary)
    streams = c.validate_streams(contents["streams"][2])
    validate_artifact_relation(summary, streams)
    for key, coverage in summary["coverage"].items():
        present = sum(row[key] is not None for row in streams["records"])
        require(
            coverage
            == {
                "present": present,
                "missing": len(streams["records"]) - present,
                "total": len(streams["records"]),
            },
            "persistence.artifact_files.3",
            "records",
        )
    return [(path, data) for path, data, _ in contents.values()]
