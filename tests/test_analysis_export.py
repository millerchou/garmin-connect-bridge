"""Analysis artifacts are generated solely from in-code synthetic inputs."""

from contextlib import closing
import copy
import json
import os
import sqlite3
import stat

import pytest

from garmin_bridge import analysis_export as export, cli, contract as c, normalize, sync
from garmin_bridge.errors import BridgeError
from garmin_bridge.storage import canonical
from .fit_synthetic import frame, minimal, raw
from .synthetic import SECRET, START, Source, activity, archive, fit_bytes
from .test_cli_storage import command
from .test_fit_fields import parse, rich_fit
from .test_sync import INTERVAL, run


DENIED_NAMES = (
    "position_lat",
    "position_long",
    "lat",
    "long",
    "latitude",
    "longitude",
    "nec",
    "swc",
    "serial_number",
    "user_profile",
    "friendly_name",
    "nec_lat",
    "swc_long",
    "start_position_lat",
    "end_position_long",
    "fit_serial_number_0",
    "garmin_reported_user_profile_value",
    "fit_friendly_name_text",
    "fit_NEC_0",
    "SWC_corner",
    "positionLat",
    "startPositionLong",
    "endLatitude",
    "startLongitude",
    "fitNECValue",
    "fitSWCCorner",
    "serialNumber",
    "fitSerialNumber0",
    "garminReportedUserProfileValue",
    "fitFriendlyNameText",
    "position-lat",
    "serial.number",
    "LaTiTuDe",
    "SERIAL_nUmber",
    "fit-USER__PrOfIlE-value",
    "fitAntDeviceNumber",
    "ant_transmission_type",
    "nested.deviceId",
    "product_name",
    "descriptor",
    "deviceSerial",
    "macAddress",
    "coordinates",
    "gpsPosition",
    "positionLng",
    "startLon",
    "position",
    "startPosition",
    "end_position",
    "homeLocation",
)


def publish(store, data=None, identity=False):
    with store.lock(write=True):
        return sync.publish_original(
            store,
            data or fit_bytes(),
            normalize.local_metadata("1001"),
            store.state(),
            local_import=True,
            include_identity=identity,
        )


def read_table(path, table):
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f'SELECT * FROM "{table}"')]


def assert_safe_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            assert not key.startswith("position_")
            assert key not in {
                "lat",
                "long",
                "latitude",
                "longitude",
                "serial_number",
                "friendly_name",
                "user_profile",
                "age",
                "gender",
                "height",
                "weight",
            }
            assert_safe_keys(item)
    elif isinstance(value, list):
        for item in value:
            assert_safe_keys(item)


def test_analysis_current_revisions_typed_values_privacy_and_receipt(
    store, tmp_path, monkeypatch, capsys
):
    source = Source([activity(1001), activity(1002)])
    run(store, source)
    old = copy.deepcopy(store.state()["activities"]["1001"])
    source.downloads["1001"] = archive(fit_bytes(power=230, developer=True))
    run(store, source)
    before = store.read_bytes("index.json")
    read = store.read_bytes

    def guarded(ref, *args, **kwargs):
        assert not ref.startswith(("raw/", "auth/"))
        return read(ref, *args, **kwargs)

    monkeypatch.setattr(store, "read_bytes", guarded)
    monkeypatch.setattr(cli, "GarminSource", lambda *a: pytest.fail("export authenticated"))
    monkeypatch.setattr(normalize, "normalize", lambda *a, **k: pytest.fail("export parsed FIT"))
    target = tmp_path / "analysis.sqlite"
    receipt = export.export_analysis(store, target)
    assert receipt["sha256"] == normalize.sha256(target.read_bytes())
    assert receipt["size"] == target.stat().st_size
    assert receipt["activity_count"] == 2 and receipt["verified"]
    assert target.stat().st_mode & 0o777 == 0o600
    assert store.read_bytes("index.json") == before
    assert store.read_json(old["summary"]["files"]["summary"]) == old["summary"]
    with store.database() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT count(*) FROM documents WHERE name LIKE '%/summary.json'"
            ).fetchone()[0]
            == 3
        )
    with closing(sqlite3.connect(target)) as db:
        assert {
            row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        } == set(export.TABLES)
        for table, columns in export.TABLES.items():
            assert (
                dict((r[1], r[2]) for r in db.execute(f'PRAGMA table_info("{table}")')) == columns
            )
            assert_safe_keys(columns)
            for row in read_table(target, table):
                for name, value in row.items():
                    if name.endswith("_json"):
                        assert_safe_keys(json.loads(value))
                    assert SECRET not in str(value)
        assert db.execute(
            "SELECT power,typeof(power) FROM records WHERE activity_id='1001' ORDER BY ordinal"
        ).fetchall() == [(230.0, "real"), (None, "null"), (230.0, "real")]
    first, second = read_table(target, "activities")
    assert (first["avg_power"], second["avg_power"]) == (230, 200)
    assert first["avg_power_source"] == "fit_session"
    assert first["garmin_reported_normalized_power"] == 215
    assert first["normalizer_version"] == "1.4.1"
    assert first["activity_time_local"] == "2025-01-02T08:00:00"
    assert first["timezone"] == "Asia/Shanghai" and first["utc_offset_seconds"] == 28800
    metadata = read_table(target, "meta")[0]
    assert (metadata["coverage_start"], metadata["coverage_end"]) == tuple(INTERVAL.values())
    assert metadata["gap_count"] == 0
    assert metadata["activities_count"] == 2 and metadata["records_count"] == 6
    assert (
        json.loads(metadata["units_json"])["activities"]["garmin_reported_normalized_power"] == "W"
    )
    assert "sha" not in " ".join(metadata)
    assert command(store, capsys, "export", "analysis", "--file", str(target))[0] == 0


def test_detail_arrays_reported_values_events_and_message_units(store, tmp_path):
    publish(store, rich_fit())
    target = tmp_path / "analysis.sqlite"
    export.export_analysis(store, target)
    record = read_table(target, "records")[0]
    assert record["left_torque_effectiveness"] == 75.5
    lap = read_table(target, "laps")[0]
    assert lap["fit_avg_left_power_phase_0"] == 64 / 0.7111111
    assert lap["fit_avg_left_power_phase_1"] is None
    assert (lap["fit_avg_power_position_0"], lap["fit_avg_power_position_1"]) == (0, 333)
    assert lap["garmin_reported_normalized_power"] == 200
    event = read_table(target, "events")[0]
    assert event["fit_event"] == 42 and event["fit_gear_change_data"] == 0x32021103
    details = [json.loads(row["detail_json"]) for row in read_table(target, "messages")]
    assert details[0]["message"] == "session"
    assert details[0]["garmin_reported"]["training_load_peak"] == {"value": 1, "unit": "score"}
    for detail in details:
        assert_safe_keys(detail)
    summary = read_table(target, "activities")[0]
    assert summary["timezone"] is None and summary["utc_offset_seconds"] == 19800


@pytest.mark.parametrize("name", DENIED_NAMES)
def test_denylist_blocks_columns_even_if_schema_allowlist_is_widened(
    store, tmp_path, monkeypatch, name
):
    target = tmp_path / "analysis.sqlite"
    export.export_analysis(store, target)
    before = target.read_bytes()
    monkeypatch.setitem(export.TABLES, "records", {**export.TABLES["records"], name: "REAL"})
    with pytest.raises(BridgeError) as error:
        export.export_analysis(store, target)
    assert error.value.code == "privacy_failed"
    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".analysis-*"))


@pytest.mark.parametrize("table", export.TABLES)
def test_denylist_checks_columns_in_every_table(store, tmp_path, monkeypatch, table):
    monkeypatch.setitem(export.TABLES, table, {**export.TABLES[table], "start_lat": "REAL"})
    with pytest.raises(BridgeError) as error:
        export.export_analysis(store, tmp_path / "analysis.sqlite")
    assert error.value.code == "privacy_failed"


@pytest.mark.parametrize("name", DENIED_NAMES)
@pytest.mark.parametrize("nested", [False, True])
def test_denylist_blocks_detail_keys_even_if_message_contract_is_widened(
    store, tmp_path, monkeypatch, name, nested
):
    publish(store)
    target = tmp_path / "analysis.sqlite"
    original = export.populate

    def inject(db, source):
        expected, metadata = original(db, source)
        detail = {"message": "session", name: None}
        if nested:
            detail = {"message": "session", "fields": [{"nested": [[{name: None}]]}]}
        db.execute("UPDATE messages SET detail_json=? WHERE ordinal=0", (export.json_text(detail),))
        return expected, metadata

    monkeypatch.setattr(export, "populate", inject)
    # The denylist must work independently of the existing closed JSON contract.
    monkeypatch.setattr(export, "valid_message", lambda *args: True)
    with pytest.raises(BridgeError) as error:
        export.export_analysis(store, target)
    assert error.value.code == "privacy_failed"
    assert not target.exists()
    assert not list(tmp_path.glob(".analysis-*"))


def test_denylist_allows_posture_sensor_and_every_current_export_field():
    allowed = {
        "left_power_position",
        "right_power_position",
        "fit_avg_power_position_0",
        "fit_max_power_position_1",
        "avg_cadence_position",
        "fit_max_cadence_position_0",
        "sensor_position",
        "rider_position_change",
        "platform",
        "longitudinal_acceleration",
        "longest",
        "along",
        "latency",
        "longestInterval",
        "alongTrack",
        "fitAvgPowerPosition0",
        "fitMaxCadencePosition1",
        "sensorPosition",
        "connected",
    }
    for columns in export.TABLES.values():
        allowed.update(columns)
    for message in export.AUXILIARY_MESSAGES | {"session", "lap", "event"}:
        allowed.update(
            name for name, spec in export.specs_for(message).items() if not spec.identity
        )
    for name in allowed:
        export.require_safe_name(name)
    export.validate_detail_keys({"fields": [{name: [{"value": 0}]} for name in allowed]})


def test_current_identity_rejected_but_identity_history_not_exported(store, tmp_path):
    publish(store, rich_fit())
    target = tmp_path / "analysis.sqlite"
    export.export_analysis(store, target)
    before = target.read_bytes()
    publish(store, rich_fit(), identity=True)
    with pytest.raises(BridgeError, match="identity-disabled"):
        export.export_analysis(store, target)
    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".analysis-*"))
    publish(store, rich_fit())
    export.export_analysis(store, target)
    assert all(row["message"] != "user_profile" for row in read_table(target, "messages"))
    with store.database() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM documents WHERE name LIKE '%/summary.json'"
            ).fetchone()[0]
            == 2
        )


def test_empty_export_and_gap_metadata(store, tmp_path):
    target = tmp_path / "analysis.sqlite"
    assert export.export_analysis(store, target)["activity_count"] == 0
    assert not store.root.exists()
    metadata = read_table(target, "meta")[0]
    assert metadata["coverage_start"] is None and metadata["activity_start_utc"] is None
    source = Source([activity(1001), activity(1002)], {"1002": BridgeError("network_failed")})
    assert not run(store, source)["complete"]
    export.export_analysis(store, target)
    metadata = read_table(target, "meta")[0]
    assert metadata["activities_count"] == 1 and metadata["gap_count"] == 1
    assert metadata["coverage_end"] is None
    assert json.loads(metadata["pending_ranges_json"]) == [INTERVAL]


def test_atomic_overwrite_retains_old_open_reader_until_publication(store, tmp_path, monkeypatch):
    publish(store)
    target = tmp_path / "analysis.sqlite"
    export.export_analysis(store, target)
    before = target.read_bytes()
    publish(store, fit_bytes(power=230))
    original = export.validate

    def validate(*args):
        assert target.read_bytes() == before
        original(*args)

    monkeypatch.setattr(export, "validate", validate)
    with closing(sqlite3.connect(target)) as old_reader:
        assert old_reader.execute("SELECT avg_power FROM activities").fetchone()[0] == 200
        export.export_analysis(store, target)
        assert old_reader.execute("SELECT avg_power FROM activities").fetchone()[0] == 200
    assert read_table(target, "activities")[0]["avg_power"] == 230
    assert not list(tmp_path.glob(".analysis-*"))


@pytest.mark.parametrize("phase", ["populate", "validate", "flush", "rename", "interrupt"])
@pytest.mark.parametrize("existing", [False, True])
def test_failed_export_preserves_destination_and_removes_staging(
    store, tmp_path, monkeypatch, phase, existing
):
    publish(store)
    target = tmp_path / "analysis.sqlite"
    before = None
    if existing:
        export.export_analysis(store, target)
        before = target.read_bytes()
    publish(store, fit_bytes(power=230))

    def fail(*args):
        raise KeyboardInterrupt() if phase == "interrupt" else OSError(SECRET)

    if phase == "rename":
        monkeypatch.setattr(export.os, "replace", fail)
    elif phase == "flush":
        monkeypatch.setattr(export.os, "fsync", fail)
    else:
        monkeypatch.setattr(export, "populate" if phase == "populate" else "validate", fail)
    with pytest.raises(KeyboardInterrupt if phase == "interrupt" else OSError):
        export.export_analysis(store, target)
    assert (target.read_bytes() if target.exists() else None) == before
    assert not list(tmp_path.glob(".analysis-*"))


@pytest.mark.parametrize(
    "corruption",
    [
        "column",
        "table",
        "trigger",
        "count",
        "ordinal",
        "identity_json",
        "position_json",
        "nested_json",
        "coverage_json",
        "meta_json",
    ],
)
def test_built_in_validation_blocks_schema_counts_and_private_json(
    store, tmp_path, monkeypatch, corruption
):
    publish(store)
    target = tmp_path / "analysis.sqlite"
    export.export_analysis(store, target)
    before = target.read_bytes()
    original = export.populate

    def corrupt(db, source):
        expected, metadata = original(db, source)
        if corruption == "column":
            db.execute("ALTER TABLE records ADD COLUMN position_lat REAL")
        elif corruption == "table":
            db.execute("CREATE TABLE private (serial_number TEXT)")
        elif corruption == "trigger":
            db.execute("CREATE TRIGGER extra AFTER INSERT ON records BEGIN SELECT 1; END")
        elif corruption == "count":
            db.execute("DELETE FROM records WHERE ordinal=1")
        elif corruption == "ordinal":
            db.execute("UPDATE records SET ordinal=100 WHERE ordinal=1")
        elif corruption == "coverage_json":
            db.execute(
                "UPDATE activities SET coverage_json=?", (export.json_text({"position_lat": 1}),)
            )
        elif corruption == "meta_json":
            db.execute("UPDATE meta SET units_json=?", (export.json_text({"serial_number": 1}),))
        else:
            detail = json.loads(
                db.execute("SELECT detail_json FROM messages WHERE ordinal=0").fetchone()[0]
            )
            if corruption == "identity_json":
                detail = {
                    "message": "device_info",
                    "fields": {"serial_number": {"value": 123, "unit": "count"}},
                    "garmin_reported": {},
                }
            elif corruption == "position_json":
                detail["fields"]["position_lat"] = {"value": 123, "unit": "semicircles"}
            else:
                detail["fields"]["sport"]["value"] = {"serial_number": 123}
            db.execute(
                "UPDATE messages SET detail_json=? WHERE ordinal=0", (export.json_text(detail),)
            )
        return expected, metadata

    monkeypatch.setattr(export, "populate", corrupt)
    with pytest.raises(BridgeError):
        export.export_analysis(store, target)
    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".analysis-*"))


def test_legacy_current_revision_exports_without_rewriting_store(store, tmp_path):
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
    streams["records"] = [{k: row[k] for k in c.LEGACY_STREAM_UNITS} for row in streams["records"]]
    for lap in streams["laps"]:
        del lap["fit"]
    streams["normalizer_version"] = summary["normalizer_version"] = "1.2.1"
    summary["coverage"] = {k: summary["coverage"][k] for k in c.LEGACY_STREAM_UNITS}
    revision = normalize.sha256(canonical([summary["source_digest"], "1.2.1", c.SCHEMA_VERSION]))
    summary["files"] = {k: f"objects/1001/{revision}/{k}.json" for k in ("summary", "streams")}
    with store.lock(write=True):
        state = store.state()
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
        store.publish_activity(streams, summary, state)
    before = store.read_bytes("index.json")
    target = tmp_path / "analysis.sqlite"
    export.export_analysis(store, target)
    assert read_table(target, "activities")[0]["normalizer_version"] == "1.2.1"
    assert len(read_table(target, "records")) == 3
    assert read_table(target, "records")[0]["accumulated_power"] is None
    events = read_table(target, "events")
    assert [row["timer_type"] for row in events] == ["start", "stop_all"]
    assert all(row["fit_event"] is None for row in events)
    assert not read_table(target, "messages")
    assert store.read_bytes("index.json") == before


@pytest.mark.parametrize("kind", ["store", "symlink", "hardlink", "permissions", "git"])
def test_analysis_rejects_unsafe_destinations(store, tmp_path, kind):
    publish(store)
    target = tmp_path / "analysis.sqlite"
    if kind == "store":
        target = store.root / "activities.sqlite"
    elif kind == "symlink":
        target.symlink_to(store.root / "activities.sqlite")
    elif kind == "hardlink":
        os.link(store.root / "activities.sqlite", target)
    elif kind == "permissions":
        target.write_bytes(b"synthetic")
        target.chmod(0o644)
    else:
        parent = tmp_path / "fake-checkout"
        parent.mkdir(mode=0o700)
        (parent / ".git").write_text("gitdir: elsewhere")
        target = parent / target.name
    before = target.read_bytes() if target.exists() else None
    with pytest.raises(BridgeError):
        export.export_analysis(store, target)
    assert (target.read_bytes() if target.exists() else None) == before


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_analysis_refuses_destination_with_sqlite_sidecars(store, tmp_path, suffix):
    publish(store)
    target = tmp_path / "analysis.sqlite"
    export.export_analysis(store, target)
    before = target.read_bytes()
    sidecar = target.with_name(target.name + suffix)
    sidecar.write_bytes(b"synthetic unfinished writer")
    sidecar.chmod(0o600)
    with pytest.raises(BridgeError) as error:
        export.export_analysis(store, target)
    assert error.value.code == "busy"
    assert target.read_bytes() == before
    assert sidecar.read_bytes() == b"synthetic unfinished writer"
    assert not list(tmp_path.glob(".analysis-*"))


def test_analysis_range_orders_instants_with_fractional_seconds(store, tmp_path):
    run(
        store,
        Source([activity(1001), activity(1002, startTimeGMT="2025-01-02 00:00:00.5")]),
    )
    target = tmp_path / "analysis.sqlite"
    export.export_analysis(store, target)
    metadata = read_table(target, "meta")[0]
    assert metadata["activity_start_utc"] == "2025-01-02T00:00:00Z"
    assert metadata["activity_end_utc"] == "2025-01-02T00:00:00.500000Z"


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("fail_flush", [False, True])
def test_analysis_flushes_published_directory_and_rolls_back_on_failure(
    store, tmp_path, monkeypatch, existing, fail_flush
):
    publish(store)
    target = tmp_path / "analysis.sqlite"
    before = None
    if existing:
        export.export_analysis(store, target)
        before = target.read_bytes()
    publish(store, fit_bytes(power=230))
    fsync = os.fsync
    published_flushes = []

    def flush(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and target.exists():
            if target.read_bytes() != before:
                published_flushes.append(fd)
                if fail_flush:
                    raise OSError("synthetic directory flush failure")
        return fsync(fd)

    monkeypatch.setattr(export.os, "fsync", flush)
    if fail_flush:
        with pytest.raises(OSError):
            export.export_analysis(store, target)
        assert (target.read_bytes() if target.exists() else None) == before
    else:
        export.export_analysis(store, target)
        assert read_table(target, "activities")[0]["avg_power"] == 230
    assert published_flushes
    assert not list(tmp_path.glob(".analysis-*"))


@pytest.mark.parametrize("kind", ["records", "laps", "events", "messages"])
def test_analysis_validates_rehashed_source_rows_before_publication(store, tmp_path, kind):
    publish(store, rich_fit())
    target = tmp_path / "analysis.sqlite"
    export.export_analysis(store, target)
    before = target.read_bytes()
    state = store.state()
    entry = state["activities"]["1001"]
    reference = entry["summary"]["files"]["streams"]
    with store.database(write=True) as db:
        row = json.loads(
            db.execute(
                "SELECT payload FROM samples WHERE name=? AND kind=? AND ordinal=0",
                (reference, kind),
            ).fetchone()[0]
        )
        if kind == "records":
            row["position_lat"] = 123
        else:
            detail = row["fit"] if kind == "laps" else row
            detail["fields"]["serial_number"] = {"value": 123, "unit": "count"}
        db.execute(
            "UPDATE samples SET payload=? WHERE name=? AND kind=? AND ordinal=0",
            (canonical(row), reference, kind),
        )
    # Recompute hashes to prove the export checks contracts, not just digests.
    entry["file_hashes"]["streams"] = normalize.sha256(store.read_bytes(reference))
    with store.database(write=True) as db:
        db.execute("UPDATE documents SET payload=? WHERE name='index.json'", (canonical(state),))
    with pytest.raises(BridgeError):
        export.export_analysis(store, target)
    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".analysis-*"))


def test_analysis_preserves_duplicate_unordered_and_untimed_records(store, tmp_path):
    observations = [(START + 1, 0), (START, 17), (START, 17), (0xFFFFFFFF, 0xFFFF)]
    records = b"".join(
        frame(20, [raw(253, 0x86, stamp), raw(7, 0x84, power)]) for stamp, power in observations
    )
    publish(store, minimal(records=records))
    target = tmp_path / "analysis.sqlite"
    export.export_analysis(store, target)
    rows = sorted(read_table(target, "records"), key=lambda row: row["ordinal"])
    assert [(r["ordinal"], r["timestamp"], r["power"]) for r in rows] == [
        (0, "2025-01-02T00:00:01Z", 0),
        (1, "2025-01-02T00:00:00Z", 17),
        (2, "2025-01-02T00:00:00Z", 17),
        (3, None, None),
    ]
