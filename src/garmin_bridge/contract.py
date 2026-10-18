"""Reviewed output vocabulary and recursive, fail-closed boundary validation."""

from datetime import date, datetime
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import BridgeError, MESSAGES

SCHEMA_VERSION = "1.0"
NORMALIZER_VERSION = "1.1.0"
STREAM_UNITS = {
    "timestamp": "UTC",
    "heart_rate": "bpm",
    "power": "W",
    "cadence": "rpm",
    "speed": "m/s",
    "altitude": "m",
    "distance": "m",
    "temperature": "degC",
    "respiration": "breaths/min",
    "balance_percent": "%",
    "balance_side": "side",
}
METRIC_UNITS = {
    "duration": "s",
    "distance": "m",
    "avg_heart_rate": "bpm",
    "max_heart_rate": "bpm",
    "avg_power": "W",
    "max_power": "W",
    "avg_cadence": "rpm",
    "max_cadence": "rpm",
    "avg_speed": "m/s",
    "max_speed": "m/s",
    "ascent": "m",
    "descent": "m",
}
REPORTED_UNITS = {
    "normalized_power": "W",
    "training_stress_score": "score",
    "aerobic_training_effect": "score",
    "anaerobic_training_effect": "score",
    "threshold_power": "W",
}
FLAGS = {
    "non_location_projection",
    "missing_timezone",
    "missing_start_time",
    "missing_samples",
    "missing_timestamps",
    "nonmonotonic_timestamps",
    "invalid_values_omitted",
    "unreviewed_fields_omitted",
    "source_reported_not_independently_validated",
    "local_file_import",
    "missing_api_metadata",
    "unverified_activity_id",
}


def numeric(value):
    return type(value) in (int, float) and abs(value) <= 1e12 and math.isfinite(value)


def identifier(value):
    return type(value) is str and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None


def digest(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def iso_time(value, utc=False):
    if type(value) is not str or len(value) > 35:
        return False
    try:
        parsed = datetime.fromisoformat(value)
        return "T" in value and (
            not utc or (value.endswith("Z") and parsed.utcoffset().total_seconds() == 0)
        )
    except (ValueError, AttributeError):
        return False


def day(value):
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def timezone_name(value):
    if type(value) is not str or len(value) > 64:
        return False
    try:
        ZoneInfo(value)
        return True
    except (ValueError, ZoneInfoNotFoundError):
        return False


def nullable(check):
    return lambda x: x is None or check(x)


def literal(*values):
    return lambda x: type(x) in (str, int) and x in values


def obj(fields):
    return lambda x: (
        type(x) is dict
        and x.keys() == fields.keys()
        and all(check(x[key]) for key, check in fields.items())
    )


def sequence(check, maximum=100):
    return lambda x: type(x) is list and len(x) <= maximum and all(check(v) for v in x)


def count(x):
    return type(x) is int and 0 <= x <= 10**9


def quantities(units):
    return obj(
        {
            name: obj(
                {
                    "value": nullable(numeric),
                    "unit": literal(unit),
                    "source": nullable(literal("fit_session", "garmin_activity")),
                }
            )
            for name, unit in units.items()
        }
    )


TIME = obj(
    {
        "utc": nullable(lambda x: iso_time(x, True)),
        "local": nullable(iso_time),
        "timezone": nullable(timezone_name),
        "utc_offset_seconds": nullable(numeric),
    }
)
COVERAGE = obj(
    {name: obj({"present": count, "missing": count, "total": count}) for name in STREAM_UNITS}
)
FILES = obj(
    {
        name: (
            lambda x, suffix=name: (
                type(x) is str
                and re.fullmatch(r"objects/[1-9][0-9]{0,19}/[0-9a-f]{64}/" + suffix + r"\.json", x)
                is not None
            )
        )
        for name in ("summary", "streams")
    }
)
QUALITY = sequence(literal(*sorted(FLAGS)), len(FLAGS))
PROVENANCE_FIELDS = {
    "parser": literal("fitdecode/0.11.0"),
    "device_messages": count,
    "unique_identifiers_retained": literal("none"),
}
LOCAL_INPUT = obj(
    {
        "kind": literal("local_file"),
        "activity_id_origin": literal("cli_argument"),
        "garmin_api_verified": lambda x: x is False,
    }
)


def provenance(value):
    return obj({**PROVENANCE_FIELDS, "input": literal("garmin_original_fit")})(value) or obj(
        {
            **PROVENANCE_FIELDS,
            "input": literal("local_fit_file"),
            "activity_id_origin": literal("cli_argument"),
            "garmin_api_verified": lambda x: x is False,
        }
    )(value)


ACTIVITY_FIELDS = {
    "schema_version": literal(SCHEMA_VERSION),
    "provider": literal("garmin"),
    "source_activity_id": identifier,
    "source_digest": digest,
    "fit_sha256": digest,
    "normalizer_version": lambda x: (
        type(x) is str and re.fullmatch(r"\d+\.\d+\.\d+", x) is not None
    ),
    "acquired_at": lambda x: iso_time(x, True),
    "activity_time": TIME,
    "sport": literal("cycling"),
    "metrics": quantities(METRIC_UNITS),
    "garmin_reported": quantities(REPORTED_UNITS),
    "coverage": COVERAGE,
    "quality_flags": QUALITY,
    "sample_count": count,
    "lap_count": count,
    "timer_event_count": count,
    "provenance": provenance,
    "files": FILES,
}
ACTIVITY = obj(ACTIVITY_FIELDS)
RANGE = obj({"start": day, "end": day})
FRESHNESS = obj(
    {
        "last_success_at": nullable(lambda x: iso_time(x, True)),
        "last_attempt_at": nullable(lambda x: iso_time(x, True)),
        "pending_ranges": sequence(RANGE, 1000),
        "watermark": nullable(RANGE),
        "last_failure": nullable(literal(*MESSAGES)),
        "history_initialized": lambda x: type(x) is bool,
    }
)
FAILURE = nullable(literal(*MESSAGES))
CARD_FIELDS = {
    k: ACTIVITY_FIELDS[k]
    for k in ("provider", "source_activity_id", "activity_time", "quality_flags", "files")
}
CARD_FIELDS["metrics"] = quantities({k: METRIC_UNITS[k] for k in ("duration", "distance")})


def job_id(value):
    return type(value) is str and re.fullmatch(r"[a-f0-9]{32}", value) is not None


JOB_ID = job_id
GAP = obj(
    {
        "source_activity_id": identifier,
        "state": literal("discovered", "retry", "unavailable"),
        "reason": literal("pending", *MESSAGES),
        "attempts": count,
    }
)
SYNC_REPORT = obj(
    {
        "schema_version": literal(SCHEMA_VERSION),
        "job_id": JOB_ID,
        "mode": literal("full", "incremental", "range"),
        "status": literal("complete", "incomplete"),
        "complete": lambda x: type(x) is bool,
        "cutoff_at": lambda x: iso_time(x, True),
        "filter": obj(
            {
                "start": nullable(day),
                "end": nullable(day),
                "activity_type": literal("cycling"),
                "region": literal("international"),
                "cutoff_basis": literal("activity_start_utc"),
            }
        ),
        "observed_range": nullable(RANGE),
        "enumeration": obj(
            {
                "complete": lambda x: type(x) is bool,
                "passes": count,
                "next_offset": count,
                "limit_reached": lambda x: type(x) is bool,
                "basis": literal("two_matching_visible_catalog_passes"),
                "server_snapshot": lambda x: x is False,
            }
        ),
        "counts": obj(
            {
                k: count
                for k in ("discovered", "completed", "skipped", "pending", "retry", "unavailable")
            }
        ),
        "updated": count,
        "unchanged": count,
        "failed": count,
        "gaps": sequence(GAP, 20),
        "gap_count": count,
        "gap_report": nullable(
            lambda x: (
                type(x) is str and re.fullmatch(r"reports/[a-f0-9]{32}/gaps\.json", x) is not None
            )
        ),
        "freshness": FRESHNESS,
        "last_failure": FAILURE,
    }
)

OUTPUTS = {
    "import-fit": obj(
        {
            "schema_version": literal(SCHEMA_VERSION),
            "status": literal("created", "updated", "unchanged"),
            "input": LOCAL_INPUT,
            "activity": ACTIVITY,
            "api_state_changed": lambda x: x is False,
        }
    ),
    "backup": obj(
        {
            "schema_version": literal(SCHEMA_VERSION),
            "action": literal("export", "verify", "restore"),
            "format": literal("garmin-bridge-nonlocation"),
            "format_version": literal(1),
            "bundle_id": JOB_ID,
            "bundle_sha256": digest,
            "file_count": count,
            "activity_count": count,
            "gap_count": count,
            "scan_status": literal("none", "complete", "incomplete"),
        }
    ),
    "error": obj(
        {
            "schema_version": literal(SCHEMA_VERSION),
            "error": literal(*MESSAGES),
            "message": literal(*MESSAGES.values()),
        }
    ),
    "auth": obj(
        {
            "schema_version": literal(SCHEMA_VERSION),
            "state": literal("saved"),
            "verified_at": lambda x: iso_time(x, True),
        }
    ),
    "status": obj(
        {
            "schema_version": literal(SCHEMA_VERSION),
            "authentication": obj(
                {
                    "state": literal("absent", "saved_unverified", "reauth_required"),
                    "last_verified_at": nullable(lambda x: iso_time(x, True)),
                }
            ),
            "activity_count": count,
            "freshness": FRESHNESS,
            "covered_ranges": sequence(RANGE, 1000),
            "last_failure": FAILURE,
            "full_completed_at": nullable(lambda x: iso_time(x, True)),
            "sync": nullable(SYNC_REPORT),
        }
    ),
    "list": obj(
        {
            "schema_version": literal(SCHEMA_VERSION),
            "freshness": FRESHNESS,
            "activities": sequence(obj(CARD_FIELDS), 50),
            "next_offset": nullable(count),
        }
    ),
    "activity": obj(
        {
            "schema_version": literal(SCHEMA_VERSION),
            "freshness": FRESHNESS,
            "activity": ACTIVITY,
            "files_available": lambda x: type(x) is bool,
        }
    ),
    "sync": SYNC_REPORT,
}

FIT_ARCHIVE_FIELDS = {
    "schema_version": literal(SCHEMA_VERSION),
    "action": literal("sanitize", "verify"),
    "format": literal("garmin-bridge-derived-training-fit"),
    "format_version": literal(1),
    "rules_version": literal("1.0.0"),
    "description": literal("去除显式位置的派生训练FIT"),
    "status": literal("verified"),
    "summary": obj(
        {
            name: count
            for name in (
                "input_messages",
                "output_messages",
                "removed_messages",
                "retained_fields",
                "removed_fields",
                "pending_review_fields",
                "compressed_timestamps_expanded",
            )
        }
    ),
    "files": lambda x: x == ["derived.fit", "manifest.json", "report.json"],
    "activity_store_changed": lambda x: x is False,
}
FIT_ARCHIVE_CHECK_NAMES = (
    "file_set",
    "document_structure_and_versions",
    "document_consistency",
    "output_sha256",
    "report_sha256",
    "strict_crc",
    "complete_byte_consumption",
    "all_definitions_approved",
    "all_data_fields_approved",
    "context_and_array_constraints",
    "references_valid",
    "canonical_framing",
    "fitdecode_reread",
    "garmin_sdk_reread",
    "no_extra_payload",
)
FIT_ARCHIVE_RECEIPT = obj(FIT_ARCHIVE_FIELDS)
FIT_VERIFY_FIELDS = {
    **FIT_ARCHIVE_FIELDS,
    "action": literal("verify"),
    "checks": obj({name: literal("passed") for name in FIT_ARCHIVE_CHECK_NAMES}),
}
FIT_VERIFY_SOURCE_RECEIPT = obj(
    {
        **FIT_VERIFY_FIELDS,
        "mode": literal("with_source"),
        "source_comparison": obj({"status": literal("passed")}),
    }
)
FIT_VERIFY_ONLY_RECEIPT = obj(
    {
        **FIT_VERIFY_FIELDS,
        # Counts observed now; do not return historical removal/retention claims.
        "summary": obj({"output_messages": count, "retained_fields": count}),
        "mode": literal("archive_only"),
        "source_comparison": obj(
            {"status": literal("not_checked"), "reason": literal("source_not_provided")}
        ),
    }
)


def fit_verify_receipt(value):
    return FIT_VERIFY_SOURCE_RECEIPT(value) or FIT_VERIFY_ONLY_RECEIPT(value)


OUTPUTS.update({"sanitize-fit": FIT_ARCHIVE_RECEIPT, "verify-fit": fit_verify_receipt})


def validate_output(kind, payload):
    try:
        valid = OUTPUTS[kind](payload)
    except (TypeError, ValueError, KeyError, OverflowError):
        valid = False
    if not valid:
        raise BridgeError("privacy_failed")
    return payload


def project_activity(value):
    # Never serialize a raw provider object, even after a recursive denylist.
    try:
        projected = {key: value[key] for key in ACTIVITY_FIELDS}
        valid = ACTIVITY(projected)
        valid = valid and all(
            f"/{projected['source_activity_id']}/" in ref for ref in projected["files"].values()
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise BridgeError("privacy_failed")
    return projected


def validate_streams(value):
    fields = {key: nullable(numeric) for key in STREAM_UNITS}
    fields["timestamp"] = nullable(lambda x: iso_time(x, True))
    fields["balance_side"] = nullable(literal("right", "unknown"))
    check = obj(
        {
            "schema_version": literal(SCHEMA_VERSION),
            "normalizer_version": ACTIVITY_FIELDS["normalizer_version"],
            "provider": literal("garmin"),
            "source_activity_id": identifier,
            "source_digest": digest,
            "units": obj({k: literal(v) for k, v in STREAM_UNITS.items()}),
            "lap_units": obj(
                {
                    "start": literal("UTC"),
                    "end": literal("UTC"),
                    "duration": literal("s"),
                    "distance": literal("m"),
                }
            ),
            "records": sequence(obj(fields), 500_000),
            "laps": sequence(
                obj(
                    {
                        "start": nullable(lambda x: iso_time(x, True)),
                        "end": nullable(lambda x: iso_time(x, True)),
                        "duration": nullable(numeric),
                        "distance": nullable(numeric),
                    }
                ),
                10_000,
            ),
            "timer_events": sequence(
                obj(
                    {
                        "timestamp": nullable(lambda x: iso_time(x, True)),
                        "type": literal(
                            "start", "stop", "stop_all", "stop_disable", "stop_disable_all"
                        ),
                    }
                ),
                100_000,
            ),
        }
    )
    if not check(value):
        raise BridgeError("privacy_failed")
    return value


def validate_gap_report(value):
    check = obj(
        {
            "schema_version": literal(SCHEMA_VERSION),
            "job_id": JOB_ID,
            "cutoff_at": lambda x: iso_time(x, True),
            "gaps": sequence(GAP, 1_000_000),
        }
    )
    if not check(value):
        raise BridgeError("privacy_failed")
    return value
