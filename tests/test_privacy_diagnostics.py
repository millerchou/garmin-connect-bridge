"""Deterministic privacy diagnostics and races using entirely synthetic stores."""

import ast
from contextlib import closing, nullcontext
import json
import os
from pathlib import Path
import sqlite3
import stat

import pytest

from garmin_bridge import analysis_export, cli, contract, privacy, sqlite_transfer
from garmin_bridge.errors import BridgeError
from garmin_bridge.sqlite_store import DATABASE, SQLiteStore
from garmin_bridge.storage import Store, canonical
from .synthetic import SECRET, Source, activity
from .test_cli_storage import command
from .test_history import execute


@pytest.mark.parametrize("exporter", ["snapshot", "analysis"])
@pytest.mark.parametrize("pinned", [False, True])
def test_export_pins_source_across_external_wal_commit(
    store, tmp_path, monkeypatch, exporter, pinned
):
    execute(store, Source([activity(1)]))
    replacement = SQLiteStore(tmp_path / "replacement")
    execute(replacement, Source([activity(1, averagePower=222), activity(2)]))
    original_state = store.state
    committed = False
    with closing(sqlite3.connect(store.path(DATABASE))) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("ATTACH DATABASE ? AS newer", (str(replacement.path(DATABASE)),))

        def state_then_commit():
            nonlocal committed
            value = original_state()
            if not committed:
                # A real WAL commit, at a precise boundary between the catalog
                # and journal/artifact reads. No thread scheduler or sleep.
                with writer:
                    for table in ("documents", "samples", "segments", "efforts"):
                        writer.execute(f"DELETE FROM {table}")
                        writer.execute(f"INSERT INTO {table} SELECT * FROM newer.{table}")
                committed = True
            return value

        monkeypatch.setattr(store, "state", state_then_commit)
        if not pinned:
            # Reproduce the former multi-connection acquisition behavior.
            monkeypatch.setattr(store, "read_snapshot", nullcontext)
        target = tmp_path / ("snapshot" if exporter == "snapshot" else "analysis.sqlite")
        export = (
            sqlite_transfer.export_snapshot
            if exporter == "snapshot"
            else analysis_export.export_analysis
        )
        if pinned:
            assert export(store, target)["activity_count"] == 1
        else:
            with pytest.raises(BridgeError) as caught:
                export(store, target)
            assert caught.value.code == "privacy_failed"
            assert caught.value.diagnostic["file"] == DATABASE
            assert not target.exists()
        assert committed
    assert len(original_state()["activities"]) == 2


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("late", [False, True])
def test_snapshot_sidecars_are_busy_without_recovery(store, tmp_path, monkeypatch, suffix, late):
    target = tmp_path / "snapshot"
    sqlite_transfer.export_snapshot(store, target)
    sidecar = target / (DATABASE + suffix)
    original = (target / DATABASE).read_bytes()
    digest = sqlite_transfer.digest_file

    def inject(path):
        Store(target).write_bytes(sidecar.name, b"synthetic transaction")
        return digest(path)

    if late:
        monkeypatch.setattr(sqlite_transfer, "digest_file", inject)
    else:
        inject(target / DATABASE)
    with pytest.raises(BridgeError) as caught:
        sqlite_transfer.verify_snapshot(target)
    assert caught.value.code == "busy"
    assert (target / DATABASE).read_bytes() == original
    assert sidecar.read_bytes() == b"synthetic transaction"
    assert not (target / "reports").exists()


@pytest.mark.parametrize("extra", [".pending-synthetic", ".analysis-synthetic.previous"])
def test_snapshot_rejects_extra_files_but_export_ignores_source_staging(
    store, tmp_path, capsys, extra
):
    store.ensure()
    store.write_bytes(extra, SECRET.encode())
    target = tmp_path / "snapshot"
    sqlite_transfer.export_snapshot(store, target)
    assert {p.name for p in target.iterdir()} == {DATABASE, "manifest.json"}
    Store(target).write_bytes(extra, SECRET.encode())
    code, result, captured = command(
        store, capsys, "snapshot", "verify", "--directory", str(target)
    )
    assert code == 2 and result["diagnostic"] == {
        "rule": "snapshot.members",
        "file": "manifest.json",
        "table": "snapshot",
        "field": "members",
    }
    assert result["report"] == privacy.REPORT
    assert SECRET not in captured.err
    assert not (target / "reports").exists()


def test_snapshot_iteration_order_and_permission_change(store, tmp_path, monkeypatch):
    target = tmp_path / "snapshot"
    sqlite_transfer.export_snapshot(store, target)
    iterdir = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda path: iter(reversed(list(iterdir(path)))))
    assert sqlite_transfer.verify_snapshot(target)["verified"]
    digest = sqlite_transfer.digest_file

    def loosen_after_checksum(path):
        result = digest(path)
        path.chmod(0o644)
        return result

    monkeypatch.setattr(sqlite_transfer, "digest_file", loosen_after_checksum)
    with pytest.raises(BridgeError) as caught:
        sqlite_transfer.verify_snapshot(target)
    assert caught.value.code == "unsafe_store"


def test_stream_diagnostics_have_rule_file_table_key_and_never_values(store, tmp_path, capsys):
    execute(store, Source())
    with store.database(write=True) as db:
        name, payload = db.execute(
            "SELECT name,payload FROM samples WHERE kind='records' AND ordinal=0"
        ).fetchone()
        row = json.loads(payload)
        row["position_lat"] = SECRET
        db.execute(
            "UPDATE samples SET payload=? WHERE name=? AND kind='records' AND ordinal=0",
            (canonical(row), name),
        )
    target = tmp_path / "snapshot"
    code, result, captured = command(
        store, capsys, "snapshot", "export", "--directory", str(target)
    )
    assert code == 2 and result["error"] == "privacy_failed"
    assert result["diagnostic"] == {
        "rule": "streams.contract",
        "file": DATABASE,
        "table": "samples",
        "field": "records.[].position_lat",
    }
    saved = json.loads((store.root / privacy.REPORT).read_bytes())
    assert saved == {"version": 1, **result["diagnostic"]}
    assert SECRET not in captured.err + json.dumps(saved)
    assert name not in captured.err + json.dumps(saved)
    assert not target.exists()


def test_analysis_diagnostics_locate_nested_key(store, tmp_path, monkeypatch, capsys):
    execute(store, Source())
    populate = analysis_export.populate

    def inject(db, source):
        expected, metadata = populate(db, source)
        db.execute(
            "UPDATE messages SET detail_json=?",
            (canonical({"fields": {"position_lat": SECRET}}).decode(),),
        )
        return expected, metadata

    monkeypatch.setattr(analysis_export, "populate", inject)
    code, result, captured = command(
        store, capsys, "export", "analysis", "--file", str(tmp_path / "analysis.sqlite")
    )
    assert code == 2 and result["error"] == "privacy_failed"
    assert result["diagnostic"] == {
        "rule": "analysis.name.1",
        "file": "analysis.sqlite",
        "table": "messages",
        "field": "detail_json.fields.position_lat",
    }
    assert SECRET not in captured.err + (store.root / privacy.REPORT).read_text()


def test_private_report_is_atomic_even_with_permissive_umask(store, monkeypatch):
    store.ensure()
    with privacy.diagnostic_scope(store.root):
        first = privacy.failure("test.initial", "power")
    target = store.root / first.report
    previous = target.read_bytes()
    replace = os.replace
    published = []

    def inspect_before_replace(source, destination):
        assert Path(destination) == target
        assert target.read_bytes() == previous
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        assert stat.S_IMODE(Path(source).parent.stat().st_mode) == 0o700
        assert json.loads(Path(source).read_bytes())["field"] == "heart_rate"
        published.append(True)
        replace(source, destination)

    monkeypatch.setattr(os, "replace", inspect_before_replace)
    mask = os.umask(0)
    try:
        with privacy.diagnostic_scope(store.root):
            second = privacy.failure("test.updated", "heart_rate")
    finally:
        os.umask(mask)
    assert published == [True] and second.report == privacy.REPORT
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(target.parent.glob(".pending-*"))


@pytest.mark.parametrize("kind", ["repo", "snapshot", "unwritable"])
def test_unavailable_report_never_changes_privacy_error_or_uses_unsafe_fallback(
    store, tmp_path, monkeypatch, kind
):
    root = tmp_path / "diagnostic-destination"
    root.mkdir(mode=0o700)
    if kind == "repo":
        (root / ".git").write_text("synthetic checkout marker")
    elif kind == "snapshot":
        Store(root).write_bytes("manifest.json", b"{}")
    else:

        def denied(*args, **kwargs):
            raise OSError(SECRET)

        monkeypatch.setattr(Store, "write_json", denied)
    with privacy.diagnostic_scope(root):
        error = privacy.failure("test.storage_failure", "power")
    assert error.code == "privacy_failed" and error.report is None
    assert error.diagnostic["field"] == "power"
    assert SECRET not in str(error)
    assert not (root / "reports").exists()


def test_output_failure_and_hostile_key_never_echo_values(store, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "execute", lambda _: {"start": SECRET, "end": "2025-01-10", SECRET: SECRET}
    )
    code, result, captured = command(store, capsys, "sync", "--since-last", "--dry-run")
    assert code == 2 and result["diagnostic"]["file"] == "stdout.json"
    assert result["diagnostic"]["field"] == "start.$"
    assert SECRET not in captured.err + (store.root / privacy.REPORT).read_text()
    with privacy.diagnostic_scope(store.root), pytest.raises(BridgeError) as caught:
        contract.validate_output(
            "sync-preview", {"start": "2025-01-01", "end": "2025-01-10", SECRET: SECRET}
        )
    assert caught.value.diagnostic["field"] == "[unreviewed-key]"


def test_every_privacy_raise_uses_the_diagnostic_gateway():
    source = Path(contract.__file__).parent
    for path in source.glob("*.py"):
        if path.name == "privacy.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "BridgeError"
            ):
                assert not any(
                    isinstance(arg, ast.Constant) and arg.value == "privacy_failed"
                    for arg in node.args
                ), path.name


def test_readback_report_stays_in_data_dir_and_names_the_failed_field(store, tmp_path, capsys):
    from .test_backup import rewrite

    execute(store, Source())
    target = tmp_path / "snapshot"
    sqlite_transfer.export_snapshot(store, target)

    def corrupt(manifest, payloads):
        for name, data in payloads.items():
            if name.endswith("/streams.json"):
                stream = json.loads(data)
                stream["records"][0]["power"] = SECRET
                payloads[name] = canonical(stream)

    rewrite(target, corrupt)
    code, result, captured = command(
        store, capsys, "snapshot", "verify", "--directory", str(target)
    )
    assert code == 2 and result["diagnostic"]["file"] == DATABASE
    assert result["diagnostic"]["field"] == "records.[].power.$"
    assert (store.root / privacy.REPORT).exists()
    assert {p.name for p in target.iterdir()} == {DATABASE, "manifest.json"}
    assert SECRET not in captured.err + (store.root / privacy.REPORT).read_text()


def test_raw_identity_checks_do_not_depend_on_row_or_field_iteration_order():
    from itertools import permutations
    from garmin_bridge.fit_unknown import RawIdentityCheck, SUSPECTED_IDENTITY_MIN

    for rows in permutations(range(4)):
        check = RawIdentityCheck()
        for ordinal in rows:
            fields = {"0": SUSPECTED_IDENTITY_MIN + ordinal, "1": SUSPECTED_IDENTITY_MIN}
            check.observe_raw(534, dict(reversed(list(fields.items()))) if ordinal % 2 else fields)
        assert check.blocked_fields() == {(534, 1): "suspected_identity"}


def test_report_scopes_are_isolated_between_concurrent_callers(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)

    def report(index):
        root = tmp_path / f"caller-{index}"
        with privacy.diagnostic_scope(root):
            barrier.wait(timeout=5)
            error = privacy.failure("test.concurrent", "power" if index == 0 else "heart_rate")
        assert json.loads((root / error.report).read_bytes()) == {"version": 1, **error.diagnostic}
        return error.diagnostic["field"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(report, (0, 1))) == ["power", "heart_rate"]


def test_failed_report_publication_preserves_previous_complete_report(store, monkeypatch):
    with privacy.diagnostic_scope(store.root):
        previous = privacy.failure("test.initial", "power")
    path = store.root / previous.report
    before = path.read_bytes()

    def interrupted(*args):
        raise OSError(SECRET)

    monkeypatch.setattr(os, "replace", interrupted)
    with privacy.diagnostic_scope(store.root):
        error = privacy.failure("test.failed_write", "heart_rate")
    assert error.code == "privacy_failed" and error.report is None
    assert error.diagnostic["field"] == "heart_rate"
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".pending-*"))
