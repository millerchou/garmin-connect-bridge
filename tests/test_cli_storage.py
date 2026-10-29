import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from garmin_bridge import cli
from garmin_bridge.errors import BridgeError
from garmin_bridge.storage import Store, default_root
from .synthetic import SECRET, Source, activity, archive, fit_bytes
from .test_sync import run


def command(store, capsys, *args):
    code = cli.main(["--legacy-store", "--data-dir", str(store.root), *args])
    captured = capsys.readouterr()
    return code, json.loads(captured.out or captured.err), captured


def test_empty_queries_are_offline_and_do_not_create_store(store, capsys, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Queries must not authenticate, sync or normalize")

    monkeypatch.setattr(cli, "GarminSource", denied)
    monkeypatch.setattr(cli, "synchronize", denied)
    monkeypatch.setattr("garmin_bridge.normalize.normalize", denied)
    code, result, _ = command(store, capsys, "status")
    assert code == 0 and result["authentication"]["state"] == "absent"
    assert result["activity_count"] == 0 and result["freshness"]["watermark"] is None
    code, result, _ = command(store, capsys, "list")
    assert code == 0 and result["activities"] == [] and result["next_offset"] is None
    code, result, _ = command(store, capsys, "activity", "1001")
    assert code == 2 and result["error"] == "not_found"
    assert not store.root.exists()


def test_full_resolution_private_streams_but_bounded_offline_activity(store, capsys, monkeypatch):
    run(store, Source(downloads={"1001": archive(fit_bytes(count=500))}))
    reference = store.state()["activities"]["1001"]["summary"]["files"]["streams"]
    assert len(store.read_json(reference)["records"]) == 500
    monkeypatch.setattr(
        cli, "GarminSource", lambda *a: pytest.fail("offline query tried authentication")
    )
    code, result, captured = command(store, capsys, "activity", "1001")
    assert code == 0 and result["activity"]["sample_count"] == 500
    assert result["files_available"]
    assert "records" not in captured.out and len(captured.out) < 10_000
    for forbidden in (SECRET, "position_lat", "position_long", "auth/session", "/raw/"):
        assert forbidden not in captured.out
    assert os.stat(store.path(reference)).st_mode & 0o777 == 0o600
    store.path(reference).unlink()
    assert command(store, capsys, "activity", "1001")[1]["files_available"] is False


def test_bounded_list_pagination_and_date_filter(store, capsys):
    run(store, Source([activity(i) for i in range(1, 56)]))
    code, result, captured = command(store, capsys, "list", "--limit", "50")
    assert code == 0 and len(result["activities"]) == 50 and result["next_offset"] == 50
    assert len(captured.out) < 100_000
    second = command(store, capsys, "list", "--offset", "50")[1]
    assert len(second["activities"]) == 5 and second["next_offset"] is None
    assert {a["source_activity_id"] for a in result["activities"]}.isdisjoint(
        a["source_activity_id"] for a in second["activities"]
    )
    assert command(store, capsys, "list", "--start", "2025-01-03")[1]["activities"] == []


@pytest.mark.parametrize(
    "args",
    [
        ["auth", "--password", SECRET],
        ["auth", "--mfa", SECRET],
        ["activity", SECRET],
        ["sync", "--start", SECRET],
        ["list", "--limit", SECRET],
        [SECRET],
        ["list", "--limit", "51"],
        ["list", "--offset", "-1"],
        ["sync", "--start", "2025-02-30"],
        ["sync", "--lookback-days", "0"],
    ],
)
def test_invalid_arguments_never_echo_user_values(store, capsys, args):
    code, result, captured = command(store, capsys, *args)
    assert code == 2 and result["error"] == "usage"
    assert SECRET not in captured.out + captured.err


def test_auth_requires_private_tty_and_sync_without_session_has_actionable_failure(
    store, capsys, monkeypatch
):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert command(store, capsys, "auth")[1]["error"] == "interactive_required"
    code, result, _ = command(store, capsys, "sync", "--start", "2025-01-01", "--end", "2025-01-03")
    assert code == 1 and not result["complete"] and result["last_failure"] == "auth_required"
    assert result["freshness"]["watermark"] is None


def test_error_boundary_and_final_output_validator_do_not_leak(store, capsys, monkeypatch):
    def fail(_):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(cli, "execute", fail)
    code, result, captured = command(store, capsys, "status")
    assert code == 2 and result["error"] == "internal_error"
    assert SECRET not in captured.err
    monkeypatch.setattr(cli, "execute", lambda _: {"token": SECRET})
    code, result, captured = command(store, capsys, "status")
    assert code == 2 and result["error"] == "privacy_failed"
    assert captured.out == "" and SECRET not in captured.err


def test_tampered_nested_index_is_not_printed(store, capsys):
    run(store, Source())
    state = store.state()
    state["activities"]["1001"]["summary"]["metrics"]["duration"]["value"] = SECRET
    with store.lock(write=True):
        store.write_json("index.json", state)
    code, result, captured = command(store, capsys, "activity", "1001")
    assert code == 2 and result["error"] == "privacy_failed"
    assert captured.out == "" and SECRET not in captured.err


def test_single_writer_and_cross_process_queries_obey_lock(store):
    with store.lock(write=True):
        with pytest.raises(BridgeError) as error:
            with store.lock(write=True):
                pytest.fail("second writer acquired lock")
        assert error.value.code == "busy"
        process = subprocess.run(
            [sys.executable, "-m", "garmin_bridge", "--data-dir", str(store.root), "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert process.returncode == 2 and json.loads(process.stderr)["error"] == "busy"
    with store.lock(write=True):
        pass  # kernel releases lock after the command exits


def test_private_permissions_do_not_depend_on_umask(store):
    previous = os.umask(0)
    try:
        run(store, Source())
    finally:
        os.umask(previous)
    for path in [store.root, *store.root.rglob("*")]:
        assert os.stat(path).st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)


def test_symlink_hardlink_and_permissive_store_rejected(store, tmp_path):
    store.ensure()
    target = tmp_path / "elsewhere"
    target.write_text(SECRET)
    os.chmod(target, 0o600)
    store.path("index.json").symlink_to(target)
    with pytest.raises(BridgeError):
        store.state()
    (store.root / "index.json").unlink()
    os.link(target, store.root / "index.json")
    with pytest.raises(BridgeError):
        store.state()
    (store.root / "index.json").unlink()
    os.chmod(store.root, 0o755)
    with pytest.raises(BridgeError):
        with store.lock(write=True):
            pass
    assert os.stat(store.root).st_mode & 0o777 == 0o755  # don't chmod someone else's directory
    assert target.read_text() == SECRET


def test_store_inside_checkout_rejected_and_platform_defaults(monkeypatch, tmp_path):
    with pytest.raises(BridgeError):
        Store(Path(__file__).parent / "private")
    fake_checkout = tmp_path / "repo"
    fake_checkout.mkdir()
    (fake_checkout / ".git").write_text("gitdir: elsewhere")
    with pytest.raises(BridgeError):
        Store(fake_checkout / "private")
    monkeypatch.delenv("GARMIN_BRIDGE_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_root() == tmp_path / "xdg/garmin-connect-bridge"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert str(default_root()).endswith("Library/Application Support/garmin-connect-bridge")


def test_failed_replace_preserves_file_and_cleans_staging(store, monkeypatch):
    with store.lock(write=True):
        store.write_json("example.json", {"value": 1})

        def failed_replace(*args):
            raise OSError("synthetic interrupted replace")

        monkeypatch.setattr(os, "replace", failed_replace)
        with pytest.raises(OSError):
            store.write_json("example.json", {"value": 2})
        assert store.read_json("example.json") == {"value": 1}
        assert not list(store.root.glob(".pending-*"))


def test_corrupt_index_is_reported_without_raw_contents(store, capsys):
    with store.lock(write=True):
        store.write_bytes("index.json", SECRET.encode())
    code, result, captured = command(store, capsys, "status")
    assert code == 2 and result["error"] == "storage_failed"
    assert SECRET not in captured.err


def test_oversized_index_update_preserves_last_readable_state(store, monkeypatch):
    run(store, Source())
    previous = store.read_bytes("index.json")
    monkeypatch.setattr("garmin_bridge.storage.MAX_FILE_BYTES", len(previous))
    with store.lock(write=True), pytest.raises(BridgeError) as error:
        store.write_bytes("index.json", previous + b" ")
    assert error.value.code == "storage_failed"
    assert store.read_bytes("index.json") == previous
    assert store.state()["activities"]["1001"]["summary"]["source_activity_id"] == "1001"


def test_missing_store_reader_remains_empty_if_first_writer_starts_concurrently(store):
    from garmin_bridge.jobs import load_job

    writer = Store(store.root)
    with store.lock():
        run(writer, Source())
        assert store.state()["activities"] == {}
        assert load_job(store) is None
    assert set(store.state()["activities"]) == {"1001"}
