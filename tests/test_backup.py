import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from unittest.mock import Mock
import zipfile

import pytest

from garmin_bridge import backup, cli, jobs, normalize, sync
from garmin_bridge.errors import BridgeError
from garmin_bridge.storage import Store, canonical
from .synthetic import SECRET, Source, activity
from .test_cli_storage import command
from .test_history import CUTOFF, execute


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(jobs, "now", lambda: CUTOFF)
    monkeypatch.setattr(sync, "now", lambda: CUTOFF)


@pytest.fixture
def bundle(tmp_path):
    directory = tmp_path / "transfer"
    directory.mkdir(mode=0o700)
    return directory / "snapshot.zip"


def rewrite(bundle, mutate):
    """Synthetic adversary can recompute all checksums; schema checks still apply."""
    with zipfile.ZipFile(bundle) as archive:
        payloads = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(payloads.pop("manifest.json"))
    mutate(manifest, payloads)
    manifest["files"] = {
        name: {"sha256": normalize.sha256(data), "size": len(data)}
        for name, data in payloads.items()
    }
    with zipfile.ZipFile(bundle, "w") as archive:
        for name, data in {**payloads, "manifest.json": canonical(manifest)}.items():
            archive.writestr(name, data)


def test_cli_export_restore_queries_and_incremental_roundtrip(
    store, bundle, tmp_path, capsys, monkeypatch
):
    source = Source([activity(1), activity(2)])
    execute(store, source)
    # Arbitrary private files and auth must never become package members.
    with store.lock(write=True):
        store.write_json(
            "auth/session.json",
            {"version": 1, "tokenstore": SECRET, "last_verified_at": CUTOFF, "rejected": False},
        )
        store.write_json("logs/private.json", {"cookie": SECRET, "position_lat": 314159265})
    before = {
        args: command(store, capsys, *args)[1]
        for args in (("status",), ("list",), ("activity", "1"))
    }
    code, exported, _ = command(store, capsys, "backup", "export", "--file", str(bundle))
    assert code == 0 and exported["activity_count"] == 2 and exported["scan_status"] == "complete"
    assert exported["bundle_sha256"] == normalize.sha256(bundle.read_bytes())
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o600
    with zipfile.ZipFile(bundle) as archive:
        assert all(
            name == "manifest.json" or backup.allowed_path(name) for name in archive.namelist()
        )
        for name in archive.namelist():
            data = archive.read(name)
            assert SECRET.encode() not in data
            assert (
                b"position_lat" not in data
                and b"position_long" not in data
                and b"serial_number" not in data
            )
    assert command(store, capsys, "backup", "verify", "--file", str(bundle))[0] == 0
    restored = Store(tmp_path / "restored")
    code, result, _ = command(restored, capsys, "backup", "restore", "--file", str(bundle))
    assert code == 0 and result["bundle_sha256"] == exported["bundle_sha256"]
    for args, expected in before.items():
        actual = command(restored, capsys, *args)[1]
        if args == ("status",):
            assert actual["authentication"]["state"] == "absent"
            expected["authentication"] = actual["authentication"]
        assert actual == expected
    for entry in restored.state()["activities"].values():
        assert entry["raw_ref"] is None and entry["raw_sha256"] is None
        for ref in entry["summary"]["files"].values():
            assert restored.read_bytes(ref) == store.read_bytes(ref)
    assert not (restored.root / "raw").exists() and not (restored.root / "auth").exists()
    source.calls.clear()
    source.activities.append(
        activity(3, startTimeGMT="2010-01-01 00:00:00", startTimeLocal="2010-01-01 08:00:00")
    )
    spy = Mock(wraps=normalize.normalize)
    monkeypatch.setattr(normalize, "normalize", spy)
    monkeypatch.setattr(cli, "GarminSource", lambda _: source)
    code, result, _ = command(restored, capsys, "sync", "--lookback-days", "3")
    assert code == 0 and result["updated"] == 1 and result["counts"]["skipped"] == 2
    assert spy.call_count == 1 and set(restored.state()["activities"]) == {"1", "2", "3"}
    assert [call for call in source.calls if call[0] == "download"] == [("download", "3")]
    for path in [restored.root, *restored.root.rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


def test_restored_unchanged_recheck_replenishes_raw_without_parsing(
    store, bundle, tmp_path, monkeypatch
):
    source = Source()
    execute(store, source)
    backup.export_bundle(store, bundle)
    restored = Store(tmp_path / "restored")
    backup.restore_bundle(restored, bundle)
    previous = restored.state()["activities"]["1001"]["summary"]
    spy = Mock(wraps=normalize.normalize)
    monkeypatch.setattr(normalize, "normalize", spy)
    result = execute(restored, source)
    entry = restored.state()["activities"]["1001"]
    assert result["unchanged"] == 1 and spy.call_count == 0
    assert entry["summary"] == previous and restored.path(entry["raw_ref"]).is_file()


@pytest.mark.parametrize("failure", [BridgeError("activity_unavailable"), KeyboardInterrupt()])
def test_incomplete_package_preserves_gaps_and_page_activity_resume(
    store, bundle, tmp_path, monkeypatch, failure
):
    source = Source([activity(1), activity(2), activity(3)], {"2": failure})
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            execute(store, source)
    else:
        assert not execute(store, source)["complete"]
    original_job = jobs.load_job(store)
    exported = backup.export_bundle(store, bundle)
    assert exported["scan_status"] == "incomplete" and exported["gap_count"] == 1
    restored = Store(tmp_path / "restored")
    backup.restore_bundle(restored, bundle)
    assert jobs.load_job(restored) == original_job
    assert not restored.state()["history_initialized"] and sync.watermark(restored.state()) is None
    source.downloads.clear()
    source.calls.clear()
    spy = Mock(wraps=normalize.normalize)
    monkeypatch.setattr(normalize, "normalize", spy)
    assert execute(restored, source, mode="resume")["complete"]
    assert ("download", "1") not in source.calls
    assert spy.call_count == (2 if isinstance(failure, KeyboardInterrupt) else 1)
    assert set(restored.state()["activities"]) == {"1", "2", "3"}


def test_checksum_failure_does_not_touch_existing_or_new_target(store, bundle, tmp_path, capsys):
    execute(store, Source())
    backup.export_bundle(store, bundle)
    previous = {
        p.relative_to(store.root): p.read_bytes() for p in store.root.rglob("*") if p.is_file()
    }
    with zipfile.ZipFile(bundle) as archive:
        payloads = {name: archive.read(name) for name in archive.namelist()}
    payloads["index.json"] += b" "  # hash no longer matches; leave manifest untouched
    with zipfile.ZipFile(bundle, "w") as archive:
        for name, data in payloads.items():
            archive.writestr(name, data)
    for target in (store, Store(tmp_path / "new")):
        code, result, captured = command(target, capsys, "backup", "restore", "--file", str(bundle))
        assert code == 2 and result["error"] == "invalid_bundle" and not captured.out
    assert not (tmp_path / "new").exists()
    assert previous == {
        p.relative_to(store.root): p.read_bytes() for p in store.root.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize(
    "attack",
    [
        "path",
        "absolute",
        "backslash",
        "auth",
        "raw",
        "unknown",
        "stream_location",
        "journal_secret",
        "journal_mismatch",
        "identity",
        "gap",
        "false_complete",
        "version",
        "duplicate_json",
    ],
)
def test_rehashed_malicious_packages_fail_closed(store, bundle, tmp_path, capsys, attack):
    execute(store, Source([activity(1), activity(2)], {"2": BridgeError("activity_unavailable")}))
    backup.export_bundle(store, bundle)

    def change(manifest, payloads):
        if attack in {"path", "absolute", "backslash", "auth", "raw", "unknown"}:
            path = {
                "path": "../escape.json",
                "absolute": "/escape.json",
                "backslash": "a\\escape.json",
                "auth": "auth/session.json",
                "raw": "raw/" + "a" * 64 + ".bin",
                "unknown": "unknown.json",
            }[attack]
            payloads[path] = canonical({"token": SECRET})
        elif attack in {"stream_location", "identity"}:
            index = json.loads(payloads["index.json"])
            entry = index["activities"]["1"]
            ref = entry["summary"]["files"]["streams"]
            streams = json.loads(payloads[ref])
            if attack == "stream_location":
                streams["records"][0]["position_lat"] = 314159265
            else:
                streams["source_activity_id"] = "9"
            payloads[ref] = canonical(streams)
            entry["file_hashes"]["streams"] = normalize.sha256(payloads[ref])
            payloads["index.json"] = canonical(index)
        elif attack in {"journal_secret", "journal_mismatch", "false_complete"}:
            job = json.loads(payloads[jobs.JOB_FILE])
            if attack == "journal_secret":
                job["items"]["2"]["metadata"]["owner"] = {"token": SECRET}
            elif attack == "journal_mismatch":
                job["items"]["1"]["metadata"]["metrics"]["avg_power"] = 999
            else:
                job["status"] = "complete"
                job["last_failure"] = None
            payloads[jobs.JOB_FILE] = canonical(job)
        elif attack == "gap":
            ref = next(n for n in payloads if n.startswith("reports/"))
            report = json.loads(payloads[ref])
            report["gaps"] = []
            payloads[ref] = canonical(report)
        elif attack == "version":
            manifest["format_version"] = 999
        else:
            payloads["index.json"] = (
                b'{"version":1,"secret":"' + SECRET.encode() + b'",' + payloads["index.json"][1:]
            )

    rewrite(bundle, change)
    restored = Store(tmp_path / "restored")
    code, result, captured = command(restored, capsys, "backup", "restore", "--file", str(bundle))
    assert code == 2 and result["error"] in {"invalid_bundle", "privacy_failed"}
    assert SECRET not in captured.out + captured.err and captured.out == ""
    assert not restored.root.exists() and not (tmp_path / "escape.json").exists()


@pytest.mark.parametrize("attack", ["symlink", "duplicate", "compressed", "truncated", "comment"])
def test_unsafe_archive_framing_is_rejected(store, bundle, attack):
    execute(store, Source())
    backup.export_bundle(store, bundle)
    if attack == "truncated":
        bundle.write_bytes(bundle.read_bytes()[:-16])
    elif attack == "compressed":
        with zipfile.ZipFile(bundle) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
    else:
        with zipfile.ZipFile(bundle, "a") as archive:
            if attack == "comment":
                archive.comment = SECRET.encode()
            elif attack == "duplicate":
                with pytest.warns(UserWarning):
                    archive.writestr("index.json", b"{}")
            else:
                info = zipfile.ZipInfo("reports/" + "a" * 32 + "/gaps.json")
                info.create_system = 3
                info.external_attr = 0o120777 << 16
                archive.writestr(info, b"/elsewhere")
    with pytest.raises(BridgeError) as error:
        backup.verify_bundle(bundle)
    assert error.value.code == "invalid_bundle"


def test_valid_bundle_cannot_overwrite_store_or_existing_package(store, bundle):
    execute(store, Source())
    backup.export_bundle(store, bundle)
    old_index, old_bundle = store.read_bytes("index.json"), bundle.read_bytes()
    with pytest.raises(BridgeError) as error:
        backup.restore_bundle(store, bundle)
    assert error.value.code == "destination_exists" and store.read_bytes("index.json") == old_index
    with pytest.raises(BridgeError) as error:
        backup.export_bundle(store, bundle)
    assert error.value.code == "destination_exists" and bundle.read_bytes() == old_bundle


def test_corrupt_local_artifact_aborts_export_without_publishing(store, bundle):
    execute(store, Source())
    ref = store.state()["activities"]["1001"]["summary"]["files"]["streams"]
    with store.lock(write=True):
        store.write_bytes(ref, SECRET.encode())
    with pytest.raises(BridgeError):
        backup.export_bundle(store, bundle)
    assert not bundle.exists() and list(bundle.parent.iterdir()) == []


def test_hard_exit_during_restore_is_gated_and_recoverable(store, bundle, tmp_path, capsys):
    execute(store, Source([activity(1), activity(2)]))
    backup.export_bundle(store, bundle)
    target = Store(tmp_path / "restored")
    script = """
import os, sys
from pathlib import Path
from garmin_bridge.backup import restore_bundle
from garmin_bridge.storage import Store
target = Store(Path(sys.argv[1]))
write = target.write_bytes
def killed(name, data):
    write(name, data)
    if name.endswith('/streams.json'):
        os._exit(9)
target.write_bytes = killed
restore_bundle(target, Path(sys.argv[2]))
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(target.root), str(bundle)],
        capture_output=True,
        timeout=15,
    )
    assert process.returncode == 9 and not process.stdout and not process.stderr
    for args in (("status",), ("sync", "--resume"), ("auth",)):
        code, result, _ = command(target, capsys, *args)
        assert code == 2 and result["error"] == "restore_pending"
    other = bundle.with_name("other.zip")
    backup.export_bundle(store, other)
    with pytest.raises(BridgeError):
        backup.restore_bundle(target, other)
    assert command(target, capsys, "backup", "restore", "--file", str(bundle))[0] == 0
    assert (
        command(target, capsys, "list")[1]["activities"]
        == command(store, capsys, "list")[1]["activities"]
    )
    assert not target.path(backup.MARKER).exists()


def test_export_and_restore_obey_locks(store, bundle, tmp_path):
    execute(store, Source())
    with store.lock(write=True), pytest.raises(BridgeError) as error:
        backup.export_bundle(store, bundle)
    assert error.value.code == "busy" and not bundle.exists()
    backup.export_bundle(store, bundle)
    target = Store(tmp_path / "restored")
    with target.lock(write=True), pytest.raises(BridgeError) as error:
        backup.restore_bundle(target, bundle)
    assert error.value.code == "busy"
    assert {p.name for p in target.root.iterdir()} == {".lock"}


def test_package_permissions_and_symlinks_are_not_bypassed(store, bundle, tmp_path):
    execute(store, Source())
    backup.export_bundle(store, bundle)
    os.chmod(bundle, 0o644)
    with pytest.raises(BridgeError) as error:
        backup.verify_bundle(bundle)
    assert error.value.code == "unsafe_store"
    os.chmod(bundle, 0o600)
    link = bundle.with_name("link.zip")
    link.symlink_to(bundle)
    with pytest.raises(BridgeError):
        backup.verify_bundle(link)
    with pytest.raises(BridgeError):
        backup.restore_bundle(Store(Path(__file__).parent / "private"), bundle)


def test_empty_store_package_is_valid_and_requires_full_import_after_restore(
    store, bundle, tmp_path
):
    assert backup.export_bundle(store, bundle)["activity_count"] == 0
    assert not store.root.exists()
    target = Store(tmp_path / "restored")
    backup.restore_bundle(target, bundle)
    assert target.state() == store.state()
    with pytest.raises(BridgeError) as error:
        execute(target, Source(), mode="incremental")
    assert error.value.code == "initial_import_required"
