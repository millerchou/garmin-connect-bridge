"""Content addressing for newly written originals; no existing raw migration."""

import copy

from garmin_bridge import normalize, segments, sync
from garmin_bridge.storage import canonical
from .synthetic import Source, activity, archive, fit_bytes
from .test_sync import run


def test_different_wrappers_and_metadata_revisions_share_one_decompressed_raw(store):
    fit = fit_bytes()
    source = Source(
        [activity(1001), activity(1002)],
        {
            "1001": archive(fit, comment=b"synthetic-first"),
            "1002": archive(fit, comment=b"synthetic-second"),
        },
    )
    assert run(store, source)["updated"] == 2
    entries = store.state()["activities"]
    digest = normalize.sha256(fit)
    assert entries["1001"]["raw_ref"] == entries["1002"]["raw_ref"] == f"raw/{digest}.bin"
    assert entries["1001"]["raw_sha256"] == digest
    raw_path = store.path(entries["1001"]["raw_ref"])
    assert raw_path.read_bytes() == fit
    inode = raw_path.stat().st_ino
    old = copy.deepcopy(entries["1001"])
    source.activities[0]["startTimeLocal"] = "2025-01-02 09:00:00"
    source.downloads["1001"] = archive(fit, comment=b"synthetic-third")
    assert run(store, source)["updated"] == 1
    current = store.state()["activities"]["1001"]
    assert current["summary"]["files"] != old["summary"]["files"]
    assert current["raw_ref"] == old["raw_ref"]
    assert raw_path.stat().st_ino == inode
    assert len(list((store.root / "raw").iterdir())) == 1
    assert store.read_bytes(old["summary"]["files"]["summary"]) == canonical(old["summary"])


def test_existing_zip_reference_is_readable_and_unchanged_until_new_revision(store):
    source = Source()
    run(store, source)
    # Reconstruct a pre-change raw reference entirely from invented ZIP bytes.
    legacy = archive(comment=b"synthetic-legacy-wrapper")
    legacy_hash = normalize.sha256(legacy)
    legacy_ref = f"raw/{legacy_hash}.bin"
    with store.lock(write=True):
        state = store.state()
        store.write_bytes(legacy_ref, legacy)
        state["activities"]["1001"].update(raw_ref=legacy_ref, raw_sha256=legacy_hash)
        store.write_json("index.json", state)
    old = copy.deepcopy(store.state()["activities"]["1001"])
    assert segments.locations(store, old)
    source.downloads["1001"] = archive(comment=b"synthetic-different-wrapper")
    assert run(store, source)["unchanged"] == 1
    assert store.state()["activities"]["1001"] == old
    assert store.read_bytes(legacy_ref) == legacy
    assert segments.locations(store, old)
    source.downloads["1001"] = archive(fit_bytes(power=240))
    assert run(store, source)["updated"] == 1
    current = store.state()["activities"]["1001"]
    assert current["raw_ref"] != legacy_ref
    assert store.read_bytes(current["raw_ref"]) == fit_bytes(power=240)
    assert segments.locations(store, current)
    assert store.read_bytes(legacy_ref) == legacy and segments.locations(store, old)
    assert store.read_json(old["summary"]["files"]["summary"]) == old["summary"]


def test_raw_free_store_replenishes_decompressed_fit_without_new_revision(store):
    source = Source()
    run(store, source)
    raw_path = store.path(store.state()["activities"]["1001"]["raw_ref"])
    inode = raw_path.stat().st_ino
    with store.lock(write=True):
        state = store.state()
        state["activities"]["1001"].update(raw_ref=None, raw_sha256=None)
        store.write_json("index.json", state)
    before = store.state()["activities"]["1001"]["summary"]
    source.downloads["1001"] = archive(comment=b"synthetic-replenished")
    assert run(store, source)["unchanged"] == 1
    entry = store.state()["activities"]["1001"]
    assert entry["summary"] == before
    assert entry["raw_sha256"] == before["fit_sha256"]
    assert store.read_bytes(entry["raw_ref"]) == fit_bytes()
    assert raw_path.stat().st_ino == inode


def test_offline_import_shares_raw_with_api_activity(store):
    run(store, Source())
    with store.lock(write=True):
        assert sync.publish_original(
            store,
            archive(comment=b"synthetic-offline-wrapper"),
            normalize.local_metadata("1002"),
            store.state(),
            local_import=True,
        )
    entries = store.state()["activities"]
    assert entries["1001"]["raw_ref"] == entries["1002"]["raw_ref"]
    assert len(list((store.root / "raw").iterdir())) == 1
