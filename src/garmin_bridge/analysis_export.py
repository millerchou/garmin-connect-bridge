"""Current, identity-disabled revisions as a standalone SQL analysis artifact."""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile

from . import __version__, contract as c
from .errors import BridgeError
from .fit_details import specs_for, valid_message
from .fit_fields import AUXILIARY_MESSAGES
from .jobs import gaps, load_job
from .persistence import artifact_files, identity_enabled, require
from .sqlite_transfer import digest_file
from .storage import Store, canonical, decode
from .sync import watermark
from .upstream import now


def detail_columns(message):
    """Only existing reviewed fields; pairs retain their original order."""
    columns = {}
    for name, spec in specs_for(message).items():
        if spec.identity:
            continue
        prefix = "garmin_reported_" if spec.reported else "fit_"
        names = [prefix + name]
        if spec.shape == "pair":
            names = [f"{prefix}{name}_{i}" for i in range(2)]
        for column in names:
            columns[column] = "TEXT" if spec.kind != "number" else "REAL"
    return columns


TABLES = {
    "activities": {
        "activity_id": "TEXT",
        "schema_version": "TEXT",
        "normalizer_version": "TEXT",
        "fit_fields_version": "TEXT",
        "provider": "TEXT",
        "sport": "TEXT",
        "acquired_at": "TEXT",
        "activity_time_utc": "TEXT",
        "activity_time_local": "TEXT",
        "timezone": "TEXT",
        "utc_offset_seconds": "REAL",
        "sample_count": "INTEGER",
        "lap_count": "INTEGER",
        "timer_event_count": "INTEGER",
        "input": "TEXT",
        "parser": "TEXT",
        "device_messages": "INTEGER",
        "activity_id_origin": "TEXT",
        "garmin_api_verified": "INTEGER",
        "coverage_json": "TEXT",
        "quality_flags_json": "TEXT",
        **{
            column: kind
            for name in c.METRIC_UNITS
            for column, kind in ((name, "REAL"), (name + "_source", "TEXT"))
        },
        **{
            column: kind
            for name in c.REPORTED_UNITS
            for column, kind in (
                ("garmin_reported_" + name, "REAL"),
                ("garmin_reported_" + name + "_source", "TEXT"),
            )
        },
    },
    "records": {
        "activity_id": "TEXT",
        "ordinal": "INTEGER",
        **{
            name: "TEXT" if name in {"timestamp", "balance_side"} else "REAL"
            for name in c.STREAM_UNITS
        },
    },
    "laps": {
        "activity_id": "TEXT",
        "ordinal": "INTEGER",
        "start": "TEXT",
        "end": "TEXT",
        "duration": "REAL",
        "distance": "REAL",
        **detail_columns("lap"),
    },
    "events": {
        "activity_id": "TEXT",
        "ordinal": "INTEGER",
        "timer_type": "TEXT",
        **detail_columns("event"),
    },
    "messages": {
        "activity_id": "TEXT",
        "ordinal": "INTEGER",
        "message": "TEXT",
        "detail_json": "TEXT",
    },
    "meta": {
        "format_version": "INTEGER",
        "generated_at": "TEXT",
        "cli_version": "TEXT",
        "activity_start_utc": "TEXT",
        "activity_end_utc": "TEXT",
        "coverage_start": "TEXT",
        "coverage_end": "TEXT",
        "covered_ranges_json": "TEXT",
        "pending_ranges_json": "TEXT",
        "gap_count": "INTEGER",
        **{
            table + "_count": "INTEGER"
            for table in ("activities", "records", "laps", "events", "messages")
        },
        "units_json": "TEXT",
    },
}

UNITS = {
    "activities": {
        **c.METRIC_UNITS,
        **{"garmin_reported_" + k: v for k, v in c.REPORTED_UNITS.items()},
        "activity_time_utc": "UTC",
        "activity_time_local": "local",
        "acquired_at": "UTC",
        "utc_offset_seconds": "s",
    },
    "records": c.STREAM_UNITS,
    "laps": {"start": "UTC", "end": "UTC", "duration": "s", "distance": "m"},
    "events": {},
}
for _table, _message in (("laps", "lap"), ("events", "event")):
    for _name, _spec in specs_for(_message).items():
        _prefix = "garmin_reported_" if _spec.reported else "fit_"
        for _suffix in ("_0", "_1") if _spec.shape == "pair" else ("",):
            UNITS[_table][_prefix + _name + _suffix] = (
                "strokes" if _name == "total_strokes" else _spec.unit
            )


def ddl(table):
    columns = [f'"{name}" {kind}' for name, kind in TABLES[table].items()]
    if table != "meta":
        key = "activity_id" if table == "activities" else "activity_id,ordinal"
        columns.append(f"PRIMARY KEY ({key})")
        if table != "activities":
            columns.append("FOREIGN KEY (activity_id) REFERENCES activities(activity_id)")
    return f'CREATE TABLE "{table}" ({",".join(columns)}) STRICT'


def insert(db, table, row):
    require(row.keys() <= TABLES[table].keys())
    db.execute(
        f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in TABLES[table])})',
        [row.get(name) for name in TABLES[table]],
    )


def json_text(value):
    return canonical(value).decode()


def flatten_detail(message):
    result = {}
    for group, prefix in (("fields", "fit_"), ("garmin_reported", "garmin_reported_")):
        for name, quantity in message[group].items():
            value = quantity["value"]
            if specs_for(message["message"])[name].shape == "pair":
                result.update(
                    {
                        f"{prefix}{name}_{i}": v
                        for i, v in enumerate(value if value is not None else [None, None])
                    }
                )
            else:
                result[prefix + name] = value
    return result


def activity_row(summary, streams):
    row = {
        name: summary[name]
        for name in (
            "schema_version",
            "normalizer_version",
            "provider",
            "sport",
            "acquired_at",
            "sample_count",
            "lap_count",
            "timer_event_count",
        )
    }
    row.update(
        {
            "activity_id": summary["source_activity_id"],
            "fit_fields_version": streams.get("fit_fields_version"),
            "activity_time_utc": summary["activity_time"]["utc"],
            "activity_time_local": summary["activity_time"]["local"],
            "timezone": summary["activity_time"]["timezone"],
            "utc_offset_seconds": summary["activity_time"]["utc_offset_seconds"],
            "coverage_json": json_text(summary["coverage"]),
            "quality_flags_json": json_text(summary["quality_flags"]),
        }
    )
    for name in ("input", "parser", "device_messages", "activity_id_origin", "garmin_api_verified"):
        row[name] = summary["provenance"].get(name)
    for group, prefix in (("metrics", ""), ("garmin_reported", "garmin_reported_")):
        for name, quantity in summary[group].items():
            row[prefix + name] = quantity["value"]
            row[prefix + name + "_source"] = quantity["source"]
    return row


def populate(db, source):
    state = source.state()
    job = load_job(source)
    expected = {}
    for ident, entry in state["activities"].items():
        if identity_enabled(entry["summary"]):
            raise BridgeError("analysis_identity")
        # Fully validate only current artifacts, without accessing raw or auth files.
        contents = dict(artifact_files(entry, source.read_bytes))
        streams = decode(contents[entry["summary"]["files"]["streams"]])
        insert(db, "activities", activity_row(entry["summary"], streams))
        events = [flatten_detail(row) for row in streams.get("events", [])]
        if "events" not in streams:
            events = [
                {"fit_timestamp": row["timestamp"], "timer_type": row["type"]}
                for row in streams["timer_events"]
            ]
        messages = ([streams["session"]] if "session" in streams else []) + streams.get(
            "messages", []
        )
        expected[ident] = {
            "records": len(streams["records"]),
            "laps": len(streams["laps"]),
            "events": len(events),
            "messages": len(messages),
        }
        for table, rows in (
            ("records", streams["records"]),
            (
                "laps",
                (
                    {
                        **{k: row[k] for k in ("start", "end", "duration", "distance")},
                        **(flatten_detail(row["fit"]) if "fit" in row else {}),
                    }
                    for row in streams["laps"]
                ),
            ),
            ("events", events),
            (
                "messages",
                ({"message": row["message"], "detail_json": json_text(row)} for row in messages),
            ),
        ):
            for ordinal, row in enumerate(rows):
                insert(db, table, {"activity_id": ident, "ordinal": ordinal, **row})
    times = [
        entry["summary"]["activity_time"]["utc"]
        for entry in state["activities"].values()
        if entry["summary"]["activity_time"]["utc"]
    ]
    mark = watermark(state)
    metadata = {
        "format_version": 1,
        "generated_at": now(),
        "cli_version": __version__,
        "activity_start_utc": min(times) if times else None,
        "activity_end_utc": max(times) if times else None,
        "coverage_start": mark["start"] if mark else None,
        "coverage_end": mark["end"] if mark else None,
        "covered_ranges_json": json_text(state["covered_ranges"]),
        "pending_ranges_json": json_text(state["pending_ranges"]),
        "gap_count": len(gaps(job)) if job else 0,
        "activities_count": len(expected),
        **{
            table + "_count": sum(counts[table] for counts in expected.values())
            for table in ("records", "laps", "events", "messages")
        },
        "units_json": json_text(UNITS),
    }
    insert(db, "meta", metadata)
    return expected, metadata


def validate(db, expected, metadata):
    require(db.execute("PRAGMA integrity_check").fetchall() == [("ok",)])
    require(db.execute("PRAGMA foreign_key_check").fetchall() == [])
    # Exact SQL rules out extra columns, views, triggers and unreviewed JSON carriers.
    require(
        dict(db.execute("SELECT name,sql FROM sqlite_schema WHERE sql IS NOT NULL"))
        == {table: ddl(table) for table in TABLES}
    )
    for table, columns in TABLES.items():
        require(
            [(row[1], row[2]) for row in db.execute(f'PRAGMA table_info("{table}")')]
            == list(columns.items())
        )
    require({row[0] for row in db.execute("SELECT activity_id FROM activities")} == set(expected))
    for table in ("records", "laps", "events", "messages"):
        counts = {
            ident: (count, low, high)
            for ident, count, low, high in db.execute(
                f'SELECT activity_id,count(*),min(ordinal),max(ordinal) FROM "{table}" GROUP BY activity_id'
            )
        }
        require(
            counts
            == {ident: (n[table], 0, n[table] - 1) for ident, n in expected.items() if n[table]}
        )
    require(
        db.execute("SELECT * FROM meta").fetchall()
        == [tuple(metadata[name] for name in TABLES["meta"])]
    )
    for coverage, flags in db.execute("SELECT coverage_json,quality_flags_json FROM activities"):
        require(c.COVERAGE(decode(coverage.encode())) and c.QUALITY(decode(flags.encode())))
    for message, detail in db.execute("SELECT message,detail_json FROM messages"):
        value = decode(detail.encode())
        require(valid_message(value, False, AUXILIARY_MESSAGES | {"session"}))
        require(message == value["message"])


def check_destination(parent, name):
    target = parent.path(name)
    if target.exists() and target.is_dir():
        raise BridgeError("unsafe_store")
    # Replacing only the main file while an old journal/WAL survives can cause
    # SQLite to replay the old destination's pages into the new artifact.
    if any(parent.path(name + suffix).exists() for suffix in ("-journal", "-wal", "-shm")):
        raise BridgeError("busy")


def export_analysis(source, destination):
    target = Store(Path(destination)).root
    if target.is_relative_to(source.root):
        raise BridgeError("unsafe_store")
    parent = Store(target.parent)
    parent.ensure()
    check_destination(parent, target.name)
    with source.lock():
        fd, name = tempfile.mkstemp(prefix=".analysis-", suffix=".sqlite", dir=parent.root)
        staging = Path(name)
        try:
            os.close(fd)
            with closing(sqlite3.connect(staging)) as db, db:
                db.execute("PRAGMA foreign_keys=ON")
                for table in TABLES:
                    db.execute(ddl(table))
                expected, metadata = populate(db, source)
            with closing(sqlite3.connect(staging.as_uri() + "?mode=ro", uri=True)) as db:
                validate(db, expected, metadata)
            receipt = c.validate_output(
                "export",
                {
                    "schema_version": c.SCHEMA_VERSION,
                    "format": "analysis_sqlite",
                    "sha256": digest_file(staging),
                    "size": staging.stat().st_size,
                    "activity_count": len(expected),
                    "verified": True,
                },
            )
            with staging.open("rb") as handle:
                os.fsync(handle.fileno())
            check_destination(parent, target.name)
            # Keep publication last so flush/validation failures preserve the old file.
            os.replace(staging, target)
            return receipt
        finally:
            for suffix in ("", "-journal", "-wal", "-shm"):
                Path(name + suffix).unlink(missing_ok=True)
