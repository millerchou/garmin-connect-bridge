"""Invented trajectories encoded from constants; never modified real exports."""

from datetime import timedelta
import json
import math
import struct

import pytest

from garmin_bridge import cli, segments, sqlite_transfer as transfer
from garmin_bridge.import_fit import import_file
from garmin_bridge.sqlite_store import DATABASE, SQLiteStore
from .synthetic import EPOCH, START, crc16, message
from .test_import_fit import private_file


def route_fit(route, *, gap=None, power=200):
    fields = [(253, 0x86, "I"), (0, 0x85, "i"), (1, 0x85, "i"), (7, 0x84, "H"), (3, 2, "B")]
    # One equatorial step is about 11m. Coordinates are invented constants.
    rows = [
        (
            START + i * 2,
            0x7FFFFFFF if gap == i else int(y / 111195 * 2**31 / 180),
            int(x / 111195 * 2**31 / 180),
            0xFFFF if i % 7 == 1 else power,
            140,
        )
        for i, (x, y) in enumerate(route)
    ]
    data = message(0, [(0, 0, "B")], [(4,)])
    data += message(20, fields, rows)
    data += message(
        18,
        [(2, 0x86, "I"), (253, 0x86, "I"), (5, 0, "B")],
        [(START, START + (len(route) - 1) * 2, 2)],
    )
    header = struct.pack("<BBHI4s", 12, 0x20, 2200, len(data), b".FIT")
    return header + data + struct.pack("<H", crc16(header + data))


def stamp(seconds):
    return (EPOCH + timedelta(seconds=START + seconds)).isoformat().replace("+00:00", "Z")


def populate(store, tmp_path, route, activity="1001", **kwargs):
    path = private_file(tmp_path, route_fit(route, **kwargs))
    with store.lock(write=True):
        return import_file(store, path, activity)


@pytest.fixture
def straight():
    return [(i * 11, 0) for i in range(31)]


@pytest.fixture
def store(tmp_path):
    return SQLiteStore(tmp_path / "segments")


def test_directed_path_repeated_efforts_metrics_and_cache(store, tmp_path, straight, monkeypatch):
    populate(store, tmp_path, straight)
    # Out, return, out, return, out, with no equal timestamps or missing positions.
    repeated = straight + straight[-2::-1] + straight[1:] + straight[-2::-1] + straight[1:]
    populate(store, tmp_path, repeated, "1002", power=0)
    populate(store, tmp_path, list(reversed(straight)), "1003")
    with store.lock(write=True):
        definition = segments.define(store, "1001", stamp(0), stamp(60))
        assert segments.refresh(store, definition["id"]) == 3
    result = segments.history(store, definition["id"], limit=50)
    efforts = [r["effort"] for r in result["results"] if r["activity_id"] == "1002"]
    assert len(efforts) == 3
    assert [e["ordinal"] for e in efforts] == [1, 2, 3]
    assert all(e["elapsed_seconds"] == 60 and e["power_mean"] == 0 for e in efforts)
    assert all(e["power_present"] < e["sample_count"] for e in efforts)
    assert [r for r in result["results"] if r["activity_id"] == "1003"][0]["status"] == "no_match"
    assert segments.history(store, definition["id"], limit=2)["next_offset"] == 2
    assert segments.history(store, definition["id"], offset=100)["results"] == []

    def denied(*args):
        pytest.fail("unchanged successful cache reparsed originals")

    monkeypatch.setattr(segments, "locations", denied)
    with store.lock(write=True):
        assert segments.refresh(store, definition["id"]) == 0
    snapshot = tmp_path / "snapshot"
    transfer.export_snapshot(store, snapshot)
    assert transfer.verify_snapshot(snapshot)["verified"]
    transfer.restore_snapshot(snapshot, tmp_path / "restored")
    assert segments.history(SQLiteStore(tmp_path / "restored"), definition["id"]) == result
    for path in snapshot.iterdir():
        data = path.read_bytes()
        for forbidden in (
            b"position_lat",
            b"position_long",
            b"polyline",
            b"raw/",
            b"serial_number",
        ):
            assert forbidden not in data


def test_matching_rejects_endpoint_only_detour_gaps_and_short_reference(store, tmp_path, straight):
    populate(store, tmp_path, straight)
    detour = [(i * 11, 110 * math.sin(i * math.pi / 30)) for i in range(31)]
    populate(store, tmp_path, detour, "1002")
    populate(store, tmp_path, straight, "1003", gap=15)
    with store.lock(write=True):
        definition = segments.define(store, "1001", stamp(0), stamp(60))
        segments.refresh(store, definition["id"])
    by_id = {r["activity_id"]: r for r in segments.history(store, definition["id"])["results"]}
    assert by_id["1002"]["status"] == "no_match"
    assert by_id["1003"]["status"] == "insufficient_data"
    with store.lock(write=True):
        definition = segments.define(store, "1001", stamp(0), stamp(4), definition["id"])
        segments.refresh(store, definition["id"])
    assert all(
        r["status"] == "insufficient_data"
        for r in segments.history(store, definition["id"])["results"]
    )


def test_revisions_hide_stale_results_and_recompute_affected_only(
    store, tmp_path, straight, monkeypatch
):
    populate(store, tmp_path, straight)
    populate(store, tmp_path, straight, "1002")
    with store.lock(write=True):
        definition = segments.define(store, "1001", stamp(0), stamp(60))
        segments.refresh(store, definition["id"])
    populate(store, tmp_path, straight, "1002", power=230)
    assert segments.history(store, definition["id"])["cached_activity_count"] == 1
    with store.lock(write=True):
        assert segments.refresh(store, definition["id"]) == 1
    assert segments.history(store, definition["id"])["cached_activity_count"] == 2
    with store.lock(write=True):
        definition = segments.define(store, "1001", stamp(2), stamp(58), definition["id"])
    assert definition["version"] == 2
    assert segments.history(store, definition["id"])["results"] == []
    with store.lock(write=True):
        assert segments.refresh(store, definition["id"]) == 2


def test_cli_segment_is_bounded_and_closed(store, tmp_path, straight, capsys):
    populate(store, tmp_path, straight)
    base = ["--data-dir", str(store.root), "segment"]
    assert (
        cli.main(base + ["define", "--reference", "1001", "--start", stamp(0), "--end", stamp(60)])
        == 0
    )
    ident = json.loads(capsys.readouterr().out)["definition"]["id"]
    assert cli.main(base + ["refresh", ident]) == 0
    assert json.loads(capsys.readouterr().out)["updated"] == 1
    assert cli.main(base + ["history", ident, "--limit", "1"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["results"]) == 1 and result["results"][0]["status"] == "matched"
    assert (store.root / DATABASE).exists()


def test_elapsed_includes_stationary_pause(store, tmp_path, straight):
    populate(store, tmp_path, straight)
    paused = straight[:16] + [straight[15]] * 10 + straight[16:]
    populate(store, tmp_path, paused, "1002")
    with store.lock(write=True):
        definition = segments.define(store, "1001", stamp(0), stamp(60))
        segments.refresh(store, definition["id"])
    row = [
        r
        for r in segments.history(store, definition["id"])["results"]
        if r["activity_id"] == "1002"
    ][0]
    assert row["effort"]["elapsed_seconds"] == 80
    assert "moving_time" not in row["effort"]


def test_reference_revision_invalidates_every_dependent_cache(store, tmp_path, straight):
    populate(store, tmp_path, straight)
    populate(store, tmp_path, straight, "1002")
    with store.lock(write=True):
        definition = segments.define(store, "1001", stamp(0), stamp(60))
        segments.refresh(store, definition["id"])
    populate(store, tmp_path, straight, power=231)
    assert segments.history(store, definition["id"])["cached_activity_count"] == 0
    with store.lock(write=True):
        assert segments.refresh(store, definition["id"]) == 2


@pytest.mark.parametrize("table", ["segments", "efforts"])
def test_nested_segment_payload_fails_snapshot(store, tmp_path, straight, table):
    from garmin_bridge.storage import canonical
    from garmin_bridge.errors import BridgeError

    populate(store, tmp_path, straight)
    with store.lock(write=True):
        definition = segments.define(store, "1001", stamp(0), stamp(60))
        segments.refresh(store, definition["id"])
        with store.database(write=True) as db:
            payload = json.loads(db.execute(f"SELECT payload FROM {table}").fetchone()[0])
            payload["extra"] = {"location": "synthetic-private"}
            db.execute(f"UPDATE {table} SET payload=?", (canonical(payload),))
    with pytest.raises(BridgeError):
        transfer.export_snapshot(store, tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()
