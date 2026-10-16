"""Closed schemas for the private catalog and resumable, non-location journal.

These are also the portable backup boundary: hashes alone cannot establish
privacy or consistency. Unknown keys and free text fail closed.
"""

from . import contract as c
from .errors import BridgeError, MESSAGES


def mapping(key_check, value_check, maximum=1_000_000):
    return lambda x: (
        type(x) is dict
        and len(x) <= maximum
        and all(key_check(k) and value_check(v) for k, v in x.items())
    )


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
JOB = c.obj(
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


def require(condition):
    if not condition:
        raise BridgeError("privacy_failed")


def validate_state(value, portable=False):
    from .normalize import LOCAL_FLAGS, input_digest, local_metadata, sha256
    from .storage import canonical

    require(STATE(value))
    for interval in value["covered_ranges"] + value["pending_ranges"]:
        require(interval["start"] <= interval["end"])
    require(value["history_initialized"] == (value["full_completed_at"] is not None))
    for source_id, entry in value["activities"].items():
        summary = c.project_activity(entry["summary"])
        require(summary["source_activity_id"] == source_id)
        if summary["provenance"]["input"] == "local_fit_file":
            meta = local_metadata(source_id)
            require(entry["metadata_digest"] == sha256(canonical(meta)))
            require(
                summary["source_digest"]
                == input_digest(meta, summary["fit_sha256"], local_import=True)
            )
            require(LOCAL_FLAGS <= set(summary["quality_flags"]))
            require(
                all(
                    summary["activity_time"][k] is None
                    for k in ("local", "timezone", "utc_offset_seconds")
                )
            )
            require(
                all(
                    q["source"] != "garmin_activity"
                    for group in ("metrics", "garmin_reported")
                    for q in summary[group].values()
                )
            )
        if portable or entry["raw_ref"] is None:
            require(entry["raw_ref"] is None and entry["raw_sha256"] is None)
        else:
            require(entry["raw_ref"] == f"raw/{entry['raw_sha256']}.bin")
    return value


def validate_job(value):
    require(JOB(value))
    interval = value["filter"]
    if value["mode"] == "range":
        require(interval["start"] is not None and interval["end"] is not None)
        require(interval["start"] <= interval["end"])
    else:
        require(interval == {"start": None, "end": None})
    require(set(value["seen"]) <= set(value["items"]))
    for source_id, item in value["items"].items():
        if item["metadata"] is not None:
            require(item["metadata"]["source_activity_id"] == source_id)
        if item["state"] == "completed":
            require(item["metadata"] is not None and item["reason"] is None)
            require(item["outcome"] in {"updated", "unchanged"})
        else:
            require(item["outcome"] is None)
        if item["state"] == "skipped":
            require(item["reason"] in {"outside_scope", "after_cutoff", "outside_recheck_window"})
        if item["state"] == "discovered":
            require(item["metadata"] is not None and item["reason"] is None)
        if item["state"] in {"retry", "unavailable"}:
            require(item["reason"] in MESSAGES)
    if value["status"] == "complete":
        require(value["enumeration_complete"] and value["last_failure"] is None)
        require(all(i["state"] in {"completed", "skipped"} for i in value["items"].values()))
    return value


def validate_pair(state, job):
    """A portable snapshot must not claim completed items whose results are absent."""
    from .normalize import input_digest, sha256
    from .storage import canonical

    validate_state(state, portable=True)
    if job is None:
        from .storage import empty_state

        require(
            all(
                s["summary"]["provenance"]["input"] == "local_fit_file"
                for s in state["activities"].values()
            )
        )
        require({**state, "activities": {}} == empty_state())
        return
    validate_job(job)
    for source_id, item in job["items"].items():
        if item["state"] == "completed" or item["reason"] == "outside_recheck_window":
            require(source_id in state["activities"])
            entry = state["activities"][source_id]
            require(item["metadata"] is not None)
            require(entry["metadata_digest"] == sha256(canonical(item["metadata"])))
            require(
                entry["summary"]["source_digest"]
                == input_digest(item["metadata"], entry["summary"]["fit_sha256"])
            )
    if job["status"] == "complete":
        require(state["last_success_at"] is not None and state["last_failure"] is None)
        if job["mode"] == "full":
            require(state["history_initialized"])
