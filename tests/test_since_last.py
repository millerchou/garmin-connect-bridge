"""The incremental date shortcut uses successful contiguous coverage only."""

from datetime import date
from contextlib import closing

import pytest

from garmin_bridge import cli, sqlite_store, sync
from garmin_bridge.errors import BridgeError
from garmin_bridge.storage import empty_state
from .synthetic import Source
from .test_cli_storage import command
from .test_sync import INTERVAL, run


@pytest.fixture
def today(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2025, 1, 10)

    monkeypatch.setattr(cli, "date", FixedDate)
    return FixedDate.today()


def test_since_last_uses_contiguous_end_minus_one_not_latest_disconnected_range(
    store, today, monkeypatch, capsys
):
    run(store, Source())
    run(store, Source([]), {"start": "2025-01-08", "end": "2025-01-09"})
    source = Source()
    monkeypatch.setattr(cli, "GarminSource", lambda _: source)
    code, result, _ = command(store, capsys, "sync", "--since-last")
    assert code == 0 and result["complete"]
    assert result["mode"] == "range"
    assert result["filter"]["start"] == "2025-01-02"
    assert result["filter"]["end"] == today.isoformat()
    pages = [call for call in source.calls if call[0] == "page"]
    assert len(pages) >= 2
    assert all(call[1:3] == ("2025-01-02", "2025-01-10") for call in pages)
    assert sync.watermark(store.state()) == {"start": INTERVAL["start"], "end": "2025-01-10"}


def test_since_last_no_watermark_is_clear_error_before_auth(store, today, monkeypatch, capsys):
    monkeypatch.setattr(cli, "GarminSource", lambda _: pytest.fail("no watermark authenticated"))
    code, result, _ = command(store, capsys, "sync", "--since-last")
    assert code == 2 and result["error"] == "no_watermark"
    assert "contiguous successful watermark" in result["message"]
    assert store.state() == empty_state()
    assert not (store.root / "activities.sqlite").exists()


def test_since_last_pending_earlier_backfill_has_no_watermark(today):
    state = empty_state()
    state["covered_ranges"] = [INTERVAL]
    state["pending_ranges"] = [{"start": "2024-12-01", "end": "2024-12-02"}]
    with pytest.raises(BridgeError) as error:
        sync.since_last_range(state, today)
    assert error.value.code == "no_watermark"


def test_since_last_failure_preserves_successful_watermark(store, today, monkeypatch, capsys):
    run(store, Source())
    before = store.state()
    source = Source(downloads={"1001": BridgeError("network_failed")})
    monkeypatch.setattr(cli, "GarminSource", lambda _: source)
    code, result, _ = command(store, capsys, "sync", "--since-last")
    assert code == 1 and not result["complete"]
    after = store.state()
    assert sync.watermark(after) == sync.watermark(before) == INTERVAL
    assert after["last_success_at"] == before["last_success_at"]
    assert after["activities"] == before["activities"]
    assert after["pending_ranges"] == [{"start": "2025-01-02", "end": "2025-01-10"}]


@pytest.mark.parametrize(
    "extra",
    [
        ["--start", "2025-01-01"],
        ["--end", "2025-01-10"],
        ["--full"],
        ["--resume"],
        ["--new-scan"],
    ],
)
def test_since_last_rejects_conflicting_modes(store, today, monkeypatch, capsys, extra):
    monkeypatch.setattr(cli, "GarminSource", lambda _: pytest.fail("bad arguments authenticated"))
    assert command(store, capsys, "sync", "--since-last", *extra)[1]["error"] == "usage"


def test_since_last_overlap_crosses_year_boundary():
    state = empty_state()
    state["covered_ranges"] = [{"start": "2025-01-01", "end": "2025-01-01"}]
    assert sync.since_last_range(state, date(2025, 1, 3)) == {
        "start": "2024-12-31",
        "end": "2025-01-03",
    }


def guard_preview(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("dry-run constructed a client, authenticated, locked or synchronized")

    for name in ("GarminSource", "authenticate", "synchronize"):
        monkeypatch.setattr(cli, name, denied)
    monkeypatch.setattr(sqlite_store.SQLiteStore, "lock", denied)
    monkeypatch.setattr("garmin_bridge.storage.Store.lock", denied)
    monkeypatch.setattr("garmin_bridge.storage.fcntl.flock", denied)
    connect = sqlite_store.connect

    def read_only(path, write=False, immutable=False):
        assert not write and not immutable
        return connect(path, write, immutable)

    monkeypatch.setattr(sqlite_store, "connect", read_only)


def saved_files(store):
    return {
        p.relative_to(store.root): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in store.root.rglob("*")
        if p.is_file()
    }


def test_dry_run_matches_real_window_without_client_lock_or_store_changes(
    store, today, monkeypatch, capsys
):
    run(store, Source())
    run(store, Source([]), {"start": "2025-01-08", "end": "2025-01-09"})
    before_state = store.state()
    store.path(".lock").unlink()
    before_files = saved_files(store)
    with monkeypatch.context() as guards:
        guard_preview(guards)
        code, preview, captured = command(store, capsys, "sync", "--since-last", "--dry-run")
        assert code == 0 and captured.err == ""
        assert preview == {"start": "2025-01-02", "end": today.isoformat()}
        assert saved_files(store) == before_files
        assert store.state() == before_state
        assert sync.watermark(store.state()) == INTERVAL
    monkeypatch.setattr(cli, "GarminSource", lambda _: Source())
    code, result, _ = command(store, capsys, "sync", "--since-last")
    assert code == 0
    assert preview == {key: result["filter"][key] for key in ("start", "end")}


@pytest.mark.parametrize("kind", ["absent", "empty", "pending_backfill"])
def test_dry_run_no_watermark_is_offline_and_leaves_store_unchanged(
    store, today, monkeypatch, capsys, kind
):
    if kind != "absent":
        state = empty_state()
        if kind == "pending_backfill":
            state["covered_ranges"] = [INTERVAL]
            state["pending_ranges"] = [{"start": "2024-12-01", "end": "2024-12-02"}]
        with store.lock(write=True):
            store.write_json("index.json", state)
    before = saved_files(store)
    guard_preview(monkeypatch)
    code, result, captured = command(store, capsys, "sync", "--since-last", "--dry-run")
    assert code == 2 and result["error"] == "no_watermark" and captured.out == ""
    assert saved_files(store) == before
    if kind == "absent":
        assert not store.root.exists()


@pytest.mark.parametrize("extra", [[], ["--full"], ["--start", "2025-01-01"]])
def test_dry_run_requires_since_last_before_locking(store, monkeypatch, capsys, extra):
    guard_preview(monkeypatch)
    code, result, _ = command(store, capsys, "sync", "--dry-run", *extra)
    assert code == 2 and result["error"] == "dry_run_requires_since_last"
    assert "--dry-run requires --since-last" in result["message"]
    assert not store.root.exists()


@pytest.mark.parametrize(
    "extra",
    [["--start", "2025-01-01"], ["--end", "2025-01-10"], ["--full"], ["--resume"], ["--new-scan"]],
)
def test_dry_run_rejects_conflicting_options_before_locking(store, monkeypatch, capsys, extra):
    guard_preview(monkeypatch)
    code, result, _ = command(store, capsys, "sync", "--since-last", "--dry-run", *extra)
    assert code == 2 and result["error"] == "usage"
    assert not store.root.exists()


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_dry_run_refuses_sidecars_without_attempting_recovery(
    store, today, monkeypatch, capsys, suffix
):
    run(store, Source())
    sidecar = store.path(sqlite_store.DATABASE + suffix)
    sidecar.write_bytes(b"synthetic unfinished transaction")
    sidecar.chmod(0o600)
    before = saved_files(store)
    guard_preview(monkeypatch)
    monkeypatch.setattr(sqlite_store, "connect", lambda *a, **k: pytest.fail("opened sidecar DB"))
    code, result, _ = command(store, capsys, "sync", "--since-last", "--dry-run")
    assert code == 2 and result["error"] == "busy"
    assert saved_files(store) == before


def test_dry_run_refuses_wal_mode_before_sqlite_can_create_sidecars(
    store, today, monkeypatch, capsys
):
    run(store, Source())
    with closing(sqlite_store.connect(store.path(sqlite_store.DATABASE), write=True)) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    assert not store.path(sqlite_store.DATABASE + "-wal").exists()
    assert not store.path(sqlite_store.DATABASE + "-shm").exists()
    before = saved_files(store)
    guard_preview(monkeypatch)
    monkeypatch.setattr(sqlite_store, "connect", lambda *a, **k: pytest.fail("opened WAL DB"))
    code, result, _ = command(store, capsys, "sync", "--since-last", "--dry-run")
    assert saved_files(store) == before
    assert code == 2 and result["error"] == "busy"


def test_dry_run_output_still_has_a_closed_contract(store, today, monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "execute",
        lambda _: {"start": "2025-01-01", "end": "2025-01-10", "unreviewed": "synthetic"},
    )
    code, result, captured = command(store, capsys, "sync", "--since-last", "--dry-run")
    assert code == 2 and result["error"] == "privacy_failed" and captured.out == ""
