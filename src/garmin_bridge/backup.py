"""Offline, raw-free portable snapshots. No Drive client or authentication data.

Only current referenced artifacts and the authoritative journal are included.
Restore validates everything before touching the destination and only accepts
an empty store. A durable marker gates queries until all files are committed.
"""

from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
import zipfile

from . import contract as c
from .errors import BridgeError
from .jobs import JOB_FILE, gaps, load_job
from .normalize import sha256
from .persistence import mapping, validate_pair
from .storage import Store, canonical, check_private
from .upstream import now

FORMAT = "garmin-bridge-nonlocation"
FORMAT_VERSION = 1
MAX_FILE = 128 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024 * 1024
MAX_MEMBERS = 200_004
MARKER = ".restore-pending.json"


def allowed_path(value):
    return type(value) is str and (
        value in {"index.json", JOB_FILE}
        or re.fullmatch(r"reports/[a-f0-9]{32}/gaps\.json", value) is not None
        or re.fullmatch(r"objects/[1-9][0-9]{0,19}/[0-9a-f]{64}/(?:summary|streams)\.json", value)
        is not None
    )


def require(condition):
    if not condition:
        raise BridgeError("invalid_bundle")


def decode(content):
    try:
        value = json.loads(content)
        # Reject duplicate keys, nonfinite numbers, trailing bytes and hidden
        # noncanonical content. Every package produced here uses this encoding.
        require(canonical(value) == content)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise BridgeError("invalid_bundle") from None


MANIFEST = c.obj(
    {
        "format": c.literal(FORMAT),
        "format_version": c.literal(FORMAT_VERSION),
        "bundle_id": c.JOB_ID,
        "created_at": lambda x: c.iso_time(x, True),
        "schema_version": c.literal(c.SCHEMA_VERSION),
        "normalizer_version": c.ACTIVITY_FIELDS["normalizer_version"],
        "files": mapping(
            allowed_path,
            c.obj({"sha256": c.digest, "size": lambda x: type(x) is int and 0 < x <= MAX_FILE}),
            MAX_MEMBERS - 1,
        ),
    }
)


def snapshot(store):
    state = copy.deepcopy(store.state())
    for entry in state["activities"].values():
        entry["raw_ref"] = entry["raw_sha256"] = None
    job = load_job(store)
    validate_pair(state, job)
    return state, job


def control_files(state, job):
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


def artifact_files(entry, read):
    summary = entry["summary"]
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
        }
    )
    contents = {}
    for kind, path in summary["files"].items():
        data = read(path)
        require(data is not None and sha256(data) == entry["file_hashes"][kind])
        contents[kind] = (path, data, decode(data))
    require(contents["summary"][2] == summary)
    c.project_activity(summary)
    streams = c.validate_streams(contents["streams"][2])
    from .persistence import identity_enabled
    from .fit_details import fill_local_time

    require(identity_enabled(summary) == streams.get("include_identity", False))
    require(summary["coverage"].keys() == streams["units"].keys())
    if summary["provenance"]["input"] == "local_fit_file" and "fit_fields_version" in streams:
        expected_time = {**summary["activity_time"], "local": None, "utc_offset_seconds": None}
        fill_local_time(expected_time, streams["messages"])
        require(summary["activity_time"] == expected_time)
    for key in (
        "schema_version",
        "normalizer_version",
        "provider",
        "source_activity_id",
        "source_digest",
    ):
        require(streams[key] == summary[key])
    for array, field in (
        ("records", "sample_count"),
        ("laps", "lap_count"),
        ("timer_events", "timer_event_count"),
    ):
        require(len(streams[array]) == summary[field])
    for key, coverage in summary["coverage"].items():
        present = sum(row[key] is not None for row in streams["records"])
        require(
            coverage
            == {
                "present": present,
                "missing": len(streams["records"]) - present,
                "total": len(streams["records"]),
            }
        )
    return [(path, data) for path, data, _ in contents.values()]


def package_path(path):
    path = Path(os.path.abspath(path.expanduser()))
    parent = Store(path.parent)
    return parent, parent.path(path.name)


def result(action, manifest, checksum, state, job):
    return {
        "schema_version": c.SCHEMA_VERSION,
        "action": action,
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "bundle_id": manifest["bundle_id"],
        "bundle_sha256": checksum,
        "file_count": len(manifest["files"]),
        "activity_count": len(state["activities"]),
        "gap_count": len(gaps(job)) if job else 0,
        "scan_status": job["status"] if job else "none",
    }


def export_bundle(store, path):
    parent, target = package_path(path)
    require(not target.is_relative_to(store.root))
    with store.lock():
        state, job = snapshot(store)
        parent.ensure()
        if target.exists():
            raise BridgeError("destination_exists")
        fd, temporary = tempfile.mkstemp(prefix=".pending-bundle-", dir=parent.root)
        try:
            with os.fdopen(fd, "w+b") as handle:
                files, total = {}, 0
                with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_STORED) as archive:

                    def add(name, data):
                        nonlocal total
                        total += len(data)
                        require(0 < len(data) <= MAX_FILE and total <= MAX_TOTAL)
                        require(name not in files and len(files) < MAX_MEMBERS - 1)
                        files[name] = {"sha256": sha256(data), "size": len(data)}
                        info = zipfile.ZipInfo(name)
                        info.create_system = 3
                        info.external_attr = (stat.S_IFREG | 0o600) << 16
                        archive.writestr(info, data)

                    for name, value in control_files(state, job).items():
                        add(name, canonical(value))
                    for entry in state["activities"].values():
                        for name, data in artifact_files(entry, store.read_bytes):
                            add(name, data)
                    manifest = {
                        "format": FORMAT,
                        "format_version": FORMAT_VERSION,
                        "bundle_id": uuid.uuid4().hex,
                        "created_at": now(),
                        "schema_version": c.SCHEMA_VERSION,
                        "normalizer_version": c.NORMALIZER_VERSION,
                        "files": files,
                    }
                    require(MANIFEST(manifest))
                    # Manifest is not included in its own content-hash map.
                    info = zipfile.ZipInfo("manifest.json")
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o600) << 16
                    payload = canonical(manifest)
                    require(len(payload) <= MAX_FILE)
                    archive.writestr(info, payload)
                handle.flush()
                os.fsync(handle.fileno())
                require(handle.tell() <= MAX_TOTAL)
                handle.seek(0)
                checksum = hashlib.file_digest(handle, "sha256").hexdigest()
            # Link publishes without overwriting an existing destination, even
            # if another exporter wins the race. The temporary link is removed.
            try:
                os.link(temporary, target)
            except FileExistsError:
                raise BridgeError("destination_exists") from None
            os.unlink(temporary)
            parent._fsync_dir(parent.root)
            return result("export", manifest, checksum, state, job)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


@contextmanager
def open_package(path):
    _, source = package_path(path)
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        check_private(source)
        require(0 < os.fstat(handle.fileno()).st_size <= MAX_TOTAL)
        try:
            with zipfile.ZipFile(handle) as archive:
                infos = archive.infolist()
                require(not archive.comment and 1 <= len(infos) <= MAX_MEMBERS)
                names, size = set(), 0
                for info in infos:
                    name = info.filename
                    size += info.file_size
                    require(
                        (name == "manifest.json" or allowed_path(name))
                        and name == info.orig_filename
                        and name not in names
                        and not info.is_dir()
                        and not info.flag_bits & 1
                        and not info.comment
                        and stat.S_IFMT(info.external_attr >> 16) in (0, stat.S_IFREG)
                        and info.compress_type == zipfile.ZIP_STORED
                        and info.compress_size == info.file_size
                        and 0 < info.file_size <= MAX_FILE
                        and size <= MAX_TOTAL
                    )
                    names.add(name)
                require("manifest.json" in names)
                yield archive, handle
        except (
            zipfile.BadZipFile,
            KeyError,
            ValueError,
            RuntimeError,
            NotImplementedError,
            EOFError,
        ):
            raise BridgeError("invalid_bundle") from None


def inspect_package(archive, handle, staging=None):
    before = os.fstat(handle.fileno())
    manifest = decode(archive.read("manifest.json"))
    require(MANIFEST(manifest))
    require(set(archive.namelist()) == {"manifest.json", *manifest["files"]})

    def read(name):
        require(name in manifest["files"])
        data = archive.read(name)
        expected = manifest["files"][name]
        require(len(data) == expected["size"] and sha256(data) == expected["sha256"])
        return data

    state = decode(read("index.json"))
    job = decode(read(JOB_FILE)) if JOB_FILE in manifest["files"] else None
    validate_pair(state, job)
    controls = control_files(state, job)
    expected = set(controls)
    for entry in state["activities"].values():
        expected.update(entry["summary"]["files"].values())
    require(expected == set(manifest["files"]))
    for name, value in controls.items():
        data = read(name)
        require(decode(data) == value)
        if staging:
            staging.write_bytes(name, data)
    for entry in state["activities"].values():
        for name, data in artifact_files(entry, read):
            if staging:
                staging.write_bytes(name, data)
    handle.seek(0)
    checksum = hashlib.file_digest(handle, "sha256").hexdigest()
    after = os.fstat(handle.fileno())
    require(
        (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        == (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    )
    return manifest, checksum, state, job


def verify_bundle(path):
    with open_package(path) as (archive, handle):
        return result("verify", *inspect_package(archive, handle))


def restore_bundle(store, path):
    parent, source = package_path(path)
    require(not source.is_relative_to(store.root))
    # All archive paths, hashes, schemas, cross-file identities and gaps are
    # validated in private staging before the target is created or changed.
    with tempfile.TemporaryDirectory(prefix=".bridge-restore-", dir=parent.root) as directory:
        staging = Store(Path(directory))
        with open_package(path) as (archive, handle):
            manifest, checksum, state, job = inspect_package(archive, handle, staging)
        with store.lock(write=True, allow_restore=True):
            marker = {"version": FORMAT_VERSION, "bundle_sha256": checksum}
            pending = store.read_json(MARKER)
            if pending is not None:
                require(pending == marker)
            elif any(p.name != ".lock" for p in store.root.iterdir()):
                raise BridgeError("destination_exists")
            else:
                store.write_json(MARKER, marker)
            for name in manifest["files"]:
                if name != "index.json":
                    store.write_bytes(name, staging.read_bytes(name))
            store.write_bytes("index.json", staging.read_bytes("index.json"))
            # No reader or sync can see a partially restored journal/index pair.
            store.path(MARKER).unlink()
            store._fsync_dir(store.root)
        return result("restore", manifest, checksum, state, job)
