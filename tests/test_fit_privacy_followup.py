"""Privacy follow-up regressions using only invented FIT/SQLite inputs."""

from contextlib import closing
import json
import sqlite3

import pytest

from garmin_bridge import analysis_export as export
from garmin_bridge.fit_unknown import REPORT_PATH
from garmin_bridge.storage import canonical
from .fit_synthetic import frame, minimal, raw
from .test_analysis_export import publish, read_table
from .test_fit_fields import detail
from .test_fit_full_review import intercepted
from .test_fit_review import compressed


@pytest.mark.parametrize("number", [3, 79])
@pytest.mark.parametrize("identity", [False, True])
def test_small_profile_values_do_not_block_unrelated_unknown_fields(number, identity):
    small = [-1024, -7, 0, 7, 37, 700, 2**16 - 1]
    streams, blocked = intercepted(
        minimal(
            frame(number, [raw(250, 0x8E, small)]),
            records=frame(20, [raw(250, 0x8E, small)]),
        ),
        identity,
    )
    assert streams["records"][0]["unknown_250"] == small
    assert blocked == {(number, 250, "identity_message"): 1}


@pytest.mark.parametrize("value", [2**16, -(2**16), 2**40])
@pytest.mark.parametrize("number", [3, 79])
def test_large_private_identity_copies_still_block_the_entire_family(number, value):
    streams, blocked = intercepted(
        minimal(
            frame(534, [raw(0, 0x8E, 17)])
            + frame(534, [raw(0, 0x8E, value)])
            + frame(number, [raw(250, 0x8E, value)])
        )
    )
    assert streams["raw_messages"] == []
    assert blocked == {(number, 250, "identity_message"): 1, (534, 0, "identity_match"): 2}


def test_small_named_identifiers_and_transmission_categories_do_not_match():
    streams, blocked = intercepted(
        minimal(
            frame(23, [raw(3, 0x8C, 37), raw(20, 0xA, 7), raw(21, 0x8B, 41)]),
            records=frame(20, [raw(250, 0x86, [37, 7, 41])]),
        )
    )
    assert streams["records"][0]["unknown_250"] == [37, 7, 41]
    assert not blocked


def test_named_serial_match_takes_priority_over_the_timestamp_exemption():
    stamp = 1_000_000_000
    streams, blocked = intercepted(
        minimal(
            frame(534, [raw(0, 0x86, stamp)])
            + frame(23, [raw(3, 0x8C, stamp), raw(253, 0x86, stamp)])
        )
    )
    assert streams["raw_messages"] == []
    assert blocked == {(534, 0, "identity_match"): 1}


@pytest.mark.parametrize("number,field", [(23, 250), (20, 250), (534, 0), (325, 42)])
@pytest.mark.parametrize("count", [1, 5])
def test_constant_large_integers_are_blocked_without_message_or_field_special_cases(
    number, field, count
):
    streams, blocked = intercepted(minimal(frame(number, [raw(field, 0x86, 2**24)]) * count))
    assert blocked == {(number, field, "suspected_identity"): count}
    assert str(2**24).encode() not in canonical(streams)


@pytest.mark.parametrize("distinct", [1, 2, 3, 4, 8])
def test_few_distinct_boundary_and_large_varying_counters(distinct):
    values = [2**40 + i for i in range(distinct)] * 2
    streams, blocked = intercepted(minimal(b"".join(frame(534, [raw(0, 0x8F, v)]) for v in values)))
    if distinct <= 3:
        assert streams["raw_messages"] == []
        assert blocked == {(534, 0, "suspected_identity"): len(values)}
    else:
        assert [r["fields"]["0"] for r in streams["raw_messages"]] == values
        assert not blocked


def test_suspected_identity_threshold_and_nulls():
    streams, blocked = intercepted(
        minimal(frame(534, [raw(0, 0x86, 2**24 - 1), raw(1, 0x86, 0xFFFFFFFF)]))
    )
    assert streams["raw_messages"] == [{"mesg_num": 534, "fields": {"0": 2**24 - 1, "1": None}}]
    assert not blocked


def test_constant_id_array_slot_cannot_hide_behind_varying_counters_and_nulls():
    extra = frame(534, [raw(0, 0x86, [0xFFFFFFFF, 0xFFFFFFFF])])
    extra += frame(534, [raw(1, 0x86, 7)])  # Missing field is not variation.
    extra += b"".join(frame(534, [raw(0, 0x86, [2**24, 2**24 + i])]) for i in range(5))
    streams, blocked = intercepted(minimal(extra))
    assert blocked == {(534, 0, "suspected_identity"): 6}
    assert streams["raw_messages"] == [{"mesg_num": 534, "fields": {"1": 7}}]


def test_distinct_counts_are_scoped_to_message_and_field():
    extra = b"".join(frame(534, [raw(0, 0x86, 2**24 + i)]) for i in range(5))
    extra += frame(534, [raw(1, 0x86, 2**24)])
    extra += frame(325, [raw(0, 0x86, 2**24)])
    streams, blocked = intercepted(minimal(extra))
    assert blocked == {(534, 1, "suspected_identity"): 1, (325, 0, "suspected_identity"): 1}
    assert len(streams["raw_messages"]) == 5


@pytest.mark.parametrize("offset", [-86401, -86400, 0, 86400, 86401])
@pytest.mark.parametrize("number,field", [(534, 0), (23, 250)])
def test_timestamp_exemption_requires_closeness_to_reviewed_file_time(number, field, offset):
    stamp = 1_000_000_000
    streams, blocked = intercepted(
        minimal(
            frame(number, [raw(field, 0x86, stamp + offset)]) + frame(23, [raw(253, 0x86, stamp)])
        )
    )
    if abs(offset) > 86400:
        assert blocked == {(number, field, "suspected_identity"): 1}
    else:
        assert not blocked
        if number == 534:
            assert streams["raw_messages"][0]["fields"] == {str(field): stamp + offset}
        else:
            assert detail(streams, "device_info")["unknown"] == {f"unknown_{field}": stamp + offset}


@pytest.mark.parametrize("value", [0x0FFFFFFF, 0x10000000, 0xFFFFFFFE, 0xFFFFFFFF])
def test_timestamp_exemption_obeys_fit_absolute_time_bounds(value):
    stamp = max(0x10000000, min(value, 0xFFFFFFFE))
    streams, blocked = intercepted(
        minimal(frame(534, [raw(0, 0x8F, value)]) + frame(23, [raw(253, 0x86, stamp)]))
    )
    assert bool(blocked) is (value in {0x0FFFFFFF, 0xFFFFFFFF})
    assert bool(streams["raw_messages"]) is (not blocked)


@pytest.mark.parametrize("anchor", ["unknown", "relative", "none", "wrong_wire_type"])
def test_unreviewed_or_missing_time_evidence_cannot_exempt_suspected_ids(anchor):
    stamp = 1_000_000_000
    extra = {
        "unknown": frame(325, [raw(253, 0x86, stamp)]),
        "relative": frame(23, [raw(253, 0x86, 17)]),
        "none": b"",
        "wrong_wire_type": frame(23, [raw(253, 0x8F, stamp)]),
    }[anchor]
    streams, blocked = intercepted(minimal(extra + frame(534, [raw(0, 0x86, stamp)])))
    assert blocked[(534, 0, "suspected_identity")] == 1
    assert not streams["raw_messages"]


def test_reviewed_compressed_timestamp_can_exempt_nearby_unknown_time():
    stamp = 1_000_000_000
    records = frame(20, [raw(253, 0x86, stamp)])
    records += compressed(20, [raw(7, 0x84, 17)], 1)
    streams, blocked = intercepted(
        minimal(frame(534, [raw(0, 0x86, stamp + 86401)]), records=records)
    )
    assert not blocked
    assert streams["raw_messages"][0]["fields"]["0"] == stamp + 86401


def test_developer_timestamp_cannot_exempt_suspected_ids():
    stamp = 1_000_000_000
    descriptions = frame(207, [raw(3, 2, 0), raw(1, 13, b"SYNTHETIC-APP-ID")])
    descriptions += frame(
        206, [raw(0, 2, 0), raw(1, 2, 253), raw(2, 2, 0x86), raw(3, 7, b"timestamp\0")]
    )
    extra = descriptions + frame(23, [], developer=[(253, 0, stamp.to_bytes(4, "little"))])
    extra += frame(534, [raw(0, 0x86, stamp)])
    streams, blocked = intercepted(minimal(extra))
    assert streams["raw_messages"] == []
    assert blocked == {(534, 0, "suspected_identity"): 1}


def test_all_blocked_raw_rows_are_omitted_from_store_export_and_value_free_report(store, tmp_path):
    extra = frame(534, [raw(0, 7, b"Synthetic text\0")])
    extra += frame(534, [raw(1, 0x86, 2**24)])
    extra += frame(534, [raw(2, 0x86, 7), raw(1, 0x86, 2**24)])
    extra += frame(534, [raw(2, 0x86, 11)])
    assert publish(store, minimal(extra))
    with store.database() as db:
        stored = db.execute(
            "SELECT ordinal,payload FROM samples WHERE kind='raw_messages' ORDER BY ordinal"
        ).fetchall()
    expected = [{"2": 7}, {"2": 11}]
    assert [r[0] for r in stored] == [0, 1]
    assert [json.loads(r[1])["fields"] for r in stored] == expected
    report = store.read_json(REPORT_PATH)
    assert report == [
        {"field": "534.0", "reason": "possible_identity", "row_count": 1},
        {"field": "534.1", "reason": "suspected_identity", "row_count": 2},
    ]
    target = tmp_path / "followup-analysis.sqlite"
    export.export_analysis(store, target)
    rows = read_table(target, "raw_messages")
    assert [r["ordinal"] for r in rows] == [0, 1]
    assert [json.loads(r["fields_json"]) for r in rows] == expected
    assert read_table(target, "meta")[0]["raw_messages_count"] == 2


@pytest.mark.parametrize("event,kind", [(0, 0), (254, 250), (255, 255)])
def test_analysis_lap_event_columns_are_sql_integers_or_null(store, tmp_path, event, kind):
    lap = frame(19, [raw(0, 0, event), raw(1, 0, kind)])
    assert publish(store, minimal(lap=lap))
    target = tmp_path / "followup-laps.sqlite"
    export.export_analysis(store, target)
    with closing(sqlite3.connect(target)) as db:
        columns = {r[1]: r[2] for r in db.execute("PRAGMA table_info(laps)")}
        assert columns["fit_event"] == columns["fit_event_type"] == "INTEGER"
        row = db.execute(
            "SELECT fit_event,fit_event_type,typeof(fit_event),typeof(fit_event_type) FROM laps"
        ).fetchone()
    assert row == (
        (None, None, "null", "null") if event == 255 else (event, kind, "integer", "integer")
    )
