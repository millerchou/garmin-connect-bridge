import copy
from datetime import date
from unittest.mock import Mock

import pytest

from garmin_bridge import normalize, sync
from garmin_bridge.errors import BridgeError
from .synthetic import Source, activity, archive, fit_bytes

INTERVAL = {"start": "2025-01-01", "end": "2025-01-03"}


def run(store, source, interval=None):
    with store.lock(write=True):
        return sync.synchronize(store, lambda: source, interval or INTERVAL)


def test_dedup_and_unchanged_inputs_skip_parsing_even_if_zip_changes(store, monkeypatch):
    spy = Mock(wraps=normalize.normalize)
    monkeypatch.setattr(normalize, "normalize", spy)
    source = Source([activity(), activity()])
    result = run(store, source)
    assert result["updated"] == 1 and result["complete"]
    assert source.calls.count(("download", "1001")) == 1
    original = copy.deepcopy(store.state()["activities"])
    source.downloads["1001"] = archive(comment=b"synthetic-new-zip-container")
    source.activities[0]["activityName"] = "different unreviewed free text"
    result = run(store, source)
    assert result["unchanged"] == 1 and result["updated"] == 0
    assert spy.call_count == 1
    assert store.state()["activities"] == original


@pytest.mark.parametrize("correction", ["fit", "metadata"])
def test_correction_replaces_only_affected_result(store, monkeypatch, correction):
    source = Source([activity(1001), activity(1002)])
    run(store, source)
    previous = copy.deepcopy(store.state()["activities"])
    spy = Mock(wraps=normalize.normalize)
    monkeypatch.setattr(normalize, "normalize", spy)
    if correction == "fit":
        source.downloads["1001"] = archive(fit_bytes(power=230))
    else:
        source.activities[0]["startTimeLocal"] = "2025-01-02 09:00:00"
    result = run(store, source)
    current = store.state()["activities"]
    assert result["updated"] == 1 and result["unchanged"] == 1
    assert spy.call_count == 1
    assert (
        current["1001"]["summary"]["source_digest"] != previous["1001"]["summary"]["source_digest"]
    )
    assert current["1002"] == previous["1002"]
    for ref in previous["1001"]["summary"]["files"].values():
        assert store.path(ref).is_file()


def test_normalizer_version_invalidates_parse_cache(store, monkeypatch):
    source = Source()
    run(store, source)
    previous = store.state()["activities"]["1001"]["summary"]
    monkeypatch.setattr(sync, "NORMALIZER_VERSION", "1.0.1")
    monkeypatch.setattr(normalize, "NORMALIZER_VERSION", "1.0.1")
    result = run(store, source)
    current = store.state()["activities"]["1001"]["summary"]
    assert result["updated"] == 1 and current["normalizer_version"] == "1.0.1"
    assert current["source_digest"] == previous["source_digest"]
    assert current["files"] != previous["files"]


def test_partial_failure_retry_commits_only_complete_activity_results(store, monkeypatch):
    source = Source([activity(1001), activity(1002)], {"1002": BridgeError("network_failed")})
    first = run(store, source)
    assert not first["complete"] and first["failed"] == 1
    assert sync.watermark(store.state()) is None
    assert list(store.state()["activities"]) == ["1001"]
    assert store.state()["pending_ranges"] == [INTERVAL]
    source.downloads.clear()
    spy = Mock(wraps=normalize.normalize)
    monkeypatch.setattr(normalize, "normalize", spy)
    second = run(store, source)
    # Counts cover the durable job, including work committed before the retry.
    assert second["updated"] == 2 and second["complete"]
    assert spy.call_count == 1
    assert sync.watermark(store.state()) == INTERVAL
    assert not store.state()["pending_ranges"]


def test_invalid_corrected_fit_preserves_last_usable_result_and_watermark(store):
    source = Source()
    run(store, source)
    previous = copy.deepcopy(store.state())
    source.downloads["1001"] = archive(fit_bytes()[:-2] + b"\x00\x00")
    result = run(store, source, {"start": "2025-01-01", "end": "2025-01-05"})
    current = store.state()
    assert not result["complete"] and result["last_failure"] == "invalid_fit"
    assert current["activities"] == previous["activities"]
    assert current["last_success_at"] == previous["last_success_at"]
    assert sync.watermark(current) == sync.watermark(previous)


def test_interruption_keeps_checkpoint_and_resumes_pending_range(store):
    source = Source([activity(1001), activity(1002)], {"1002": KeyboardInterrupt()})
    with pytest.raises(KeyboardInterrupt):
        run(store, source)
    state = store.state()
    assert sync.watermark(state) is None
    assert state["last_failure"] == "interrupted"
    assert set(state["activities"]) == {"1001"}
    interval = sync.choose_range(state, today=date(2025, 1, 20), lookback=3)
    assert interval == {"start": "2025-01-01", "end": "2025-01-20"}
    source.downloads.clear()
    with store.lock(write=True):
        resumed = sync.synchronize(store, lambda: source, mode="resume")
    assert resumed["complete"] and resumed["filter"]["end"] == INTERVAL["end"]


@pytest.mark.parametrize("target", ["streams", "index"])
def test_failed_atomic_write_cannot_replace_prior_index(store, monkeypatch, target):
    source = Source()
    run(store, source)
    previous = copy.deepcopy(store.state())
    source.downloads["1001"] = archive(fit_bytes(power=240))
    write = store.write_bytes
    count = 0

    def fail(relative, content):
        nonlocal count
        if relative == "index.json":
            count += 1
        if (target == "streams" and relative.endswith("/streams.json")) or (
            target == "index" and count == 2
        ):
            raise OSError("synthetic storage failure")
        return write(relative, content)

    monkeypatch.setattr(store, "write_bytes", fail)
    with pytest.raises(OSError):
        run(store, source)
    state = store.state()
    assert state["activities"] == previous["activities"]
    assert sync.watermark(state) == sync.watermark(previous)
    assert state["pending_ranges"] == [INTERVAL]
    monkeypatch.setattr(store, "write_bytes", write)
    assert run(store, source)["complete"]


def test_disconnected_success_does_not_skip_gap_and_old_backfill_does_not_regress(store):
    run(store, Source([]))
    later = {"start": "2025-01-08", "end": "2025-01-10"}
    run(store, Source([]), later)
    assert sync.watermark(store.state()) == INTERVAL
    run(store, Source([]), {"start": "2025-01-04", "end": "2025-01-07"})
    assert sync.watermark(store.state()) == {"start": "2025-01-01", "end": "2025-01-10"}
    run(store, Source([]))
    assert sync.watermark(store.state())["end"] == "2025-01-10"


def test_other_sync_does_not_drop_unrelated_pending_backfill(store):
    with pytest.raises(KeyboardInterrupt):
        run(store, Source(downloads={"1001": KeyboardInterrupt()}))
    with pytest.raises(BridgeError) as error:
        run(store, Source([]), {"start": "2025-01-08", "end": "2025-01-10"})
    assert error.value.code == "resume_required"
    assert store.state()["pending_ranges"] == [INTERVAL]
    assert sync.watermark(store.state()) is None


def test_lookback_and_explicit_date_rescan(store):
    run(store, Source([]))
    assert sync.choose_range(store.state(), today=date(2025, 1, 10), lookback=2) == {
        "start": "2025-01-02",
        "end": "2025-01-10",
    }
    assert sync.choose_range(
        store.state(), "2024-12-01", "2024-12-31", today=date(2025, 1, 10)
    ) == {"start": "2024-12-01", "end": "2024-12-31"}


def test_missing_artifact_is_repaired_without_duplicating_activity(store):
    source = Source()
    run(store, source)
    summary = store.state()["activities"]["1001"]["summary"]
    store.path(summary["files"]["streams"]).unlink()
    assert not sync.files_available(store, summary)
    result = run(store, source)
    assert result["updated"] == 1 and len(store.state()["activities"]) == 1
    assert sync.files_available(store, summary)


def test_only_reviewed_cycling_types_and_safe_ids(store):
    source = Source([activity(1001), activity(1002, activityType={"typeKey": "running"})])
    assert run(store, source)["updated"] == 1
    assert ("download", "1002") not in source.calls
    assert not run(store, Source([activity("../../unsafe")]))["complete"]


def test_conflicting_duplicate_discovery_fails_before_downloading(store):
    source = Source([activity(), activity(distance=999)])
    result = run(store, source)
    assert not result["complete"] and result["last_failure"] == "upstream_invalid"
    assert not any(c[0] == "download" for c in source.calls)
