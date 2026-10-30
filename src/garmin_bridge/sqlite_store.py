"""SQLite primary storage; document references are logical identifiers.

Only canonical, closed-contract documents enter this database. Ordered rows store
one record/lap/event each, including duplicates and nulls; no JSON stream file is
written. JSON cells preserve numeric types and signed zero exactly.
"""

from contextlib import closing, contextmanager
import re
import fcntl
import os
import tempfile
import sqlite3

from . import contract as c
from .errors import BridgeError
from .persistence import require, validate_job, validate_state, validate_summary_provenance
from .storage import Store, canonical, check_private, decode, empty_state

DATABASE = "activities.sqlite"
STORAGE_VERSION = 1
# The physical schema already stores typed ordered JSON rows. New detail kinds
# require no ALTER TABLE or rewriting of immutable historical revisions.
ARRAY_LIMITS = {
    "records": 500_000,
    "laps": 10_000,
    "timer_events": 100_000,
    "events": 100_000,
    "messages": 100_000,
}


def stream_arrays(value):
    return (
        tuple(ARRAY_LIMITS)
        if "fit_fields_version" in value
        else ("records", "laps", "timer_events")
    )


DDL = (
    "CREATE TABLE documents (name TEXT PRIMARY KEY, payload BLOB NOT NULL) WITHOUT ROWID",
    "CREATE TABLE samples (name TEXT NOT NULL, kind TEXT NOT NULL, ordinal INTEGER NOT NULL, payload BLOB NOT NULL, PRIMARY KEY(name,kind,ordinal)) WITHOUT ROWID",
    "CREATE TABLE segments (id TEXT PRIMARY KEY, payload BLOB NOT NULL) WITHOUT ROWID",
    "CREATE TABLE efforts (segment TEXT NOT NULL, activity TEXT NOT NULL, payload BLOB NOT NULL, PRIMARY KEY(segment,activity)) WITHOUT ROWID",
)
OBJECT = re.compile(r"objects/[1-9][0-9]{0,19}/[a-f0-9]{64}/(summary|streams)\.json")


def document_kind(name):
    if name == "index.json":
        return "state"
    if name == "sync-job.json" or re.fullmatch(r"jobs/[a-f0-9]{32}\.json", name):
        return "job"
    if re.fullmatch(r"reports/[a-f0-9]{32}/gaps\.json", name):
        return "gaps"
    match = OBJECT.fullmatch(name)
    return match[1] if match else None


def validate_document(name, value):
    kind = document_kind(name)
    if kind == "state":
        validate_state(value)
    elif kind == "job":
        validate_job(value)
        require(name == "sync-job.json" or name == f"jobs/{value['id']}.json")
    elif kind == "gaps":
        c.validate_gap_report(value)
        require(name == f"reports/{value['job_id']}/gaps.json")
    elif kind == "summary":
        require(c.ACTIVITY(value))
        c.project_activity(value)
        validate_summary_provenance(value)
        require(value["files"]["summary"] == name)
    elif kind == "streams":
        c.validate_streams(value)
        require(name.split("/")[1] == value["source_activity_id"])
    else:
        raise BridgeError("privacy_failed")
    return value


def connect(path, write=False, immutable=False):
    require(not (write and immutable))
    uri = path.as_uri() + ("?mode=rw" if write else "?mode=ro")
    db = sqlite3.connect(uri + ("&immutable=1" if immutable else ""), uri=True)
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 128 * 1024 * 1024)
    db.execute("PRAGMA trusted_schema=OFF")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=0")
    if not write:
        db.execute("PRAGMA query_only=ON")
    else:
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        db.execute(f"PRAGMA max_page_count={16 * 1024**3 // page_size}")
    return db


def check_schema(db):
    rows = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY name").fetchall()
    require(rows == sorted(("table", sql.split()[2], sql.split()[2], sql) for sql in DDL))
    require(db.execute("PRAGMA user_version").fetchone()[0] == STORAGE_VERSION)


class SQLiteStore(Store):
    @contextmanager
    def lock(self, write=False):
        # A portable snapshot is immutable, even for ordinary query locks.
        if self.path("manifest.json").exists():
            if write:
                raise BridgeError("unsafe_store")
            fd = os.open(self.path(DATABASE), os.O_RDONLY | os.O_NOFOLLOW)
            try:
                check_private(self.path(DATABASE))
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise BridgeError("busy") from None
                yield
            finally:
                os.close(fd)
            return
        with super().lock(write=write):
            yield

    def _database(self, create=False):
        path = self.path(DATABASE)
        if path.exists() and self.path("index.json").exists():
            raise BridgeError("storage_format_conflict")
        portable = self.path("manifest.json").exists()
        require(not (portable and create))
        if create and not path.exists():
            # Never mix a file catalog with SQLite documents.
            if super().read_bytes("index.json") is not None:
                raise BridgeError("storage_format_conflict")
            fd, name = tempfile.mkstemp(prefix=".sqlite-init-", dir=self.root)
            os.close(fd)
            try:
                with closing(sqlite3.connect(name)) as db, db:
                    db.execute("PRAGMA page_size=4096")
                    db.execute("PRAGMA auto_vacuum=NONE")
                    db.execute("PRAGMA user_version=1")
                    for sql in DDL:
                        db.execute(sql)
                with open(name, "rb") as handle:
                    os.fsync(handle.fileno())
                os.link(name, path)
                os.unlink(name)
                self._fsync_dir(self.root)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = self.path(DATABASE + suffix)
            require(not (portable and sidecar.exists()))
        if path.exists():
            require(0 < path.stat().st_size <= 16 * 1024**3)
        return path

    @contextmanager
    def database(self, write=False):
        if getattr(self, "_transaction", None) is not None:
            yield self._transaction
            return
        path = self._database(create=write)
        try:
            # Roll back a hot rollback journal left by a killed writer. This is
            # SQLite recovery of the last commit, never a schema/data migration.
            if not write and self.path(DATABASE + "-journal").exists():
                recovery = connect(path, True)
                try:
                    recovery.execute("SELECT count(*) FROM sqlite_schema").fetchone()
                finally:
                    recovery.close()
            db = connect(path, write, immutable=self.path("manifest.json").exists())
        except sqlite3.Error:
            raise BridgeError("storage_failed") from None
        try:
            check_schema(db)
            with db:
                yield db
        except sqlite3.Error:
            raise BridgeError("storage_failed") from None
        finally:
            db.close()

    @contextmanager
    def atomic(self):
        with self.database(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            self._transaction = db
            try:
                yield
            finally:
                self._transaction = None

    def publish_activity(self, streams, summary, committed):
        with self.atomic():
            self.write_json(summary["files"]["streams"], streams)
            self.write_json(summary["files"]["summary"], summary)
            self.write_json("index.json", committed)

    def revision_artifacts(self, source_id, digest, fit_hash):
        """Reuse an existing immutable revision when a source correction is reverted."""
        from .persistence import artifact_files
        from .normalize import sha256

        revision = sha256(canonical([digest, c.NORMALIZER_VERSION, c.SCHEMA_VERSION]))
        prefix = f"objects/{source_id}/{revision}"
        summary_data = self.read_bytes(f"{prefix}/summary.json")
        if summary_data is None:
            return None
        stream_data = self.read_bytes(f"{prefix}/streams.json")
        require(stream_data is not None)
        summary = decode(summary_data)
        require(summary["fit_sha256"] == fit_hash and summary["source_digest"] == digest)
        require(summary["normalizer_version"] == c.NORMALIZER_VERSION)
        artifact_files(
            {
                "summary": summary,
                "file_hashes": {
                    "summary": sha256(summary_data),
                    "streams": sha256(stream_data),
                },
            },
            self.read_bytes,
        )
        return summary, decode(stream_data)

    def preview_state(self):
        """Read one committed catalog without the bridge lock or journal recovery."""
        path = self._database()
        if not path.exists():
            if self.path("index.json").exists():
                raise BridgeError("storage_format_conflict")
            return empty_state()
        # Ordinary readers can recover journals. A preview must never write,
        # including SQLite's recovery or WAL shared-memory bookkeeping.
        if any(self.path(DATABASE + suffix).exists() for suffix in ("-journal", "-wal", "-shm")):
            raise BridgeError("busy")
        # A closed WAL database may have no sidecars yet. SQLite can create
        # them even with mode=ro; inspect its read/write format before opening.
        with path.open("rb") as handle:
            header = handle.read(20)
        if header[18:20] == b"\x02\x02":
            raise BridgeError("busy")
        if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
            raise BridgeError("storage_failed")
        try:
            with closing(connect(path)) as db:
                db.execute("BEGIN")
                check_schema(db)
                row = db.execute("SELECT payload FROM documents WHERE name='index.json'").fetchone()
                if row is None:
                    require(
                        db.execute(
                            "SELECT 1 FROM documents WHERE name LIKE 'objects/%' LIMIT 1"
                        ).fetchone()
                        is None
                    )
                    return empty_state()
                return validate_state(decode(row[0]))
        except sqlite3.Error:
            raise BridgeError("storage_failed") from None

    def state(self):
        value = self.read_json("index.json")
        value = empty_state() if value is None else validate_state(value)
        if not self._absent_snapshot and self.path(DATABASE).exists():
            with self.database() as db:
                # A first scan can checkpoint its job before its first index.
                # Published activity objects, however, require a committed index.
                indexed = db.execute("SELECT 1 FROM documents WHERE name='index.json'").fetchone()
                if indexed is None:
                    require(
                        db.execute(
                            "SELECT 1 FROM documents WHERE name LIKE 'objects/%' LIMIT 1"
                        ).fetchone()
                        is None
                    )
        return value

    def read_bytes(self, relative, limit=None):
        kind = document_kind(relative)
        if not kind:
            return super().read_bytes(relative, limit)
        if self._absent_snapshot or not self.path(DATABASE).exists():
            if super().read_bytes("index.json") is not None:
                raise BridgeError("storage_format_conflict")
            return None
        with self.database() as db:
            row = db.execute("SELECT payload FROM documents WHERE name=?", (relative,)).fetchone()
            if row is None:
                return None
            value = decode(row[0])
            require(type(value) is dict)
            if kind == "streams":
                require(not (ARRAY_LIMITS.keys() & value.keys()))
                arrays = stream_arrays(value)
                require(
                    all(
                        row[0] in arrays
                        for row in db.execute(
                            "SELECT DISTINCT kind FROM samples WHERE name=?", (relative,)
                        )
                    )
                )
                for array in arrays:
                    count, size = db.execute(
                        "SELECT count(*),max(length(payload)) FROM samples WHERE name=? AND kind=?",
                        (relative, array),
                    ).fetchone()
                    require(count <= ARRAY_LIMITS[array])
                    require(size is None or size <= 32_768)
                    rows = db.execute(
                        "SELECT ordinal,payload FROM samples WHERE name=? AND kind=? ORDER BY ordinal",
                        (relative, array),
                    ).fetchall()
                    require(all(type(r[0]) is int and r[0] == i for i, r in enumerate(rows)))
                    value[array] = [decode(r[1]) for r in rows]
            content = canonical(value)
            if limit is not None and len(content) > limit:
                raise BridgeError("storage_failed")
            return content

    def write_bytes(self, relative, content):
        kind = document_kind(relative)
        if not kind:
            return super().write_bytes(relative, content)
        value = validate_document(relative, decode(content))
        with self.database(write=True) as db:
            if kind in {"streams", "summary"}:
                existing = db.execute(
                    "SELECT payload FROM documents WHERE name=?", (relative,)
                ).fetchone()
                if existing is not None:
                    require(self.read_bytes(relative) == content)
                    return
            if kind == "streams":
                metadata = {k: v for k, v in value.items() if k not in ARRAY_LIMITS}
                for array in stream_arrays(value):
                    db.executemany(
                        "INSERT INTO samples VALUES (?,?,?,?)",
                        (
                            (relative, array, i, canonical(row))
                            for i, row in enumerate(value[array])
                        ),
                    )
                content = canonical(metadata)
            if kind == "state":
                # Commit point: all referenced immutable data must already exist.
                from .persistence import artifact_files

                previous = db.execute(
                    "SELECT payload FROM documents WHERE name='index.json'"
                ).fetchone()
                old_entries = decode(previous[0])["activities"] if previous else {}
                for ident, entry in value["activities"].items():
                    if old_entries.get(ident) != entry:
                        artifact_files(entry, self.read_bytes)
            db.execute("INSERT OR REPLACE INTO documents VALUES (?,?)", (relative, content))

    def has_artifacts(self, summary):
        return all(self.read_bytes(ref) is not None for ref in summary["files"].values())
