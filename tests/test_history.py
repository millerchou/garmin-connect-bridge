import copy
import json
import subprocess
import sys
from unittest.mock import Mock

import pytest

from garmin_bridge import cli, jobs, normalize, sync
from garmin_bridge.contract import validate_output
from garmin_bridge.errors import BridgeError
from garmin_bridge.storage import Store
from .synthetic import START, Source, activity, archive, fit_bytes

CUTOFF = "2025-01-10T12:00:00Z"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(jobs, "now", lambda: CUTOFF)
    monkeypatch.setattr(sync, "now", lambda: CUTOFF)


def execute(store, source, mode="full", **options):
    with store.lock(write=True):
        result = sync.synchronize(store, lambda: source, mode=mode, page_size=2, **options)
    return validate_output("sync", result)


def test_new_store_requires_full_import_and_all_history_is_unbounded(store, capsys):
    assert cli.main(["--data-dir", str(store.root), "sync"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "initial_import_required"
    source = Source(
        [
            activity(1, startTimeGMT="2010-01-01 00:00:00", startTimeLocal="2010-01-01 08:00:00"),
            activity(2),
            activity(2),
            activity(3),
        ]
    )
    result = execute(store, source)
    assert result["complete"] and result["enumeration"]["complete"]
    assert result["enumeration"]["passes"] == 2
    assert result["counts"] == {
        "discovered": 3,
        "completed": 3,
        "skipped": 0,
        "pending": 0,
        "retry": 0,
        "unavailable": 0,
    }
    assert result["cutoff_at"] == CUTOFF
    assert result["filter"]["start"] is None and result["filter"]["end"] is None
    assert result["observed_range"]["start"] == "2010-01-01"
    assert source.calls.count(("download", "2")) == 1
    assert all(call[1:3] == (None, None) for call in source.calls if call[0] == "page")
    assert store.state()["history_initialized"]


def test_mutating_offset_catalog_reconciles_new_old_dated_upload(store):
    class Changing(Source):
        def page(self, start, end, offset, limit):
            if offset == 2 and len(self.activities) == 3:
                self.activities.insert(
                    0,
                    activity(
                        4, startTimeGMT="2020-01-01 00:00:00", startTimeLocal="2020-01-01 08:00:00"
                    ),
                )
            return super().page(start, end, offset, limit)

    source = Changing([activity(1), activity(2), activity(3)])
    result = execute(store, source)
    assert result["complete"] and result["enumeration"]["passes"] == 3
    assert set(store.state()["activities"]) == {"1", "2", "3", "4"}
    assert all(source.calls.count(("download", str(i))) == 1 for i in range(1, 5))


def test_force_exit_resumes_page_and_activity_and_matches_uninterrupted(
    store, tmp_path, monkeypatch
):
    script = """
import os, sys
from pathlib import Path
from garmin_bridge import jobs, sync
from garmin_bridge.storage import Store
from tests.synthetic import Source, activity
jobs.now = sync.now = lambda: "2025-01-10T12:00:00Z"
class Killed(Source):
    def download(self, source_id):
        if source_id == "2":
            os._exit(9)
        return super().download(source_id)
store = Store(Path(sys.argv[1]))
with store.lock(write=True):
    sync.synchronize(store, lambda: Killed([activity(1), activity(2), activity(3)]), mode="full", page_size=2)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(store.root)], capture_output=True, timeout=15
    )
    assert child.returncode == 9 and child.stdout == b"" and child.stderr == b""
    assert set(store.state()["activities"]) == {"1"}
    assert not store.state()["history_initialized"]
    assert jobs.load_job(store)["offset"] == 2
    source = Source([activity(1), activity(2), activity(3)])
    spy = Mock(wraps=normalize.normalize)
    monkeypatch.setattr(normalize, "normalize", spy)
    result = execute(store, source, mode="resume")
    assert result["complete"] and spy.call_count == 2
    assert ("download", "1") not in source.calls
    assert next(c for c in source.calls if c[0] == "page")[3] == 2
    once = Store(tmp_path / "uninterrupted")
    execute(once, Source([activity(1), activity(2), activity(3)]))
    resumed = {k: v["summary"] for k, v in store.state()["activities"].items()}
    direct = {k: v["summary"] for k, v in once.state()["activities"].items()}
    assert resumed == direct


def test_corrupt_or_unavailable_activity_is_explicit_gap_not_full_success(store, capsys):
    source = Source(
        [activity(1), activity(2), activity(3)],
        {"2": b"broken-fit", "3": BridgeError("activity_unavailable")},
    )
    result = execute(store, source)
    assert not result["complete"] and result["status"] == "incomplete"
    assert result["enumeration"]["complete"] and result["gap_count"] == 2
    assert result["counts"]["completed"] == 1 and result["counts"]["unavailable"] == 2
    assert sync.watermark(store.state()) is None
    report = store.read_json(result["gap_report"])
    assert [g["source_activity_id"] for g in report["gaps"]] == ["2", "3"]
    assert cli.main(["--data-dir", str(store.root), "activity", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["activity"]["source_activity_id"] == "1"
    assert cli.main(["--data-dir", str(store.root), "status"]) == 0
    assert json.loads(capsys.readouterr().out)["sync"]["status"] == "incomplete"
    source.downloads.clear()
    result = execute(store, source, mode="resume")
    assert result["complete"] and result["gap_count"] == 0


def test_unchanged_full_rescan_never_reparses_and_no_duplicate_identity(store, monkeypatch):
    source = Source([activity(1), activity(2)])
    execute(store, source)
    original = copy.deepcopy(store.state()["activities"])
    spy = Mock(wraps=normalize.normalize)
    monkeypatch.setattr(normalize, "normalize", spy)
    result = execute(store, source)
    assert result["complete"] and result["unchanged"] == 2 and result["updated"] == 0
    assert spy.call_count == 0 and store.state()["activities"] == original


def test_daily_catalog_detects_delayed_upload_and_only_downloads_new_id(store, monkeypatch):
    source = Source([activity(1), activity(2)])
    execute(store, source)
    original = copy.deepcopy(store.state()["activities"])
    source.calls.clear()
    source.activities.append(
        activity(3, startTimeGMT="2020-01-01 00:00:00", startTimeLocal="2020-01-01 08:00:00")
    )
    spy = Mock(wraps=normalize.normalize)
    monkeypatch.setattr(normalize, "normalize", spy)
    result = execute(store, source, mode="incremental", lookback=3)
    assert result["complete"] and result["updated"] == 1 and result["counts"]["skipped"] == 2
    assert [c for c in source.calls if c[0] == "download"] == [("download", "3")]
    assert spy.call_count == 1
    assert all(store.state()["activities"][key] == value for key, value in original.items())


def test_recent_recheck_and_explicit_old_review_update_one_identity(store):
    recent = activity(2, startTimeGMT="2025-01-09 00:00:00", startTimeLocal="2025-01-09 08:00:00")
    source = Source([activity(1), recent])
    execute(store, source)
    old = copy.deepcopy(store.state()["activities"])
    source.downloads = {"1": archive(fit_bytes(power=250)), "2": archive(fit_bytes(power=230))}
    result = execute(store, source, mode="incremental", lookback=3)
    assert result["updated"] == 1 and store.state()["activities"]["1"] == old["1"]
    assert store.state()["activities"]["2"]["summary"]["metrics"]["avg_power"]["value"] == 230
    with store.lock(write=True):
        result = sync.synchronize(
            store, lambda: source, {"start": "2025-01-01", "end": "2025-01-03"}
        )
    assert result["complete"] and result["updated"] == 1
    assert len(store.state()["activities"]) == 2
    assert store.state()["activities"]["1"]["summary"]["metrics"]["avg_power"]["value"] == 250


def test_old_metadata_change_also_triggers_recheck(store):
    source = Source([activity(1)])
    execute(store, source)
    source.calls.clear()
    source.activities[0]["avgPower"] = 230
    source.downloads["1"] = archive(fit_bytes(power=230))
    result = execute(store, source, mode="incremental", lookback=3)
    assert result["updated"] == 1 and ("download", "1") in source.calls


@pytest.mark.parametrize("mode", ["incremental", "resume"])
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_rechecks_and_resume_repair_damaged_confirmed_artifacts(store, mode, damage):
    source = Source([activity(1), activity(2)])
    if mode == "resume":
        source.downloads["2"] = BridgeError("network_failed")
    result = execute(store, source)
    assert result["complete"] == (mode == "incremental")
    ref = store.state()["activities"]["1"]["summary"]["files"]["streams"]
    valid = store.read_bytes(ref)
    with store.lock(write=True):
        if damage == "missing":
            store.path(ref).unlink()
        else:
            store.write_bytes(ref, b"synthetic damaged content")
    source.calls.clear()
    source.downloads.clear()
    result = execute(store, source, mode=mode, lookback=3)
    assert result["complete"] and store.read_bytes(ref) == valid
    assert ("download", "1") in source.calls
    assert len(store.state()["activities"]) == 2


def test_backoff_is_bounded_and_retry_state_survives_exhaustion(store):
    class Limited(Source):
        failures = 2

        def download(self, source_id):
            if self.failures:
                self.failures -= 1
                raise BridgeError("rate_limited")
            return super().download(source_id)

    delays = []
    result = execute(store, Limited(), sleep=delays.append)
    assert result["complete"] and delays == [1, 2]
    assert jobs.load_job(store)["items"]["1001"]["attempts"] == 3
    old_watermark = sync.watermark(store.state())
    source = Source([activity(1001), activity(2)], {"2": BridgeError("rate_limited")})
    delays.clear()
    result = execute(store, source, sleep=delays.append)
    assert not result["complete"] and result["counts"]["retry"] == 1
    assert delays == [1, 2] and result["last_failure"] == "rate_limited"
    assert sync.watermark(store.state()) == old_watermark
    assert jobs.load_job(store)["items"]["2"]["attempts"] == 3
    source.downloads.clear()
    assert execute(store, source, mode="resume")["complete"]


def test_page_network_failure_and_auth_expiry_preserve_progress(store):
    class InterruptedPage(Source):
        fail = True

        def page(self, start, end, offset, limit):
            if self.fail and offset == 2:
                raise BridgeError("network_failed")
            return super().page(start, end, offset, limit)

    source = InterruptedPage([activity(1), activity(2), activity(3)])
    result = execute(store, source)
    assert not result["complete"] and result["last_failure"] == "network_failed"
    assert jobs.load_job(store)["offset"] == 2 and len(store.state()["activities"]) == 2
    before = copy.deepcopy(store.state()["activities"])

    def expired():
        raise BridgeError("reauth_required")

    with store.lock(write=True):
        result = sync.synchronize(store, expired, mode="resume")
    assert result["last_failure"] == "reauth_required" and not result["complete"]
    assert store.state()["activities"] == before
    source.fail = False
    assert execute(store, source, mode="resume")["complete"]


def test_page_budget_and_nonconverging_catalog_are_incomplete(store):
    source = Source([activity(i) for i in range(1, 5)])
    result = execute(store, source, max_pages=2)
    assert not result["complete"] and result["enumeration"]["limit_reached"]
    assert result["gap_count"] == 0 and result["counts"]["completed"] == 4
    assert not store.state()["history_initialized"]
    assert execute(store, source, mode="resume", max_pages=3)["complete"]

    class Unstable(Source):
        passes = 0

        def page(self, start, end, offset, limit):
            if offset == 0:
                self.passes += 1
                self.activities = [activity(1, averageHR=100 + self.passes)]
            return super().page(start, end, offset, limit)

    result = execute(store, Unstable())
    assert not result["complete"] and result["last_failure"] == "catalog_unstable"


def test_cutoff_exclusions_are_explicit_and_missing_history_fields_not_filled(store):
    source = Source(
        [
            activity(1, startTimeGMT="2025-01-10 13:00:00", startTimeLocal="2025-01-10 21:00:00"),
            activity(2, startTimeGMT="2025-01-10 11:00:00", startTimeLocal="2025-01-11 01:00:00"),
            activity(3, startTimeGMT=None, startTimeLocal=None, timeZoneUnitDTO=None),
        ]
    )
    result = execute(store, source)
    assert result["complete"] and result["counts"]["skipped"] == 1
    assert ("download", "1") not in source.calls
    assert "2" in store.state()["activities"]  # local date differs from UTC cutoff date
    summary = store.state()["activities"]["3"]["summary"]
    assert summary["activity_time"]["timezone"] is None
    assert summary["metrics"]["max_heart_rate"]["value"] is None


def test_new_scan_carries_unresolved_ids_and_catalog_absence_never_deletes(store):
    source = Source([activity(1), activity(2)], {"2": BridgeError("activity_unavailable")})
    assert not execute(store, source)["complete"]
    source.activities = [activity(3)]
    result = execute(store, source, new_scan=True)
    assert not result["complete"] and result["gaps"][0]["source_activity_id"] == "2"
    assert set(store.state()["activities"]) == {"1", "3"}
    source.downloads.clear()
    assert execute(store, source, mode="resume")["complete"]
    source.activities = []
    assert execute(store, source)["complete"]
    assert set(store.state()["activities"]) == {"1", "2", "3"}


def test_after_cutoff_additions_do_not_prevent_catalog_convergence(store):
    class Newer(Source):
        def page(self, start, end, offset, limit):
            if offset == 0:
                self.activities.append(
                    activity(
                        len(self.activities) + 1,
                        startTimeGMT="2025-01-10 13:00:00",
                        startTimeLocal="2025-01-10 21:00:00",
                    )
                )
            return super().page(start, end, offset, limit)

    result = execute(store, Newer([activity(1)]))
    assert result["complete"] and result["enumeration"]["passes"] == 2
    assert set(store.state()["activities"]) == {"1"}


def test_unknown_cycling_subtype_is_a_gap_while_other_activity_finishes(store):
    result = execute(
        store, Source([activity(1, activityType={"typeKey": "unreviewed_cycling"}), activity(2)])
    )
    assert not result["complete"] and result["gaps"][0]["reason"] == "unsupported_activity_type"
    assert result["counts"]["unavailable"] == 1 and "2" in store.state()["activities"]


def test_cutoff_is_also_checked_when_start_time_is_available_only_in_fit(store):
    source = Source(
        [activity(1, startTimeGMT=None, startTimeLocal=None)],
        {"1": archive(fit_bytes(start=START + 10 * 86400))},
    )
    result = execute(store, source)
    assert result["complete"] and result["counts"]["skipped"] == 1
    assert store.state()["activities"] == {}


def test_publication_before_journal_interruption_does_not_reparse_on_resume(store, monkeypatch):
    write = store.write_json

    def interrupt(relative, value):
        if relative == jobs.JOB_FILE and value["items"].get("1", {}).get("state") == "completed":
            raise KeyboardInterrupt
        return write(relative, value)

    monkeypatch.setattr(store, "write_json", interrupt)
    with pytest.raises(KeyboardInterrupt):
        execute(store, Source([activity(1)]))
    assert "1" in store.state()["activities"] and not store.state()["history_initialized"]
    monkeypatch.setattr(store, "write_json", write)
    spy = Mock(wraps=normalize.normalize)
    monkeypatch.setattr(normalize, "normalize", spy)
    result = execute(store, Source([activity(1)]), mode="resume")
    assert result["complete"] and spy.call_count == 0
