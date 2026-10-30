"""Bounded offline windows; no SQL, code, or arbitrary file query interface."""

from . import contract as c
from .errors import BridgeError
from .fit_unknown import valid_unknown
from .persistence import require
from .privacy import located, diagnosed
from .segments import seconds


@diagnosed(source=True)
@located("activities.sqlite", "samples")
def window(store, activity, start, end, offset=0, limit=100):
    require(
        c.identifier(activity) and c.iso_time(start, True) and c.iso_time(end, True),
        "sqlite_query.window.1",
        "activity_id,start,end",
    )
    require(0 < seconds(end) - seconds(start) <= 600, "sqlite_query.window.2", "start,end")
    require(
        type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200,
        "sqlite_query.window.3",
        "offset,limit",
    )
    entry = store.state()["activities"].get(activity)
    if entry is None:
        raise BridgeError("not_found")
    summary = entry["summary"]
    streams = store.read_json(summary["files"]["streams"])
    records = [
        {"ordinal": i, "sample": row}
        for i, row in enumerate(streams["records"])
        if row["timestamp"] is not None
        and seconds(start) <= seconds(row["timestamp"]) <= seconds(end)
    ]
    page = records[offset : offset + limit]
    for row in page:
        sample = row["sample"]
        unknown = {key: value for key, value in sample.items() if key not in streams["units"]}
        require(
            not unknown
            or streams.get("fit_fields_version") == c.FIT_FIELDS_VERSION
            and valid_unknown(unknown, "record"),
            "sqlite_query.window.4",
            "sample",
        )
        row["sample"] = {key: sample[key] for key in streams["units"]}
    return {
        "schema_version": c.SCHEMA_VERSION,
        "normalizer_version": summary["normalizer_version"],
        "revision": summary["files"]["summary"].split("/")[2],
        "activity_id": activity,
        "start": start,
        "end": end,
        "units": streams["units"],
        "coverage": summary["coverage"],
        "quality_flags": summary["quality_flags"],
        "records": page,
        "next_offset": offset + len(page) if offset + len(page) < len(records) else None,
    }
