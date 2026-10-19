"""Cadence evidence from FIT bytes built entirely from invented constants."""

import copy
import struct

import pytest

from garmin_bridge import backup, contract, normalize, sanitize_fit, sync
from garmin_bridge.errors import BridgeError
from garmin_bridge.storage import Store
from .synthetic import START, activity, crc16, message
from .test_cli_storage import command
from .test_import_fit import importing, private_file

SUMMARY = "cadence_summary_max_below_avg"
RECORD = "cadence_record_max_exceeds_session"


def cadence_fit(
    avg=70, maximum=80, samples=((0, 80, 64),), start=0, end=60, fraction=True, session_fraction=64
):
    fields = [(253, 0x86, "I")]
    integer_present = all(c is not None for _, c, _ in samples)
    if integer_present:
        fields.append((4, 2, "B"))
    if fraction:
        fields.append((53, 2, "B"))
    rows = [
        (
            0xFFFFFFFF if t is None else START + t,
            *([c] if integer_present else []),
            *([f] if fraction else []),
        )
        for t, c, f in samples
    ]
    data = message(0, [(0, 0, "B")], [(4,)])
    data += message(20, fields, rows)
    fields = [(5, 0, "B"), (8, 0x86, "I")]
    values = [2, 1000]  # Active time deliberately differs from elapsed time.
    for number, value, base, fmt in (
        (2, None if start is None else START + start, 0x86, "I"),
        (253, None if end is None else START + end, 0x86, "I"),
        (18, avg, 2, "B"),
        (19, maximum, 2, "B"),
        (92, session_fraction, 2, "B"),
        (93, session_fraction, 2, "B"),
    ):
        if value is not None:
            fields.append((number, base, fmt))
            values.append(value)
    data += message(18, fields, [values])
    header = struct.pack("<BBHI4s", 12, 0x20, 2200, len(data), b".FIT")
    return header + data + struct.pack("<H", crc16(header + data))


def parse(meta=None, **options):
    return normalize.normalize(
        cadence_fit(**options),
        meta or normalize.local_metadata("1001"),
        "2025-01-03T00:00:00Z",
        "a" * 64,
    )


@pytest.mark.parametrize(
    "options,expected",
    [
        ({}, set()),
        ({"avg": 90}, {SUMMARY}),
        ({"samples": ((60, 81, 0),)}, {RECORD}),
        ({"samples": ((0, 81, 0),)}, {RECORD}),
        ({"samples": ((0, None, 64),)}, set()),
        ({"samples": ((0, 200, 0), (1, 200, 0))}, {RECORD}),
        ({"samples": ((30, 81, 0),)}, {RECORD}),
        ({"maximum": 100}, set()),
        ({"avg": 0, "maximum": 0, "samples": ((0, 0, 0),)}, set()),
        ({"avg": None}, set()),
        ({"avg": 255}, set()),
        ({"maximum": None}, set()),
        ({"maximum": 255}, set()),
        ({"end": None, "samples": ((0, 200, 0),)}, set()),
        ({"start": None, "samples": ((0, 200, 0),)}, set()),
        ({"end": 0, "samples": ((0, 200, 0),)}, set()),
        ({"end": -1, "samples": ((0, 200, 0),)}, set()),
        ({"samples": ((61, 200, 0), (-1, 200, 0), (0, 80, 0))}, set()),
        ({"samples": ((None, 200, 0),)}, set()),
        ({"samples": ()}, set()),
        ({"samples": ((0, 255, 64),)}, set()),
        ({"samples": ((0, 80, 255),)}, set()),
        ({"fraction": False}, set()),
        ({"session_fraction": None}, set()),
        ({"session_fraction": 255}, set()),
        ({"samples": ((0, 255, 64), (30, 81, 64))}, {RECORD}),
        ({"maximum": 240, "samples": ((0, 200, 0), (1, 80, 0))}, set()),
        ({"maximum": 240, "samples": ((0, 200, 0), (1, 200, 0))}, set()),
    ],
)
def test_comparability_and_preservation(options, expected):
    summary, streams = parse(**options)
    assert set(summary["quality_flags"]) & {SUMMARY, RECORD} == expected
    samples = options.get("samples", ((0, 80, 64),))
    values = [
        None
        if c in (None, 255)
        else c + (f / 128 if options.get("fraction", True) and f != 255 else 0)
        for _, c, f in samples
    ]
    assert [r["cadence"] for r in streams["records"]] == values
    assert summary["coverage"]["cadence"] == {
        "present": sum(v is not None for v in values),
        "missing": sum(v is None for v in values),
        "total": len(values),
    }
    for key, option, default in (("avg_cadence", "avg", 70), ("max_cadence", "maximum", 80)):
        value = options.get(option, default)
        assert summary["metrics"][key]["value"] == (None if value == 255 else value)


@pytest.mark.parametrize(
    "avg,maximum,expected", [(None, 80, set()), (90, None, set()), (None, None, {SUMMARY})]
)
def test_mixed_sources_and_api_scope(avg, maximum, expected):
    meta = normalize.metadata(
        activity(averageBikingCadenceInRevPerMinute=100, maxBikingCadenceInRevPerMinute=70)
    )
    summary, _ = parse(meta=meta, avg=avg, maximum=maximum, samples=((0, 75, 0),))
    assert set(summary["quality_flags"]) & {SUMMARY, RECORD} == expected


@pytest.mark.parametrize("old_version", ["1.0.0", "1.1.0", "1.2.0"])
def test_old_backup_cache_reparse_and_immutable_revision(
    store, tmp_path, capsys, monkeypatch, old_version
):
    path = private_file(tmp_path, cadence_fit(avg=90, samples=((30, 100, 64),)))
    with monkeypatch.context() as patch:
        patch.setattr(normalize, "NORMALIZER_VERSION", old_version)
        patch.setattr(sync, "NORMALIZER_VERSION", old_version)
        patch.setattr(contract, "NORMALIZER_VERSION", old_version)
        patch.setattr(normalize, "_cadence_quality", lambda *args: set())
        old = importing(store, capsys, path)[1]["activity"]
        bundle = tmp_path / "old.zip"
        backup.export_bundle(store, bundle)
    before = {ref: store.read_bytes(ref) for ref in old["files"].values()}
    backup.verify_bundle(bundle)
    restored = Store(tmp_path / "restored")
    backup.restore_bundle(restored, bundle)
    assert command(restored, capsys, "activity", "1001")[1]["activity"] == old
    for target in (store, restored):
        result = importing(target, capsys, path)[1]
        assert result["status"] == "updated"
        new = result["activity"]
        assert new["normalizer_version"] == "1.2.1"
        assert {SUMMARY, RECORD} <= set(new["quality_flags"])
        assert new["files"] != old["files"]
        assert importing(target, capsys, path)[1]["status"] == "unchanged"
        assert all(target.read_bytes(ref) == content for ref, content in before.items())


def test_sanitize_import_backup_restore_preserves_values_and_flags(store, tmp_path, capsys):
    content = cadence_fit(avg=90, samples=((0, 0, 0), (30, 100, 64), (31, 255, 64)))
    path = private_file(tmp_path, content)
    destination = tmp_path / "derived"
    sanitize_fit.sanitize_file(path, destination)
    clean = (destination / "derived.fit").read_bytes()
    original_summary, original_streams = normalize.normalize(
        content, normalize.local_metadata("1001"), "2025-01-03T00:00:00Z", "a" * 64
    )
    clean_summary, clean_streams = normalize.normalize(
        clean, normalize.local_metadata("1001"), "2025-01-03T00:00:00Z", "a" * 64
    )
    assert original_streams == clean_streams
    for key in ("metrics", "coverage", "quality_flags"):
        assert original_summary[key] == clean_summary[key]
    summary = importing(store, capsys, destination / "derived.fit")[1]["activity"]
    assert {SUMMARY, RECORD} <= set(summary["quality_flags"])
    bundle = tmp_path / "new.zip"
    backup.export_bundle(store, bundle)
    backup.verify_bundle(bundle)
    restored = Store(tmp_path / "restored")
    backup.restore_bundle(restored, bundle)
    assert command(restored, capsys, "activity", "1001")[1]["activity"] == summary
    assert restored.read_json(summary["files"]["streams"])["records"] == original_streams["records"]
    bad = copy.deepcopy(summary)
    bad["quality_flags"].append("cadence_unreviewed_guess")
    with pytest.raises(BridgeError):
        contract.project_activity(bad)


@pytest.mark.parametrize(
    "avg,maximum,records,expected",
    [
        (None, -1, (0,), set()),
        (None, -2, (-1,), set()),
        (None, 80, (81,), {RECORD}),
        (None, 80, (-1, 81, -2), {RECORD}),
        (None, 80, (81, -1), {RECORD}),
        (None, 80, (-1, 80), set()),
        (-1, -2, (-1,), set()),
        (80, -1, (0,), set()),
        (-1, 80, (80,), set()),
        (81, 80, (80,), {SUMMARY}),
        (0, 0, (-1, 0), set()),
    ],
)
def test_signed_fit_cadence_comparison_eligibility(avg, maximum, records, expected, monkeypatch):
    # Intentionally use FIT sint8 definitions: fitdecode retains these negatives.
    data = message(
        20,
        [(253, 0x86, "I"), (4, 1, "b")],
        [(START + i, value) for i, value in enumerate(records)],
    )
    fields = [(5, 0, "B"), (2, 0x86, "I"), (253, 0x86, "I"), (19, 1, "b")]
    values = [2, START, START + 60, maximum]
    if avg is not None:
        fields.append((18, 1, "b"))
        values.append(avg)
    data += message(18, fields, [values])
    header = struct.pack("<BBHI4s", 12, 0x20, 2200, len(data), b".FIT")
    fit = header + data + struct.pack("<H", crc16(header + data))
    args = (fit, normalize.local_metadata("1001"), "2025-01-03T00:00:00Z", "a" * 64)
    summary, streams = normalize.normalize(*args)
    assert set(summary["quality_flags"]) & {SUMMARY, RECORD} == expected
    assert summary["metrics"]["avg_cadence"]["value"] == avg
    assert summary["metrics"]["max_cadence"]["value"] == maximum
    assert [r["cadence"] for r in streams["records"]] == list(records)
    assert summary["coverage"]["cadence"] == {
        "present": len(records),
        "missing": 0,
        "total": len(records),
    }
    # Removing only semantic hints leaves every other value and flag identical.
    monkeypatch.setattr(normalize, "_cadence_quality", lambda *args: set())
    baseline_summary, baseline_streams = normalize.normalize(*args)
    summary["quality_flags"] = sorted(set(summary["quality_flags"]) - expected)
    assert summary == baseline_summary
    assert streams == baseline_streams
