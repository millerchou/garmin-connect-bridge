"""Explicit read-only migration and no-replace, self-contained SQLite snapshots."""

import hashlib
import os
import fcntl
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path
import sqlite3
import tempfile

from . import contract as c
from .backup import artifact_files, control_files, decode, inspect_package, open_package, snapshot
from .errors import BridgeError
from .jobs import gaps
from .normalize import sha256
from .persistence import require
from .sanitize_fit import publish_directory
from .sqlite_store import (
    DATABASE,
    STORAGE_VERSION,
    SQLiteStore,
    validate_document,
    ARRAY_LIMITS,
    stream_arrays,
)
from .segments import ALGORITHM, ALGORITHMS
from .storage import Store, canonical, check_private


@contextmanager
def read_lock(store):
    """Lock an existing legacy input without creating or modifying any file."""
    path = store.path(".lock")
    if not path.exists():
        raise BridgeError("unsafe_store")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        check_private(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError("busy") from None
        yield
    finally:
        os.close(fd)


def physical_digest(path):
    # SQLite's creator-library version is the only environment-specific header
    # field. Validate its numeric domain; every other byte must be canonical.
    with path.open("rb") as handle:
        header = bytearray(handle.read(100))
        require(
            len(header) == 100 and 3_000_000 <= int.from_bytes(header[96:100], "big") < 4_000_000
        )
        header[96:100] = bytes(4)
        digest = hashlib.sha256(header)
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()


def digest_file(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def inspect_store(store, *, legacy_manifest=False):
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
            content = store.read_bytes(name)  # closed validation of every document
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
                artifact_files(entry, store.read_bytes)
                counts["revisions"] += 1
            if name.endswith("/streams.json"):
                value = decode(content)
                require(name.replace("/streams.json", "/summary.json") in names)
                if "fit_fields_version" in value:
                    field_versions.add(value["fit_fields_version"])
                    detail_counts["sessions"] += 1
                    detail_counts["identity_revisions"] += value["include_identity"]
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
        artifact_files(entry, store.read_bytes)
    times = [
        e["summary"]["activity_time"]["utc"]
        for e in state["activities"].values()
        if e["summary"]["activity_time"]["utc"]
    ]
    require(not legacy_manifest or not field_versions)
    extension = (
        {}
        if legacy_manifest
        else {
            "fit_fields_versions": sorted(field_versions),
            "fit_counts": detail_counts,
            "identity_included": detail_counts["identity_revisions"] > 0,
        }
    )
    return {
        **extension,
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
    inspect_store(target)


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
        info = inspect_store(staging)
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
    require(type(manifest.get("format_version")) is int and manifest["format_version"] in (1, 2))
    info = inspect_store(store, legacy_manifest=manifest["format_version"] == 1)
    expected = {
        "format": "garmin-bridge-sqlite",
        "format_version": manifest["format_version"],
        **info,
        "segment_algorithm_version": manifest["segment_algorithm_version"],
        "size": store.path(DATABASE).stat().st_size,
        "sha256": digest_file(store.path(DATABASE)),
    }
    require(canonical(manifest) == canonical(expected))
    # Require canonical physical encoding too: hidden freed pages, trailing data,
    # alternate indexes, injected metadata and unused payload cannot travel.
    with tempfile.TemporaryDirectory(prefix=".sqlite-check-", dir=store.root.parent) as temp:
        clean = SQLiteStore(Path(temp) / "clean")
        clean.ensure()
        copy_logical(store, clean)
        candidate = Path(temp) / DATABASE
        with clean.database() as db, closing(sqlite3.connect(candidate)) as out:
            db.backup(out)
        require(physical_digest(candidate) == physical_digest(store.path(DATABASE)))
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
        info = inspect_store(staging)
        require(Store.read_bytes(source, "manifest.json") == source_manifest)
        require(digest_file(source.path(DATABASE)) == decode(source_manifest)["sha256"])
        staging._fsync_dir(staging.root)
        publish_directory(staging.root, target.root)
        parent._fsync_dir(parent.root)
    return receipt("restore", info)


def migrate(source_path, destination, bundle=False):
    source_root = Store(Path(source_path)).root if not bundle else None
    if source_root is not None and Store(source_root).path(DATABASE).exists():
        raise BridgeError("storage_format_conflict")
    target, parent = new_destination(destination, source_root)
    with tempfile.TemporaryDirectory(prefix=".sqlite-migrate-", dir=parent.root) as temp:
        source = Store(Path(source_path)) if not bundle else Store(Path(temp) / "legacy")
        if bundle:
            source.ensure()
            with open_package(Path(source_path)) as (archive, handle):
                inspect_package(archive, handle, source)
        with nullcontext() if bundle else read_lock(source):
            require(source.read_bytes("index.json") is not None)
            state, job = snapshot(source)
            check_report(source, state, job)
            originals = {"index.json": source.read_bytes("index.json")}

            values = control_files(state, job)
            for path in sorted(source.root.glob("objects/*/*/*.json")):
                name = path.relative_to(source.root).as_posix()
                originals[name] = source.read_bytes(name)
                values[name] = decode(originals[name])
            for path in sorted(source.root.glob("jobs/*.json")):
                name = path.relative_to(source.root).as_posix()
                originals[name] = source.read_bytes(name)
                values[name] = decode(originals[name])
            for name in values:
                if name not in originals:
                    originals[name] = source.read_bytes(name)
            for name, content in originals.items():
                if content is not None:
                    validate_document(name, decode(content))
            staging = SQLiteStore(Path(temp) / "store")
            staging.ensure()
            for name in sorted(values, key=lambda n: n == "index.json"):
                staging.write_json(name, values[name])
            info = inspect_store(staging)
            # Exact canonical bytes compare all values/types/order/null/zero; only
            # raw references and reconstructed gap reports intentionally differ.
            for name, value in values.items():
                require(staging.read_bytes(name) == canonical(value))
            require(all(source.read_bytes(n) == data for n, data in originals.items()))
            staging._fsync_dir(staging.root)
            publish_directory(staging.root, target.root)
            parent._fsync_dir(parent.root)
    return receipt("migrate", info)
