import copy
import json
import struct
import zipfile
import io

import pytest

from garmin_bridge.contract import project_activity, validate_streams
from garmin_bridge.errors import BridgeError
from garmin_bridge.normalize import extract_fit, input_digest, metadata, normalize, sha256
from .synthetic import SECRET, activity, archive, fit_bytes


def parsed(**options):
    fit = fit_bytes(**options)
    meta = metadata(activity())
    summary, streams = normalize(fit, meta, "2025-01-03T00:00:00Z", input_digest(meta, sha256(fit)))
    summary["files"] = {k: f"objects/1001/{'a' * 64}/{k}.json" for k in ("streams", "summary")}
    return summary, streams


def test_real_binary_parser_contract_missing_units_and_provenance():
    summary, streams = parsed(missing_time=True)
    project_activity(summary)
    validate_streams(streams)
    assert len(streams["records"]) == 3
    assert streams["records"][1]["power"] is None
    assert streams["records"][1]["heart_rate"] is None
    assert streams["records"][1]["timestamp"] is None
    assert streams["records"][2]["timestamp"] == "2025-01-02T00:00:04Z"
    assert streams["records"][0]["cadence"] == 80.5
    assert streams["records"][0]["respiration"] == 18.25
    assert streams["records"][0]["balance_side"] == "right"
    assert streams["units"]["speed"] == "m/s"
    assert summary["coverage"]["power"] == {"present": 2, "missing": 1, "total": 3}
    assert summary["metrics"]["max_heart_rate"]["value"] is None
    assert summary["metrics"]["avg_power"] == {"value": 200, "unit": "W", "source": "fit_session"}
    assert summary["garmin_reported"]["training_stress_score"]["value"] == 12.3
    assert summary["garmin_reported"]["threshold_power"]["value"] == 250
    assert summary["activity_time"]["utc_offset_seconds"] == 28800
    assert summary["lap_count"] == 1 and summary["timer_event_count"] == 2
    assert "missing_timestamps" in summary["quality_flags"]


def test_nested_privacy_and_spoofed_developer_fields():
    summary, streams = parsed(developer=True)
    assert streams["records"][0]["power"] == 200  # not the spoofed developer value
    encoded = json.dumps([summary, streams, metadata(activity())])
    for forbidden in (
        SECRET,
        "Latitude",
        "Longitude",
        "position_lat",
        "position_long",
        "serial_number",
        "314159265",
        "271828182",
        "61000",
        "activityName",
        "owner",
        "application_id",
    ):
        assert forbidden not in encoded


def test_final_privacy_validator_rejects_nested_and_value_injections():
    summary, streams = parsed()
    for mutate in (
        lambda s: s["metrics"]["avg_power"].update(token=SECRET),
        lambda s: s["metrics"]["avg_power"].update(value=SECRET),
        lambda s: s["files"].update(streams="../../auth/session.json"),
        lambda s: s.update(source_activity_id=SECRET),
        lambda s: s["quality_flags"].append(SECRET),
        lambda s: s["activity_time"].update(timezone=SECRET),
        lambda s: s["metrics"]["avg_power"].update(value=float("nan")),
        lambda s: s["metrics"]["avg_power"].update(value=True),
    ):
        bad = copy.deepcopy(summary)
        mutate(bad)
        with pytest.raises(BridgeError, match="privacy"):
            project_activity(bad)
    streams["records"][0]["position_lat"] = 123
    with pytest.raises(BridgeError, match="privacy"):
        validate_streams(streams)


def test_empty_samples_and_missing_metadata_are_explicit():
    meta = metadata(
        activity(startTimeGMT=None, startTimeLocal=None, timeZoneUnitDTO=None, duration=None)
    )
    summary, streams = normalize(fit_bytes(count=0), meta, "2025-01-03T00:00:00Z", "a" * 64)
    assert summary["activity_time"]["timezone"] is None
    assert "missing_samples" in summary["quality_flags"]
    assert summary["coverage"]["power"] == {"present": 0, "missing": 0, "total": 0}
    assert streams["records"] == []


def test_real_zero_is_preserved_without_inventing_missing_time():
    summary, streams = parsed(power=0, missing=False, balance=128)
    assert summary["metrics"]["avg_power"]["value"] == 0
    assert streams["records"][0]["balance_percent"] == 0
    assert streams["records"][0]["balance_side"] == "right"
    meta = metadata(activity(startTimeGMT="2025-01-02", avgPower=10**400))
    assert meta["activity_time"]["utc"] is None and meta["metrics"]["avg_power"] is None


@pytest.mark.parametrize(
    "name", ["../escape.fit", "/absolute.fit", "a/../../escape.fit", "a\\escape.fit", "C:/x.fit"]
)
def test_zip_paths_rejected(name):
    with pytest.raises(BridgeError, match="archive"):
        extract_fit(archive(name=name))


def test_zip_ambiguity_links_bombs_and_nonfit_rejected():
    symlink = zipfile.ZipInfo("bad.fit")
    symlink.create_system = 3
    symlink.external_attr = 0o120777 << 16
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        output.writestr(symlink, b"/private/elsewhere")
    invalid = [
        buffer.getvalue(),
        archive(extra={"second.fit": fit_bytes()}),
        archive(name="file.txt"),
        archive(content=b"0" * 200_000),
        b"PKbroken",
    ]
    for value in invalid:
        with pytest.raises(BridgeError):
            extract_fit(value)


def test_crc_truncation_and_concatenation_rejected():
    good = fit_bytes()
    assert extract_fit(archive(good)) == good
    assert extract_fit(good) == good
    for bad in (good[:-1], good + good, good[:4] + struct.pack("<I", 2**31) + good[8:]):
        with pytest.raises(BridgeError):
            extract_fit(bad)
    bad_crc = good[:-2] + b"\x00\x00"
    with pytest.raises(BridgeError, match="FIT"):
        normalize(bad_crc, metadata(activity()), "2025-01-03T00:00:00Z", "a" * 64)
