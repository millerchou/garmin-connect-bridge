"""Private no-replace, self-contained SQLite snapshots."""

import hashlib
from contextlib import closing, contextmanager
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
from .privacy import diagnosed, located, deferred_report
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


@located(DATABASE, "documents")
def inspect_store(store, *, full_validation=True):
    require(store.read_bytes("index.json") is not None, "sqlite_transfer.inspect_store.1", "name")
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
        require(
            db.execute("PRAGMA integrity_check").fetchall() == [("ok",)],
            "sqlite_transfer.inspect_store.2",
            "integrity_check",
            table="sqlite_schema",
        )
        names = [r[0] for r in db.execute("SELECT name FROM documents ORDER BY name")]
        for name in names:
            require(document_kind(name) is not None, "sqlite_transfer.inspect_store.3", "name")
            content = store.read_bytes(name)
            value = decode(content)
            if (
                full_validation
                or not name.endswith("/streams.json")
                # Few-distinct checks need every raw row: spot sampling a valid
                # varying counter would make it look like a constant identity.
                or value.get("fit_fields_version") == "1.1"
            ):
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
                require(
                    stream_content is not None, "sqlite_transfer.inspect_store.4", "files.streams"
                )
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
                require(
                    name.replace("/streams.json", "/summary.json") in names,
                    "sqlite_transfer.inspect_store.5",
                    "name",
                )
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
            == 0,
            "sqlite_transfer.inspect_store.6",
            "name",
            table="samples",
        )
        require(
            all(
                r[0] in ARRAY_LIMITS and r[1].endswith("/streams.json")
                for r in db.execute("SELECT DISTINCT kind,name FROM samples")
            ),
            "sqlite_transfer.inspect_store.7",
            "kind,name",
            table="samples",
        )
        from .segments import validate_segments

        validate_segments(db, store)
    for entry in state["activities"].values():
        if full_validation:
            artifact_files(entry, store.read_bytes)
        else:
            summary_content = store.read_bytes(entry["summary"]["files"]["summary"])
            require(summary_content is not None, "sqlite_transfer.inspect_store.8", "files.summary")
            require(
                decode(summary_content) == entry["summary"],
                "sqlite_transfer.inspect_store.9",
                "summary",
            )
            require(
                sha256(summary_content) == entry["file_hashes"]["summary"],
                "sqlite_transfer.inspect_store.10",
                "summary,file_hashes",
            )
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
        require(
            store.read_bytes(name) == canonical(control_files(state, job)[name]),
            "sqlite_transfer.check_report.1",
            "gaps",
        )


def copy_logical(source, target):
    """Validated records only; excludes auth, originals, private paths and free pages."""
    if source._absent_snapshot or not source.path(DATABASE).exists():
        from .storage import empty_state

        require(source.state() == empty_state(), "sqlite_transfer.copy_logical.1", "index.json")
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
    if source_root is not None and target.root.is_relative_to(source_root):
        raise BridgeError("unsafe_store")
    if target.root.exists():
        raise BridgeError("destination_exists")
    parent = Store(target.root.parent)
    parent.ensure()
    return target, parent


@diagnosed(source=True, excluded_args=(1,))
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
        with source.read_snapshot():
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


def snapshot_members(store):
    # SQLite sidecars indicate an active/recoverable database, not a privacy
    # contract violation. Never recover or ignore them in a portable snapshot.
    names = {p.name for p in store.root.iterdir()}
    if names & {DATABASE + suffix for suffix in ("-journal", "-wal", "-shm")}:
        raise BridgeError("busy")
    require(
        names == {DATABASE, "manifest.json"},
        "snapshot.members",
        "members",
        file="manifest.json",
        table="snapshot",
    )


@contextmanager
def snapshot_read(store):
    """Immutable SQLite reads need filesystem guards, including on failure."""

    def signature():
        # Check sidecars before checksum/schema failures can mask a writer.
        for suffix in ("-journal", "-wal", "-shm"):
            path = store.root / (DATABASE + suffix)
            if path.exists() or path.is_symlink():
                raise BridgeError("busy") from None
        result = []
        for name in (DATABASE, "manifest.json"):
            info = store.path(name).stat()
            result.append(
                (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            )
        return result

    with deferred_report():
        snapshot_members(store)
        before = signature()
        try:
            yield
        finally:
            try:
                after = signature()
            except FileNotFoundError:
                raise BridgeError("busy") from None
            if before != after:
                raise BridgeError("busy") from None
            snapshot_members(store)


@diagnosed(excluded_args=(0,))
def verify_snapshot(directory):
    store = SQLiteStore(Path(directory))
    with snapshot_read(store):
        return verify_contents(store)


@located("manifest.json", "manifest")
def verify_contents(store):
    manifest = decode(Store.read_bytes(store, "manifest.json"))
    require(
        type(manifest) is dict and manifest.get("segment_algorithm_version") in ALGORITHMS,
        "snapshot.algorithm_version",
        "segment_algorithm_version",
    )
    require(
        type(manifest.get("size")) is int and 0 < manifest["size"] <= 16 * 1024**3,
        "snapshot.size_format",
        "size",
    )
    require(c.digest(manifest.get("sha256")), "snapshot.checksum_format", "sha256")
    require(
        store.path(DATABASE).stat().st_size == manifest["size"],
        "snapshot.size",
        "size",
    )
    require(
        digest_file(store.path(DATABASE)) == manifest["sha256"],
        "snapshot.checksum",
        "sha256",
    )
    require(
        type(manifest.get("format_version")) is int and manifest["format_version"] == 2,
        "snapshot.format_version",
        "format_version",
    )
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
    require(canonical(manifest) == canonical(expected), "snapshot.manifest_match", "manifest")
    require(
        digest_file(store.path(DATABASE)) == manifest["sha256"],
        "snapshot.checksum_changed",
        "sha256",
    )
    require(
        Store.read_bytes(store, "manifest.json") == canonical(manifest),
        "snapshot.manifest_changed",
        "manifest",
    )
    snapshot_members(store)
    return receipt("verify", info)


@diagnosed(root_arg=1, excluded_args=(0,))
def restore_snapshot(directory, destination):
    source = SQLiteStore(Path(directory))
    with snapshot_read(source):
        source_manifest = Store.read_bytes(source, "manifest.json")
        verify_contents(source)
    target, parent = new_destination(destination, source.root)
    with tempfile.TemporaryDirectory(prefix=".sqlite-restore-", dir=parent.root) as temp:
        staging = SQLiteStore(Path(temp) / "store")
        staging.ensure()
        with snapshot_read(source):
            copy_logical(source, staging)
            require(
                Store.read_bytes(source, "manifest.json") == source_manifest,
                "sqlite_transfer.restore_snapshot.1",
                "manifest",
                file="manifest.json",
                table="manifest",
            )
            require(
                digest_file(source.path(DATABASE)) == decode(source_manifest)["sha256"],
                "sqlite_transfer.restore_snapshot.2",
                "sha256",
                file="manifest.json",
                table="manifest",
            )
        info = inspect_store(staging, full_validation=False)
        staging._fsync_dir(staging.root)
        publish_directory(staging.root, target.root)
        parent._fsync_dir(parent.root)
    return receipt("restore", info)
