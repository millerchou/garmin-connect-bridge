"""Closed public output schemas for the SQLite commands."""

from . import contract as c
from .segments import DEFINITION, EFFORT, ALGORITHM

TRANSFER = c.obj(
    {
        "schema_version": c.literal(c.SCHEMA_VERSION),
        "action": c.literal("export", "verify", "restore"),
        "storage_version": c.literal(1),
        "activity_count": c.count,
        "revision_count": c.count,
        "sample_count": c.count,
        "gap_count": c.count,
        "verified": lambda x: x is True,
    }
)


def TIME(x):
    return c.iso_time(x, True)


def window_contract(units):
    sample = {k: c.nullable(c.numeric) for k in units}
    sample["timestamp"] = c.nullable(TIME)
    sample["balance_side"] = c.nullable(c.literal("right", "unknown"))
    return c.obj(
        {
            "schema_version": c.literal(c.SCHEMA_VERSION),
            "normalizer_version": c.ACTIVITY_FIELDS["normalizer_version"],
            "revision": c.digest,
            "activity_id": c.identifier,
            "start": TIME,
            "end": TIME,
            "units": c.obj({k: c.literal(v) for k, v in units.items()}),
            "coverage": c.coverage_for(units),
            "quality_flags": c.QUALITY,
            "records": c.sequence(
                c.obj(
                    {"ordinal": c.count, "sample": c.record_check(sample, units == c.STREAM_UNITS)}
                ),
                200,
            ),
            "next_offset": c.nullable(c.count),
        }
    )


def WINDOW(value):
    return any(
        window_contract(units)(value)
        for units in (c.STREAM_UNITS, c.V1_STREAM_UNITS, c.LEGACY_STREAM_UNITS)
    )


HISTORY_ROW = c.obj(
    {
        "segment_id": c.JOB_ID,
        "definition_version": c.count,
        "algorithm_version": c.literal(ALGORITHM),
        "activity_id": c.identifier,
        "revision": c.digest,
        "reference_revision": c.digest,
        "status": c.literal("matched", "no_match", "unavailable", "insufficient_data"),
        "effort": c.nullable(EFFORT),
        "provenance": c.provenance,
        "quality_flags": c.QUALITY,
        "normalizer_version": c.ACTIVITY_FIELDS["normalizer_version"],
        "reference_normalizer_version": c.ACTIVITY_FIELDS["normalizer_version"],
    }
)
HISTORY = c.obj(
    {
        "definition": DEFINITION,
        "results": c.sequence(HISTORY_ROW, 50),
        "next_offset": c.nullable(c.count),
        "cached_activity_count": c.count,
        "activity_count": c.count,
    }
)


def validate(kind, value):
    if kind in {"snapshot", "backup"}:
        return TRANSFER(value)
    if kind == "window":
        return WINDOW(value)
    return (
        c.obj({"definition": DEFINITION})(value)
        or c.obj({"updated": c.count})(value)
        or HISTORY(value)
    )


def validate_query(kind, value):
    if value.get("storage_version") != 1 or type(value["storage_version"]) is not int:
        return False
    base = {k: v for k, v in value.items() if k != "storage_version"}
    if kind == "list":
        fields = {
            "coverage": c.COVERAGE,
            "normalizer_version": c.ACTIVITY_FIELDS["normalizer_version"],
            "sample_count": c.count,
            "revision": c.digest,
        }
        rows = value.get("activities")
        if not c.sequence(c.obj({**c.CARD_FIELDS, **fields}), 50)(rows):
            return False
        base["activities"] = [{k: v for k, v in row.items() if k not in fields} for row in rows]
    return c.OUTPUTS[kind](base)
