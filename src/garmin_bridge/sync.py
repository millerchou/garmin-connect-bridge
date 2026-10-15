"""Incremental acquisition with explicit coverage and crash-safe checkpoints."""

from datetime import date, datetime, timedelta

from . import normalize as parser
from .contract import NORMALIZER_VERSION, SCHEMA_VERSION, project_activity, validate_streams
from .errors import BridgeError
from .storage import canonical
from .upstream import now


def merge_ranges(ranges):
    merged = []
    for item in sorted(ranges, key=lambda r: r["start"]):
        start, end = date.fromisoformat(item["start"]), date.fromisoformat(item["end"])
        if start > end:
            raise BridgeError("storage_failed")
        if merged and start <= date.fromisoformat(merged[-1]["end"]) + timedelta(days=1):
            merged[-1]["end"] = max(merged[-1]["end"], end.isoformat())
        else:
            merged.append({"start": start.isoformat(), "end": end.isoformat()})
    return merged


def subtract_range(ranges, completed):
    result = []
    a, b = date.fromisoformat(completed["start"]), date.fromisoformat(completed["end"])
    for interval in ranges:
        start, end = date.fromisoformat(interval["start"]), date.fromisoformat(interval["end"])
        if b < start or a > end:
            result.append(interval)
            continue
        if start < a:
            result.append({"start": start.isoformat(), "end": (a - timedelta(days=1)).isoformat()})
        if b < end:
            result.append({"start": (b + timedelta(days=1)).isoformat(), "end": end.isoformat()})
    return result


def watermark(state):
    covered = merge_ranges(state["covered_ranges"])
    # Start the contiguous horizon at the earliest completed OR pending day.
    # A failed backfill before existing coverage must not be hidden by it.
    pending = state["pending_ranges"]
    if not covered or (pending and min(r["start"] for r in pending) < covered[0]["start"]):
        return None
    result = dict(covered[0])
    # Failed rechecks don't erase historical successful coverage; pending_ranges
    # and last_success_at explicitly describe that freshness limitation.
    return result


def freshness(state):
    return {
        "last_success_at": state["last_success_at"],
        "last_attempt_at": state["last_attempt_at"],
        "pending_ranges": state["pending_ranges"],
        "watermark": watermark(state),
        "last_failure": state["last_failure"],
        "history_initialized": state["history_initialized"],
    }


def choose_range(state, start=None, end=None, lookback=14, today=None):
    today = today or date.today()
    explicit_end = end is not None
    end = date.fromisoformat(end) if end else today
    if start:
        start = date.fromisoformat(start)
    else:
        mark = watermark(state)
        anchor = min(date.fromisoformat(mark["end"]), end) if mark else end
        start = anchor - timedelta(days=lookback - 1)
        if state["pending_ranges"]:
            pending = [
                r
                for r in state["pending_ranges"]
                if not explicit_end or r["start"] <= end.isoformat()
            ]
            if pending:
                start = min(start, *(date.fromisoformat(r["start"]) for r in pending))
                if not explicit_end:
                    end = max(end, *(date.fromisoformat(r["end"]) for r in pending))
    if start > end or end > today or lookback < 1 or lookback > 365:
        raise BridgeError("usage")
    return {"start": start.isoformat(), "end": end.isoformat()}


def files_available(store, summary):
    return all(store.path(ref).is_file() for ref in summary["files"].values())


def artifacts_intact(store, entry):
    """Derived artifacts are sufficient after a reviewed, raw-free restore."""
    for name, reference in entry["summary"]["files"].items():
        content = store.read_bytes(reference)
        if content is None or parser.sha256(content) != entry["file_hashes"][name]:
            return False
    return True


def acquire(store, source, meta, state, cutoff_at=None):
    source_id = meta["source_activity_id"]
    original = source.download(source_id)
    fit = parser.extract_fit(original)
    digest = parser.input_digest(meta, parser.sha256(fit))
    previous = state["activities"].get(source_id)
    if previous:
        summary = project_activity(previous["summary"])
        if (
            summary["source_digest"] == digest
            and summary["normalizer_version"] == NORMALIZER_VERSION
            and summary["schema_version"] == SCHEMA_VERSION
            and artifacts_intact(store, previous)
        ):
            if (
                cutoff_at
                and summary["activity_time"]["utc"]
                and datetime.fromisoformat(summary["activity_time"]["utc"])
                > datetime.fromisoformat(cutoff_at)
            ):
                return None
            # A portable backup deliberately omits originals. A recheck can
            # replenish one without parsing or changing the reviewed result.
            raw = store.read_bytes(previous["raw_ref"]) if previous["raw_ref"] else None
            if raw is None or parser.sha256(raw) != previous["raw_sha256"]:
                raw_hash = parser.sha256(original)
                raw_ref = f"raw/{raw_hash}.bin"
                store.write_bytes(raw_ref, original)
                entry = {**previous, "raw_ref": raw_ref, "raw_sha256": raw_hash}
                committed = {**state, "activities": {**state["activities"], source_id: entry}}
                store.write_json("index.json", committed)
                state.update(committed)
            return False
    summary, streams = parser.normalize(fit, meta, now(), digest)
    validate_streams(streams)
    if (
        cutoff_at
        and summary["activity_time"]["utc"]
        and datetime.fromisoformat(summary["activity_time"]["utc"])
        > datetime.fromisoformat(cutoff_at)
    ):
        return None
    revision = parser.sha256(canonical([digest, NORMALIZER_VERSION, SCHEMA_VERSION]))
    prefix = f"objects/{source_id}/{revision}"
    summary["files"] = {kind: f"{prefix}/{kind}.json" for kind in ("summary", "streams")}
    summary = project_activity(summary)
    raw_hash = parser.sha256(original)
    raw_ref = f"raw/{raw_hash}.bin"
    # Immutable content first, index last. Any crash leaves the prior index usable.
    store.write_bytes(raw_ref, original)
    file_hashes = {}
    for name, value in (("streams", streams), ("summary", summary)):
        content = canonical(value)
        store.write_bytes(summary["files"][name], content)
        file_hashes[name] = parser.sha256(content)
    entry = {
        "summary": summary,
        "file_hashes": file_hashes,
        "raw_ref": raw_ref,
        "raw_sha256": raw_hash,
        "metadata_digest": parser.sha256(canonical(meta)),
    }
    committed = {**state, "activities": {**state["activities"], source_id: entry}}
    store.write_json("index.json", committed)
    state.update(committed)
    return True


def synchronize(store, source_factory, interval=None, **options):
    """Caller holds the exclusive store lock, including authentication/refresh."""
    from .jobs import run_job

    return run_job(store, source_factory, interval, **options)
