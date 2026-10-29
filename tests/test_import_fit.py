"""All bytes and identifiers here are invented; no downloaded fixtures."""

import copy
import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest

from garmin_bridge import cli, jobs, normalize, sync, upstream
from garmin_bridge.errors import BridgeError
from garmin_bridge.import_fit import import_file
from garmin_bridge.sqlite_store import SQLiteStore as Store
from garmin_bridge.storage import canonical, empty_state
from .synthetic import corrupt_document, SECRET, Source, activity, archive, fit_bytes
from .test_cli_storage import command
from .test_history import CUTOFF, execute


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(sync, "now", lambda: CUTOFF)
    monkeypatch.setattr(jobs, "now", lambda: CUTOFF)


def private_file(tmp_path, content, name="synthetic.fit"):
    directory = tmp_path / "inputs"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / name
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def importing(store, capsys, path, source_id="1001"):
    return command(store, capsys, "import-fit", "--file", str(path), "--activity-id", source_id)


def api_snapshot(store):
    state = store.state()
    with store.database() as db:
        refs = [
            r[0]
            for r in db.execute(
                "SELECT name FROM documents WHERE name='sync-job.json' OR name LIKE 'reports/%' OR name LIKE 'jobs/%'"
            )
        ]
    return {
        "state": {k: v for k, v in state.items() if k != "activities"},
        "watermark": sync.watermark(state),
        "files": {ref: store.read_bytes(ref) for ref in refs},
    }


def assert_private_output(data):
    for forbidden in (
        SECRET,
        "position_lat",
        "position_long",
        "serial_number",
        "application_id",
        "314159265",
        "271828182",
        "12345678",
        "87654321",
        "61000",
        "auth/session",
        "raw/",
    ):
        assert forbidden not in data


@pytest.mark.parametrize("zipped", [False, True])
@pytest.mark.parametrize("saved_auth", [False, True])
def test_import_is_offline_without_touching_auth(
    store, tmp_path, capsys, monkeypatch, zipped, saved_auth
):
    fit = fit_bytes(developer=True, count=500)
    original = archive(fit, comment=SECRET.encode()) if zipped else fit
    path = private_file(tmp_path, original, "synthetic.zip" if zipped else "synthetic.fit")
    auth = store.root / "auth/session.json"
    if saved_auth:
        with store.lock(write=True):
            store.write_json("auth/session.json", {"synthetic_secret": SECRET})
        previous_auth = (auth.read_bytes(), auth.stat().st_mtime_ns)

    def denied(*args, **kwargs):
        raise AssertionError("Offline import touched authentication or acquisition")

    for module in (cli, upstream):
        for name in ("GarminSource", "authenticate", "auth_status"):
            monkeypatch.setattr(module, name, denied)
    monkeypatch.setattr(upstream, "saved_session", denied)
    monkeypatch.setattr(cli, "synchronize", denied)
    read, write = Store.read_bytes, Store.write_bytes

    def guarded_read(self, ref, *args, **kwargs):
        if ref.startswith("auth/"):
            denied()
        return read(self, ref, *args, **kwargs)

    def guarded_write(self, ref, *args, **kwargs):
        if ref.startswith("auth/"):
            denied()
        return write(self, ref, *args, **kwargs)

    monkeypatch.setattr(Store, "read_bytes", guarded_read)
    monkeypatch.setattr(Store, "write_bytes", guarded_write)
    code, receipt, captured = importing(store, capsys, path)
    assert code == 0 and receipt["status"] == "created"
    assert receipt["input"] == {
        "kind": "local_file",
        "activity_id_origin": "cli_argument",
        "garmin_api_verified": False,
    }
    assert receipt["api_state_changed"] is False and "complete" not in receipt
    assert len(captured.out) < 10_000 and "records" not in captured.out and not captured.err
    assert_private_output(captured.out)
    summary = receipt["activity"]
    assert summary["provenance"]["input"] == "local_fit_file"
    assert summary["provenance"]["garmin_api_verified"] is False
    assert normalize.LOCAL_FLAGS <= set(summary["quality_flags"])
    assert summary["activity_time"] == {
        "utc": "2025-01-02T00:00:00Z",
        "local": None,
        "timezone": None,
        "utc_offset_seconds": None,
    }
    assert summary["metrics"]["max_power"] == {"value": None, "unit": "W", "source": None}
    assert summary["metrics"]["avg_power"]["source"] == "fit_session"
    streams = store.read_json(summary["files"]["streams"])
    assert len(streams["records"]) == 500 and streams["records"][1]["power"] is None
    assert_private_output(json.dumps(streams))
    entry = store.state()["activities"]["1001"]
    assert store.read_bytes(entry["raw_ref"]) == original
    assert command(store, capsys, "list")[1]["activities"][0]["source_activity_id"] == "1001"
    assert command(store, capsys, "activity", "1001")[1]["activity"] == summary
    assert {**store.state(), "activities": {}} == empty_state()
    assert not (store.root / jobs.JOB_FILE).exists()
    if saved_auth:
        assert (auth.read_bytes(), auth.stat().st_mtime_ns) == previous_auth
    else:
        assert not (store.root / "auth").exists()
    for output in store.root.rglob("*"):
        assert output.stat().st_mode & 0o777 == (0o700 if output.is_dir() else 0o600)


def test_idempotence_zip_wrapping_content_revision_and_parser_version(
    store, tmp_path, capsys, monkeypatch
):
    spy = Mock(wraps=normalize.normalize)
    monkeypatch.setattr(normalize, "normalize", spy)
    fit = fit_bytes()
    path = private_file(tmp_path, fit)
    first = importing(store, capsys, path)[1]["activity"]
    original = store.read_bytes("index.json")
    for content, name in (
        (fit, "same.fit"),
        (archive(fit), "one.zip"),
        (archive(fit, name="other.fit", comment=SECRET.encode()), "two.zip"),
    ):
        result = importing(store, capsys, private_file(tmp_path, content, name))[1]
        assert result["status"] == "unchanged" and result["activity"] == first
        assert store.read_bytes("index.json") == original
    assert spy.call_count == 1
    changed = private_file(tmp_path, fit_bytes(power=220))
    result = importing(store, capsys, changed)[1]
    assert (
        result["status"] == "updated" and result["activity"]["metrics"]["avg_power"]["value"] == 220
    )
    assert len(store.state()["activities"]) == 1 and spy.call_count == 2
    assert result["activity"]["files"] != first["files"]
    assert all(store.read_bytes(ref) is not None for ref in first["files"].values())
    before_version = result["activity"]
    monkeypatch.setattr("garmin_bridge.contract.NORMALIZER_VERSION", "1.1.1")
    monkeypatch.setattr(sync, "NORMALIZER_VERSION", "1.1.1")
    monkeypatch.setattr(normalize, "NORMALIZER_VERSION", "1.1.1")
    result = importing(store, capsys, changed)[1]
    assert result["status"] == "updated" and spy.call_count == 3
    assert result["activity"]["source_digest"] == before_version["source_digest"]
    assert result["activity"]["files"] != before_version["files"]


@pytest.mark.parametrize("old_version", [False, True])
def test_api_metadata_is_never_overwritten_by_local_input(
    store, tmp_path, capsys, monkeypatch, old_version
):
    if old_version:
        with monkeypatch.context() as patch:
            patch.setattr(sync, "NORMALIZER_VERSION", "1.0.0")
            patch.setattr(normalize, "NORMALIZER_VERSION", "1.0.0")
            execute(store, Source())
    else:
        execute(store, Source())
    index = store.read_bytes("index.json")
    api = api_snapshot(store)
    spy = Mock(wraps=normalize.normalize)
    monkeypatch.setattr(normalize, "normalize", spy)
    receipt = importing(store, capsys, private_file(tmp_path, fit_bytes()))[1]
    assert receipt["status"] == "unchanged" and receipt["input"]["garmin_api_verified"] is False
    assert receipt["activity"]["provenance"]["input"] == "garmin_original_fit"
    assert receipt["activity"]["activity_time"]["timezone"] == "Asia/Shanghai"
    code, result, captured = importing(store, capsys, private_file(tmp_path, fit_bytes(power=210)))
    assert code == 2 and result["error"] == "import_conflict"
    assert not captured.out and not spy.call_count
    assert store.read_bytes("index.json") == index and api_snapshot(store) == api
    assert command(store, capsys, "activity", "1001")[0] == 0


def test_offline_import_cannot_replace_missing_api_scan_evidence(store, tmp_path, capsys):
    execute(store, Source([activity(1001), activity(1002)]))
    state = store.state()
    state["activities"].pop("1002")
    with store.lock(write=True):
        store.write_json("index.json", state)
    before = api_snapshot(store)
    index = store.read_bytes("index.json")
    result = importing(store, capsys, private_file(tmp_path, fit_bytes()), "1002")
    assert result[0] == 2 and result[1]["error"] == "import_conflict"
    assert api_snapshot(store) == before and store.read_bytes("index.json") == index
    assert command(store, capsys, "activity", "1001")[0] == 0


def test_damaged_api_artifact_rejects_offline_overwrite(store, tmp_path, capsys):
    execute(store, Source())
    entry = store.state()["activities"]["1001"]
    with store.lock(write=True):
        with store.database() as db:
            ref = entry["summary"]["files"]["streams"]
            metadata = json.loads(
                db.execute("SELECT payload FROM documents WHERE name=?", (ref,)).fetchone()[0]
            )
        metadata["source_digest"] = "0" * 64
        corrupt_document(store, ref, canonical(metadata))
    index = store.read_bytes("index.json")
    result = importing(store, capsys, private_file(tmp_path, fit_bytes()))
    assert result[0] == 2 and result[1]["error"] == "import_conflict"
    assert store.read_bytes("index.json") == index


@pytest.mark.parametrize("phase", ["pagination", "activity", "complete"])
def test_api_progress_and_existing_scan_files_are_byte_for_byte_unchanged(
    store, tmp_path, capsys, phase
):
    class InterruptedSource(Source):
        def page(self, start, end, offset, limit):
            if offset >= 2:
                raise KeyboardInterrupt()
            return super().page(start, end, offset, limit)

    if phase == "pagination":
        with pytest.raises(KeyboardInterrupt):
            execute(store, InterruptedSource([activity(1001), activity(1002), activity(1003)]))
        source_id = "1003"
    elif phase == "activity":
        result = execute(
            store, Source([activity(1001), activity(1002)], {"1002": BridgeError("network_failed")})
        )
        assert not result["complete"] and result["gap_count"] == 1
        source_id = "1002"
    else:
        assert execute(store, Source())["complete"]
        source_id = "1002"
    before = api_snapshot(store)
    old = copy.deepcopy(store.state()["activities"]["1001"])
    path = private_file(tmp_path, fit_bytes())
    assert importing(store, capsys, path, source_id)[1]["status"] == "created"
    assert api_snapshot(store) == before
    assert store.state()["activities"]["1001"] == old
    assert importing(store, capsys, path, source_id)[1]["status"] == "unchanged"
    assert api_snapshot(store) == before
    assert (
        importing(store, capsys, private_file(tmp_path, fit_bytes(power=210)), source_id)[1][
            "status"
        ]
        == "updated"
    )
    assert api_snapshot(store) == before
    if phase != "complete":
        assert not command(store, capsys, "status")[1]["sync"]["complete"]
    result = importing(store, capsys, private_file(tmp_path, b"invalid"), source_id)
    assert result[0] == 2 and result[1]["error"] == "invalid_fit"
    assert api_snapshot(store) == before


def test_missing_start_is_not_inferred_from_records_or_host(store, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("TZ", "Pacific/Honolulu")
    result = importing(store, capsys, private_file(tmp_path, fit_bytes(missing_start=True)))[1]
    assert result["activity"]["activity_time"] == dict.fromkeys(
        ("utc", "local", "timezone", "utc_offset_seconds")
    )
    assert "missing_start_time" in result["activity"]["quality_flags"]


@pytest.mark.parametrize("source_id", ["0", "01", "-1", "1.5", "9" * 21, "../1", SECRET, ""])
def test_explicit_id_validation_never_echoes_input(store, tmp_path, capsys, source_id):
    path = private_file(tmp_path, fit_bytes())
    code, result, captured = importing(store, capsys, path, source_id)
    assert code == 2 and result["error"] == "usage" and not captured.out
    assert SECRET not in captured.err and not store.root.exists()


def test_id_is_required_even_if_filename_looks_like_one(store, tmp_path, capsys):
    path = private_file(tmp_path, fit_bytes(), "1001.fit")
    assert command(store, capsys, "import-fit", "--file", str(path))[1]["error"] == "usage"
    assert not store.root.exists()


def bad_archive(kind):
    if kind == "multi_fit":
        return archive(extra={"second.fit": fit_bytes()})
    if kind == "traversal":
        return archive(name="../escape.fit")
    if kind == "bomb":
        return archive(b"0" * 200_000)
    if kind == "non_fit":
        return archive(name="notes.txt")
    if kind == "link":
        info = zipfile.ZipInfo("activity.fit")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as output:
            output.writestr(info, b"elsewhere")
        return buffer.getvalue()
    if kind == "zip_crc":
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as output:
            output.writestr("activity.fit", fit_bytes())
        data = bytearray(buffer.getvalue())
        data[30 + len("activity.fit") + 20] ^= 1
        return bytes(data)
    return {
        "crc": fit_bytes()[:-2] + b"\x00\x00",
        "truncated": fit_bytes()[:-1],
        "non_cycling": fit_bytes(sport=1),
        "unknown": SECRET.encode(),
    }[kind]


@pytest.mark.parametrize(
    "kind",
    [
        "crc",
        "truncated",
        "non_cycling",
        "unknown",
        "multi_fit",
        "traversal",
        "bomb",
        "non_fit",
        "link",
        "zip_crc",
    ],
)
def test_invalid_file_never_replaces_existing_results(store, tmp_path, capsys, kind):
    importing(store, capsys, private_file(tmp_path, fit_bytes()))
    index = store.read_bytes("index.json")
    before = command(store, capsys, "activity", "1001")[1]
    path = private_file(tmp_path, bad_archive(kind), SECRET + ".zip")
    code, result, captured = importing(store, capsys, path)
    assert code == 2 and result["error"] in {"invalid_fit", "unsafe_archive"}
    assert not captured.out and SECRET not in captured.err
    assert store.read_bytes("index.json") == index
    assert command(store, capsys, "activity", "1001")[1] == before
    assert not (tmp_path / "escape.fit").exists()


@pytest.mark.parametrize(
    "kind",
    [
        "symlink",
        "hardlink",
        "file_mode",
        "directory_mode",
        "parent",
        "missing",
        "auth",
        "git",
        "oversize",
    ],
)
def test_unsafe_file_paths_preserve_existing_store(store, tmp_path, capsys, monkeypatch, kind):
    importing(store, capsys, private_file(tmp_path, fit_bytes()))
    before = store.read_bytes("index.json")
    path = private_file(tmp_path, fit_bytes(power=210), "next.fit")
    if kind == "symlink":
        link = path.parent / "link.fit"
        link.symlink_to(path)
        path = link
    elif kind == "hardlink":
        os.link(path, path.parent / "link.fit")
    elif kind == "file_mode":
        path.chmod(0o644)
    elif kind == "directory_mode":
        path.parent.chmod(0o755)
    elif kind == "parent":
        path = path.parent / ".." / "inputs" / path.name
    elif kind == "missing":
        path.unlink()
    elif kind == "auth":
        with store.lock(write=True):
            store.write_bytes("auth/input.fit", fit_bytes())
        path = store.root / "auth/input.fit"
        read = Store.read_bytes

        def guard(self, ref, *args, **kwargs):
            assert self.root != store.root / "auth", "auth content was read"
            return read(self, ref, *args, **kwargs)

        monkeypatch.setattr(Store, "read_bytes", guard)
    elif kind == "git":
        # Construct a fake checkout; no binary ever enters the actual repository.
        (path.parent / ".git").write_text("gitdir: elsewhere")
    else:
        monkeypatch.setattr(normalize, "MAX_ARCHIVE", len(path.read_bytes()) - 1)
    code, result, captured = importing(store, capsys, path)
    assert code == 2 and result["error"] in {"unsafe_store", "unsafe_input", "storage_failed"}
    assert not captured.out and SECRET not in captured.err
    assert store.read_bytes("index.json") == before
    assert command(store, capsys, "activity", "1001")[0] == 0


@pytest.mark.parametrize("target", ["streams", "index", "interrupt"])
def test_failed_publication_preserves_index_and_old_artifacts(
    store, tmp_path, capsys, monkeypatch, target
):
    importing(store, capsys, private_file(tmp_path, fit_bytes()))
    before = store.read_bytes("index.json")
    query = command(store, capsys, "activity", "1001")[1]
    original = Store.write_bytes

    def fail(self, ref, content):
        if (target == "streams" and ref.endswith("/streams.json")) or (
            target in {"index", "interrupt"} and ref == "index.json"
        ):
            if target == "interrupt":
                raise KeyboardInterrupt()
            raise OSError(SECRET)
        return original(self, ref, content)

    with monkeypatch.context() as patch:
        patch.setattr(Store, "write_bytes", fail)
        code, result, captured = importing(
            store, capsys, private_file(tmp_path, fit_bytes(power=210))
        )
        assert code == (130 if target == "interrupt" else 2)
        assert not captured.out and SECRET not in captured.err
    assert store.read_bytes("index.json") == before
    assert command(store, capsys, "activity", "1001")[1] == query
    assert (
        importing(store, capsys, private_file(tmp_path, fit_bytes(power=210)))[1]["status"]
        == "updated"
    )


def test_concurrent_import_obeys_store_lock(store, tmp_path, capsys):
    path = private_file(tmp_path, fit_bytes())
    importing(store, capsys, path)
    before = store.read_bytes("index.json")
    with store.lock(write=True):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "garmin_bridge",
                "--data-dir",
                str(store.root),
                "import-fit",
                "--file",
                str(path),
                "--activity-id",
                "1001",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode == 2 and json.loads(result.stderr)["error"] == "busy"
    assert not result.stdout and store.read_bytes("index.json") == before


@pytest.mark.parametrize("pending_api", [False, True])
def test_backup_roundtrip_and_later_api_acquisition(
    store, tmp_path, capsys, monkeypatch, pending_api
):
    if pending_api:
        assert not execute(
            store, Source([activity(1001), activity(1002)], {"1002": BridgeError("network_failed")})
        )["complete"]
        source_id = "1002"
    else:
        source_id = "1001"
    path = private_file(tmp_path, fit_bytes())
    importing(store, capsys, path, source_id)
    before_api = api_snapshot(store)
    queries = {
        args: command(store, capsys, *args)[1] for args in (("list",), ("activity", source_id))
    }
    bundle = path.parent / "snapshot"
    code, receipt, _ = command(store, capsys, "backup", "export", "--file", str(bundle))
    assert code == 0 and receipt["verified"]
    assert {p.name for p in bundle.iterdir()} == {"activities.sqlite", "manifest.json"}
    with Store(bundle).database() as db:
        for table in ("documents", "samples"):
            for (payload,) in db.execute(f"SELECT payload FROM {table}"):
                assert_private_output(payload.decode())
    assert command(store, capsys, "backup", "verify", "--file", str(bundle))[0] == 0
    restored = Store(tmp_path / "restored")
    assert command(restored, capsys, "backup", "restore", "--file", str(bundle))[0] == 0
    for args, expected in queries.items():
        assert command(restored, capsys, *args)[1] == expected
    assert api_snapshot(restored)["state"] == before_api["state"]
    if pending_api:
        assert jobs.load_job(restored) == jobs.load_job(store)
    assert not (restored.root / "raw").exists() and not (restored.root / "auth").exists()
    spy = Mock(wraps=normalize.normalize)
    monkeypatch.setattr(normalize, "normalize", spy)
    assert importing(restored, capsys, path, source_id)[1]["status"] == "unchanged"
    assert not spy.call_count
    assert command(restored, capsys, "activity", source_id)[1] == queries[("activity", source_id)]
    source = Source([activity(1001), activity(1002)])
    result = execute(restored, source, mode="resume" if pending_api else "full")
    assert result["complete"]
    assert ("download", source_id) in source.calls
    assert (
        restored.state()["activities"][source_id]["summary"]["provenance"]["input"]
        == "garmin_original_fit"
    )
    source.activities.append(activity(1003))
    result = execute(restored, source, mode="incremental")
    assert result["complete"] and result["updated"] == 1
    assert set(restored.state()["activities"]) == {"1001", "1002", "1003"}
    assert (
        command(
            restored, capsys, "backup", "export", "--file", str(path.parent / "later-snapshot")
        )[0]
        == 0
    )


def test_api_with_missing_metadata_still_replaces_local_provenance(store, tmp_path, capsys):
    importing(store, capsys, private_file(tmp_path, fit_bytes()))
    meta = normalize.local_metadata("1001")
    with store.lock(write=True):
        assert sync.acquire(store, Source(), meta, store.state())
    assert (
        store.state()["activities"]["1001"]["summary"]["provenance"]["input"]
        == "garmin_original_fit"
    )


def test_input_allowlist_runs_before_reading_disallowed_field_values():
    class ForbiddenField:
        field_def = None
        units = None

        def __init__(self, name):
            self.name = name

        @property
        def value(self):
            pytest.fail("Unreviewed field value was accessed")

        @property
        def raw_value(self):
            pytest.fail("Unreviewed raw field value was accessed")

    class DeveloperField(ForbiddenField):
        field_def = SimpleNamespace(is_dev=True)

        @property
        def name(self):
            pytest.fail("Developer free-text name was accessed")

    for message in ("record", "session", "lap", "event"):
        fields = [
            ForbiddenField(name)
            for name in (
                "position_lat",
                "position_long",
                "serial_number",
                "unknown_999",
                "activity_name",
            )
        ]
        fields.append(DeveloperField.__new__(DeveloperField))
        # Even a standard field allowed in another message cannot pass here.
        fields.append(ForbiddenField("heart_rate" if message != "record" else "sport"))
        assert normalize._values(SimpleNamespace(name=message, fields=fields)) == {}

    class IgnoredMessage:
        @property
        def fields(self):
            pytest.fail("Fields of an unreviewed message were accessed")

    for name in ("file_id", "device_info", "field_description", "developer_data_id", "unknown"):
        frame = IgnoredMessage()
        frame.name = name
        assert normalize._values(frame) == {}


def test_synthetic_location_and_ids_never_enter_intermediate_projection(
    store, tmp_path, monkeypatch
):
    original = normalize._values
    projections = []

    def checked(frame):
        result = original(frame)
        assert set(result) <= normalize.MESSAGE_FIELDS.get(frame.name, frozenset())
        projections.append(result)
        return result

    monkeypatch.setattr(normalize, "_values", checked)
    with store.lock(write=True):
        import_file(store, private_file(tmp_path, fit_bytes(developer=True)), "1001")
    assert projections
    assert_private_output(json.dumps(projections, default=str))


@pytest.mark.parametrize("target", ["stream", "provenance", "exception", "receipt"])
def test_final_recursive_validation_and_safe_exception_boundary(
    store, tmp_path, capsys, monkeypatch, target
):
    path = private_file(tmp_path, fit_bytes())
    importing(store, capsys, path)
    before = store.read_bytes("index.json")
    original = normalize.normalize

    def injected(*args, **kwargs):
        if target == "exception":
            raise RuntimeError(SECRET)
        summary, streams = original(*args, **kwargs)
        if target == "stream":
            streams["records"][0]["position_lat"] = SECRET
        else:
            summary["provenance"]["account"] = SECRET
        return summary, streams

    if target == "receipt":
        monkeypatch.setattr(cli, "execute", lambda _: {"token": SECRET})
    else:
        monkeypatch.setattr(normalize, "normalize", injected)
    code, result, captured = importing(store, capsys, private_file(tmp_path, fit_bytes(power=220)))
    assert code == 2 and result["error"] == (
        "internal_error" if target == "exception" else "privacy_failed"
    )
    assert not captured.out and SECRET not in captured.err
    assert store.read_bytes("index.json") == before


def test_local_only_backup_cannot_claim_api_success_without_journal(store, tmp_path, capsys):
    path = private_file(tmp_path, fit_bytes())
    importing(store, capsys, path)
    state = store.state()
    state["last_success_at"] = CUTOFF
    with store.lock(write=True):
        store.write_bytes("index.json", canonical(state))
    result = command(store, capsys, "backup", "export", "--file", str(path.parent / "bad-snapshot"))
    assert result[0] == 2 and result[1]["error"] == "privacy_failed"
    assert not (path.parent / "bad-snapshot").exists()
