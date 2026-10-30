"""Independent full-field review regressions; entirely synthetic FIT/SQLite inputs."""

from collections import Counter
import copy
import json

import pytest

from garmin_bridge import analysis_export as export, contract as c, normalize, sync
from garmin_bridge.errors import BridgeError
from garmin_bridge.fit_unknown import REPORT_PATH
from garmin_bridge.sqlite_store import MAX_SAMPLE_BYTES
from garmin_bridge.storage import canonical
from .fit_synthetic import frame, minimal, raw
from .test_analysis_export import publish, read_table
from .test_cli_storage import command
from .test_fit_fields import parse
from .test_fit_review import ANCHOR, compressed


INTEGER_BASES = [0, 1, 2, 0x83, 0x84, 0x86, 0xA, 0x8B, 0x8C, 0x8E, 0x8F, 0x90]


def intercepted(data, identity=False):
    blocked = Counter()
    summary, streams = normalize.normalize(
        data,
        normalize.local_metadata("1001"),
        "2025-01-03T00:00:00Z",
        "a" * 64,
        include_identity=identity,
        blocked_fields=blocked,
    )
    c.validate_streams(streams)
    return streams, blocked


@pytest.mark.parametrize("base", INTEGER_BASES)
@pytest.mark.parametrize("number", [20, 534])
def test_text_arrays_are_blocked_for_every_integer_base(base, number):
    encoded = frame(number, [raw(250, base, [83, 89, 78, 84, 72, 0])])
    data = minimal(records=encoded) if number == 20 else minimal(encoded)
    streams, blocked = intercepted(data)
    assert blocked == {(number, 250, "possible_identity"): 1}
    assert b"83,89,78,84,72" not in canonical(streams)


@pytest.mark.parametrize("base,invalid", [(2, 255), (0xA, 0), (0x84, 65535)])
def test_null_arrays_are_not_misclassified_as_text(base, invalid):
    streams, blocked = intercepted(minimal(frame(534, [raw(0, base, [invalid] * 4)])))
    assert streams["raw_messages"][0]["fields"] == {"0": [None] * 4}
    assert not blocked


@pytest.mark.parametrize("base", [2, 0x84, 0x86])
def test_utf8_names_are_blocked_in_integer_arrays(base):
    data = minimal(frame(534, [raw(0, base, list("Synthétique".encode("utf-8")))]))
    streams, blocked = intercepted(data)
    assert streams["raw_messages"] == []
    assert blocked == {(534, 0, "possible_identity"): 1}


def test_raw_64bit_extremes_and_invalid_slots_roundtrip_sql_without_precision_loss(store, tmp_path):
    data = minimal(
        # Vary each non-null slot beyond the suspected-identity distinct limit.
        b"".join(
            frame(
                534,
                [
                    raw(0, 0x8E, [-(2**63) + i, 2**63 - 2 - i, 2**63 - 1]),
                    raw(1, 0x8F, [2**64 - 2 - i, 2**64 - 1]),
                    raw(2, 0x90, [0, 2**64 - 1 - i]),
                ],
            )
            for i in range(4)
        )
    )
    assert publish(store, data)
    expected = {"0": [-(2**63), 2**63 - 2, None], "1": [2**64 - 2, None], "2": [None, 2**64 - 1]}
    summary = store.state()["activities"]["1001"]["summary"]
    assert store.read_json(summary["files"]["streams"])["raw_messages"][0]["fields"] == expected
    target = tmp_path / "review-integers.sqlite"
    export.export_analysis(store, target)
    assert json.loads(read_table(target, "raw_messages")[0]["fields_json"]) == expected


@pytest.mark.parametrize("base", INTEGER_BASES)
@pytest.mark.parametrize("number", [20, 534])
def test_coordinate_copies_block_entire_field_even_before_location_is_seen(base, number):
    # Small/constant coordinates also matter. No magnitude threshold may exempt
    # them, and switching integer base types must not evade the interception.
    encoded = frame(number, [raw(250, base, [7, 17])])
    encoded += frame(number, [raw(250, base, [19, 21])])
    coordinates = frame(20, [raw(0, 0x85, 7), raw(1, 0x85, 13)])
    data = minimal(records=encoded + coordinates)
    streams, blocked = intercepted(data)
    assert blocked == {(number, 250, "position_match"): 2}
    assert all("unknown_250" not in row for row in streams["records"])
    assert all("250" not in row["fields"] for row in streams["raw_messages"])


@pytest.mark.parametrize("base", [0x86, 0x8C, 0x8E, 0x8F, 0x90])
def test_unsigned_semicircle_copies_are_blocked(base):
    data = minimal(
        frame(534, [raw(0, base, (-(123456789)) & 0xFFFFFFFF)]),
        records=frame(20, [raw(0, 0x85, -123456789)]),
    )
    streams, blocked = intercepted(data)
    assert streams["raw_messages"] == []
    assert blocked == {(534, 0, "position_match"): 1}


@pytest.mark.parametrize("base,offset", [(0x84, 17), (0x86, 17), (0x8E, 2**60), (0x8F, 2**63)])
def test_scaled_position_correlations_block_arrays_without_float_precision_loss(base, offset):
    records = frame(20, [raw(250, base, [1, 7])])
    for coordinate in (1000, 2000, 3000):
        records += frame(
            20, [raw(0, 0x85, coordinate), raw(250, base, [coordinate * 2 + offset, 7])]
        )
    streams, blocked = intercepted(minimal(records=records))
    assert all("unknown_250" not in row for row in streams["records"])
    assert blocked == {(20, 250, "position_correlated"): 4}


def test_noncorrelated_raw_measurements_survive_and_invalid_positions_do_not_match():
    records = b"".join(
        frame(20, [raw(0, 0x85, position), raw(250, 0x86, value)])
        for position, value in [(1000, 10), (2000, 5), (3000, 30), (4000, 2), (0x7FFFFFFF, 0)]
    )
    streams, blocked = intercepted(minimal(records=records))
    assert [row["unknown_250"] for row in streams["records"]] == [10, 5, 30, 2, 0]
    assert not blocked


@pytest.mark.parametrize("identity", [False, True])
def test_unknown_profile_is_always_audited_and_copied_identifiers_are_blocked(identity):
    data = minimal(
        frame(534, [raw(0, 0x86, 424242)])
        + frame(3, [raw(250, 0x86, 424242)])
        + frame(23, [raw(3, 0x8C, 314159)]),
        records=frame(20, [raw(250, 0x86, 314159)]),
    )
    streams, blocked = intercepted(data, identity)
    assert blocked == {
        (3, 250, "identity_message"): 1,
        (534, 0, "identity_match"): 1,
        (20, 250, "identity_match"): 1,
    }
    assert streams["raw_messages"] == []
    assert "unknown_250" not in streams["records"][0]


def test_blocked_values_never_reach_structured_store_export_report_or_cli(store, tmp_path, capsys):
    data = minimal(
        frame(534, [raw(1, 0x86, 123456789), raw(2, 1, [83, 89, 78, 84, 72])]),
        records=frame(20, [raw(0, 0x85, 123456789), raw(253, 0x86, 1000000000)]),
    )
    assert publish(store, data)
    with store.database() as db:
        contents = [
            r[0]
            for r in db.execute(
                "SELECT payload FROM documents UNION ALL SELECT payload FROM samples"
            )
        ]
    assert all(
        b"123456789" not in payload and b"83,89,78,84,72" not in payload for payload in contents
    )
    report = store.read_bytes(REPORT_PATH)
    assert b"123456789" not in report and b"83,89,78,84,72" not in report
    assert all(set(row) == {"field", "reason", "row_count"} for row in json.loads(report))
    code, output, captured = command(store, capsys, "fit-blocked")
    assert code == 0 and b"123456789" not in canonical(output)
    assert "123456789" not in captured.err
    target = tmp_path / "review-analysis.sqlite"
    export.export_analysis(store, target)
    assert read_table(target, "raw_messages") == []


@pytest.mark.parametrize("number", [20, 534])
@pytest.mark.parametrize("second_base", [0x86, 0x85])
def test_duplicate_physical_fields_fail_with_constant_diagnostics(number, second_base, capsys):
    data = minimal(frame(number, [raw(250, 0x86, 123456789), raw(250, second_base, 123456789)]))
    with pytest.raises(BridgeError) as caught:
        parse(data)
    assert caught.value.code == "invalid_fit"
    assert "123456789" not in str(caught.value)
    captured = capsys.readouterr()
    assert "123456789" not in captured.out + captured.err


@pytest.mark.parametrize("number", [3, 79, 534])
def test_unreviewed_compressed_header_cannot_advance_trusted_clock(number):
    records = frame(20, [raw(253, 0x86, ANCHOR)])
    records += compressed(number, [raw(250, 0x86, 17)], 1)
    records += compressed(20, [raw(7, 0x84, 10)], 2)
    with pytest.raises(BridgeError) as caught:
        parse(minimal(records=records))
    assert caught.value.code == "invalid_fit"


def test_relative_timestamp_cannot_roll_over_into_a_valid_absolute_anchor():
    records = frame(20, [raw(253, 0x86, 0x0FFFFFFF)])
    records += compressed(20, [raw(7, 0x84, 10)], 0)
    with pytest.raises(BridgeError):
        parse(minimal(records=records))


@pytest.mark.parametrize("number", [142, 162])
def test_new_documented_messages_can_supply_compressed_timestamp_anchor(number):
    records = frame(number, [raw(253, 0x86, ANCHOR)])
    records += compressed(20, [raw(7, 0x84, 10)], 0)
    _, streams = parse(minimal(records=records))
    assert streams["records"][0]["timestamp"] == "2021-09-08T01:47:12Z"


def test_developer_payloads_cannot_enter_raw_messages_or_unknown_objects():
    description = frame(207, [raw(3, 2, 0), raw(1, 13, b"SYNTHETIC-APP-ID")])
    description += frame(
        206, [raw(0, 2, 0), raw(1, 2, 250), raw(2, 2, 0x86), raw(3, 7, b"unknown_250\0")]
    )
    for number in (20, 534, 3, 79):
        description += frame(
            number, [raw(249, 0x86, 7)], developer=[(250, 0, (123456789).to_bytes(4, "little"))]
        )
    streams, _ = intercepted(minimal(description))
    assert b"123456789" not in canonical(streams)


def test_window_exposes_only_documented_measurements(store, capsys):
    data = minimal(records=frame(20, [raw(253, 0x86, 1000000000), raw(250, 0x86, 777)]))
    assert publish(store, data)
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
    assert code == 0
    assert window["records"][0]["sample"].keys() == c.STREAM_UNITS.keys()
    window["records"][0]["sample"]["unknown_250"] = 777
    with pytest.raises(BridgeError):
        c.validate_output("window", window)


def test_oversized_raw_row_is_rejected_before_any_immutable_rows_commit(store):
    assert publish(store, minimal())
    before = copy.deepcopy(store.state())
    streams = store.read_json(before["activities"]["1001"]["summary"]["files"]["streams"])
    row = {"mesg_num": 534, "fields": {str(i): [2**64 - 2] * 255 for i in range(255)}}
    assert len(canonical(row)) > MAX_SAMPLE_BYTES
    streams["raw_messages"] = [row]
    name = "objects/1001/" + "a" * 64 + "/streams.json"
    with store.lock(write=True), pytest.raises(BridgeError):
        store.write_json(name, streams)
    assert store.state() == before and store.read_json(name) is None
    with store.database() as db:
        assert db.execute("SELECT count(*) FROM samples WHERE name=?", (name,)).fetchone()[0] == 0


def test_raw_message_count_limit_preserves_current_revision_and_report(store, monkeypatch):
    assert publish(store, minimal(frame(3, [raw(250, 0x86, 424242)])))
    before = copy.deepcopy(store.state())
    report = store.read_bytes(REPORT_PATH)
    monkeypatch.setattr(normalize, "MAX_RECORDS", 2)
    with pytest.raises(BridgeError) as caught:
        publish(store, minimal(frame(534, [raw(0, 0x86, 7)]) * 3))
    assert caught.value.code == "invalid_fit"
    assert store.state() == before and store.read_bytes(REPORT_PATH) == report


@pytest.mark.parametrize("table", ["records", "laps", "messages", "raw_messages"])
def test_text_array_injection_fails_export_validation_and_preserves_destination(
    store, tmp_path, monkeypatch, table
):
    assert publish(store, minimal(frame(534, [raw(0, 0x86, 7)])))
    target = tmp_path / "review-text-array.sqlite"
    export.export_analysis(store, target)
    before = target.read_bytes()
    insert = export.insert

    def inject(db, name, row):
        row = dict(row)
        if name == table:
            if name == "messages":
                if row["message"] == "session":
                    value = json.loads(row["detail_json"])
                    value["unknown"] = {"unknown_250": [83, 89, 78, 84, 72]}
                    row["detail_json"] = json.dumps(value)
            else:
                key = "fields_json" if name == "raw_messages" else "unknown_json"
                field = "250" if name == "raw_messages" else "unknown_250"
                row[key] = json.dumps({field: [83, 89, 78, 84, 72]})
        return insert(db, name, row)

    monkeypatch.setattr(export, "insert", inject)
    with pytest.raises(BridgeError):
        export.export_analysis(store, target)
    assert target.read_bytes() == before


def test_reparse_preserves_prior_revisions(store, monkeypatch):
    with monkeypatch.context() as previous:
        for module in (c, normalize, sync):
            previous.setattr(module, "NORMALIZER_VERSION", "1.3.0")
        assert publish(store, minimal())
    prior = copy.deepcopy(store.state())
    files = prior["activities"]["1001"]["summary"]["files"]
    saved = {name: store.read_bytes(name) for name in files.values()}
    assert publish(store, minimal())
    assert not publish(store, minimal())
    assert all(store.read_bytes(name) == value for name, value in saved.items())
    assert store.state()["activities"]["1001"]["summary"]["normalizer_version"] == "1.4.0"
    assert store.state()["covered_ranges"] == prior["covered_ranges"]
