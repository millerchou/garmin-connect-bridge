"""Private no-replace, self-contained SQLite snapshots."""

import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile

from . import contract as c
from .persistence import (
    artifact_files,
    control_files,
    require,
    snapshot,
    validate_artifact_relation,
    validate_state,
)
from .errors import BridgeError
from .jobs import gaps
from .normalize import sha256
from .sqlite_store import (
    DATABASE,
    STORAGE_VERSION,
    SQLiteStore,
    validate_document,
    document_kind,
    ARRAY_LIMITS,
    stream_arrays,
)
from .segments import ALGORITHM, ALGORITHMS
from .storage import Store, canonical, decode, publish_directory


def digest_file(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def inspect_store(store, *, full_validation=True):
    require(store.read_bytes("index.json") is not None)
    state, job = snapshot(store)
    check_report(store, state, job)
    counts = {
        "activities": len(state["activities"]),
        "revisions": 0,
        "records": 0,
        "laps": 0,
        "timer_events": 0,
    }
    detail_counts = {"events": 0, "messages": 0, "sessions": 0, "identity_revisions": 0}
    field_versions = set()
    with store.database() as db:
        require(db.execute("PRAGMA integrity_check").fetchall() == [("ok",)])
        names = [r[0] for r in db.execute("SELECT name FROM documents ORDER BY name")]
        for name in names:
            require(document_kind(name) is not None)
            content = store.read_bytes(name)
            value = decode(content)
            if full_validation or not name.endswith("/streams.json"):
                validate_document(name, value)
            else:
                # Sample records/events; keep lap/message context for zone references.
                # Export validates every row.
                sample = dict(value)
                for key in ("records", "timer_events", "events", "raw_messages"):
                    if key in sample:
                        rows = sample[key]
                        sample[key] = [
                            rows[i] for i in sorted({0, len(rows) // 2, len(rows) - 1}) if rows
                        ]
                validate_document(name, sample)
            if name.endswith("/summary.json"):
                summary = decode(content)
                streams = summary["files"]["streams"]
                stream_content = store.read_bytes(streams)
                require(stream_content is not None)
                entry = {
                    "summary": summary,
                    "file_hashes": {
                        "summary": sha256(content),
                        "streams": sha256(stream_content),
                    },
                }
                if full_validation:
                    artifact_files(entry, store.read_bytes)
                else:
                    validate_artifact_relation(summary, decode(stream_content))
                counts["revisions"] += 1
            if name.endswith("/streams.json"):
                value = decode(content)
                require(name.replace("/streams.json", "/summary.json") in names)
                if "fit_fields_version" in value:
                    field_versions.add(value["fit_fields_version"])
                    detail_counts["sessions"] += 1
                    detail_counts["identity_revisions"] += value["include_identity"]
                if value.get("fit_fields_version") == "1.1":
                    detail_counts.setdefault("raw_messages", 0)
                for key in stream_arrays(value):
                    target = counts if key in counts else detail_counts
                    target[key] += len(value[key])
        require(
            db.execute(
                "SELECT COUNT(*) FROM samples WHERE name NOT IN (SELECT name FROM documents)"
            ).fetchone()[0]
            == 0
        )
        require(
            all(
                r[0] in ARRAY_LIMITS and r[1].endswith("/streams.json")
                for r in db.execute("SELECT DISTINCT kind,name FROM samples")
            )
        )
        from .segments import validate_segments

        validate_segments(db, store)
    for entry in state["activities"].values():
        if full_validation:
            artifact_files(entry, store.read_bytes)
        else:
            summary_content = store.read_bytes(entry["summary"]["files"]["summary"])
            require(summary_content is not None)
            require(decode(summary_content) == entry["summary"])
            require(sha256(summary_content) == entry["file_hashes"]["summary"])
    times = [
        e["summary"]["activity_time"]["utc"]
        for e in state["activities"].values()
        if e["summary"]["activity_time"]["utc"]
    ]
    return {
        "fit_fields_versions": sorted(field_versions),
        "fit_counts": detail_counts,
        "identity_included": detail_counts["identity_revisions"] > 0,
        "storage_version": STORAGE_VERSION,
        "schema_version": c.SCHEMA_VERSION,
        "normalizer_versions": sorted(
            {
                decode(store.read_bytes(n))["normalizer_version"]
                for n in names
                if n.endswith("/summary.json")
            }
        ),
        "segment_algorithm_version": ALGORITHM,
        "counts": counts,
        "range": {"start": min(times) if times else None, "end": max(times) if times else None},
        "gaps": gaps(job) if job else [],
    }


def check_report(store, state, job):
    if job is not None and job["report_valid"]:
        name = f"reports/{job['id']}/gaps.json"
        require(store.read_bytes(name) == canonical(control_files(state, job)[name]))


def copy_logical(source, target):
    """Validated records only; excludes auth, originals, private paths and free pages."""
    if source._absent_snapshot or not source.path(DATABASE).exists():
        from .storage import empty_state

        require(source.state() == empty_state())
        target.write_json("index.json", empty_state())
        return
    inspect_store(source)
    # Decide final control values before inserting anything: copying a stale
    # report and replacing it later leaves allocation-history-dependent bytes.
    state, job = snapshot(source)
    controls = control_files(state, job)
    with source.database() as db:
        names = sorted({r[0] for r in db.execute("SELECT name FROM documents")} | controls.keys())
        for name in sorted(names, key=lambda n: n == "index.json"):
            value = controls[name] if name in controls else decode(source.read_bytes(name))
            target.write_json(name, value)
        with target.database(write=True) as out:
            for table in ("segments", "efforts"):
                for row in db.execute(f"SELECT * FROM {table} ORDER BY 1,2"):
                    out.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in row)})", row)


def receipt(action, info):
    return {
        "schema_version": c.SCHEMA_VERSION,
        "action": action,
        "storage_version": STORAGE_VERSION,
        "activity_count": info["counts"]["activities"],
        "revision_count": info["counts"]["revisions"],
        "sample_count": info["counts"]["records"],
        "gap_count": len(info["gaps"]),
        "verified": True,
    }


def new_destination(path, source_root=None):
    target = Store(Path(path))
    require(source_root is None or not target.root.is_relative_to(source_root))
    if target.root.exists():
        raise BridgeError("destination_exists")
    parent = Store(target.root.parent)
    parent.ensure()
    return target, parent


def export_snapshot(source, destination):
    target, parent = new_destination(destination, source.root)
    with (
        source.lock(),
        tempfile.TemporaryDirectory(prefix=".sqlite-export-", dir=parent.root) as temp,
    ):
        staging = SQLiteStore(Path(temp) / "stage")
        staging.ensure()
        clean = SQLiteStore(Path(temp) / "clean")
        clean.ensure()
        copy_logical(source, clean)
        # The SQLite backup API creates a consistent independent file, never a
        # copy of a live main file or a dependency on its journal/WAL/SHM.
        staging.write_bytes(DATABASE, b"")
        with clean.database() as db, closing(sqlite3.connect(staging.path(DATABASE))) as out:
            db.backup(out)
        info = inspect_store(staging, full_validation=False)
        manifest = {
            "format": "garmin-bridge-sqlite",
            "format_version": 2,
            **info,
            "size": staging.path(DATABASE).stat().st_size,
            "sha256": digest_file(staging.path(DATABASE)),
        }
        staging.write_json("manifest.json", manifest)
        verify_snapshot(staging.root)
        staging._fsync_dir(staging.root)
        publish_directory(staging.root, target.root)
        parent._fsync_dir(parent.root)
    return receipt("export", info)


def verify_snapshot(directory):
    store = SQLiteStore(Path(directory))
    require({p.name for p in store.root.iterdir()} == {DATABASE, "manifest.json"})
    manifest = decode(Store.read_bytes(store, "manifest.json"))
    require(type(manifest) is dict and manifest.get("segment_algorithm_version") in ALGORITHMS)
    require(type(manifest.get("size")) is int and 0 < manifest["size"] <= 16 * 1024**3)
    require(c.digest(manifest.get("sha256")))
    require(store.path(DATABASE).stat().st_size == manifest["size"])
    require(digest_file(store.path(DATABASE)) == manifest["sha256"])
    require(type(manifest.get("format_version")) is int and manifest["format_version"] == 2)
    validate_state(store.state(), portable=True)
    info = inspect_store(store, full_validation=False)
    expected = {
        "format": "garmin-bridge-sqlite",
        "format_version": manifest["format_version"],
        **info,
        "segment_algorithm_version": manifest["segment_algorithm_version"],
        "size": store.path(DATABASE).stat().st_size,
        "sha256": manifest["sha256"],
    }
    require(canonical(manifest) == canonical(expected))
    require(digest_file(store.path(DATABASE)) == manifest["sha256"])
    require(Store.read_bytes(store, "manifest.json") == canonical(manifest))
    return receipt("verify", info)


def restore_snapshot(directory, destination):
    source = SQLiteStore(Path(directory))
    source_manifest = Store.read_bytes(source, "manifest.json")
    verify_snapshot(directory)
    target, parent = new_destination(destination, source.root)
    with tempfile.TemporaryDirectory(prefix=".sqlite-restore-", dir=parent.root) as temp:
        staging = SQLiteStore(Path(temp) / "store")
        staging.ensure()
        copy_logical(source, staging)
        info = inspect_store(staging, full_validation=False)
        require(Store.read_bytes(source, "manifest.json") == source_manifest)
        require(digest_file(source.path(DATABASE)) == decode(source_manifest)["sha256"])
        staging._fsync_dir(staging.root)
        publish_directory(staging.root, target.root)
        parent._fsync_dir(parent.root)
    return receipt("restore", info)
