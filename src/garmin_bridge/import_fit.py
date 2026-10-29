"""Offline file adapter. No authentication or API acquisition is involved."""

from pathlib import Path

from . import normalize
from .contract import SCHEMA_VERSION, project_activity
from .errors import BridgeError
from .jobs import load_job
from .storage import Store
from .sync import publish_original


def read_input(store, filename):
    path = Path(filename).expanduser()
    if ".." in path.parts or path.suffix.lower() not in {".fit", ".zip"}:
        raise BridgeError("unsafe_input")
    source = Store(path.parent)
    candidate = source.path(path.name)
    if store is not None and candidate.is_relative_to(store.root / "auth"):
        raise BridgeError("unsafe_input")
    original = source.read_bytes(path.name, limit=normalize.MAX_ARCHIVE)
    if original is None:
        raise BridgeError("unsafe_input")
    return original


def import_file(store, filename, source_id, *, include_identity=False):
    """Caller holds the exclusive lock. Never checkpoint/resolve an API job."""
    meta = normalize.local_metadata(source_id)
    state = store.state()
    previous = state["activities"].get(source_id)
    job = load_job(store)
    item = job["items"].get(source_id) if job else None
    if item and (item["state"] == "completed" or item["reason"] == "outside_recheck_window"):
        if not previous or previous["summary"]["provenance"]["input"] != "garmin_original_fit":
            raise BridgeError("import_conflict")
    changed = publish_original(
        store,
        read_input(store, filename),
        meta,
        state,
        local_import=True,
        include_identity=include_identity,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": ("updated" if previous else "created") if changed else "unchanged",
        "input": {
            "kind": "local_file",
            "activity_id_origin": "cli_argument",
            "garmin_api_verified": False,
        },
        "activity": project_activity(state["activities"][source_id]["summary"]),
        "api_state_changed": False,
    }
