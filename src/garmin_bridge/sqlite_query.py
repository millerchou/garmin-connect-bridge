"""Bounded offline windows; no SQL, code, or arbitrary file query interface."""

from . import contract as c
from .errors import BridgeError
from .persistence import require
from .segments import seconds


def window(store, activity, start, end, offset=0, limit=100):
    require(c.identifier(activity) and c.iso_time(start, True) and c.iso_time(end, True))
    require(0 < seconds(end) - seconds(start) <= 600)
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200)
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
