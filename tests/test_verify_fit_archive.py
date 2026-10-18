"""Archive-only checks use invented data; no source, credentials or network."""

from hashlib import sha256
import json
import os
import secrets

import pytest

from garmin_bridge import cli, sanitize_fit
from garmin_bridge.contract import FIT_ARCHIVE_CHECK_NAMES, validate_output
from garmin_bridge.errors import BridgeError
from garmin_bridge.storage import Store, canonical

from .sanitize_synthetic import definition, finish, frame, minimal, raw
from .synthetic import archive
from .test_sanitize_fit import privacy, private_input


def snapshot(directory):
    return {
        p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
        for p in directory.iterdir()
    }


def create_archive(tmp_path, content=None):
    source = private_input(tmp_path, content)
    target = tmp_path / "clean"
    sanitize_fit.sanitize_file(source, target)
    return source, target


def rehash(target, *, fit=None, manifest=None, report=None):
    if fit is not None:
        (target / "derived.fit").write_bytes(fit)
    if report is not None:
        (target / "report.json").write_bytes(canonical(report))
    if manifest is None:
        manifest = json.loads((target / "manifest.json").read_bytes())
    manifest["output_sha256"] = sha256((target / "derived.fit").read_bytes()).hexdigest()
    manifest["report_sha256"] = sha256((target / "report.json").read_bytes()).hexdigest()
    (target / "manifest.json").write_bytes(canonical(manifest))


def archive_cli(target):
    return cli.main(["verify-fit", "--archive-only", "--directory", str(target)])


@pytest.mark.parametrize("zipped", [False, True])
def test_deleted_original_archive_only_is_offline_read_only_and_has_no_fidelity_claim(
    tmp_path, monkeypatch, capsys, zipped
):
    # Exercise invalid arrays, zero, definitions eligible but unused, discarded
    # messages, compressed timestamps and a surviving cross-message reference.
    records = frame(20, [raw(253, 0x86, 100031), raw(7, 0x84, 0)])
    records += definition(20, [raw(7, 0x84, 65535)], slot=1) + b"\xa0\xff\xff"
    records += frame(20, [raw(69, 2, [255, 1]), raw(70, 2, [255, 255])], endian="big")
    extra = frame(216, [raw(0, 0x84, 18), raw(1, 0x84, 0), raw(5, 0x86, [0, 0xFFFFFFFF])])
    extra += frame(60000, [raw(1, 2, 1)]) + definition(20, [raw(4, 2, 0)])
    data = minimal(extra, records=records)
    source = private_input(
        tmp_path, archive(data) if zipped else data, "original.zip" if zipped else "original.fit"
    )
    target = tmp_path / "clean"
    created = sanitize_fit.sanitize_file(source, target)
    assert created["summary"]["removed_messages"] == 1
    assert created["summary"]["compressed_timestamps_expanded"] == 1
    source.unlink()
    source.parent.rmdir()
    before = snapshot(target)
    read_bytes = Store.read_bytes
    reads, denied_calls = [], []

    def denied(*args, **kwargs):
        denied_calls.append(True)
        raise AssertionError("Archive verification may only read its three members")

    def bounded(store, name, limit=None):
        assert store.root == target
        assert limit == sanitize_fit.ARCHIVE_LIMITS[name]
        reads.append(name)
        return read_bytes(store, name, limit)

    monkeypatch.setattr(sanitize_fit, "read_input", denied)
    monkeypatch.setattr(sanitize_fit, "prepare", denied)
    monkeypatch.setattr(Store, "state", denied)
    monkeypatch.setattr(Store, "write_bytes", denied)
    monkeypatch.setattr(Store, "lock", denied)
    monkeypatch.setattr(Store, "ensure", denied)
    monkeypatch.setattr(Store, "read_bytes", bounded)
    monkeypatch.setattr(cli, "GarminSource", denied)
    monkeypatch.setattr(cli, "authenticate", denied)
    monkeypatch.setattr("garmin_bridge.jobs.load_job", denied)
    store = tmp_path / "absent-api-store"
    assert (
        cli.main(
            ["--data-dir", str(store), "verify-fit", "--archive-only", "--directory", str(target)]
        )
        == 0
    )
    capture = capsys.readouterr()
    result = json.loads(capture.out)
    assert capture.err == ""
    assert result["mode"] == "archive_only"
    assert result["source_comparison"] == {"status": "not_checked", "reason": "source_not_provided"}
    assert result["checks"] == dict.fromkeys(FIT_ARCHIVE_CHECK_NAMES, "passed")
    assert result["summary"] == {
        key: created["summary"][key] for key in ("output_messages", "retained_fields")
    }
    assert "physical_bytes_equal" not in capture.out
    assert "presence_invalid_arrays_precision_equal" not in capture.out
    assert reads == list(sanitize_fit.FILES) and not denied_calls
    assert snapshot(target) == before
    assert not store.exists()


@pytest.mark.parametrize("mode", ["missing_source", "both", "neither"])
def test_source_is_required_or_explicitly_disclaimed_never_implicitly_downgraded(
    tmp_path, monkeypatch, capsys, mode
):
    source, target = create_archive(tmp_path)
    source.unlink()

    def denied(*args, **kwargs):
        pytest.fail("Full verification must not fall back to archive-only", pytrace=False)

    monkeypatch.setattr(sanitize_fit, "verify_archive_only", denied)
    args = ["verify-fit", "--directory", str(target)]
    if mode != "neither":
        args += ["--source", str(source)]
    if mode == "both":
        args += ["--archive-only"]
    assert cli.main(args) == 2
    capture = capsys.readouterr()
    assert capture.out == ""
    assert json.loads(capture.err)["error"] == (
        "sanitize_verification" if mode == "missing_source" else "usage"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "missing",
        "directory",
        "symlink",
        "hardlink",
        "permissions",
        "fit_hash",
        "report_hash",
    ],
)
def test_member_set_private_files_and_hashes_are_checked(tmp_path, capsys, mutation):
    _, target = create_archive(tmp_path)
    fit = target / "derived.fit"
    if mutation == "extra":
        (target / "extra").touch(mode=0o600)
    elif mutation == "missing":
        fit.unlink()
    elif mutation == "directory":
        fit.unlink()
        fit.mkdir(mode=0o700)
    elif mutation in {"symlink", "hardlink"}:
        outside = tmp_path / "outside.fit"
        fit.rename(outside)
        if mutation == "symlink":
            fit.symlink_to(outside)
        else:
            os.link(outside, fit)
    elif mutation == "permissions":
        fit.chmod(0o644)
    elif mutation == "fit_hash":
        fit.write_bytes(minimal(records=frame(20, [raw(7, 0x84, 1)])))
    else:
        report = json.loads((target / "report.json").read_bytes())
        report["summary"]["removed_messages"] += 1
        (target / "report.json").write_bytes(canonical(report))
    assert archive_cli(target) == 2
    capture = capsys.readouterr()
    assert not capture.out
    assert json.loads(capture.err)["error"] == "sanitize_verification"


@pytest.mark.parametrize(
    "mutation",
    [
        "format_version",
        "rules_version",
        "profile_version",
        "sdk_version",
        "fitdecode_version",
        "report_version",
        "extra_key",
        "duplicate_key",
        "nonfinite",
        "noncanonical",
        "trailing_json",
        "boolean_count",
        "unknown_reason",
        "duplicate_inventory",
        "inventory_count",
        "report_count",
        "historical_flag",
        "report_flag",
        "oversized",
    ],
)
def test_strict_versioned_documents_and_inventory_even_with_rehashed_report(
    tmp_path, capsys, mutation
):
    _, target = create_archive(tmp_path)
    manifest = json.loads((target / "manifest.json").read_bytes())
    report = json.loads((target / "report.json").read_bytes())
    if mutation in {
        "format_version",
        "rules_version",
        "profile_version",
        "sdk_version",
        "fitdecode_version",
    }:
        manifest[mutation] = 2 if mutation == "format_version" else "unreviewed"
    elif mutation == "report_version":
        report["format_version"] = 2
    elif mutation == "extra_key":
        manifest["fields"][0]["unapproved"] = "not permitted"
    elif mutation == "boolean_count":
        manifest["summary"]["removed_messages"] = False
    elif mutation == "unknown_reason":
        manifest["fields"][0]["reason"] = "unreviewed_reason"
    elif mutation == "duplicate_inventory":
        manifest["fields"].append(manifest["fields"][0])
    elif mutation == "inventory_count":
        manifest["fields"][0]["occurrences"] += 1
    elif mutation == "report_count":
        report["summary"]["removed_messages"] += 1
    elif mutation == "historical_flag":
        manifest["validations"]["strict_crc"] = False
    elif mutation == "report_flag":
        report["validations"]["physical_bytes_equal"] = False
    rehash(target, manifest=manifest, report=report)
    if mutation in {"duplicate_key", "nonfinite", "noncanonical", "trailing_json", "oversized"}:
        report_path = target / "report.json"
        data = report_path.read_bytes()
        if mutation == "duplicate_key":
            data = b'{"status":"verified",' + data[1:]
        elif mutation == "nonfinite":
            data = data.replace(b'"removed_messages":0', b'"removed_messages":NaN')
        elif mutation == "noncanonical":
            data += b"\n"
        elif mutation == "trailing_json":
            data += b"{}"
        else:
            data += b" " * sanitize_fit.ARCHIVE_LIMITS["report.json"]
        report_path.write_bytes(data)
        rehash(target)
    before = snapshot(target)
    assert archive_cli(target) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "sanitize_verification"
    assert snapshot(target) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "record_location",
        "lap_location",
        "split_location",
        "uuid",
        "unknown_message",
        "unknown_field",
        "developer",
        "unused_private_definition",
        "unused_approved_definition",
        "wrong_base",
        "pair_shape",
        "zone_array",
        "event_payload",
        "dangling_reference",
        "duplicate_lap",
        "noncycling",
        "crc",
        "header_crc",
        "trailing",
        "chained",
        "truncated",
    ],
)
def test_physical_checks_reject_recomputed_hashes_and_crcs_without_value_disclosure(
    tmp_path, monkeypatch, capsys, caplog, mutation
):
    _, target = create_archive(tmp_path)
    # Neither markers nor coordinate values exist outside this synthetic process.
    coordinate = secrets.randbelow(1_000_000_000) + 100_000_000
    marker = secrets.token_hex(16).encode()
    fields = {
        "record_location": (20, [raw(0, 0x85, coordinate)]),
        "lap_location": (19, [raw(27, 0x85, coordinate)]),
        "split_location": (312, [raw(17, 0x85, coordinate)]),
        "uuid": (142, [raw(65, 7, marker + b"\0")]),
        "unknown_message": (60000, [raw(1, 7, marker + b"\0")]),
        "unknown_field": (20, [raw(240, 7, marker + b"\0")]),
        "wrong_base": (20, [raw(7, 0x86, 1)]),
        "pair_shape": (20, [raw(69, 2, [0, 1, 2])]),
        "zone_array": (216, [raw(0, 0x84, 18), raw(1, 0x84, 0), raw(5, 0x86, [0] * 33)]),
        "event_payload": (21, [raw(0, 0, 0), raw(1, 0, 0), raw(3, 0x86, 7)]),
        "dangling_reference": (216, [raw(0, 0x84, 19), raw(1, 0x84, 1)]),
        "duplicate_lap": (19, [raw(254, 0x84, 0)]),
    }
    data = (target / "derived.fit").read_bytes()
    if mutation in fields:
        data = finish(data[14:-2] + frame(*fields[mutation]))
    elif mutation == "developer":
        data = finish(data[14:-2] + frame(20, [raw(7, 0x84, 0)], developer=[(1, 0, marker)]))
    elif mutation.startswith("unused_"):
        field = (
            raw(0, 0x85, coordinate) if mutation == "unused_private_definition" else raw(7, 0x84, 0)
        )
        data = finish(data[14:-2] + definition(20, [field]))
    elif mutation == "noncycling":
        data = minimal(session=frame(18, [raw(5, 0, 1)]))
    elif mutation == "crc":
        data = data[:-1] + bytes((data[-1] ^ 1,))
    elif mutation == "header_crc":
        data = data[:12] + bytes((data[12] ^ 1,)) + data[13:]
    elif mutation == "trailing":
        data += marker
    elif mutation == "chained":
        data += data
    else:
        data = data[:-2]
    # Every structurally valid mutation has newly computed FIT/header CRCs.
    # All cases have matching output/report SHA-256, and historical pass flags.
    rehash(target, fit=data)
    original_validator = sanitize_fit.validate_archive_fit
    rejections = []

    def validate_fit(observed):
        try:
            return original_validator(observed)
        except BridgeError:
            rejections.append(True)
            raise

    monkeypatch.setattr(sanitize_fit, "validate_archive_fit", validate_fit)
    before = snapshot(target)
    assert archive_cli(target) == 2
    capture = capsys.readouterr()
    privacy(str(coordinate) not in capture.out + capture.err + caplog.text)
    privacy(marker.decode() not in capture.out + capture.err + caplog.text)
    privacy(snapshot(target) == before)
    assert not capture.out
    assert json.loads(capture.err)["error"] == "sanitize_verification"
    # Failure came from physical validation, before inventory consistency checks.
    assert rejections == [True]


@pytest.mark.parametrize("parser", ["fitdecode.FitReader", "Decoder.read"])
def test_both_parsers_are_reexecuted_despite_historical_pass_flags(
    tmp_path, monkeypatch, capsys, caplog, parser
):
    source, target = create_archive(tmp_path)
    source.unlink()
    marker = secrets.token_hex(16)
    calls = []

    def failed(*args, **kwargs):
        calls.append(True)
        raise ValueError(marker)

    monkeypatch.setattr("garmin_bridge.fit_verify." + parser, failed)
    assert archive_cli(target) == 2
    captured = capsys.readouterr()
    privacy(marker not in captured.out + captured.err + caplog.text)
    assert not captured.out
    assert json.loads(captured.err)["error"] == "sanitize_verification"
    assert calls == [True]


@pytest.mark.parametrize("power,heart_rate", [(1, 255), (0, 0)])
def test_consistent_approved_value_or_invalid_pattern_changes_cannot_prove_source_fidelity(
    tmp_path, power, heart_rate
):
    source, target = create_archive(tmp_path)
    full = sanitize_fit.verify_archive(source, target)
    assert full["mode"] == "with_source" and full["source_comparison"] == {"status": "passed"}
    changed = minimal(
        records=frame(20, [raw(253, 0x86, 100000), raw(7, 0x84, power), raw(3, 2, heart_rate)])
    )
    rehash(target, fit=changed)
    only = sanitize_fit.verify_archive_only(target)
    assert only["source_comparison"] == {"status": "not_checked", "reason": "source_not_provided"}
    # Matching unkeyed hashes and a valid policy are not source authenticity.
    with pytest.raises(BridgeError, match="Derived FIT archive failed"):
        sanitize_fit.verify_archive(source, target)


def test_receipt_contract_forbids_fidelity_claims_and_extra_values_in_archive_only(tmp_path):
    _, target = create_archive(tmp_path)
    result = sanitize_fit.verify_archive_only(target)
    result["source_comparison"] = {"status": "passed"}
    with pytest.raises(BridgeError):
        validate_output("verify-fit", result)
    result = sanitize_fit.verify_archive_only(target)
    result["checks"]["physical_bytes_equal"] = "passed"
    with pytest.raises(BridgeError):
        validate_output("verify-fit", result)
    result = sanitize_fit.verify_archive_only(target)
    result["summary"]["removed_fields"] = 0
    with pytest.raises(BridgeError):
        validate_output("verify-fit", result)
