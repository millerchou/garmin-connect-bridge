"""All inputs are constructed from invented constants, never exported FIT data."""

import copy
import io
import json
from types import SimpleNamespace

import fitdecode
import pytest

from garmin_bridge import contract as c, normalize, sync
from garmin_bridge import sqlite_transfer as transfer
from garmin_bridge.persistence import artifact_files
from garmin_bridge.errors import BridgeError
from garmin_bridge.fit_details import project_message
from garmin_bridge.fit_fields import FIELDS, MESSAGE_NUMBERS, RECORD_UNITS
from garmin_bridge.import_fit import import_file
from garmin_bridge.sqlite_store import SQLiteStore
from garmin_bridge.storage import canonical
from .fit_synthetic import body, finish, frame, minimal, raw
from .synthetic import Source, activity, fit_bytes
from .test_import_fit import private_file
from .test_sync import run


def parse(data, identity=False):
    meta = normalize.local_metadata("1001")
    summary, streams = normalize.normalize(
        data,
        meta,
        "2025-01-03T00:00:00Z",
        normalize.input_digest(
            meta, normalize.sha256(data), local_import=True, include_identity=identity
        ),
        local_import=True,
        include_identity=identity,
    )
    c.validate_streams(streams)
    return summary, streams


def detail(streams, message):
    if message == "session":
        return streams["session"]
    if message == "lap":
        return streams["laps"][0]["fit"]
    return next(row for row in streams["events"] + streams["messages"] if row["message"] == message)


def case(message, name):
    mid = MESSAGE_NUMBERS[message]
    spec = FIELDS[message][name]
    base = spec.base
    value = 17
    if spec.shape != "scalar":
        value = [0, 17]
    if base == 7:
        value = b"Cycling\0" if message != "user_profile" else b"Synthetic Person\0"
    if base == 0:
        value = [0, 0] if spec.shape != "scalar" else 0
    if spec.kind in ("utc", "local"):
        value = 1000000000
    context = {
        "session": {254: (0x84, 0), 5: (0, 2), 25: (0x84, 0), 26: (0x84, 1)},
        "lap": {254: (0x84, 0), 25: (0, 2)},
        "event": {0: (0, 13 if name == "data16" else 42), 1: (0, 0)},
        "time_in_zone": {0: (0x84, 18), 1: (0x84, 0)},
        "file_id": {0: (0, 4)},
        "sport": {0: (0, 2)},
        "split": {11: (0, 2)},
        "segment_lap": {23: (0, 2)},
        "split_summary": {1: (0, 2)},
        "activity": {1: (0x84, 1), 3: (0, 26), 4: (0, 0)},
    }.get(message, {})
    number = spec.number
    if number in context:
        base, value = context[number]
    if name in ("message_index", "reference_index", "hr_calc_type", "pwr_calc_type"):
        value = 0
    context[number] = (base, value)
    encoded = frame(mid, [raw(n, b, v) for n, (b, v) in context.items()])
    if message == "file_id":
        data = finish(encoded + body()[len(frame(0, [raw(0, 0, 4)])) :])
    elif message in ("session", "lap"):
        data = minimal(**{message: encoded})
    elif message == "record":
        data = minimal(records=encoded)
    else:
        data = minimal(encoded)
    return data, spec, value


CASES = [(message, name) for message, fields in FIELDS.items() for name in fields]


@pytest.mark.parametrize("message,name", CASES)
def test_reviewed_fields_through_binary_parser(message, name):
    data, spec, raw_value = case(message, name)
    _, streams = parse(data, identity=True)
    if message == "record":
        actual = streams["records"][0][name]
        assert streams["units"][name] == spec.unit
    else:
        row = detail(streams, message)
        key = "total_strokes" if name == "total_cycles" else name
        actual = row["garmin_reported" if spec.reported else "fields"][key]["value"]
    if spec.kind in {"utc", "local"}:
        assert actual == "2021-09-08T01:46:40" + ("Z" if spec.kind == "utc" else "")
    elif base_string := isinstance(raw_value, bytes):
        assert base_string and actual == raw_value.rstrip(b"\0").decode()
    elif isinstance(raw_value, list):
        assert actual == [v / spec.scale - spec.offset for v in raw_value]
    else:
        assert actual == raw_value / spec.scale - spec.offset


def rich_fit():
    record = frame(
        20,
        [
            raw(253, 0x86, 1000000000),
            raw(43, 2, 151),
            raw(44, 2, 149),
            raw(45, 2, 41),
            raw(46, 2, 43),
            raw(47, 2, 39),
            raw(29, 0x86, 12345),
        ],
    )
    lap = frame(
        19,
        [
            raw(254, 0x84, 0),
            raw(25, 0, 2),
            raw(13, 0x84, 1000),
            raw(110, 0x86, 2345),
            raw(33, 0x84, 200),
            raw(80, 2, 64),
            raw(102, 2, [64, 255]),
            raw(106, 0x84, [0, 333]),
            raw(24, 0, 0),
        ],
    )
    session = frame(
        18,
        [
            raw(254, 0x84, 0),
            raw(5, 0, 2),
            raw(2, 0x86, 1000000000),
            raw(25, 0x84, 0),
            raw(26, 0x84, 1),
            raw(10, 0x86, 42),
            raw(168, 0x85, 65536),
            raw(110, 7, b"Cycling\0"),
            raw(180, 0x84, 1234),
        ],
    )
    extra = frame(21, [raw(0, 0, 42), raw(1, 0, 3), raw(3, 0x86, 0x32021103), raw(4, 2, 1)])
    extra += frame(34, [raw(253, 0x86, 1000000100), raw(5, 0x86, 1000019900)])
    extra += frame(
        23,
        [
            raw(0, 2, 0),
            raw(3, 0x8C, 424242),
            raw(10, 0x84, 768),
            raw(27, 7, b"PRIVATE_SYNTHETIC_PRODUCT\0"),
        ],
    )
    extra += frame(
        3, [raw(0, 7, b"Synthetic Person\0"), raw(2, 2, 33), raw(3, 2, 175), raw(4, 0x84, 650)]
    )
    extra += frame(
        312,
        [
            raw(3, 0x86, 12345),
            *[raw(n, 0x85, 123456) for n in range(17, 25)],
            raw(250, 0x86, 98765),
        ],
    )
    extra += frame(2, [raw(0, 2, 0), raw(5, 1, [22])])
    extra += frame(7, [raw(3, 0x84, 210)])
    extra += frame(12, [raw(0, 0, 2), raw(3, 7, b"Cycling\0")])
    extra += frame(13, [raw(33, 0x86, 60)])
    extra += frame(49, [raw(0, 0x84, 13)])
    extra += frame(216, [raw(0, 0x84, 18), raw(1, 0x84, 0), raw(5, 0x86, [0, 1000])])
    extra += frame(313, [raw(1, 0, 2), raw(3, 0x84, 1), raw(25, 0x84, 200)])
    extra += frame(375, [raw(1, 0x84, 768), raw(2, 2, 3)])
    return minimal(extra, session=session, lap=lap, records=record)


def test_combined_metrics_units_estimates_arrays_and_timezone():
    summary, streams = parse(rich_fit())
    assert set(summary["metrics"]) == set(c.METRIC_UNITS) and len(summary["metrics"]) == 12
    assert len(summary["garmin_reported"]) == 5
    assert streams["records"][0]["left_torque_effectiveness"] == 75.5
    assert streams["records"][0]["accumulated_power"] == 12345
    assert streams["units"]["accumulated_power"] == "W"
    lap = detail(streams, "lap")
    assert lap["fields"]["avg_speed"] == {"value": 2.345, "unit": "m/s"}
    assert lap["fields"]["avg_power_position"]["value"] == [0, 333]
    assert lap["fields"]["avg_left_power_phase"]["value"] == [64 / 0.7111111, None]
    assert lap["fields"]["avg_fractional_cadence"]["value"] == 0.5
    assert lap["garmin_reported"]["normalized_power"] == {"value": 200, "unit": "W"}
    assert streams["session"]["garmin_reported"]["training_load_peak"]["value"] == 1
    assert streams["session"]["fields"]["total_strokes"] == {"value": 42, "unit": "strokes"}
    assert summary["activity_time"] == {
        "utc": "2021-09-08T01:46:40Z",
        "local": "2021-09-08T07:16:40",
        "timezone": None,
        "utc_offset_seconds": 19800,
    }
    event = streams["events"][0]["fields"]
    assert {
        name: event[name]["value"]
        for name in ("front_gear", "front_gear_num", "rear_gear", "rear_gear_num")
    } == {"front_gear": 50, "front_gear_num": 2, "rear_gear": 17, "rear_gear_num": 3}
    assert event["gear_change_data"] == event["data"]
    assert summary["timer_event_count"] == 0


@pytest.mark.parametrize("identity", [False, True])
def test_identity_is_explicit_and_positions_device_text_are_excluded(identity):
    summary, streams = parse(rich_fit(), identity)
    encoded = json.dumps([summary, streams])
    for forbidden in (
        "PRIVATE_SYNTHETIC_PRODUCT",
        "position_lat",
        "position_long",
        "nec_lat",
        "swc_long",
        "123456",
    ):
        assert forbidden not in encoded
    assert detail(streams, "split")["unknown"] == {"unknown_250": 98765}
    assert ("serial_number" in encoded) is identity
    assert ("Synthetic Person" in encoded) is identity
    assert ("user_profile" in encoded) is identity
    assert summary["provenance"]["unique_identifiers_retained"] == (
        "explicit_opt_in" if identity else "none"
    )


@pytest.mark.parametrize(
    "name",
    [
        "position_lat",
        "end_position_long",
        "nec_lat",
        "serial_number",
        "unknown_250",
        "product_name",
    ],
)
def test_new_projection_never_reads_disallowed_values(name):
    class Forbidden:
        @property
        def raw_value(self):
            pytest.fail("Unreviewed value was read")

    field = Forbidden()
    field.name = name
    field.field_def = SimpleNamespace(
        is_dev=False, def_num={"serial_number": 3, "product_name": 27}.get(name, 250)
    )
    assert project_message(SimpleNamespace(name="device_info", fields=[field])) is None

    class Ignored:
        name = "user_profile"

        @property
        def fields(self):
            pytest.fail("Default-off profile was accessed")

    assert project_message(Ignored()) is None


@pytest.mark.parametrize("payload,expected", [(0, 0), (254, 254), (255, None), (1000, None)])
def test_documented_event_payloads_and_data16(payload, expected):
    extra = frame(21, [raw(0, 0, 13), raw(1, 0, 3), raw(2, 0x84, payload), raw(3, 0x86, payload)])
    _, streams = parse(minimal(extra))
    fields = streams["events"][0]["fields"]
    for key in ("data", "data16"):
        assert fields.get(key, {}).get("value") == expected


@pytest.mark.parametrize("event", [3, 4, 5, 6, 8, 9, 21, 22, 26, 32, 36, 45, 46])
def test_documented_events_keep_headers_but_not_opaque_payloads(event):
    _, streams = parse(minimal(frame(21, [raw(0, 0, event), raw(1, 0, 3), raw(3, 0x86, 123)])))
    fields = streams["events"][0]["fields"]
    assert fields["event"]["value"] == event and "data" not in fields


@pytest.mark.parametrize(
    "fields,expected",
    [
        ([(0, 2, 1), (5, 1, [0, 22])], 19800),
        ([(0, 2, 0), (5, 1, [-14])], -12600),
        ([(0, 2, 0), (1, 0x86, 1000), (2, 0x86, [20800])], 19800),
        ([(0, 2, 0), (1, 0x86, 1000), (2, 0x86, [2**32 - 11600])], -12600),
        ([(1, 0x86, 19800)], None),
        ([(2, 0x86, [19800])], None),
        ([(0, 2, 3), (5, 1, [0, 22])], None),
        ([(5, 1, [0, 22])], None),
        ([(0, 2, 0), (5, 1, [60])], None),
    ],
)
def test_device_local_time_uses_valid_historical_offset(fields, expected):
    extra = frame(2, [raw(*f) for f in fields])
    data = minimal(extra, session=frame(18, [raw(5, 0, 2), raw(2, 0x86, 1000000000)]))
    summary, _ = parse(data)
    assert summary["activity_time"]["utc_offset_seconds"] == expected
    assert summary["activity_time"]["timezone"] is None


def test_conflicting_offsets_and_missing_start_are_not_guessed():
    extra = frame(2, [raw(5, 1, [22])]) + frame(2, [raw(5, 1, [24])])
    summary, _ = parse(minimal(extra, session=frame(18, [raw(5, 0, 2), raw(2, 0x86, 1000000000)])))
    assert summary["activity_time"]["local"] is None
    summary, _ = parse(minimal(frame(2, [raw(5, 1, [22])])))
    assert summary["activity_time"]["utc"] is summary["activity_time"]["local"] is None


def test_missing_invalid_zero_and_absent_new_fields():
    data = minimal(records=frame(20, [raw(43, 2, 0), raw(44, 2, 255), raw(29, 0x86, 0xFFFFFFFF)]))
    summary, streams = parse(data)
    assert streams["records"][0]["left_torque_effectiveness"] == 0
    for key in set(RECORD_UNITS) - {"left_torque_effectiveness"}:
        assert streams["records"][0][key] is None
        assert summary["coverage"][key] == {"present": 0, "missing": 1, "total": 1}
    assert "intensity_factor" not in streams["laps"][0]["fit"]["garmin_reported"]
    assert "sport_profile_name" not in streams["session"]["fields"]


@pytest.mark.parametrize(
    "target",
    ["serial", "profile", "position", "unknown", "unit", "estimate", "array", "text", "event"],
)
def test_recursive_contract_rejects_new_detail_injections(target):
    _, streams = parse(rich_fit())
    fields = streams["session"]["fields"]
    if target == "serial":
        detail(streams, "device_info")["fields"]["serial_number"] = {"value": 42, "unit": "count"}
    elif target == "profile":
        streams["messages"].append({"message": "user_profile", "fields": {}, "garmin_reported": {}})
    elif target in ("position", "unknown"):
        fields["position_lat" if target == "position" else "unknown_222"] = {
            "value": 42,
            "unit": "m",
        }
    elif target == "unit":
        fields["total_strokes"]["unit"] = "kg"
    elif target == "estimate":
        fields["training_load_peak"] = streams["session"]["garmin_reported"].pop(
            "training_load_peak"
        )
    elif target == "array":
        streams["laps"][0]["fit"]["fields"]["avg_power_position"]["value"] = [1, 2, 3]
    elif target == "text":
        fields["sport_profile_name"]["value"] = "\x00INVALID_SYNTHETIC_LABEL"
    elif target == "event":
        streams["events"][0]["fields"]["event"]["value"] = 250
    with pytest.raises(BridgeError):
        c.validate_streams(streams)


def test_sqlite_snapshot_identity_policy_revisions_and_restore(tmp_path):
    store = SQLiteStore(tmp_path / "store")
    source = private_file(tmp_path, rich_fit())
    with store.lock(write=True):
        first = import_file(store, source, "1001")
        assert import_file(store, source, "1001")["status"] == "unchanged"
        enabled = import_file(store, source, "1001", include_identity=True)
        assert enabled["status"] == "updated"
        assert import_file(store, source, "1001", include_identity=True)["status"] == "unchanged"
        assert import_file(store, source, "1001")["activity"]["files"] == first["activity"]["files"]
    old_bytes = store.read_bytes(enabled["activity"]["files"]["streams"])
    assert {row["message"] for row in json.loads(old_bytes)["messages"]} == set(FIELDS) - {
        "record",
        "lap",
        "session",
        "event",
        "segment_lap",
        "timestamp_correlation",
    }
    snapshot = tmp_path / "snapshot"
    transfer.export_snapshot(store, snapshot)
    manifest = json.loads((snapshot / "manifest.json").read_bytes())
    assert manifest["format_version"] == 2
    assert manifest["fit_fields_versions"] == ["1.1"]
    assert manifest["identity_included"] is True
    assert manifest["fit_counts"]["identity_revisions"] == 1
    assert transfer.verify_snapshot(snapshot)["revision_count"] == 2
    target = tmp_path / "restored"
    transfer.restore_snapshot(snapshot, target)
    restored = SQLiteStore(target)
    assert restored.read_bytes(enabled["activity"]["files"]["streams"]) == old_bytes
    for entry in store.state()["activities"].values():
        artifact_files(entry, store.read_bytes)
    assert store.state()["covered_ranges"] == []


def test_range_sync_version_revision_idempotence_and_failed_watermark(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "store")
    with monkeypatch.context() as old:
        for module in (c, normalize, sync):
            old.setattr(module, "NORMALIZER_VERSION", "1.2.1")
        assert run(store, Source())["complete"]
    prior = copy.deepcopy(store.state())
    old_files = prior["activities"]["1001"]["summary"]["files"]
    assert run(store, Source())["updated"] == 1
    current = store.state()["activities"]["1001"]["summary"]
    assert current["normalizer_version"] == "1.4.1" and current["files"] != old_files
    assert store.read_json(old_files["summary"])["normalizer_version"] == "1.2.1"
    assert run(store, Source())["unchanged"] == 1
    before = copy.deepcopy(store.state())
    result = run(store, Source([activity(), activity(1002)], downloads={"1002": b"invalid"}))
    assert not result["complete"]
    after = store.state()
    assert after["covered_ranges"] == before["covered_ranges"]
    assert after["last_success_at"] == before["last_success_at"]
    assert after["activities"] == before["activities"]


def test_legacy_stream_contract_and_sqlite_schema_remain_readable(tmp_path):
    summary, streams = parse(fit_bytes())
    for key in (
        "fit_fields_version",
        "include_identity",
        "session",
        "events",
        "messages",
        "raw_messages",
    ):
        del streams[key]
    streams["units"] = dict(c.LEGACY_STREAM_UNITS)
    for row in streams["records"]:
        for key in RECORD_UNITS:
            del row[key]
    for lap in streams["laps"]:
        del lap["fit"]
    streams["normalizer_version"] = summary["normalizer_version"] = "1.2.1"
    summary["coverage"] = {
        k: v for k, v in summary["coverage"].items() if k in c.LEGACY_STREAM_UNITS
    }
    revision = normalize.sha256(canonical([summary["source_digest"], "1.2.1", c.SCHEMA_VERSION]))
    summary["files"] = {k: f"objects/1001/{revision}/{k}.json" for k in ("summary", "streams")}
    store = SQLiteStore(tmp_path / "legacy")
    with store.lock(write=True):
        state = store.state()
        store.write_json(summary["files"]["streams"], streams)
        store.write_json(summary["files"]["summary"], summary)
        state["activities"]["1001"] = {
            "summary": summary,
            "file_hashes": {
                k: normalize.sha256(canonical(v))
                for k, v in (("summary", summary), ("streams", streams))
            },
            "raw_ref": None,
            "raw_sha256": None,
            "metadata_digest": normalize.sha256(canonical(normalize.local_metadata("1001"))),
        }
        store.write_json("index.json", state)
    snapshot = tmp_path / "old-snapshot"
    transfer.export_snapshot(store, snapshot)
    assert transfer.verify_snapshot(snapshot)["verified"]
    transfer.restore_snapshot(snapshot, tmp_path / "restored")
    assert SQLiteStore(tmp_path / "restored").read_json(summary["files"]["streams"]) == streams
    from garmin_bridge.sqlite_query import window

    c.validate_output(
        "window", window(store, "1001", "2025-01-02T00:00:00Z", "2025-01-02T00:00:06Z")
    )
    source = private_file(tmp_path, fit_bytes())
    with store.lock(write=True):
        assert import_file(store, source, "1001")["status"] == "updated"
        assert import_file(store, source, "1001")["status"] == "unchanged"
    assert store.read_json(summary["files"]["streams"]) == streams
    assert transfer.inspect_store(store)["counts"]["revisions"] == 2


def test_lap_allowlist_does_not_invent_unsupported_fields():
    names = set(FIELDS["lap"])
    assert not names & {
        "intensity_factor",
        "training_load_peak",
        "enhanced_min_respiration_rate",
        "sport_profile_name",
    }


def test_developer_field_cannot_spoof_detail():
    class Developer:
        field_def = SimpleNamespace(is_dev=True)

        @property
        def name(self):
            pytest.fail("Developer name read")

        @property
        def raw_value(self):
            pytest.fail("Developer value read")

    assert project_message(SimpleNamespace(name="device_info", fields=[Developer()])) is None


def test_sdk_documented_fields_do_not_leak_fitdecode_unknown_names():
    # fitdecode's older Profile lacks these SDK-documented split_summary fields.
    # They are admitted by reviewed numeric IDs/types, never an unknown_* name.
    unknowns = set()
    with fitdecode.FitReader(io.BytesIO(rich_fit())) as reader:
        for message in reader:
            if isinstance(message, fitdecode.FitDataMessage):
                for field in message.fields:
                    if field.name.startswith("unknown_"):
                        unknowns.add((message.global_mesg_num, field.field_def.def_num))
    assert {(313, 1), (313, 25)} <= unknowns
    _, streams = parse(rich_fit())
    fields = detail(streams, "split_summary")["fields"]
    assert fields["sport"]["value"] == 2 and fields["avg_power"]["value"] == 200
    assert "unknown" not in detail(streams, "split_summary")


@pytest.mark.parametrize("event", [10, 47, 54, 56, 75, 82])
def test_other_documented_event_headers_and_payloads(event):
    _, streams = parse(minimal(frame(21, [raw(0, 0, event), raw(1, 0, 3), raw(3, 0x86, 1)])))
    fields = streams["events"][0]["fields"]
    assert fields["event"]["value"] == event
    assert ("data" in fields) is (event in {47, 54, 56, 75})


def test_invalid_zone_references_and_ambiguous_settings_are_omitted():
    zones = frame(216, [raw(0, 0x84, 19), raw(1, 0x84, 1), raw(5, 0x86, [1000])])
    targets = frame(7, [raw(3, 0x84, 200)]) * 2
    _, streams = parse(minimal(zones + targets))
    assert not any(r["message"] in {"time_in_zone", "zones_target"} for r in streams["messages"])
    _, valid = parse(case("time_in_zone", "time_in_hr_zone")[0])
    detail(valid, "time_in_zone")["fields"]["reference_index"]["value"] = 123
    with pytest.raises(BridgeError):
        c.validate_streams(valid)


def test_sync_identity_change_requeues_completed_items_on_resume(tmp_path):
    store = SQLiteStore(tmp_path / "store")
    source = Source(
        [activity(), activity(1002)], downloads={"1001": rich_fit(), "1002": b"invalid"}
    )
    with store.lock(write=True):
        first = sync.synchronize(store, lambda: source, mode="full", include_identity=True)
    assert not first["complete"]
    entry = store.state()["activities"]["1001"]
    assert entry["summary"]["provenance"]["unique_identifiers_retained"] == "explicit_opt_in"
    source.downloads["1002"] = rich_fit()
    with store.lock(write=True):
        result = sync.synchronize(store, lambda: source, mode="resume")
    assert result["complete"] and result["updated"] == 2
    summary = store.state()["activities"]["1001"]["summary"]
    assert summary["files"] != entry["summary"]["files"]
    assert summary["provenance"]["unique_identifiers_retained"] == "none"
    assert transfer.inspect_store(store)["fit_counts"]["identity_revisions"] == 1


@pytest.mark.parametrize("injection", ["position", "identity", "kind", "column", "manifest"])
def test_snapshot_rejects_rehashed_injections_in_new_rows(tmp_path, injection):
    import sqlite3

    store = SQLiteStore(tmp_path / "store")
    source = private_file(tmp_path, rich_fit())
    with store.lock(write=True):
        result = import_file(store, source, "1001")
    snapshot = tmp_path / "snapshot"
    transfer.export_snapshot(store, snapshot)
    database = snapshot / "activities.sqlite"
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    with sqlite3.connect(database) as db:
        if injection == "column":
            db.execute("ALTER TABLE samples ADD COLUMN private TEXT")
        elif injection == "kind":
            db.execute("UPDATE samples SET kind='private' WHERE kind='messages'")
        elif injection in {"position", "identity"}:
            name = result["activity"]["files"]["streams"]
            ordinal, payload = db.execute(
                "SELECT ordinal,payload FROM samples WHERE name=? AND kind='messages' ORDER BY ordinal LIMIT 1",
                (name,),
            ).fetchone()
            row = json.loads(payload)
            row["fields"]["position_lat" if injection == "position" else "serial_number"] = {
                "value": 42,
                "unit": "count",
            }
            db.execute(
                "UPDATE samples SET payload=? WHERE name=? AND kind='messages' AND ordinal=?",
                (canonical(row), name, ordinal),
            )
    manifest["size"] = database.stat().st_size
    manifest["sha256"] = transfer.digest_file(database)
    if injection == "manifest":
        manifest["identity_included"] = True
    manifest_path.write_bytes(canonical(manifest))
    with pytest.raises(BridgeError):
        transfer.verify_snapshot(snapshot)


@pytest.mark.parametrize("argument", [[], ["--include-fit-identity"]])
def test_identity_cli_setting_and_receipt_excludes_values(tmp_path, capsys, argument):
    from garmin_bridge import cli

    source = private_file(tmp_path, rich_fit())
    assert (
        cli.main(
            [
                "--data-dir",
                str(tmp_path / "store"),
                "import-fit",
                "--file",
                str(source),
                "--activity-id",
                "1001",
                *argument,
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Synthetic Person" not in output and "424242" not in output
    summary = json.loads(output)["activity"]
    assert summary["provenance"]["unique_identifiers_retained"] == (
        "explicit_opt_in" if argument else "none"
    )


def test_sport_labels_retain_custom_text():
    session = frame(18, [raw(5, 0, 2), raw(110, 7, b"PRIVATE_SYNTHETIC_LABEL\0")])
    data = minimal(
        frame(12, [raw(0, 0, 2), raw(3, 7, b"PRIVATE_SYNTHETIC_LABEL\0")]), session=session
    )
    summary, streams = parse(data, identity=True)
    assert streams["session"]["fields"]["sport_profile_name"]["value"] == "PRIVATE_SYNTHETIC_LABEL"
    assert detail(streams, "sport")["fields"]["name"]["value"] == "PRIVATE_SYNTHETIC_LABEL"
    assert "PRIVATE_SYNTHETIC_LABEL" not in json.dumps(summary)


@pytest.mark.parametrize("fields", [[raw(116, 2, [1, 2, 3])], [raw(101, 0x84, 1)]])
def test_wrong_wire_types_and_array_shapes_are_not_coerced(fields):
    data = minimal(session=frame(18, [raw(5, 0, 2), *fields]))
    _, streams = parse(data)
    assert set(streams["session"]["fields"]) == {"sport"}


def test_relative_system_time_is_not_mislabelled_as_utc():
    data = minimal(session=frame(18, [raw(5, 0, 2), raw(2, 0x86, 100000)]))
    summary, streams = parse(data)
    assert summary["activity_time"]["utc"] is None
    assert streams["session"]["fields"]["start_time"]["value"] is None
