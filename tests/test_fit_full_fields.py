"""Full-field projections using only invented, in-memory FIT messages."""

from collections import Counter
import copy
import json

import pytest

from garmin_bridge import analysis_export as export, contract as c, normalize, sync
from garmin_bridge import sqlite_transfer as transfer
from garmin_bridge.errors import BridgeError
from garmin_bridge.fit_unknown import REPORT_PATH, validate_report
from garmin_bridge.sqlite_store import SQLiteStore
from garmin_bridge.storage import canonical
from .fit_synthetic import frame, minimal, raw
from .test_analysis_export import publish, read_table, assert_safe_keys, DENIED_NAMES
from .test_cli_storage import command
from .test_fit_fields import parse, detail


def full_fit():
    segment = frame(
        142,
        [
            raw(253, 0x86, 1000000000),
            raw(2, 0x86, 1000000000),
            *[raw(n, 0x85, 123456789) for n in (3, 4, 5, 6, 25, 26, 27, 28)],
            raw(8, 0x86, 12000),
            raw(9, 0x86, 20000),
            raw(19, 0x84, 210),
            raw(30, 0x84, 220),
            raw(29, 7, b"Invented Segment\0"),
            raw(65, 7, b"synthetic-segment-token\0"),
            raw(79, 0x84, [200, 220]),
        ],
    )
    split = frame(312, [raw(254, 0x84, 2), raw(3, 0x86, 100), raw(250, 0x86, [0, 7])])
    zone = frame(216, [raw(0, 0x84, 312), raw(1, 0x84, 2), raw(5, 0x86, [0, 1000])])
    event = frame(21, [raw(0, 0, 39), raw(1, 0, 3), raw(3, 0x86, 123456789)])
    unknown = frame(534, [raw(253, 0x86, 1000000000), raw(0, 0x84, 7), raw(1, 2, [0, 4])])
    private = frame(3, [raw(0, 7, b"Synthetic private name\0"), raw(250, 0x86, 987654321)])
    private += frame(79, [raw(0, 0x86, 987654321)])
    private += frame(534, [raw(2, 0x85, 123456789), raw(3, 7, b"Synthetic device serial\0")])
    record = frame(
        20,
        [
            raw(253, 0x86, 1000000000),
            raw(4, 2, 80),
            raw(53, 2, 64),
            raw(90, 0x86, [0, 7, 0xFFFFFFFF]),
            raw(250, 0x85, 123456789),
            raw(0, 0x85, 123456789),
            raw(1, 0x85, 234567890),
        ],
    )
    return minimal(segment + split + zone + event + unknown * 2 + private, records=record)


def test_new_fields_roundtrip_export_report_and_snapshot(store, tmp_path, capsys, monkeypatch):
    data = full_fit()
    assert publish(store, data)
    current = store.state()["activities"]["1001"]["summary"]
    streams = store.read_json(current["files"]["streams"])
    assert streams["fit_fields_version"] == "1.1"
    assert streams["records"][0]["unknown_90"] == [0, 7, None]
    assert streams["records"][0]["fractional_cadence"] == 0.5
    assert streams["records"][0]["cadence"] == 80.5
    segment = detail(streams, "segment_lap")
    assert segment["fields"]["name"]["value"] == "Invented Segment"
    assert segment["fields"]["uuid"]["value"] == "synthetic-segment-token"
    assert segment["fields"]["total_distance"]["value"] == 200
    assert segment["garmin_reported"]["normalized_power"]["value"] == 220
    assert detail(streams, "split")["unknown"] == {"unknown_250": [0, 7]}
    assert detail(streams, "time_in_zone")["fields"]["reference_mesg"]["value"] == 312
    assert streams["events"][0]["fields"]["event"]["value"] == 39
    assert "data" not in streams["events"][0]["fields"]
    assert (
        streams["raw_messages"][:2]
        == [{"mesg_num": 534, "fields": {"253": 1000000000, "0": 7, "1": [0, 4]}}] * 2
    )
    assert_safe_keys(streams)
    encoded = json.dumps(streams)
    for private in ("123456789", "234567890", "987654321", "Synthetic private", "Synthetic device"):
        assert private not in encoded
    report_bytes = store.read_bytes(REPORT_PATH)
    report = json.loads(report_bytes)
    assert {r["field"] for r in report} == {"3.0", "3.250", "79.0", "20.250", "534.2", "534.3"}
    assert all(r["row_count"] == 1 for r in report)
    assert validate_report(report) == report
    assert all(set(r) == {"field", "reason", "row_count"} for r in report)
    assert not publish(store, data)
    assert store.read_bytes(REPORT_PATH) == report_bytes
    assert store.path(REPORT_PATH).stat().st_mode & 0o777 == 0o600
    assert store.path("reports").stat().st_mode & 0o777 == 0o700
    monkeypatch.setattr("garmin_bridge.cli.GarminSource", lambda *a: pytest.fail("authenticated"))
    code, result, _ = command(store, capsys, "fit-blocked")
    assert code == 0 and result == {"report": REPORT_PATH, "blocked": report}
    code, window, _ = command(
        store,
        capsys,
        "window",
        "1001",
        "--start",
        "2021-09-08T01:46:39Z",
        "--end",
        "2021-09-08T01:46:41Z",
    )
    assert code == 0 and "unknown_90" not in window["records"][0]["sample"]
    target = tmp_path / "analysis.sqlite"
    export.export_analysis(store, target)
    assert json.loads(read_table(target, "records")[0]["unknown_json"]) == {
        "unknown_90": [0, 7, None]
    }
    assert read_table(target, "records")[0]["fractional_cadence"] == 0.5
    messages = [json.loads(r["detail_json"]) for r in read_table(target, "messages")]
    assert segment in messages
    assert detail(streams, "split") in messages
    assert read_table(target, "events")[0]["fit_event"] == 39
    assert [json.loads(r["fields_json"]) for r in read_table(target, "raw_messages")] == [
        r["fields"] for r in streams["raw_messages"]
    ]
    assert read_table(target, "meta")[0]["format_version"] == 2
    for table in export.TABLES:
        for row in read_table(target, table):
            assert_safe_keys(row)
            for key, value in row.items():
                if key.endswith("_json"):
                    export.validate_detail_keys(json.loads(value))
    with store.database() as db:
        assert (
            db.execute("SELECT count(*) FROM samples WHERE kind='raw_messages'").fetchone()[0] == 2
        )
    snap = tmp_path / "snapshot"
    transfer.export_snapshot(store, snap)
    assert not (snap / REPORT_PATH).exists()
    assert json.loads((snap / "manifest.json").read_bytes())["fit_counts"]["raw_messages"] == 2
    transfer.restore_snapshot(snap, tmp_path / "restored")
    restored = SQLiteStore(tmp_path / "restored")
    assert restored.read_json(current["files"]["streams"]) == streams


@pytest.mark.parametrize("number", [22, 104, 113, 140, 141, 147, 288, 325, 326, 327, 394, 428, 534])
def test_undocumented_messages_preserve_order_raw_integers_and_arrays(number):
    _, streams = parse(minimal(frame(number, [raw(0, 0x86, 65536), raw(1, 0x84, [0, 17])]) * 2))
    assert (
        streams["raw_messages"] == [{"mesg_num": number, "fields": {"0": 65536, "1": [0, 17]}}] * 2
    )


@pytest.mark.parametrize(
    "message,number",
    [
        ("session", 18),
        ("lap", 19),
        ("split", 312),
        ("split_summary", 313),
        ("sport", 12),
        ("training_settings", 13),
        ("device_settings", 2),
        ("zones_target", 7),
        ("device_info", 23),
    ],
)
def test_unknown_detail_objects_keep_raw_values_without_scale(message, number):
    fields = [raw(250, 0x86, [0, 65536])]
    if message == "session":
        fields.append(raw(5, 0, 2))
    encoded = frame(number, fields)
    data = minimal(**{message: encoded}) if message in {"session", "lap"} else minimal(encoded)
    _, streams = parse(data)
    assert detail(streams, message)["unknown"] == {"unknown_250": [0, 65536]}


@pytest.mark.parametrize("event", [39, 83, 250, 254])
def test_unknown_event_codes_keep_generic_headers(event):
    _, streams = parse(minimal(frame(21, [raw(0, 0, event), raw(1, 0, 250)])))
    assert streams["events"][0]["fields"]["event"]["value"] == event
    assert streams["events"][0]["fields"]["event_type"]["value"] == 250


@pytest.mark.parametrize("values", [0, 7, -7, 123456789, -123456789, [0, 7, 123456789]])
def test_all_undocumented_sint32_values_are_blocked_including_position_correlations(values):
    blocked = Counter()
    encoded = frame(20, [raw(250, 0x85, values), raw(0, 0x85, values)])
    _, streams = normalize.normalize(
        minimal(records=encoded),
        normalize.local_metadata("1001"),
        "2025-01-03T00:00:00Z",
        "a" * 64,
        blocked_fields=blocked,
    )
    assert "unknown_250" not in streams["records"][0]
    assert blocked == {(20, 250, "possible_semicircle"): 1}


@pytest.mark.parametrize(
    "base,value,reason",
    [
        (7, b"Synthetic Name\0", "possible_identity"),
        (13, b"Synthetic serial\0", "possible_identity"),
        (2, [65, 66, 67, 68, 0], "possible_identity"),
        (0x88, 12.25, "non_integer_raw"),
    ],
)
def test_ambiguous_raw_types_are_reported_without_values(base, value, reason):
    blocked = Counter()
    _, streams = normalize.normalize(
        minimal(frame(534, [raw(5, base, value)])),
        normalize.local_metadata("1001"),
        "2025-01-03T00:00:00Z",
        "a" * 64,
        blocked_fields=blocked,
    )
    assert streams["raw_messages"] == []
    assert blocked == {(534, 5, reason): 1}


@pytest.mark.parametrize("number", [3, 79])
def test_whole_identity_messages_block_every_field_and_count_rows(store, number):
    assert publish(store, minimal(frame(number, [raw(250, 0x86, 987654321)]) * 3))
    assert json.loads(store.read_bytes(REPORT_PATH)) == [
        {"field": f"{number}.250", "reason": "identity_message", "row_count": 3}
    ]
    current = store.state()["activities"]["1001"]["summary"]
    assert b"987654321" not in store.read_bytes(current["files"]["streams"])


def test_absent_and_tampered_report_do_not_leak_or_create_store(store, capsys):
    assert command(store, capsys, "fit-blocked")[1] == {"report": REPORT_PATH, "blocked": []}
    assert not store.root.exists()
    store.ensure()
    store.write_json(
        REPORT_PATH, [{"field": "534.0", "reason": "Synthetic secret", "row_count": 1}]
    )
    code, value, captured = command(store, capsys, "fit-blocked")
    assert code == 2 and value["error"] == "privacy_failed"
    assert "Synthetic secret" not in captured.err


def test_large_raw_array_row_roundtrips_without_truncation(store, tmp_path):
    # A physical definition can have many array fields; JSON expansion exceeds
    # the old 32 KiB per-row limit even though this FIT input is under 64 KiB.
    message = frame(534, [raw(i, 0x86, [1000000] * 63) for i in range(200)])
    assert publish(store, minimal(message))
    summary = store.state()["activities"]["1001"]["summary"]
    streams = store.read_json(summary["files"]["streams"])
    row = streams["raw_messages"][0]
    assert len(canonical(row)) > 32768
    assert row == {"mesg_num": 534, "fields": {str(i): [1000000] * 63 for i in range(200)}}
    target = tmp_path / "large-analysis.sqlite"
    export.export_analysis(store, target)
    assert json.loads(read_table(target, "raw_messages")[0]["fields_json"]) == row["fields"]


def test_split_zones_require_unique_indices():
    zones = frame(216, [raw(0, 0x84, 312), raw(1, 0x84, 1), raw(5, 0x86, [1000])])
    split = frame(312, [raw(254, 0x84, 1)])
    for extra in (zones, zones + split * 2):
        _, streams = parse(minimal(extra))
        assert all(row["message"] != "time_in_zone" for row in streams["messages"])


def test_timestamp_correlation_keeps_relative_system_clock_seconds():
    _, streams = parse(minimal(frame(162, [raw(1, 0x86, 17), raw(2, 0x84, 16384)])))
    fields = detail(streams, "timestamp_correlation")["fields"]
    assert fields["system_timestamp"] == {"value": 17, "unit": "s"}
    assert fields["fractional_system_timestamp"] == {"value": 0.5, "unit": "s"}


def test_long_custom_labels_remain_text_and_do_not_enter_summary():
    label = b"Synthetic custom ride profile " * 5
    _, streams = parse(minimal(session=frame(18, [raw(5, 0, 2), raw(110, 7, label + b"\0")])))
    assert streams["session"]["fields"]["sport_profile_name"]["value"] == label.decode()


@pytest.mark.parametrize("container", ["record", "lap", "raw_message", "session"])
@pytest.mark.parametrize("name", DENIED_NAMES)
def test_unknown_json_export_backstop_preserves_destination(
    store, tmp_path, monkeypatch, container, name
):
    publish(store, full_fit())
    target = tmp_path / "analysis.sqlite"
    export.export_analysis(store, target)
    before = target.read_bytes()
    insert = export.insert

    def inject(db, table, row):
        row = dict(row)
        match = {
            "record": "records",
            "lap": "laps",
            "raw_message": "raw_messages",
            "session": "messages",
        }[container]
        if table == match:
            key = (
                "detail_json"
                if container == "session"
                else "fields_json"
                if container == "raw_message"
                else "unknown_json"
            )
            value = json.loads(row[key])
            if container == "session":
                value["unknown"] = {"unknown_250": {name: 1}}
            else:
                value[name] = 1
            row[key] = json.dumps(value)
        return insert(db, table, row)

    monkeypatch.setattr(export, "insert", inject)
    # The denylist must independently reject even if structural checks regress.
    monkeypatch.setattr(export, "valid_unknown", lambda *a: True)
    monkeypatch.setattr(export, "valid_raw_message", lambda *a: True)
    monkeypatch.setattr(export, "valid_message", lambda *a: True)
    with pytest.raises(BridgeError):
        export.export_analysis(store, target)
    assert target.read_bytes() == before


def test_version_1_details_remain_readable_and_reimport_creates_immutable_revision(
    store, tmp_path, monkeypatch, capsys
):
    original = normalize.normalize

    def historical(*args, **kwargs):
        summary, streams = original(*args, **kwargs)
        streams["fit_fields_version"] = "1.0"
        del streams["raw_messages"]
        del streams["units"]["fractional_cadence"]
        del summary["coverage"]["fractional_cadence"]
        for row in streams["records"]:
            del row["fractional_cadence"]
        return summary, streams

    with monkeypatch.context() as patch:
        for module in (c, normalize, sync):
            patch.setattr(module, "NORMALIZER_VERSION", "1.3.0")
        patch.setattr(normalize, "normalize", historical)
        publish(store, minimal())
    prior = copy.deepcopy(store.state())
    summary = prior["activities"]["1001"]["summary"]
    before = {p: store.read_bytes(p) for p in summary["files"].values()}
    c.validate_streams(json.loads(before[summary["files"]["streams"]]))
    export.export_analysis(store, tmp_path / "old-analysis.sqlite")
    snap = tmp_path / "old-snapshot"
    transfer.export_snapshot(store, snap)
    assert "raw_messages" not in json.loads((snap / "manifest.json").read_bytes())["fit_counts"]
    transfer.verify_snapshot(snap)
    assert publish(store, minimal())
    assert not publish(store, minimal())
    assert all(store.read_bytes(p) == content for p, content in before.items())
    assert store.state()["covered_ranges"] == prior["covered_ranges"]
    assert store.state()["last_success_at"] == prior["last_success_at"]
    assert store.state()["activities"]["1001"]["summary"]["normalizer_version"] == "1.4.0"
    # Existing manifest format remains valid for mixed version history.
    transfer.export_snapshot(store, tmp_path / "mixed-snapshot")
    transfer.verify_snapshot(tmp_path / "mixed-snapshot")
    assert (
        canonical(store.read_json(summary["files"]["streams"]))
        == before[summary["files"]["streams"]]
    )
