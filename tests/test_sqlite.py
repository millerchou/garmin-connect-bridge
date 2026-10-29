"""Synthetic-only primary-store, migration and portable snapshot regression."""

import copy
import json
import sqlite3
from unittest.mock import Mock

import pytest

from garmin_bridge import cli, normalize, segments, sync, sqlite_transfer as transfer
from garmin_bridge.errors import BridgeError
from garmin_bridge.import_fit import import_file
from garmin_bridge.sqlite_store import DATABASE, SQLiteStore
from garmin_bridge.storage import canonical
from .synthetic import Source, activity, fit_bytes
from .test_import_fit import private_file
from .test_sync import run


@pytest.fixture
def database(tmp_path):
    return SQLiteStore(tmp_path / "sqlite")


def put(store, tmp_path, **kwargs):
    path = private_file(tmp_path, fit_bytes(**kwargs))
    with store.lock(write=True):
        return import_file(store, path, "1001")


def test_sqlite_primary_fidelity_idempotence_revisions(database, tmp_path, monkeypatch):
    spy = Mock(wraps=normalize.normalize)
    monkeypatch.setattr(normalize, "normalize", spy)
    put(database, tmp_path, count=50, power=0)
    first = copy.deepcopy(database.state())
    summary = first["activities"]["1001"]["summary"]
    streams = database.read_json(summary["files"]["streams"])
    assert streams["records"][0]["power"] == 0
    assert streams["records"][1]["power"] is None
    assert len(streams["records"]) == 50
    assert not list(database.root.glob("objects/**/*.json"))
    assert not database.path("index.json").exists()
    assert put(database, tmp_path, count=50, power=0)["status"] == "unchanged"
    assert spy.call_count == 1
    put(database, tmp_path, count=50, power=251)
    assert database.read_json(summary["files"]["streams"]) == streams
    assert transfer.inspect_store(database)["counts"] == {
        "activities": 1,
        "revisions": 2,
        "records": 100,
        "laps": 2,
        "timer_events": 4,
    }


def test_sqlite_api_protection_and_watermarks(database, tmp_path):
    run(database, Source())
    prior = copy.deepcopy(database.state())
    path = private_file(tmp_path, fit_bytes(power=333))
    with database.lock(write=True), pytest.raises(BridgeError, match="conflicts"):
        import_file(database, path, "1001")
    assert database.state() == prior
    result = run(database, Source([activity(), activity(1002)]))
    assert result["complete"] and len(database.state()["activities"]) == 2


def test_failed_transaction_preserves_revision(database, tmp_path, monkeypatch):
    put(database, tmp_path)
    prior = database.state()
    original = database.write_json

    def fail(name, value):
        if name == "index.json":
            raise RuntimeError("synthetic interruption")
        return original(name, value)

    monkeypatch.setattr(database, "write_json", fail)
    with pytest.raises(RuntimeError):
        put(database, tmp_path, power=300)
    assert database.state() == prior
    assert transfer.inspect_store(database)["counts"]["revisions"] == 1


def test_migrate_readonly_full_history_and_no_originals(store, tmp_path):
    put(store, tmp_path)
    old = store.state()["activities"]["1001"]["summary"]
    put(store, tmp_path, power=222)
    raw = store.state()["activities"]["1001"]["raw_ref"]
    store.path(raw).unlink()  # Synthetic raw-free input.
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in store.root.rglob("*") if p.is_file()
    }
    dest = tmp_path / "migrated"
    result = transfer.migrate(store.root, dest)
    assert result["revision_count"] == 2 and result["sample_count"] == 6
    migrated = SQLiteStore(dest)
    assert migrated.read_json(old["files"]["streams"]) == store.read_json(old["files"]["streams"])
    assert before == {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in store.root.rglob("*") if p.is_file()
    }
    assert migrated.state()["activities"]["1001"]["raw_ref"] is None
    with pytest.raises(BridgeError):
        transfer.migrate(store.root, dest)


def test_snapshot_roundtrip_and_old_zip(database, tmp_path):
    from garmin_bridge.backup import export_bundle

    put(database, tmp_path)
    put(database, tmp_path, power=222)
    directory = tmp_path / "snapshot"
    transfer.export_snapshot(database, directory)
    assert {p.name for p in directory.iterdir()} == {DATABASE, "manifest.json"}
    assert transfer.verify_snapshot(directory)["verified"]
    target = tmp_path / "restored"
    transfer.restore_snapshot(directory, target)
    restored = SQLiteStore(target)
    assert transfer.inspect_store(restored) == transfer.inspect_store(database)
    assert restored.state()["activities"]["1001"]["raw_ref"] is None
    assert not (target / "auth").exists() and not (target / "raw").exists()
    with pytest.raises(BridgeError):
        transfer.export_snapshot(database, directory)
    package = tmp_path / "old.zip"
    export_bundle(database, package)
    result = transfer.migrate(package, tmp_path / "zip-restored", bundle=True)
    assert result["activity_count"] == 1 and result["revision_count"] == 1


@pytest.mark.parametrize("attack", ["table", "column", "nested", "trailing", "freed", "manifest"])
def test_snapshot_rejects_rehashed_injection(database, tmp_path, attack):
    from garmin_bridge.normalize import sha256

    put(database, tmp_path)
    directory = tmp_path / "snapshot"
    transfer.export_snapshot(database, directory)
    path = directory / DATABASE
    if attack in {"table", "column", "nested", "freed"}:
        with sqlite3.connect(path) as db:
            if attack == "table":
                db.execute("CREATE TABLE secret (position_lat REAL)")
            elif attack == "column":
                db.execute("ALTER TABLE segments ADD COLUMN secret TEXT")
            elif attack == "nested":
                name, payload = db.execute(
                    "SELECT name,payload FROM documents WHERE name LIKE '%summary.json'"
                ).fetchone()
                value = json.loads(payload)
                value["provenance"]["position_lat"] = 12
                db.execute("UPDATE documents SET payload=? WHERE name=?", (canonical(value), name))
            else:
                db.execute("CREATE TABLE secret (value BLOB)")
                db.execute("INSERT INTO secret VALUES (?)", (b"private-synthetic" * 400,))
                db.execute("DROP TABLE secret")
    elif attack == "trailing":
        with path.open("ab") as handle:
            handle.write(b"synthetic-private")
    manifest = json.loads((directory / "manifest.json").read_bytes())
    manifest["size"], manifest["sha256"] = path.stat().st_size, sha256(path.read_bytes())
    if attack == "manifest":
        manifest["position_lat"] = 42
    (directory / "manifest.json").write_bytes(canonical(manifest))
    with pytest.raises(BridgeError):
        transfer.verify_snapshot(directory)


def test_cli_default_sqlite_window_and_no_implicit_auth(tmp_path, capsys, monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("offline command accessed authentication")

    monkeypatch.setattr(cli, "GarminSource", denied)
    path = private_file(tmp_path, fit_bytes(count=40))
    root = tmp_path / "default"
    assert (
        cli.main(
            ["--data-dir", str(root), "import-fit", "--file", str(path), "--activity-id", "1001"]
        )
        == 0
    )
    capsys.readouterr()
    assert (root / DATABASE).exists() and not (root / "index.json").exists()
    args = [
        "--data-dir",
        str(root),
        "window",
        "1001",
        "--start",
        "2025-01-02T00:00:00Z",
        "--end",
        "2025-01-02T00:05:00Z",
        "--limit",
        "2",
    ]
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["records"]) == 2 and result["next_offset"] == 2
    assert result["records"][0]["ordinal"] == 0
    assert cli.main(args + ["--offset", "999"]) == 0
    assert json.loads(capsys.readouterr().out)["records"] == []
    assert cli.main(args + ["--end", "2025-01-02T00:10:01Z"]) == 2
    capsys.readouterr()


def test_missing_original_segment_explicit(database, tmp_path):
    put(database, tmp_path)
    with database.lock(write=True):
        definition = segments.define(
            database, "1001", "2025-01-02T00:00:00Z", "2025-01-02T00:00:04Z"
        )
        state = database.state()
        entry = state["activities"]["1001"]
        entry["raw_ref"] = entry["raw_sha256"] = None
        database.write_json("index.json", state)
        assert segments.refresh(database, definition["id"]) == 1
    result = segments.history(database, definition["id"])
    assert result["results"][0]["status"] == "unavailable"


def test_exact_numeric_types_signed_zero_duplicates_and_order(database, tmp_path, monkeypatch):
    original = normalize.normalize

    def special(*args, **kwargs):
        summary, streams = original(*args, **kwargs)
        streams["records"][0]["power"] = -0.0
        streams["records"][2]["power"] = 0.12345678901234566
        streams["records"][1]["timestamp"] = streams["records"][0]["timestamp"]
        summary["quality_flags"].append("nonmonotonic_timestamps")
        return summary, streams

    monkeypatch.setattr(normalize, "normalize", special)
    put(database, tmp_path)
    summary = database.state()["activities"]["1001"]["summary"]
    before = database.read_bytes(summary["files"]["streams"])
    assert b'"power":-0.0' in before and b"0.12345678901234566" in before
    transfer.export_snapshot(database, tmp_path / "snapshot")
    transfer.restore_snapshot(tmp_path / "snapshot", tmp_path / "restored")
    restored = SQLiteStore(tmp_path / "restored")
    assert restored.read_bytes(summary["files"]["streams"]) == before


def test_snapshot_queries_do_not_modify_package(database, tmp_path, capsys):
    put(database, tmp_path)
    path = tmp_path / "snapshot"
    transfer.export_snapshot(database, path)
    before = {p.name: p.read_bytes() for p in path.iterdir()}
    assert cli.main(["--data-dir", str(path), "list"]) == 0
    capsys.readouterr()
    assert before == {p.name: p.read_bytes() for p in path.iterdir()}
    assert transfer.verify_snapshot(path)["verified"]
    assert (
        cli.main(
            [
                "--data-dir",
                str(path),
                "segment",
                "define",
                "--reference",
                "1001",
                "--start",
                "2025-01-02T00:00:00Z",
                "--end",
                "2025-01-02T00:00:04Z",
            ]
        )
        == 2
    )
    capsys.readouterr()


def test_migration_requires_read_lock_and_does_not_publish_partial(store, tmp_path, monkeypatch):
    put(store, tmp_path)
    with store.lock(write=True), pytest.raises(BridgeError) as error:
        transfer.migrate(store.root, tmp_path / "busy-target")
    assert error.value.code == "busy"
    assert not (tmp_path / "busy-target").exists()
    original = transfer.inspect_store

    def fail(*args):
        original(*args)
        raise RuntimeError("synthetic validation interruption")

    monkeypatch.setattr(transfer, "inspect_store", fail)
    with pytest.raises(RuntimeError):
        transfer.migrate(store.root, tmp_path / "failed-target")
    assert not (tmp_path / "failed-target").exists()
    assert store.state()["activities"]["1001"]


@pytest.mark.parametrize("attack", ["unknown", "nested", "duplicate", "damaged"])
def test_migration_rejects_invalid_old_input(store, tmp_path, attack):
    put(store, tmp_path)
    summary = store.state()["activities"]["1001"]["summary"]
    name = summary["files"]["streams"]
    if attack in {"unknown", "nested"}:
        value = store.read_json(name)
        if attack == "unknown":
            value["position_lat"] = 42
        else:
            value["records"][0]["nested"] = {"token": "synthetic"}
        store.write_json(name, value)
    elif attack == "duplicate":
        value = store.read_bytes("index.json")
        store.write_bytes("index.json", b'{"version":1,' + value[1:])
    else:
        store.write_bytes(name, b"broken-synthetic")
    with pytest.raises(BridgeError):
        transfer.migrate(store.root, tmp_path / "target")
    assert not (tmp_path / "target").exists()


def test_killed_sqlite_transaction_recovers_prior_commit(database, tmp_path):
    import os
    import subprocess
    import sys

    put(database, tmp_path)
    prior = database.state()
    code = """
import os, sys
from pathlib import Path
from garmin_bridge.sqlite_store import SQLiteStore
s=SQLiteStore(Path(sys.argv[1]))
with s.lock(write=True), s.atomic():
    s._transaction.execute("PRAGMA cache_size=5")
    s._transaction.execute("UPDATE documents SET payload=? WHERE name='index.json'", (b'x'*1000000,))
    os._exit(17)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(__import__("pathlib").Path(__file__).parents[1] / "src")
    process = subprocess.run(
        [sys.executable, "-c", code, str(database.root)], env=env, capture_output=True
    )
    assert process.returncode == 17
    with database.lock():
        assert database.state() == prior
    assert transfer.inspect_store(database)["counts"]["activities"] == 1


def test_concurrent_sqlite_writer_and_snapshot_fail_busy(database, tmp_path):
    put(database, tmp_path)
    other = SQLiteStore(database.root)
    with database.lock(write=True):
        with pytest.raises(BridgeError) as error:
            with other.lock(write=True):
                pytest.fail("acquired competing lock")
        assert error.value.code == "busy"
        with pytest.raises(BridgeError) as error:
            transfer.export_snapshot(other, tmp_path / "busy-snapshot")
        assert error.value.code == "busy"
    assert not (tmp_path / "busy-snapshot").exists()


def test_incomplete_api_gaps_survive_snapshot_resume(database, tmp_path):
    source = Source([activity(), activity(1002)], downloads={"1002": b"broken-synthetic"})
    result = run(database, source)
    assert not result["complete"]
    before = database.state()
    transfer.export_snapshot(database, tmp_path / "snapshot")
    transfer.restore_snapshot(tmp_path / "snapshot", tmp_path / "restored")
    restored = SQLiteStore(tmp_path / "restored")
    for key in (
        "covered_ranges",
        "pending_ranges",
        "last_success_at",
        "full_completed_at",
        "history_initialized",
    ):
        assert restored.state()[key] == before[key]
    result = run(restored, Source([activity(), activity(1002)]))
    assert result["complete"] and len(restored.state()["activities"]) == 2


def test_backup_cli_uses_sqlite_and_preserves_revisions(database, tmp_path, capsys):
    put(database, tmp_path)
    put(database, tmp_path, power=299)
    snap = tmp_path / "backup"
    assert (
        cli.main(["--data-dir", str(database.root), "backup", "export", "--file", str(snap)]) == 0
    )
    assert json.loads(capsys.readouterr().out)["revision_count"] == 2
    assert cli.main(["backup", "verify", "--file", str(snap)]) == 0
    assert json.loads(capsys.readouterr().out)["verified"]
    target = tmp_path / "target"
    assert cli.main(["--data-dir", str(target), "backup", "restore", "--file", str(snap)]) == 0
    assert json.loads(capsys.readouterr().out)["revision_count"] == 2
    assert cli.main(["--data-dir", str(target), "list"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["storage_version"] == 1
    assert listing["activities"][0]["normalizer_version"] == "1.3.0"
    assert listing["activities"][0]["coverage"]["power"]["missing"] == 1


def test_default_legacy_query_never_migrates_and_acquisition_requires_migration(
    store, tmp_path, capsys
):
    put(store, tmp_path)
    before = {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    assert cli.main(["--data-dir", str(store.root), "list"]) == 0
    capsys.readouterr()
    assert not store.path(DATABASE).exists()
    path = private_file(tmp_path, fit_bytes())
    assert (
        cli.main(
            [
                "--data-dir",
                str(store.root),
                "import-fit",
                "--file",
                str(path),
                "--activity-id",
                "1001",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"] == "migration_required"
    assert before == {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}


def test_snapshot_cross_sqlite_creator_header(database, tmp_path):
    put(database, tmp_path)
    path = tmp_path / "snapshot"
    transfer.export_snapshot(database, path)
    db = path / DATABASE
    content = bytearray(db.read_bytes())
    content[96:100] = (3046000).to_bytes(4, "big")
    db.write_bytes(content)
    manifest = json.loads((path / "manifest.json").read_bytes())
    manifest["sha256"] = transfer.digest_file(db)
    (path / "manifest.json").write_bytes(canonical(manifest))
    assert transfer.verify_snapshot(path)["verified"]


@pytest.mark.parametrize("incomplete", [False, True])
def test_legacy_api_migration_preserves_all_progress_and_resumes(store, tmp_path, incomplete):
    from garmin_bridge.backup import snapshot
    from garmin_bridge.jobs import load_job

    source = Source(
        [activity(), activity(1002)], downloads={"1002": b"broken-synthetic"} if incomplete else {}
    )
    with store.lock(write=True):
        result = sync.synchronize(store, lambda: source, mode="full")
    assert result["complete"] is not incomplete
    expected_state, expected_job = snapshot(store)
    transfer.migrate(store.root, tmp_path / "converted")
    target = SQLiteStore(tmp_path / "converted")
    assert target.state() == expected_state
    assert load_job(target) == expected_job
    for entry in expected_state["activities"].values():
        for ref in entry["summary"]["files"].values():
            assert target.read_bytes(ref) == store.read_bytes(ref)
    if incomplete:
        source.downloads = {}
        with target.lock(write=True):
            assert sync.synchronize(target, lambda: source, mode="resume")["complete"]
    assert target.state()["history_initialized"]


def test_empty_sqlite_snapshot_and_invalid_db_are_safe(tmp_path):
    empty = SQLiteStore(tmp_path / "empty")
    with empty.lock(write=True):
        empty.write_json("index.json", empty.state())
    transfer.export_snapshot(empty, tmp_path / "snapshot")
    assert transfer.verify_snapshot(tmp_path / "snapshot")["activity_count"] == 0
    path = empty.path(DATABASE)
    path.write_bytes(b"not a synthetic database")
    with pytest.raises(BridgeError):
        empty.state()


def test_export_absent_store_freezes_empty_snapshot(tmp_path):
    absent = SQLiteStore(tmp_path / "absent")
    transfer.export_snapshot(absent, tmp_path / "snapshot")
    assert not absent.root.exists()
    assert transfer.verify_snapshot(tmp_path / "snapshot")["activity_count"] == 0
