"""Independent regression cases; all stores, keys and values are invented."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import traceback

import pytest

from garmin_bridge import analysis_export, contract, privacy, sqlite_store, sqlite_transfer
from garmin_bridge.errors import BridgeError
from garmin_bridge.sqlite_store import DATABASE, SQLiteStore
from garmin_bridge.storage import Store, canonical, empty_state
from .synthetic import SECRET, Source
from .test_cli_storage import command
from .test_history import execute


@pytest.mark.parametrize("key", [SECRET, "137", "unknown_999", "private/path"])
def test_unreviewed_nested_keys_cannot_become_diagnostic_labels(store, key):
    with privacy.diagnostic_scope(store.root), pytest.raises(BridgeError) as caught:
        analysis_export.validate_detail_keys({key: {"position_lat": SECRET}}, "detail_json.")
    error = caught.value
    assert error.diagnostic["field"] == "detail_json.[unreviewed-key].position_lat"
    saved = (store.root / error.report).read_text()
    assert key not in saved and SECRET not in saved
    assert contract.validate_output(
        "error",
        {
            "schema_version": contract.SCHEMA_VERSION,
            "error": error.code,
            "message": str(error),
            "diagnostic": error.diagnostic,
            "report": error.report,
        },
    )


def test_privacy_traceback_does_not_chain_private_exception(store):
    with privacy.diagnostic_scope(store.root), pytest.raises(BridgeError) as caught:
        try:
            raise ValueError(SECRET)
        except ValueError:
            privacy.require(False, "test.context", "power")
    error = caught.value
    assert error.__cause__ is None and error.__suppress_context__
    rendered = "".join(traceback.format_exception(error)) + repr(error) + str(error)
    assert SECRET not in rendered + (store.root / error.report).read_text()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("point", ["checksum", "contract"])
def test_late_sidecar_overrides_failed_validation_without_replacing_report(
    store, tmp_path, monkeypatch, capsys, suffix, point
):
    target = tmp_path / "snapshot"
    sqlite_transfer.export_snapshot(store, target)
    with privacy.diagnostic_scope(store.root):
        previous = privacy.failure("test.previous", "power")
    report = store.root / previous.report
    before = report.read_bytes()

    def inject(*args, **kwargs):
        Store(target).write_bytes(DATABASE + suffix, b"synthetic active transaction")
        if point == "contract":
            privacy.require(False, "test.racing_contract", "payload")
        return "0" * 64

    monkeypatch.setattr(
        sqlite_transfer, "digest_file" if point == "checksum" else "inspect_store", inject
    )
    code, result, captured = command(
        store, capsys, "snapshot", "verify", "--directory", str(target)
    )
    assert code == 2 and result["error"] == "busy" and not captured.out
    assert "diagnostic" not in result and report.read_bytes() == before
    assert not (target / "reports").exists()


@pytest.mark.parametrize("name", [DATABASE, "manifest.json"])
def test_partial_snapshot_file_change_is_busy_even_if_checksum_fails(
    store, tmp_path, monkeypatch, capsys, name
):
    target = tmp_path / "snapshot"
    sqlite_transfer.export_snapshot(store, target)
    digest = sqlite_transfer.digest_file

    def change_during_checksum(path):
        with (target / name).open("ab") as handle:
            handle.write(b"synthetic partial write")
        return digest(path)

    monkeypatch.setattr(sqlite_transfer, "digest_file", change_during_checksum)
    code, result, _ = command(store, capsys, "snapshot", "verify", "--directory", str(target))
    assert code == 2 and result["error"] == "busy"
    assert not (store.root / privacy.REPORT).exists()


@pytest.mark.parametrize("fail", [False, True])
def test_restore_checks_sidecars_after_copy_and_before_publication(
    store, tmp_path, monkeypatch, capsys, fail
):
    target = tmp_path / "snapshot"
    sqlite_transfer.export_snapshot(store, target)
    restored = SQLiteStore(tmp_path / "restored")
    copy = sqlite_transfer.copy_logical

    def copy_then_race(source, destination):
        copy(source, destination)
        Store(target).write_bytes(DATABASE + "-wal", b"synthetic transaction")
        if fail:
            privacy.require(False, "test.copy_race", "payload")

    monkeypatch.setattr(sqlite_transfer, "copy_logical", copy_then_race)
    code, result, _ = command(restored, capsys, "snapshot", "restore", "--directory", str(target))
    assert code == 2 and result["error"] == "busy"
    assert not restored.root.exists()
    assert not list(tmp_path.glob(".sqlite-restore-*"))


@pytest.mark.parametrize("fail", [False, True])
def test_absent_database_snapshot_stays_empty_and_always_cleans_up(store, fail):
    store.ensure()
    later = {**empty_state(), "last_attempt_at": "2025-02-03T00:00:00Z"}
    try:
        with store.read_snapshot():
            SQLiteStore(store.root).write_json("index.json", later)
            assert store.state() == empty_state()
            assert store.read_bytes("index.json") is None
            if fail:
                raise RuntimeError("synthetic interruption")
    except RuntimeError:
        assert fail
    assert not store._absent_snapshot and store.state() == later


@pytest.mark.parametrize("fail", [False, True])
def test_read_snapshot_pins_wal_checkpoint_and_releases_connection_on_error(store, fail):
    store.ensure()
    store.write_json("index.json", empty_state())
    later = {**empty_state(), "last_attempt_at": "2025-02-03T00:00:00Z"}
    with closing(sqlite3.connect(store.path(DATABASE), timeout=0)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        try:
            with store.read_snapshot():
                reader = store._transaction
                assert reader.in_transaction
                with store.read_snapshot(), store.database() as nested:
                    assert reader is nested
                with writer:
                    writer.execute(
                        "UPDATE documents SET payload=? WHERE name='index.json'",
                        (canonical(later),),
                    )
                assert store.state() == empty_state()
                assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 1
                if fail:
                    raise RuntimeError("synthetic interruption")
        except RuntimeError:
            assert fail
        assert store._transaction is None
        with pytest.raises(sqlite3.ProgrammingError):
            reader.execute("SELECT 1")
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
        assert store.state() == later


def test_rollback_writer_can_commit_after_read_snapshot_exits(store):
    store.ensure()
    store.write_json("index.json", empty_state())
    later = {**empty_state(), "last_attempt_at": "2025-02-03T00:00:00Z"}
    with closing(sqlite3.connect(store.path(DATABASE), timeout=0)) as writer:
        with store.read_snapshot():
            writer.execute("UPDATE documents SET payload=?", (canonical(later),))
            with pytest.raises(sqlite3.OperationalError) as caught:
                writer.commit()
            assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
            assert store.state() == empty_state()
        writer.commit()
    assert store.state() == later


@pytest.mark.parametrize("kind", ["snapshot", "analysis"])
def test_export_releases_source_before_destination_validation(store, tmp_path, monkeypatch, kind):
    execute(store, Source())
    module = sqlite_transfer if kind == "snapshot" else analysis_export
    name = "inspect_store" if kind == "snapshot" else "validate"
    validate = getattr(module, name)
    checked = []

    def validate_after_release(*args, **kwargs):
        if args[0] is not store:
            assert store._transaction is None
            checked.append(True)
        return validate(*args, **kwargs)

    monkeypatch.setattr(module, name, validate_after_release)
    export = module.export_snapshot if kind == "snapshot" else module.export_analysis
    assert export(store, tmp_path / "artifact")["verified"]
    assert checked


@pytest.mark.parametrize("kind", ["snapshot", "analysis"])
def test_external_sqlite_lock_returns_busy_and_preserves_source(store, tmp_path, capsys, kind):
    execute(store, Source())
    original = store.path(DATABASE).read_bytes()
    target = tmp_path / "artifact"
    args = (
        ("snapshot", "export", "--directory", str(target))
        if kind == "snapshot"
        else ("export", "analysis", "--file", str(target))
    )
    with closing(sqlite3.connect(store.path(DATABASE))) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        code, result, _ = command(store, capsys, *args)
        writer.rollback()
    assert code == 2 and result["error"] == "busy"
    assert not target.exists() and not (store.root / privacy.REPORT).exists()
    assert store.path(DATABASE).read_bytes() == original
    assert not list(tmp_path.glob(".analysis-*"))
    assert not list(tmp_path.glob(".sqlite-export-*"))


def test_dry_run_does_not_publish_a_report_for_a_superseded_privacy_failure(
    store, monkeypatch, capsys
):
    execute(store, Source())
    check_schema = sqlite_store.check_schema
    with closing(sqlite3.connect(store.path(DATABASE))) as writer:

        def change_schema_during_preview(db):
            check_schema(db)
            writer.execute("PRAGMA user_version=2")
            privacy.require(False, "test.preview_race", "user_version")

        monkeypatch.setattr(sqlite_store, "check_schema", change_schema_during_preview)
        code, result, captured = command(store, capsys, "sync", "--since-last", "--dry-run")
    assert code == 2 and result["error"] == "busy" and not captured.out
    assert not (store.root / privacy.REPORT).exists()


def test_connection_setup_failure_closes_connection(store, monkeypatch):
    store.ensure()
    store.write_json("index.json", empty_state())
    db = sqlite3.connect(store.path(DATABASE))
    db.set_authorizer(lambda *args: sqlite3.SQLITE_DENY)
    monkeypatch.setattr(sqlite_store.sqlite3, "connect", lambda *args, **kwargs: db)
    with pytest.raises(sqlite3.Error):
        sqlite_store.connect(store.path(DATABASE))
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("SELECT 1")


def test_nested_snapshot_destination_is_unsafe_without_writing_a_report(store, capsys):
    store.ensure()
    code, result, _ = command(
        store, capsys, "snapshot", "export", "--directory", str(store.root / "nested")
    )
    assert code == 2 and result["error"] == "unsafe_store"
    assert not (store.root / "nested").exists()
    assert not (store.root / privacy.REPORT).exists()


def test_simultaneous_reports_share_only_atomic_complete_files(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import os

    store.ensure()
    barrier = Barrier(2)
    replace = os.replace

    def publish_together(source, destination):
        assert Path(source).stat().st_mode & 0o777 == 0o600
        assert Path(source).parent.stat().st_mode & 0o777 == 0o700
        barrier.wait(timeout=5)
        replace(source, destination)

    monkeypatch.setattr(os, "replace", publish_together)

    def report(field):
        with privacy.diagnostic_scope(store.root):
            return privacy.failure("test.concurrent", field)

    with ThreadPoolExecutor(max_workers=2) as pool:
        errors = list(pool.map(report, ("power", "heart_rate")))
    assert [error.diagnostic["field"] for error in errors] == ["power", "heart_rate"]
    assert all(error.report == privacy.REPORT for error in errors)
    assert json.loads((store.root / privacy.REPORT).read_bytes()) in [
        {"version": 1, **error.diagnostic} for error in errors
    ]
    assert not list((store.root / "reports").glob(".pending-*"))
