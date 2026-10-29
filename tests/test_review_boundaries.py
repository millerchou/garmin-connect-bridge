"""Review regressions, using exclusively invented in-memory activities."""

import copy
import json
from unittest.mock import Mock

import pytest

from garmin_bridge import normalize, segments, sqlite_transfer as transfer, sync
from garmin_bridge.errors import BridgeError
from garmin_bridge.jobs import load_job
from garmin_bridge.sqlite_store import SQLiteStore
from garmin_bridge.storage import Store, canonical
from .synthetic import Source, activity
from .test_sqlite import put
from .test_sync import run
from .test_segments import populate, stamp


@pytest.fixture
def database(tmp_path):
    return SQLiteStore(tmp_path / "database")


@pytest.fixture
def straight():
    return [(i * 11, 0) for i in range(31)]


def defined(database, tmp_path, straight):
    populate(database, tmp_path, straight)
    with database.lock(write=True):
        definition = segments.define(database, "1001", stamp(0), stamp(60))
        segments.refresh(database, definition["id"])
    return definition


def test_truncated_path_is_not_a_completed_effort(database, tmp_path, straight):
    definition = defined(database, tmp_path, straight)
    populate(database, tmp_path, straight[1:-1], "1002")
    with database.lock(write=True):
        segments.refresh(database, definition["id"])
    result = [
        r
        for r in segments.history(database, definition["id"])["results"]
        if r["activity_id"] == "1002"
    ][0]
    assert result["status"] == "insufficient_data"
    assert result["effort"] is None


@pytest.mark.parametrize("mutation", ["outside_time", "wrong_mean", "wrong_count", "overlap"])
def test_efforts_must_agree_with_revision_samples(database, tmp_path, straight, mutation):
    defined(database, tmp_path, straight)
    with database.database(write=True) as db:
        payload = json.loads(db.execute("SELECT payload FROM efforts").fetchone()[0])
        effort = payload["efforts"][0]
        if mutation == "outside_time":
            effort["start"], effort["end"] = stamp(1000), stamp(1060)
        elif mutation == "wrong_mean":
            effort["power_mean"] += 1
        elif mutation == "wrong_count":
            effort["sample_count"] += 1
        else:
            payload["efforts"].append({**effort, "ordinal": 2})
        db.execute("UPDATE efforts SET payload=?", (canonical(payload),))
    with pytest.raises(BridgeError):
        transfer.export_snapshot(database, tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()


def test_migration_rejects_lost_catalog(tmp_path):
    old = Store(tmp_path / "old")
    put(old, tmp_path)
    old.path("index.json").unlink()
    with pytest.raises(BridgeError):
        transfer.migrate(old.root, tmp_path / "target")
    assert not (tmp_path / "target").exists()


def test_snapshot_rejects_lost_catalog(database, tmp_path):
    put(database, tmp_path)
    with database.database(write=True) as db:
        db.execute("DELETE FROM documents WHERE name='index.json'")
    with pytest.raises(BridgeError):
        transfer.export_snapshot(database, tmp_path / "snapshot")


def test_valid_gap_report_must_agree_with_job(database, tmp_path):
    run(database, Source([activity(), activity(1002)], downloads={"1002": b"broken-synthetic"}))
    job = load_job(database)
    assert job["report_valid"]
    name = f"reports/{job['id']}/gaps.json"
    bad = database.read_json(name)
    assert bad["gaps"]
    bad["gaps"] = []
    with database.lock(write=True):
        database.write_json(name, bad)
    with pytest.raises(BridgeError):
        transfer.export_snapshot(database, tmp_path / "snapshot")


def test_reverting_source_reuses_immutable_revision(database, tmp_path, monkeypatch):
    spy = Mock(wraps=normalize.normalize)
    monkeypatch.setattr(normalize, "normalize", spy)
    monkeypatch.setattr(sync, "now", lambda: stamp(100))
    put(database, tmp_path, power=200)
    first = copy.deepcopy(database.state()["activities"]["1001"]["summary"])
    monkeypatch.setattr(sync, "now", lambda: stamp(200))
    put(database, tmp_path, power=300)
    monkeypatch.setattr(sync, "now", lambda: stamp(300))
    assert put(database, tmp_path, power=200)["status"] == "updated"
    assert database.state()["activities"]["1001"]["summary"] == first
    assert transfer.inspect_store(database)["counts"]["revisions"] == 2
    assert spy.call_count == 2


def test_same_definition_is_idempotent(database, tmp_path, straight):
    definition = defined(database, tmp_path, straight)
    before = segments.history(database, definition["id"])
    with database.lock(write=True):
        again = segments.define(database, "1001", stamp(0), stamp(60), definition["id"])
    assert again == definition
    assert segments.history(database, definition["id"]) == before


@pytest.mark.parametrize("operation", ["migrate", "restore", "export"])
def test_destinations_cannot_mutate_inputs(database, tmp_path, operation):
    if operation == "migrate":
        source = Store(tmp_path / "old")
        put(source, tmp_path)

        def call(target):
            return transfer.migrate(source.root, target)
    elif operation == "restore":
        put(database, tmp_path)
        transfer.export_snapshot(database, tmp_path / "snapshot")
        source = SQLiteStore(tmp_path / "snapshot")

        def call(target):
            return transfer.restore_snapshot(source.root, target)
    else:
        put(database, tmp_path)
        source = database

        def call(target):
            return transfer.export_snapshot(source, target)

    before = {
        p.relative_to(source.root).as_posix(): p.read_bytes()
        for p in source.root.rglob("*")
        if p.is_file()
    }
    with pytest.raises(BridgeError):
        call(source.root / "nested" / "result")
    assert not (source.root / "nested").exists()
    assert before == {
        p.relative_to(source.root).as_posix(): p.read_bytes()
        for p in source.root.rglob("*")
        if p.is_file()
    }


def test_stale_algorithm_is_portable_but_never_returned_as_current(database, tmp_path, straight):
    definition = defined(database, tmp_path, straight)
    with database.database(write=True) as db:
        for table in ("segments", "efforts"):
            value = json.loads(db.execute(f"SELECT payload FROM {table}").fetchone()[0])
            value["algorithm_version"] = "1.0.0"
            db.execute(f"UPDATE {table} SET payload=?", (canonical(value),))
    assert segments.history(database, definition["id"])["cached_activity_count"] == 0
    path = tmp_path / "snapshot"
    transfer.export_snapshot(database, path)
    manifest = json.loads((path / "manifest.json").read_bytes())
    manifest["segment_algorithm_version"] = "1.0.0"
    (path / "manifest.json").write_bytes(canonical(manifest))
    assert transfer.verify_snapshot(path)["verified"]
    transfer.restore_snapshot(path, tmp_path / "restored")
    assert (
        segments.history(SQLiteStore(tmp_path / "restored"), definition["id"])[
            "cached_activity_count"
        ]
        == 0
    )
    with database.lock(write=True):
        assert segments.refresh(database, definition["id"]) == 1
    result = segments.history(database, definition["id"])
    assert result["definition"]["algorithm_version"] == segments.ALGORITHM == "1.0.1"
    assert result["results"][0]["algorithm_version"] == segments.ALGORITHM
    assert result["results"][0]["normalizer_version"] == "1.2.1"


@pytest.mark.parametrize("operation", ["list", "backup", "import-fit"])
def test_legacy_flag_cannot_silently_ignore_sqlite(database, tmp_path, capsys, operation):
    from garmin_bridge import cli
    from .test_import_fit import private_file
    from .synthetic import fit_bytes

    put(database, tmp_path)
    before = database.state()
    extra = ["list"]
    if operation == "backup":
        extra = ["backup", "export", "--file", str(tmp_path / "wrong.zip")]
    elif operation == "import-fit":
        extra = [
            "import-fit",
            "--file",
            str(private_file(tmp_path, fit_bytes(power=300))),
            "--activity-id",
            "1001",
        ]
    assert cli.main(["--legacy-store", "--data-dir", str(database.root), *extra]) == 2
    capsys.readouterr()
    assert database.state() == before
    assert not (tmp_path / "wrong.zip").exists()
    assert not (database.root / "index.json").exists()


def test_historical_local_provenance_cannot_drop_unverified_flag(database, tmp_path):
    put(database, tmp_path)
    old = database.state()["activities"]["1001"]["summary"]
    put(database, tmp_path, power=300)
    with database.database(write=True) as db:
        name = old["files"]["summary"]
        value = json.loads(
            db.execute("SELECT payload FROM documents WHERE name=?", (name,)).fetchone()[0]
        )
        value["quality_flags"].remove("unverified_activity_id")
        db.execute("UPDATE documents SET payload=? WHERE name=?", (canonical(value), name))
    with pytest.raises(BridgeError):
        transfer.export_snapshot(database, tmp_path / "snapshot")


@pytest.mark.parametrize("key", ["format_version", "storage_version", "size"])
def test_manifest_rejects_bool_and_float_equivalents(database, tmp_path, key):
    put(database, tmp_path)
    path = tmp_path / "snapshot"
    transfer.export_snapshot(database, path)
    manifest = json.loads((path / "manifest.json").read_bytes())
    manifest[key] = True if key != "size" else float(manifest[key])
    (path / "manifest.json").write_bytes(canonical(manifest))
    with pytest.raises(BridgeError):
        transfer.verify_snapshot(path)


def test_declared_stale_gap_report_is_rebuilt_without_modifying_source(database, tmp_path):
    from garmin_bridge.jobs import load_job

    run(database, Source([activity(), activity(1002)], downloads={"1002": b"broken-synthetic"}))
    job = load_job(database)
    report_name = f"reports/{job['id']}/gaps.json"
    stale = database.read_json(report_name)
    stale["gaps"] = []
    job["report_valid"] = False
    with database.lock(write=True):
        database.write_json(report_name, stale)
        database.write_json("sync-job.json", job)
    transfer.export_snapshot(database, tmp_path / "snapshot")
    transfer.restore_snapshot(tmp_path / "snapshot", tmp_path / "restored")
    assert database.read_json(report_name) == stale
    assert len(SQLiteStore(tmp_path / "restored").read_json(report_name)["gaps"]) == 1


def test_report_filename_must_match_job_identity(database, tmp_path):
    run(database, Source())
    job = load_job(database)
    report = database.read_json(f"reports/{job['id']}/gaps.json")
    with database.lock(write=True), pytest.raises(BridgeError):
        database.write_json("reports/" + "f" * 32 + "/gaps.json", report)


def test_mixed_storage_is_rejected_instead_of_choosing_a_catalog(database, tmp_path):
    from garmin_bridge.storage import empty_state
    from garmin_bridge.sqlite_store import open_store

    put(database, tmp_path)
    # An owner manually creates an incompatible second catalog. Never merge or delete it.
    (database.root / "index.json").write_bytes(canonical(empty_state()))
    (database.root / "index.json").chmod(0o600)
    with pytest.raises(BridgeError):
        open_store(database.root).state()
    with pytest.raises(BridgeError):
        transfer.migrate(database.root, tmp_path / "target")
    assert (database.root / "index.json").exists()


def test_location_reader_does_not_access_unreviewed_or_developer_values(
    database, tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from datetime import datetime, timezone

    put(database, tmp_path)
    entry = database.state()["activities"]["1001"]

    class Field:
        def __init__(self, name, value=None, developer=False, forbidden=False):
            self.name = name
            self.field_def = SimpleNamespace(is_dev=developer)
            self._value, self.forbidden = value, forbidden

        @property
        def value(self):
            assert not self.forbidden, "unreviewed value was read"
            return self._value

    class Frame:
        name = "record"
        fields = [
            Field("timestamp", datetime(2025, 1, 2, tzinfo=timezone.utc)),
            Field("position_lat", 0),
            Field("position_long", 0),
            Field("position_lat", developer=True, forbidden=True),
            Field("serial_number", forbidden=True),
            Field("power", forbidden=True),
        ]

    class Reader:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return iter([Frame()])

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(segments.fitdecode, "FitReader", Reader)
    monkeypatch.setattr(segments.fitdecode, "FitDataMessage", Frame)
    assert len(segments.locations(database, entry)) == 1


def test_legacy_query_cannot_create_lock_inside_snapshot(database, tmp_path, capsys):
    from garmin_bridge import cli

    put(database, tmp_path)
    path = tmp_path / "snapshot"
    transfer.export_snapshot(database, path)
    before = {p.name: p.read_bytes() for p in path.iterdir()}
    assert cli.main(["--legacy-store", "--data-dir", str(path), "list"]) == 2
    capsys.readouterr()
    assert before == {p.name: p.read_bytes() for p in path.iterdir()}
    assert transfer.verify_snapshot(path)["verified"]


def test_cached_api_revision_must_match_downloaded_fit_hash(database):
    from .synthetic import fit_bytes

    run(database, Source())
    first = database.state()["activities"]["1001"]["summary"]
    run(database, Source(downloads={"1001": fit_bytes(power=300)}))
    previous = copy.deepcopy(database.state()["activities"]["1001"])
    with database.database(write=True) as db:
        value = {**first, "fit_sha256": "0" * 64}
        db.execute(
            "UPDATE documents SET payload=? WHERE name=?",
            (canonical(value), first["files"]["summary"]),
        )
    result = run(database, Source())
    assert not result["complete"]
    assert database.state()["activities"]["1001"] == previous
