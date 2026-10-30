"""Synthetic-only primary-store and portable snapshot regression."""

import copy
import json
import sqlite3
from unittest.mock import Mock

import pytest

from garmin_bridge import cli, normalize, segments, sqlite_transfer as transfer
from garmin_bridge.errors import BridgeError
from garmin_bridge.import_fit import import_file
from garmin_bridge.sqlite_store import ARRAY_LIMITS, DATABASE, SQLiteStore
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


def test_snapshot_roundtrip(database, tmp_path):

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


@pytest.mark.parametrize(
    "attack", ["table", "column", "trigger", "nested", "trailing", "freed", "manifest"]
)
def test_snapshot_rejects_rehashed_injection(database, tmp_path, attack):
    from garmin_bridge.normalize import sha256

    put(database, tmp_path)
    directory = tmp_path / "snapshot"
    transfer.export_snapshot(database, directory)
    path = directory / DATABASE
    if attack in {"table", "column", "trigger", "nested", "freed"}:
        with sqlite3.connect(path) as db:
            if attack == "table":
                db.execute("CREATE TABLE secret (position_lat REAL)")
            elif attack == "column":
                db.execute("ALTER TABLE segments ADD COLUMN secret TEXT")
            elif attack == "trigger":
                db.execute(
                    "CREATE TRIGGER unexpected AFTER INSERT ON documents BEGIN SELECT 1; END"
                )
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
    if attack in {"trailing", "freed"}:
        # Private integrity checks no longer certify physical canonical encoding.
        assert transfer.verify_snapshot(directory)["verified"]
    else:
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
    assert listing["activities"][0]["normalizer_version"] == "1.4.0"
    assert listing["activities"][0]["coverage"]["power"]["missing"] == 1


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


def test_reads_trust_stored_rows_but_writes_exports_and_cli_validate(database, tmp_path, capsys):
    put(database, tmp_path, count=8)
    ref = database.state()["activities"]["1001"]["summary"]["files"]["streams"]
    with database.database(write=True) as db:
        row = json.loads(
            db.execute(
                "SELECT payload FROM samples WHERE name=? AND kind='records' AND ordinal=1", (ref,)
            ).fetchone()[0]
        )
        row["unreviewed"] = {"position_lat": 123}
        db.execute(
            "UPDATE samples SET payload=? WHERE name=? AND kind='records' AND ordinal=1",
            (canonical(row), ref),
        )
    # Normal reads trust the store; boundary validation remains explicit.
    content = database.read_bytes(ref)
    assert json.loads(content)["records"][1] == row
    with pytest.raises(BridgeError):
        database.write_bytes(ref, content)
    with pytest.raises(BridgeError):
        transfer.export_snapshot(database, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()
    assert (
        cli.main(
            [
                "--data-dir",
                str(database.root),
                "window",
                "1001",
                "--start",
                "2025-01-02T00:00:00Z",
                "--end",
                "2025-01-02T00:00:20Z",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert not captured.out and "position_lat" not in captured.err
    assert json.loads(captured.err)["error"] == "privacy_failed"


@pytest.mark.parametrize("damage", ["ordinal", "kind", "count", "row_size", "read_limit"])
def test_normal_reads_keep_row_order_and_size_limits(database, tmp_path, monkeypatch, damage):
    put(database, tmp_path)
    ref = database.state()["activities"]["1001"]["summary"]["files"]["streams"]
    if damage == "count":
        monkeypatch.setitem(ARRAY_LIMITS, "records", 2)
    elif damage != "read_limit":
        with database.database(write=True) as db:
            if damage == "ordinal":
                db.execute("UPDATE samples SET ordinal=99 WHERE kind='records' AND ordinal=1")
            elif damage == "kind":
                db.execute("UPDATE samples SET kind='unknown' WHERE kind='records'")
            else:
                db.execute(
                    "UPDATE samples SET payload=? WHERE kind='records' AND ordinal=1",
                    (canonical({"synthetic": "x" * (512 * 1024)}),),
                )
    with pytest.raises(BridgeError):
        database.read_bytes(ref, limit=1 if damage == "read_limit" else None)


def test_verify_does_not_rebuild_or_require_writable_parent(database, tmp_path, monkeypatch):
    put(database, tmp_path)
    path = tmp_path / "snapshot"
    transfer.export_snapshot(database, path)
    before = {p.name: p.read_bytes() for p in path.iterdir()}

    def denied(*args, **kwargs):
        pytest.fail("verification attempted to rebuild or create temporary files")

    monkeypatch.setattr(transfer, "copy_logical", denied)
    monkeypatch.setattr(transfer.tempfile, "TemporaryDirectory", denied)
    assert transfer.verify_snapshot(path)["verified"]
    assert before == {p.name: p.read_bytes() for p in path.iterdir()}


def test_format_one_manifest_is_rejected(database, tmp_path):
    put(database, tmp_path)
    path = tmp_path / "snapshot"
    transfer.export_snapshot(database, path)
    manifest = json.loads((path / "manifest.json").read_bytes())
    manifest["format_version"] = 1
    for key in ("fit_fields_versions", "fit_counts", "identity_included"):
        del manifest[key]
    (path / "manifest.json").write_bytes(canonical(manifest))
    with pytest.raises(BridgeError):
        transfer.verify_snapshot(path)


@pytest.mark.parametrize("field_version", ["1.0", "1.1"])
def test_verify_full_raw_streams_and_spot_check_legacy_streams(
    database, tmp_path, monkeypatch, field_version
):
    original = normalize.normalize

    def historical(*args, **kwargs):
        summary, streams = original(*args, **kwargs)
        if field_version == "1.0":
            streams["fit_fields_version"] = "1.0"
            del streams["raw_messages"]
            del streams["units"]["fractional_cadence"]
            del summary["coverage"]["fractional_cadence"]
            for row in streams["records"]:
                del row["fractional_cadence"]
        return summary, streams

    monkeypatch.setattr(normalize, "normalize", historical)
    put(database, tmp_path, count=8)
    path = tmp_path / "snapshot"
    transfer.export_snapshot(database, path)
    with sqlite3.connect(path / DATABASE) as db:
        name, payload = db.execute(
            "SELECT name,payload FROM samples WHERE kind='records' AND ordinal=1"
        ).fetchone()
        row = json.loads(payload)
        row["unreviewed"] = "synthetic"
        db.execute(
            "UPDATE samples SET payload=? WHERE name=? AND kind='records' AND ordinal=1",
            (canonical(row), name),
        )
    manifest = json.loads((path / "manifest.json").read_bytes())
    manifest["sha256"] = transfer.digest_file(path / DATABASE)
    manifest["size"] = (path / DATABASE).stat().st_size
    (path / "manifest.json").write_bytes(canonical(manifest))
    if field_version == "1.0":
        # Legacy streams retain spot checking; raw streams need full evidence.
        assert transfer.verify_snapshot(path)["verified"]
    else:
        with pytest.raises(BridgeError):
            transfer.verify_snapshot(path)
    with pytest.raises(BridgeError):
        transfer.restore_snapshot(path, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


@pytest.mark.parametrize("command", ["sanitize-fit", "verify-fit", "migrate"])
def test_removed_commands_are_not_available(capsys, command):
    assert cli.main([command]) == 2
    result = json.loads(capsys.readouterr().err)
    assert result["error"] == "removed_" + command.replace("-", "_")
    assert command in result["message"] and "removed in this version" in result["message"]


def test_snapshot_rejects_raw_references_even_with_updated_checksum(database, tmp_path):
    put(database, tmp_path)
    original = database.state()["activities"]["1001"]
    path = tmp_path / "snapshot"
    transfer.export_snapshot(database, path)
    with sqlite3.connect(path / DATABASE) as db:
        state = json.loads(
            db.execute("SELECT payload FROM documents WHERE name='index.json'").fetchone()[0]
        )
        for key in ("raw_ref", "raw_sha256"):
            state["activities"]["1001"][key] = original[key]
        db.execute("UPDATE documents SET payload=? WHERE name='index.json'", (canonical(state),))
    manifest = json.loads((path / "manifest.json").read_bytes())
    manifest["sha256"] = transfer.digest_file(path / DATABASE)
    manifest["size"] = (path / DATABASE).stat().st_size
    (path / "manifest.json").write_bytes(canonical(manifest))
    with pytest.raises(BridgeError):
        transfer.verify_snapshot(path)


@pytest.mark.parametrize("damage", ["current_summary", "stream_revision", "stream_count"])
def test_snapshot_spot_checks_retain_revision_relations(database, tmp_path, damage):
    put(database, tmp_path)
    path = tmp_path / "snapshot"
    transfer.export_snapshot(database, path)
    with sqlite3.connect(path / DATABASE) as db:
        if damage == "current_summary":
            name = "index.json"
        else:
            name = db.execute(
                "SELECT name FROM documents WHERE name LIKE '%/streams.json'"
            ).fetchone()[0]
        value = json.loads(
            db.execute("SELECT payload FROM documents WHERE name=?", (name,)).fetchone()[0]
        )
        if damage == "current_summary":
            value["activities"]["1001"]["summary"]["metrics"]["avg_power"]["value"] += 1
        elif damage == "stream_revision":
            value["source_digest"] = "0" * 64
        else:
            db.execute("DELETE FROM samples WHERE kind='records' AND ordinal=2")
        db.execute("UPDATE documents SET payload=? WHERE name=?", (canonical(value), name))
    manifest = json.loads((path / "manifest.json").read_bytes())
    if damage == "stream_count":
        manifest["counts"]["records"] -= 1
    manifest["sha256"] = transfer.digest_file(path / DATABASE)
    manifest["size"] = (path / DATABASE).stat().st_size
    (path / "manifest.json").write_bytes(canonical(manifest))
    with pytest.raises(BridgeError):
        transfer.verify_snapshot(path)
