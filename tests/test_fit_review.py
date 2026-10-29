"""Independent review regressions; all FIT bytes and values are invented here."""

from datetime import timedelta

import pytest

from garmin_bridge import contract, sync
from garmin_bridge.errors import BridgeError
from garmin_bridge.fit_details import EPOCH
from garmin_bridge.sqlite_store import SQLiteStore
from garmin_bridge import sqlite_transfer as transfer
from garmin_bridge.import_fit import import_file
from .sanitize_synthetic import definition, frame, minimal, raw
from .synthetic import Source, activity
from .test_fit_fields import detail, parse
from .test_import_fit import private_file
from .test_sync import run


ANCHOR = 1_000_000_031  # Synthetic protocol clock, chosen to exercise a rollover.


def compressed(message, fields, offset, slot=1):
    return (
        definition(message, fields, slot=slot)
        + bytes((0x80 | slot << 5 | offset,))
        + b"".join(value for _, _, value in fields)
    )


@pytest.mark.parametrize(
    "message,number,fields",
    [
        ("event", 21, [raw(0, 0, 42), raw(1, 0, 3), raw(3, 0x86, 17)]),
        ("lap", 19, [raw(25, 0, 2)]),
        ("session", 18, [raw(5, 0, 2)]),
        ("activity", 34, [raw(1, 0x84, 1)]),
        ("device_info", 23, [raw(0, 2, 0)]),
    ],
)
def test_compressed_timestamps_survive_in_all_detail_views(message, number, fields):
    anchor = frame(20, [raw(253, 0x86, ANCHOR)])
    encoded = compressed(number, fields, 0)
    kwargs = {message: encoded} if message in {"lap", "session"} else {}
    data = minimal(b"" if kwargs else encoded, records=anchor, **kwargs)
    _, streams = parse(data)
    expected = (EPOCH + timedelta(seconds=ANCHOR + 1)).isoformat().replace("+00:00", "Z")
    assert detail(streams, message)["fields"]["timestamp"] == {"value": expected, "unit": "UTC"}


@pytest.mark.parametrize(
    "anchor",
    [
        frame(60000, [raw(253, 0x86, ANCHOR)]),
        frame(20, [raw(253, 0x85, ANCHOR)]),
        frame(20, [raw(253, 0x86, 0xFFFFFFFF)]),
    ],
    ids=["unknown-message", "wrong-wire-type", "invalid-sentinel"],
)
@pytest.mark.parametrize("preceding_valid", [False, True])
def test_unapproved_timestamp_cannot_enter_records_through_compression(anchor, preceding_valid):
    prefix = frame(20, [raw(253, 0x86, ANCHOR)]) if preceding_valid else b""
    data = minimal(records=prefix + anchor + compressed(20, [raw(7, 0x84, 17)], 0))
    with pytest.raises(BridgeError, match="FIT"):
        parse(data)


def test_compression_without_an_anchor_is_rejected():
    with pytest.raises(BridgeError, match="FIT"):
        parse(minimal(records=compressed(20, [raw(7, 0x84, 17)], 0)))


@pytest.mark.parametrize("identity", [False, True])
def test_developer_timestamp_cannot_replace_the_approved_clock(identity):
    descriptions = frame(207, [raw(3, 2, 0), raw(1, 13, b"SYNTHETIC-APP-ID")])
    descriptions += frame(
        206,
        [raw(0, 2, 0), raw(1, 2, 253), raw(2, 2, 0x86), raw(3, 7, b"timestamp\0")],
    )
    records = descriptions + frame(20, [raw(253, 0x86, ANCHOR)])
    records += frame(
        20, [raw(7, 0x84, 17)], developer=[(253, 0, (ANCHOR + 4096).to_bytes(4, "little"))]
    )
    records += compressed(20, [raw(7, 0x84, 17)], 0)
    _, streams = parse(minimal(records=records), identity=identity)
    expected = (EPOCH + timedelta(seconds=ANCHOR + 1)).isoformat().replace("+00:00", "Z")
    assert streams["records"][-1]["timestamp"] == expected
    assert streams["records"][-2]["timestamp"] is None


def test_compressed_activity_time_evidence_survives_snapshot_restore(tmp_path):
    data = minimal(
        compressed(34, [raw(5, 0x86, ANCHOR + 1 + 3600)], 0),
        records=frame(20, [raw(253, 0x86, ANCHOR)]),
        session=frame(18, [raw(5, 0, 2), raw(2, 0x86, ANCHOR)]),
    )
    summary, streams = parse(data)
    assert summary["activity_time"]["utc_offset_seconds"] == 3600
    store = SQLiteStore(tmp_path / "store")
    source = private_file(tmp_path, data)
    with store.lock(write=True):
        result = import_file(store, source, "1001")
        assert import_file(store, source, "1001")["status"] == "unchanged"
    destination = tmp_path / "snapshot"
    transfer.export_snapshot(store, destination)
    assert transfer.verify_snapshot(destination)["verified"]
    transfer.restore_snapshot(destination, tmp_path / "restored")
    restored = SQLiteStore(tmp_path / "restored")
    saved = restored.read_json(result["activity"]["files"]["streams"])
    assert saved["messages"] == streams["messages"]


def test_compressed_timestamp_failure_preserves_sqlite_revision_and_watermark(tmp_path):
    store = SQLiteStore(tmp_path / "store")
    assert run(store, Source())["complete"]
    before = store.state()
    malicious = minimal(
        records=frame(60000, [raw(253, 0x86, ANCHOR)]) + compressed(20, [raw(7, 0x84, 17)], 0)
    )
    result = run(store, Source([activity()], downloads={"1001": malicious}))
    assert not result["complete"]
    after = store.state()
    for key in ("activities", "covered_ranges", "last_success_at"):
        assert after[key] == before[key]
    assert transfer.inspect_store(store)["counts"]["revisions"] == 1


@pytest.mark.parametrize("active", [255, 2])
def test_explicit_invalid_active_zone_never_selects_the_only_offset(tmp_path, active):
    data = minimal(
        frame(2, [raw(0, 2, active), raw(5, 1, [22])]),
        session=frame(18, [raw(5, 0, 2), raw(2, 0x86, ANCHOR)]),
    )
    summary, _ = parse(data)
    assert summary["activity_time"]["local"] is None
    assert summary["activity_time"]["utc_offset_seconds"] is None
    store = SQLiteStore(tmp_path / "store")
    source = private_file(tmp_path, data)
    with store.lock(write=True):
        assert import_file(store, source, "1001")["status"] == "created"
    destination = tmp_path / "snapshot"
    transfer.export_snapshot(store, destination)
    assert transfer.verify_snapshot(destination)["verified"]


def test_version_bump_adds_exactly_one_sqlite_revision(tmp_path, monkeypatch):
    from garmin_bridge import normalize

    store = SQLiteStore(tmp_path / "store")
    with monkeypatch.context() as old:
        for module in (contract, normalize, sync):
            old.setattr(module, "NORMALIZER_VERSION", "1.2.1")
        assert run(store, Source())["updated"] == 1
    assert transfer.inspect_store(store)["counts"]["revisions"] == 1
    assert run(store, Source())["updated"] == 1
    assert transfer.inspect_store(store)["counts"]["revisions"] == 2
    with monkeypatch.context() as cached:

        def unexpected_parse(*args, **kwargs):
            pytest.fail("Same-version recheck must not normalize again")

        cached.setattr(normalize, "normalize", unexpected_parse)
        assert run(store, Source())["unchanged"] == 1
    assert transfer.inspect_store(store)["counts"]["revisions"] == 2
