"""Independent privacy review: invented FIT bytes and synthetic stores only."""

import copy
import json
from unittest.mock import Mock

import pytest

from garmin_bridge import analysis_export as export, contract as c, fit_unknown, jobs, normalize
from garmin_bridge import sqlite_transfer as transfer
from garmin_bridge.errors import BridgeError
from garmin_bridge.fit_details import timestamp_value
from garmin_bridge.sqlite_store import SQLiteStore
from garmin_bridge.storage import canonical
from .fit_synthetic import frame, minimal, raw
from .synthetic import Source, activity
from .test_analysis_export import publish, read_table
from .test_fit_full_review import intercepted
from .test_history import execute
from .test_sync import run


@pytest.mark.parametrize("identity", [False, True])
@pytest.mark.parametrize("field", [28, 29])
def test_known_profile_clock_settings_are_not_identifiers(identity, field):
    value = 20 * 3600
    streams, blocked = intercepted(
        minimal(frame(3, [raw(field, 0x86, value)]) + frame(534, [raw(0, 0x86, value)])),
        identity,
    )
    assert streams["raw_messages"][0]["fields"] == {"0": value}
    assert blocked == ({} if identity else {(3, field, "identity_message"): 1})


@pytest.mark.parametrize("value", [-(2**24 - 1), -(2**24), -(2**63)])
def test_signed_magnitudes_and_invalid_slots(value):
    streams, blocked = intercepted(minimal(frame(534, [raw(0, 0x8E, [value, 2**63 - 1])])))
    if abs(value) >= 2**24:
        assert streams["raw_messages"] == []
        assert blocked == {(534, 0, "suspected_identity"): 1}
    else:
        assert streams["raw_messages"][0]["fields"] == {"0": [value, None]}
        assert not blocked


@pytest.mark.parametrize(
    "base,invalid", [(0x86, 0xFFFFFFFF), (0x8C, 0), (0x8E, 2**63 - 1), (0x8F, 2**64 - 1), (0x90, 0)]
)
def test_sentinels_do_not_supply_variation_or_identity_values(base, invalid):
    extra = frame(534, [raw(0, base, [invalid, invalid])]) * 4
    streams, blocked = intercepted(minimal(extra))
    assert not blocked
    assert [r["fields"] for r in streams["raw_messages"]] == [{"0": [None, None]}] * 4
    streams, blocked = intercepted(minimal(extra + frame(534, [raw(0, base, [2**24, invalid])])))
    assert streams["raw_messages"] == []
    assert blocked == {(534, 0, "suspected_identity"): 5}


@pytest.mark.parametrize("offset", [0, 11, 3600])
def test_timestamp_evidence_requires_matching_variation_per_array_slot(offset):
    stamp = 1_000_000_000
    records = b"".join(frame(20, [raw(253, 0x86, stamp + i)]) for i in range(3))
    extra = b"".join(frame(534, [raw(0, 0x86, [stamp + offset + i, 0xFFFFFFFF])]) for i in range(3))
    streams, blocked = intercepted(minimal(extra, records=records))
    if offset:
        assert blocked == {(534, 0, "suspected_identity"): 3}
        assert streams["raw_messages"] == []
    else:
        assert not blocked
        assert [r["fields"]["0"] for r in streams["raw_messages"]] == [
            [stamp + i, None] for i in range(3)
        ]
    # A constant slot cannot borrow the varying slot's time evidence.
    extra = b"".join(frame(534, [raw(0, 0x86, [stamp, stamp + i])]) for i in range(3))
    streams, blocked = intercepted(minimal(extra, records=records))
    assert not streams["raw_messages"]
    assert blocked == {(534, 0, "suspected_identity"): 3}


def test_varying_signed_counters_and_reviewed_times_survive_all_handoffs(store, tmp_path):
    stamp = 1_000_000_000
    records = b"".join(frame(20, [raw(253, 0x86, stamp + i)]) for i in range(3))
    extra = b"".join(frame(534, [raw(0, 0x8E, -(2**40) + i)]) for i in range(8))
    extra += b"".join(frame(534, [raw(1, 0x86, stamp + i)]) for i in range(3))
    assert publish(store, minimal(extra, records=records))
    summary = store.state()["activities"]["1001"]["summary"]
    streams = store.read_json(summary["files"]["streams"])
    target = tmp_path / "review-analysis.sqlite"
    export.export_analysis(store, target)
    assert [json.loads(r["fields_json"]) for r in read_table(target, "raw_messages")] == [
        r["fields"] for r in streams["raw_messages"]
    ]
    snapshot = tmp_path / "review-snapshot"
    transfer.export_snapshot(store, snapshot)
    assert transfer.verify_snapshot(snapshot)["verified"]
    restored = SQLiteStore(tmp_path / "review-restored")
    transfer.restore_snapshot(snapshot, restored.root)
    assert restored.read_json(summary["files"]["streams"]) == streams


@pytest.mark.parametrize("value", [-(2**40), 1_000_000_001])
def test_old_unsafe_raw_values_fail_all_handoffs_without_values_in_errors(
    store, tmp_path, monkeypatch, capsys, value
):
    assert publish(store, minimal())
    target = tmp_path / "review-analysis.sqlite"
    export.export_analysis(store, target)
    before = target.read_bytes()
    snapshot = tmp_path / "review-old-snapshot"
    with monkeypatch.context() as old:
        old.setattr(fit_unknown, "suspected_identity_fields", lambda _: {})
        assert publish(
            store,
            minimal(
                frame(534, [raw(0, 0x8E, value)]),
                records=frame(20, [raw(253, 0x86, 1_000_000_000)]),
            ),
        )
        transfer.export_snapshot(store, snapshot)
    for action in (
        lambda: export.export_analysis(store, target),
        lambda: transfer.export_snapshot(store, tmp_path / "review-rejected-export"),
        lambda: transfer.verify_snapshot(snapshot),
        lambda: transfer.restore_snapshot(snapshot, tmp_path / "review-rejected-restore"),
    ):
        with pytest.raises(BridgeError) as caught:
            action()
        assert caught.value.code == "privacy_failed"
        assert str(value) not in str(caught.value)
    captured = capsys.readouterr()
    assert str(value) not in captured.out + captured.err
    assert target.read_bytes() == before
    assert not (tmp_path / "review-rejected-export").exists()
    assert {
        str(p.relative_to(tmp_path / "review-rejected-restore"))
        for p in (tmp_path / "review-rejected-restore").rglob("*")
        if p.is_file()
    } == {"reports/privacy-failure.json"}


@pytest.mark.parametrize("table", ["records", "laps", "messages", "raw_messages"])
def test_analysis_rechecks_suspected_ids_after_construction(store, tmp_path, monkeypatch, table):
    assert publish(store, minimal(frame(534, [raw(0, 0x86, 7)])))
    target = tmp_path / "review-analysis.sqlite"
    export.export_analysis(store, target)
    before = target.read_bytes()
    insert = export.insert

    def inject(db, name, row):
        row = dict(row)
        if name == table:
            if name == "messages":
                value = json.loads(row["detail_json"])
                value["unknown"] = {"unknown_250": [None, -(2**40)]}
                row["detail_json"] = export.json_text(value)
            else:
                key = "fields_json" if name == "raw_messages" else "unknown_json"
                field = "250" if name == "raw_messages" else "unknown_250"
                row[key] = export.json_text({field: [None, -(2**40)]})
        return insert(db, name, row)

    monkeypatch.setattr(export, "insert", inject)
    with pytest.raises(BridgeError) as caught:
        export.export_analysis(store, target)
    assert caught.value.code == "privacy_failed"
    assert target.read_bytes() == before


@pytest.mark.parametrize("value", [-1, 1.5, 255, 256, True])
@pytest.mark.parametrize("field", ["event", "event_type"])
def test_lap_enum_contract_rejects_non_codes(value, field):
    streams, _ = intercepted(minimal(lap=frame(19, [raw(0, 0, 0), raw(1, 0, 0)])))
    streams["laps"][0]["fit"]["fields"][field]["value"] = value
    with pytest.raises(BridgeError) as caught:
        c.validate_streams(streams)
    assert caught.value.code == "privacy_failed"


def old_input_digest(meta, fit_hash, *, local_import=False, include_identity=False, legacy=False):
    """Exactly the pre-discriminator key, using synthetic metadata and bytes."""
    value = {"provider": "garmin", "metadata": meta, "fit_sha256": fit_hash}
    if local_import:
        value["input"] = "local_fit_file"
    if include_identity:
        value["include_fit_identity"] = True
    return normalize.sha256(canonical(value))


@pytest.mark.parametrize("mode", ["offline", "range", "incremental", "resume"])
def test_same_normalizer_version_reprocesses_old_cache_and_preserves_history(
    store, monkeypatch, mode
):
    monkeypatch.setattr(jobs, "now", lambda: "2025-02-01T00:00:00Z")
    source = Source()
    if mode == "resume":
        source = Source([activity(), activity(1002)], {"1002": BridgeError("network_failed")})
    with monkeypatch.context() as old:
        old.setattr(normalize, "input_digest", old_input_digest)
        if mode == "offline":
            assert publish(store, minimal())
        elif mode in {"incremental", "resume"}:
            result = execute(store, source)
            assert result["complete"] is (mode != "resume")
        else:
            assert run(store, source)["complete"]
    before = copy.deepcopy(store.state())
    prior = before["activities"]["1001"]["summary"]
    saved = {path: store.read_bytes(path) for path in prior["files"].values()}
    spy = Mock(wraps=normalize.normalize)
    monkeypatch.setattr(normalize, "normalize", spy)
    source.downloads.clear()
    if mode == "offline":
        assert publish(store, minimal())
        assert not publish(store, minimal())
    elif mode in {"incremental", "resume"}:
        assert execute(store, source, mode=mode)["complete"]
    else:
        assert run(store, source)["updated"] == 1
        assert run(store, source)["unchanged"] == 1
    current = store.state()["activities"]["1001"]["summary"]
    assert current["normalizer_version"] == prior["normalizer_version"] == "1.4.0"
    assert current["files"] != prior["files"]
    assert all(store.read_bytes(path) == data for path, data in saved.items())
    assert spy.call_count == (2 if mode == "resume" else 1)
    if mode in {"offline", "range"}:
        assert store.state()["covered_ranges"] == before["covered_ranges"]


def test_time_evidence_is_not_shared_between_activities(store, tmp_path, monkeypatch):
    assert run(store, Source([activity(), activity(1002)]))["complete"]
    insert = export.insert

    def inject(db, table, row):
        row = dict(row)
        if table == "records":
            # The first activity has raw values; only the second supplies times.
            stamp = 1_000_000_000 + row["ordinal"]
            if row["activity_id"] == "1001":
                row["timestamp"] = None
                row["unknown_json"] = export.json_text({"unknown_250": stamp})
            else:
                row["timestamp"] = timestamp_value(stamp)
        return insert(db, table, row)

    monkeypatch.setattr(export, "insert", inject)
    with pytest.raises(BridgeError):
        export.export_analysis(store, tmp_path / "review-cross-activity.sqlite")
