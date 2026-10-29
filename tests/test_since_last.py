"""The incremental date shortcut uses successful contiguous coverage only."""

from datetime import date

import pytest

from garmin_bridge import cli, sync
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
