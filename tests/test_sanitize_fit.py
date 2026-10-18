"""Only synthetic input; negative payloads must never appear in diagnostics."""

import hashlib
import json
import multiprocessing
import os
import secrets
import struct

from garmin_fit_sdk.profile import Profile
import pytest

from garmin_bridge import cli, sanitize_fit
from garmin_bridge.contract import validate_output
from garmin_bridge.errors import BridgeError
from garmin_bridge.fit_policy import assert_approved, project
from garmin_bridge.fit_rules import RULES
from garmin_bridge.fit_verify import validate_derived
from garmin_bridge.fit_wire import read_wire, write_wire
from garmin_bridge.storage import Store

from .sanitize_synthetic import body, definition, finish, frame, minimal, raw
from .synthetic import archive


def private_input(tmp_path, content=None, name="original.fit"):
    folder = tmp_path / "inputs"
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / name
    path.write_bytes(content if content is not None else minimal())
    path.chmod(0o600)
    return path


def successful(content):
    selected, inventory = project(read_wire(content))
    output = write_wire(selected)
    validate_derived(output, selected)
    return read_wire(output), inventory


def fields_of(document, number):
    return [{f.number: f for f in m.fields} for m in document.messages if m.number == number]


def privacy(condition):
    # Do not let pytest's assertion rewriting print negative payloads on failure.
    if not condition:
        pytest.fail("Synthetic privacy boundary failed", pytrace=False)


@pytest.mark.parametrize("zipped", [False, True])
def test_offline_cli_no_store_auth_or_source(tmp_path, monkeypatch, capsys, zipped):
    data = minimal()
    source = private_input(
        tmp_path, archive(data) if zipped else data, "original.zip" if zipped else "original.fit"
    )
    original_bytes = source.read_bytes()
    target = tmp_path / "clean"
    store = tmp_path / "api"
    store.mkdir(mode=0o700)
    state = {
        "covered_ranges": [["2000-01-01", "2000-02-01"]],
        "pending_ranges": ["synthetic"],
        "history_initialized": True,
        "full_completed_at": None,
        "last_success_at": "synthetic",
        "last_attempt_at": "synthetic",
        "last_failure": "network_failed",
    }
    for name, value in (("index.json", state), ("sync-job.json", {"cursor": 17, "gaps": [1]})):
        (store / name).write_text(json.dumps(value))
        (store / name).chmod(0o600)
    before = {p.name: p.read_bytes() for p in store.iterdir()}

    def denied(*args, **kwargs):
        raise AssertionError("No activity/auth/API operation is permitted")

    monkeypatch.setattr(cli, "GarminSource", denied)
    monkeypatch.setattr(Store, "state", denied)
    monkeypatch.setattr("garmin_bridge.jobs.load_job", denied)
    monkeypatch.setattr(cli, "authenticate", denied)
    args = ["--data-dir", str(store)]
    assert (
        cli.main([*args, "sanitize-fit", "--file", str(source), "--output-dir", str(target)]) == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert cli.main([*args, "verify-fit", "--source", str(source), "--directory", str(target)]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert created["summary"] == verified["summary"]
    assert created["activity_store_changed"] is False
    assert before == {p.name: p.read_bytes() for p in store.iterdir()}
    assert not (store / "auth").exists()
    assert source.read_bytes() == original_bytes
    assert target.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in target.iterdir())


CORE = {
    7: "1-3,5,7",
    18: "2,5-11,13,16-24,34-37,45,48,57-58,92-94,101-105,112-125,137,139,150,168-170,180-181,183,187,192-193,253",
    19: "2,7-12,15-23,25,33-34,39-41,50-51,80-82,91-95,98-111,121,124,136-137,149,151,154,253",
    20: "3-5,7,13,29-30,43-46,53,73,78,108,253",
    21: "0-1,253",
    216: "0-2,5-6,9-15,253",
}


def candidates():
    for mid, ranges in CORE.items():
        for item in ranges.split(","):
            first, *last = map(int, item.split("-"))
            for number in range(first, (last[0] if last else first) + 1):
                yield mid, number


def test_official_fixed_profile_and_candidate_audit():
    assert len(list(candidates())) == 152
    assert Profile["version"] == {"major": 21, "minor": 218, "patch": 0, "type": "Release"}
    bases = {
        "enum": 0,
        "sint8": 1,
        "uint8": 2,
        "sint16": 0x83,
        "uint16": 0x84,
        "sint32": 0x85,
        "uint32": 0x86,
        "float32": 0x88,
    }
    for mid, rules in RULES.items():
        for number, rule in rules.items():
            official = Profile["messages"][mid]["fields"][number]
            assert (rule.name, rule.base) == (official["name"], bases[official["base_type"]])
            assert (rule.maximum > 1) == (official["array"] == "true")
    assert [(mid, n) for mid, n in candidates() if n not in RULES[mid]] == [(19, 40)]


@pytest.mark.parametrize("endian", ["little", "big"])
@pytest.mark.parametrize("mid,number", list(candidates()), ids=lambda n: str(n))
def test_every_candidate_encoding_and_disposition(mid, number, endian):
    # Encode from the official Profile facts, independently of Bridge's rule.
    field = Profile["messages"][mid]["fields"][number]
    bases = {
        "enum": 0,
        "sint8": 1,
        "uint8": 2,
        "uint16": 0x84,
        "sint32": 0x85,
        "uint32": 0x86,
        "float32": 0x88,
    }
    value = [0, 255] if field["array"] == "true" else 0
    context = {
        18: {5: (0, 2)},
        19: {25: (0, 2)},
        21: {0: (0, 0), 1: (0, 0)},
        216: {0: (0x84, 18), 1: (0x84, 0)},
    }
    attributes = context.get(mid, {}).copy()
    attributes[number] = (bases[field["base_type"]], value)
    if (mid, number) in ((18, 5), (19, 25)):
        attributes[number] = (0, 2)
    if (mid, number) == (216, 0):
        attributes[number] = (0x84, 18)
    if mid == 18:
        attributes[254] = (0x84, 0)
    encoded = [raw(n, b, v, endian) for n, (b, v) in attributes.items()]
    kwargs = {"session": frame(mid, encoded, endian)} if mid == 18 else {}
    if mid == 19:
        kwargs["lap"] = frame(mid, encoded, endian)
    source = minimal(b"" if mid in (18, 19) else frame(mid, encoded, endian), **kwargs)
    document, inventory = successful(source)
    matching = [
        f for m in document.messages if m.number == mid for f in m.fields if f.number == number
    ]
    if (mid, number) == (19, 40):
        assert not matching
        assert any(r["reason"] == "length_context" for r in inventory["fields"])
    else:
        expected = next(payload for n, _, payload in encoded if n == number)
        assert any(f.raw == expected for f in matching)


def test_presence_invalid_zero_array_order_bitfields_precision_and_redefinitions():
    records = frame(
        20,
        [
            raw(7, 0x84, 0),
            raw(53, 2, 127),
            raw(30, 2, 0xB0),
            raw(69, 2, [255, 1]),
            raw(70, 2, [255, 255]),
            raw(78, 0x86, 2537),
            raw(108, 0x84, 1833),
        ],
    )
    records += frame(20, [raw(7, 0x84, 65535)])  # Heart rate absent, not zero.
    records += frame(20, [raw(7, 0x84, 123), raw(3, 2, 255)])
    document, _ = successful(minimal(records=records))
    rows = fields_of(document, 20)
    assert len(rows) == 3 and 3 not in rows[1] and 3 in rows[2]
    assert rows[0][7].raw == b"\0\0" and rows[1][7].raw == b"\xff\xff"
    assert rows[0][69].raw == bytes([255, 1]) and rows[0][70].raw == bytes([255, 255])
    assert rows[0][30].raw == bytes([0xB0])
    assert 73 not in rows[0]  # No component-derived fields invented.


@pytest.mark.parametrize(
    "event,payload,kept",
    [
        (0, 0, True),
        (0, 2, True),
        (0, 3, False),
        (44, 3, True),
        (44, 4, False),
        (42, 0x01020304, True),
        (43, 0xFFFFFFFF, True),
        (10, 0, False),
        (13, 10, False),
    ],
)
def test_event_data_is_context_specific(event, payload, kept):
    document, inventory = successful(
        minimal(frame(21, [raw(0, 0, event), raw(1, 0, 0), raw(3, 0x86, payload)]))
    )
    assert any(3 in row for row in fields_of(document, 21)) is kept
    if not kept:
        assert any(r["reason"] in {"event_context", "event_payload"} for r in inventory["fields"])


@pytest.mark.parametrize(
    "target,index,survives",
    [(18, 0, True), (19, 0, True), (19, 1, False), (142, 0, False), (18, 65535, False)],
)
def test_zone_references_and_historical_missing_configuration(target, index, survives):
    zone = frame(
        216,
        [
            raw(0, 0x84, target),
            raw(1, 0x84, index),
            raw(5, 0x86, [0, 0xFFFFFFFF]),
            raw(9, 0x84, [100, 65535]),
        ],
    )
    document, inventory = successful(minimal(zone))
    rows = fields_of(document, 216)
    assert bool(rows) is survives
    if survives:
        assert 15 not in rows[0]  # No current FTP or another message's config is substituted.
    else:
        assert any(r["reason"] == "dependency_removed" for r in inventory["fields"])


def test_duplicate_lap_indices_remove_references_not_training():
    lap = frame(19, [raw(254, 0x84, 0), raw(19, 0x84, 200)]) * 2
    zone = frame(216, [raw(0, 0x84, 19), raw(1, 0x84, 0), raw(5, 0x86, [1000])])
    document, _ = successful(minimal(zone, lap=lap))
    assert len(fields_of(document, 19)) == 2
    assert all(254 not in row for row in fields_of(document, 19))
    assert not fields_of(document, 216)
    assert 25 not in fields_of(document, 18)[0] and 26 not in fields_of(document, 18)[0]


@pytest.mark.parametrize("first", [None, 0, 65535])
def test_zero_lap_count_kept_without_inventing_first_lap(first):
    fields = [raw(5, 0, 2), raw(26, 0x84, 0)]
    if first is not None:
        fields.append(raw(25, 0x84, first))
    document, _ = successful(minimal(session=frame(18, fields), lap=b""))
    session = fields_of(document, 18)[0]
    assert session[26].raw == b"\0\0"
    assert (25 in session) is (first == 65535)


def test_compressed_timestamp_rollover_becomes_exact_explicit_timestamp():
    anchor = frame(20, [raw(253, 0x86, 100031), raw(7, 0x84, 0)], slot=1)
    fields = [raw(7, 0x84, 100)]
    compressed = definition(20, fields, slot=2) + bytes((0xC0,)) + fields[0][2]
    compressed += bytes((0xC5,)) + fields[0][2]
    document, report = successful(minimal(records=anchor + compressed))
    timestamps = [int.from_bytes(row[253].raw, "little") for row in fields_of(document, 20)]
    assert timestamps == [100031, 100032, 100037]
    assert report["compressed_timestamps_expanded"] == 2
    assert all(not m.compressed for m in document.messages)


def test_deleted_compressed_message_keeps_protocol_clock_but_not_payload():
    start = frame(20, [raw(253, 0x86, 100031), raw(7, 0x84, 0)])
    deleted = definition(60000, [raw(7, 0x84, 0)]) + b"\x80\0\0"
    following = definition(20, [raw(7, 0x84, 0)]) + b"\x85\0\0"
    document, report = successful(minimal(records=start + deleted + following))
    timestamps = [int.from_bytes(row[253].raw, "little") for row in fields_of(document, 20)]
    assert timestamps == [100031, 100037]
    assert report["compressed_timestamps_expanded"] == 1
    assert not fields_of(document, 60000)


@pytest.mark.parametrize("unknown_message", [True, False])
def test_unapproved_timestamp_cannot_seed_compressed_output(unknown_message, tmp_path, capsys):
    marker = secrets.randbelow(1_000_000_000) + 100_000_000
    start = frame(20, [raw(253, 0x86, 100000), raw(7, 0x84, 0)])
    if unknown_message:
        unapproved = frame(60000, [raw(253, 0x86, marker)])
    else:
        unapproved = frame(20, [raw(253, 0x85, marker)])
    compressed = definition(20, [raw(7, 0x84, 0)]) + b"\x80\0\0"
    source = private_input(tmp_path, minimal(records=start + unapproved + compressed))
    target = tmp_path / "clean"
    assert cli.main(["sanitize-fit", "--file", str(source), "--output-dir", str(target)]) == 2
    capture = capsys.readouterr()
    privacy(str(marker) not in capture.out + capture.err)
    assert not target.exists()


def test_sensitive_fields_all_frames_developer_unknown_uuid_text_no_diagnostics(
    tmp_path, capsys, caplog
):
    # Generated only inside the test process; never committed as raw fixtures.
    coordinate = secrets.randbelow(1_000_000_000) + 100_000_000
    marker = secrets.token_hex(16).encode()
    extra = b""
    for mid, numbers in ((19, range(27, 31)), (312, range(17, 21)), (18, (3, 4, 29, 30, 31, 32))):
        if mid == 18:
            continue  # The single session below contains these coordinates.
        extra += frame(mid, [raw(n, 0x85, coordinate) for n in numbers])
    extra += frame(142, [raw(65, 7, marker + b"\0")])
    extra += frame(60000, [raw(3, 7, marker + b"\0")])
    extra += definition(20, [raw(0, 0x85, coordinate)])  # Unused private definition.
    records = frame(
        20,
        [raw(7, 0x84, 0), raw(0, 0x85, coordinate), raw(1, 0x85, -coordinate), raw(250, 7, marker)],
        developer=[(0, 0, marker)],
    )
    extra += frame(23, [raw(3, 0x8C, coordinate)])
    session = frame(
        18,
        [
            raw(5, 0, 2),
            *[raw(n, 0x85, coordinate) for n in (3, 4, 29, 30, 31, 32)],
            raw(110, 7, marker + b"\0"),
        ],
    )
    content = minimal(extra, session=session, records=records)
    source = private_input(tmp_path, content)
    target = tmp_path / "clean"
    assert cli.main(["sanitize-fit", "--file", str(source), "--output-dir", str(target)]) == 0
    captured = capsys.readouterr()
    outputs = (
        b"".join(p.read_bytes() for p in target.iterdir())
        + captured.out.encode()
        + captured.err.encode()
        + caplog.text.encode()
    )
    privacy(marker not in outputs and str(coordinate).encode() not in outputs)
    privacy(struct.pack("<i", coordinate) not in (target / "derived.fit").read_bytes())
    privacy(str(source).encode() not in outputs and source.name.encode() not in outputs)
    document = read_wire((target / "derived.fit").read_bytes())
    assert_approved(document)
    privacy(all(f.developer is None for m in document.definitions for f in m.fields))
    privacy(all(m.number not in (23, 142, 312, 60000) for m in document.messages))


@pytest.mark.parametrize(
    "case",
    [
        "crc",
        "header_crc",
        "trailing",
        "multi_zip",
        "unsafe_zip",
        "noncycling",
        "no_anchor",
        "ambiguous_timestamp",
        "duplicate_field",
        "truncated",
    ],
)
def test_invalid_inputs_preserve_source_and_existing_output(tmp_path, case):
    good = minimal()
    data = good
    name = "original.fit"
    if case == "crc":
        data = good[:-1] + bytes((good[-1] ^ 1,))
    elif case == "header_crc":
        header = bytearray(good)
        header[12] ^= 1
        data = bytes(header)
    elif case == "trailing":
        data += b"unexpected"
    elif case == "multi_zip":
        data, name = archive(good, extra={"second.fit": good}), "original.zip"
    elif case == "unsafe_zip":
        data, name = archive(good, name="../escape.fit"), "original.zip"
    elif case == "noncycling":
        data = minimal(session=frame(18, [raw(5, 0, 1)]))
    elif case == "no_anchor":
        data = minimal(records=definition(20, [raw(7, 0x84, 0)]) + b"\x80\0\0")
    elif case == "ambiguous_timestamp":
        fields = [raw(253, 0x86, 100000), raw(7, 0x84, 0)]
        data = minimal(records=frame(20, fields) + b"\x80" + b"".join(f[2] for f in fields))
    elif case == "duplicate_field":
        data = minimal(records=frame(20, [raw(7, 0x84, 0), raw(7, 0x84, 1)]))
    elif case == "truncated":
        data = good[:-3]
    source = private_input(tmp_path, data, name)
    before = hashlib.sha256(source.read_bytes()).digest()
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o700)
    (existing / "sentinel").write_bytes(b"previous result")
    with pytest.raises(BridgeError):
        sanitize_fit.sanitize_file(source, tmp_path / "new")
    assert not (tmp_path / "new").exists()
    with pytest.raises(BridgeError):
        sanitize_fit.sanitize_file(source, existing)
    assert (existing / "sentinel").read_bytes() == b"previous result"
    assert hashlib.sha256(source.read_bytes()).digest() == before


@pytest.mark.parametrize(
    "mutation", ["extra_file", "fit_byte", "manifest", "report", "valid_crc_private_field"]
)
def test_independent_verify_rejects_modified_archives(tmp_path, mutation):
    source = private_input(tmp_path)
    target = tmp_path / "clean"
    sanitize_fit.sanitize_file(source, target)
    if mutation == "extra_file":
        (target / "extra").write_bytes(b"unexpected")
    elif mutation == "fit_byte":
        (target / "derived.fit").write_bytes(minimal() + b"tail")
    elif mutation == "manifest":
        manifest = json.loads((target / "manifest.json").read_bytes())
        manifest["validations"]["strict_crc"] = False
        (target / "manifest.json").write_text(json.dumps(manifest))
    elif mutation == "report":
        (target / "report.json").write_bytes(b"{}")
    else:
        secret = secrets.randbelow(1_000_000_000)
        modified = finish(body() + definition(20, [raw(0, 0x85, secret)]))
        (target / "derived.fit").write_bytes(modified)
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    with pytest.raises(BridgeError, match="Derived FIT archive failed"):
        sanitize_fit.verify_archive(source, target)
    privacy(before == {p.name: p.read_bytes() for p in target.iterdir()})


def test_final_recursive_json_contracts_reject_unapproved_nested_values():
    files, summary = sanitize_fit.prepare(minimal())
    manifest = json.loads(files["manifest.json"])
    report = json.loads(files["report.json"])
    manifest["fields"][0]["extra"] = "not permitted"
    with pytest.raises(BridgeError):
        sanitize_fit.validate_documents(manifest, report)
    receipt = sanitize_fit.receipt("sanitize", summary)
    receipt["summary"]["secret"] = "not permitted"
    with pytest.raises(BridgeError):
        validate_output("sanitize-fit", receipt)


def test_source_alias_symlink_hardlink_and_store_destination_are_rejected(tmp_path):
    source = private_input(tmp_path)
    with pytest.raises(BridgeError):
        sanitize_fit.sanitize_file(source, source)
    with pytest.raises(BridgeError):
        sanitize_fit.sanitize_file(source, source.parent)
    link = source.parent / "link.fit"
    link.symlink_to(source)
    with pytest.raises(BridgeError):
        sanitize_fit.sanitize_file(link, tmp_path / "clean")
    link.unlink()
    os.link(source, link)
    with pytest.raises(BridgeError):
        sanitize_fit.sanitize_file(link, tmp_path / "clean")
    link.unlink()
    with pytest.raises(BridgeError):
        sanitize_fit.sanitize_file(source, tmp_path / "api" / "clean", tmp_path / "api")
    assert not (tmp_path / "api").exists()


def _publish_worker(source, target, queue):
    try:
        sanitize_fit.sanitize_file(source, target)
        queue.put("published")
    except BridgeError as exc:
        queue.put(exc.code)


def test_concurrent_publication_never_replaces_even_empty_destination(tmp_path):
    source = private_input(tmp_path)
    target = tmp_path / "clean"
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    processes = [
        context.Process(target=_publish_worker, args=(source, target, queue)) for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert sorted(queue.get(timeout=1) for _ in processes) == ["destination_exists", "published"]
    sanitize_fit.verify_archive(source, target)
    # Exercise the kernel no-replace guard, not only the earlier existence check.
    empty = tmp_path / "empty"
    empty.mkdir(mode=0o700)
    with pytest.raises(BridgeError, match="Destination is not empty"):
        sanitize_fit.publish_directory(target, empty)
    assert target.is_dir() and empty.is_dir()


def _crash_worker(source, target):
    sanitize_fit.publish_directory = lambda *_: os._exit(29)
    sanitize_fit.sanitize_file(source, target)


def test_hard_exit_leaves_only_unverifiable_staging_directory(tmp_path):
    source = private_input(tmp_path)
    target = tmp_path / "clean"
    before = source.read_bytes()
    process = multiprocessing.get_context("fork").Process(
        target=_crash_worker, args=(source, target)
    )
    process.start()
    process.join(10)
    assert process.exitcode == 29 and not target.exists()
    pending = list(tmp_path.glob(".fit-pending-*"))
    assert len(pending) == 1
    with pytest.raises(BridgeError):
        sanitize_fit.verify_archive(source, pending[0])
    assert source.read_bytes() == before
    sanitize_fit.sanitize_file(source, target)
    sanitize_fit.verify_archive(source, target)


def test_interrupted_write_cleans_temporary_output(tmp_path, monkeypatch):
    source = private_input(tmp_path)
    target = tmp_path / "clean"
    original = Store.write_bytes

    def interrupted(store, name, data):
        original(store, name, data)
        raise KeyboardInterrupt

    monkeypatch.setattr(Store, "write_bytes", interrupted)
    with pytest.raises(KeyboardInterrupt):
        sanitize_fit.sanitize_file(source, target)
    assert not target.exists() and not list(tmp_path.glob(".fit-pending-*"))


@pytest.mark.parametrize("endian", ["little", "big"])
def test_all_invalid_and_partial_arrays_keep_physical_presence(endian):
    session = frame(
        18,
        [
            raw(5, 0, 2),
            raw(254, 0x84, 0, endian),
            raw(168, 0x85, 0x7FFFFFFF, endian),
            raw(181, 0x88, b"\xff" * 4),
            raw(187, 0x88, -0.0, endian),
            raw(57, 1, 127),
            raw(120, 0x84, [65535, 0], endian),
        ],
        endian,
    )
    zone = frame(
        216,
        [
            raw(0, 0x84, 18, endian),
            raw(1, 0x84, 0, endian),
            raw(2, 0x86, [0xFFFFFFFF, 0xFFFFFFFF], endian),
            raw(5, 0x86, [0xFFFFFFFF, 0, 12345], endian),
            raw(6, 2, [255, 0, 150], endian),
        ],
        endian,
    )
    document, _ = successful(minimal(zone, session=session))
    result = fields_of(document, 216)[0]
    assert len(result[2].raw) == 8 and result[2].raw == b"\xff" * 8
    assert result[5].raw == raw(5, 0x86, [0xFFFFFFFF, 0, 12345], endian)[2]
    summary = fields_of(document, 18)[0]
    assert summary[181].raw == b"\xff" * 4
    assert summary[187].raw == raw(187, 0x88, -0.0, endian)[2]


@pytest.mark.parametrize(
    "field",
    [raw(7, 0x86, 100), raw(7, 0x84, [100, 200]), raw(69, 2, [1, 2, 3]), raw(3, 2, b"\0\1")],
)
def test_wrong_types_and_array_shapes_are_reported_not_coerced(field):
    document, report = successful(minimal(frame(20, [raw(253, 0x86, 100001), field])))
    assert field[0] not in fields_of(document, 20)[-1]
    assert any(
        r["reason"] in ("wire_type", "array_shape") and r["occurrences"] for r in report["fields"]
    )


def test_zone_configuration_is_file_historical_and_ambiguous_sets_are_dropped():
    config = frame(7, [raw(3, 0x84, 250), raw(7, 0, 1)])
    zones = frame(9, [raw(254, 0x84, 0), raw(1, 0x84, 150)])
    document, _ = successful(minimal(config + zones))
    assert fields_of(document, 7)[0][3].raw == raw(3, 0x84, 250)[2]
    assert fields_of(document, 9)
    document, report = successful(minimal(config + zones + config))
    assert not fields_of(document, 7) and not fields_of(document, 9)
    assert any(r["reason"] == "ambiguous_context" for r in report["fields"])


def test_new_profile_recognition_does_not_authorize_new_field(monkeypatch):
    field = Profile["messages"][20]["fields"][7].copy()
    field.update(num=240, name="future_training_field")
    monkeypatch.setitem(Profile["messages"][20]["fields"], 240, field)
    document, report = successful(minimal(frame(20, [raw(7, 0x84, 1), raw(240, 0x84, 2)])))
    assert all(240 not in row for row in fields_of(document, 20))
    assert any(r["field"] == 240 and r["reason"] == "unreviewed_field" for r in report["fields"])


def test_upstream_exception_and_warning_payloads_never_escape(
    tmp_path, monkeypatch, capsys, caplog
):
    marker = secrets.token_hex(16)
    source = private_input(tmp_path)

    def failed(*args, **kwargs):
        raise ValueError(marker)

    monkeypatch.setattr("garmin_bridge.fit_verify.Decoder.read", failed)
    assert (
        cli.main(["sanitize-fit", "--file", str(source), "--output-dir", str(tmp_path / "clean")])
        == 2
    )
    captured = capsys.readouterr()
    privacy(marker not in captured.out + captured.err + caplog.text)
    assert json.loads(captured.err)["error"] == "sanitize_verification"
    assert not (tmp_path / "clean").exists()


def test_valid_crc_extra_definition_rejected_by_physical_validator():
    content = minimal()
    selected, _ = project(read_wire(content))
    # A known and a forbidden unused definition both fail the exact transcript.
    for field in (raw(7, 0x84, 0), raw(0, 0x85, secrets.randbelow(1_000_000_000))):
        output = write_wire(selected)
        modified = finish(output[14:-2] + definition(20, [field]))
        with pytest.raises(BridgeError):
            validate_derived(modified, selected)


@pytest.mark.parametrize("phase", ["pagination", "activity", "complete"])
def test_actual_mocked_api_job_gap_and_auth_bytes_are_untouched(store, tmp_path, phase):
    from .synthetic import Source, activity
    from .test_history import execute

    class Interrupted(Source):
        def page(self, start, end, offset, limit):
            if offset >= 2:
                raise KeyboardInterrupt
            return super().page(start, end, offset, limit)

    if phase == "pagination":
        with pytest.raises(KeyboardInterrupt):
            execute(store, Interrupted([activity(1001), activity(1002), activity(1003)]))
    elif phase == "activity":
        result = execute(
            store, Source([activity(1001), activity(1002)], {"1002": BridgeError("network_failed")})
        )
        assert result["gap_count"] == 1 and not result["complete"]
    else:
        assert execute(store, Source())["complete"]
    store.write_bytes("auth/synthetic-marker", b"synthetic-not-a-credential")

    def snapshot():
        return {
            str(p.relative_to(store.root)): (
                hashlib.sha256(p.read_bytes()).digest(),
                p.stat().st_mtime_ns,
                p.stat().st_mode,
            )
            for p in store.root.rglob("*")
            if p.is_file()
        }

    before = snapshot()
    source = private_input(tmp_path)
    target = tmp_path / "clean"
    sanitize_fit.sanitize_file(source, target, store.root)
    sanitize_fit.verify_archive(source, target, store.root)
    sanitize_fit.verify_archive_only(target, store.root)
    privacy(snapshot() == before)


def test_verifier_reads_one_source_snapshot_and_bounds_archive_reads(tmp_path, monkeypatch):
    source = private_input(tmp_path)
    target = tmp_path / "clean"
    sanitize_fit.sanitize_file(source, target)
    original = sanitize_fit.read_input
    calls = []

    def source_snapshot(*args):
        calls.append(True)
        if len(calls) != 1:
            raise AssertionError("Verification must use a single source snapshot")
        return original(*args)

    reader = Store.read_bytes

    def bounded(store, name, limit=None):
        if store.root == target:
            assert limit is not None and limit <= (target / name).stat().st_size
        return reader(store, name, limit)

    monkeypatch.setattr(sanitize_fit, "read_input", source_snapshot)
    monkeypatch.setattr(Store, "read_bytes", bounded)
    sanitize_fit.verify_archive(source, target)
    assert len(calls) == 1
