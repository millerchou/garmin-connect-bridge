"""Conservative directed whole-path matching. Coordinates live only in memory.

No normalizer field policy is expanded. This module reads only timestamp,
position_lat/long from record frames of an already verified private original.
No location hash, name, path, or coordinate enters a persisted result.
"""

from datetime import datetime
import io
import math
import uuid

import fitdecode

from . import contract as c
from .errors import BridgeError
from .normalize import extract_fit, sha256, timestamp
from .persistence import require
from .privacy import diagnosed, located, require_schema
from .storage import canonical, decode

ALGORITHM = "1.0.1"
# Historical caches remain readable/portable, but are not current evidence.
ALGORITHMS = ("1.0.0", ALGORITHM)
DEFINITION = c.obj(
    {
        "id": c.JOB_ID,
        "version": c.count,
        "algorithm_version": c.literal(*ALGORITHMS),
        "reference_activity": c.identifier,
        "start": lambda x: c.iso_time(x, True),
        "end": lambda x: c.iso_time(x, True),
    }
)
EFFORT = c.obj(
    {
        "ordinal": c.count,
        "start": lambda x: c.iso_time(x, True),
        "end": lambda x: c.iso_time(x, True),
        "elapsed_seconds": c.numeric,
        "distance_m": c.numeric,
        "sample_count": c.count,
        "heart_rate_mean": c.nullable(c.numeric),
        "power_mean": c.nullable(c.numeric),
        "heart_rate_present": c.count,
        "power_present": c.count,
        "aggregation": c.literal("arithmetic_mean_observed_samples_inclusive"),
        "match_quality": c.literal("directed_full_path_within_20m"),
        "boundary_sampling_interval_seconds": c.numeric,
    }
)
RESULT = c.obj(
    {
        "segment_id": c.JOB_ID,
        "definition_version": c.count,
        "algorithm_version": c.literal(*ALGORITHMS),
        "activity_id": c.identifier,
        "revision": c.digest,
        "reference_revision": c.digest,
        "status": c.literal("matched", "no_match", "unavailable", "insufficient_data"),
        "efforts": c.sequence(EFFORT, 1000),
    }
)


def revision(entry):
    return entry["summary"]["files"]["summary"].split("/")[2]


def seconds(stamp):
    return datetime.fromisoformat(stamp).timestamp()


@located("activities.sqlite", "segments")
def definition_valid(value):
    require_schema(DEFINITION, value, "segment.definition_contract")
    require(value["version"] >= 1, "segments.definition_valid.1", "version")
    require(
        0 < seconds(value["end"]) - seconds(value["start"]) <= 7200,
        "segments.definition_valid.2",
        "end,start",
    )
    return value


@located("activities.sqlite", "efforts")
def result_valid(value):
    require_schema(RESULT, value, "segment.result_contract")
    require(
        (value["status"] == "matched") == bool(value["efforts"]),
        "segments.result_valid.2",
        "status,efforts",
    )
    prior_end = None
    for i, effort in enumerate(value["efforts"]):
        require(effort["ordinal"] == i + 1, "segments.result_valid.3", "ordinal")
        require(
            effort["elapsed_seconds"] == seconds(effort["end"]) - seconds(effort["start"]) > 0,
            "segments.result_valid.4",
            "elapsed_seconds,end,start",
        )
        require(effort["elapsed_seconds"] <= 7200, "segments.result_valid.5", "elapsed_seconds")
        require(
            prior_end is None or seconds(effort["start"]) > prior_end,
            "segments.result_valid.6",
            "start",
        )
        prior_end = seconds(effort["end"])
        require(
            effort["distance_m"] >= 85 and effort["sample_count"] >= 1,
            "segments.result_valid.7",
            "distance_m,sample_count",
        )
        require(
            0 <= effort["boundary_sampling_interval_seconds"] <= 30,
            "segments.result_valid.8",
            "boundary_sampling_interval_seconds",
        )
        for metric in ("heart_rate", "power"):
            require(
                effort[f"{metric}_present"] <= effort["sample_count"],
                "segments.result_valid.9",
                "sample_count",
            )
            require(
                (effort[f"{metric}_mean"] is None) == (effort[f"{metric}_present"] == 0),
                "segments.result_valid.10",
                metric + "_mean," + metric + "_present",
            )
    return value


def observed_statistics(rows):
    result = {"sample_count": len(rows)}
    for metric in ("heart_rate", "power"):
        observed = [r[metric] for r in rows if r[metric] is not None]
        result[f"{metric}_mean"] = sum(observed) / len(observed) if observed else None
        result[f"{metric}_present"] = len(observed)
    return result


@located("activities.sqlite", "efforts")
def validate_result_context(store, definition, value):
    """Check reproducible non-location evidence; this cannot re-prove GPS matching."""
    result_valid(value)
    if not value["efforts"]:
        return
    ref_name = (
        f"objects/{definition['reference_activity']}/{value['reference_revision']}/streams.json"
    )
    ref = store.read_json(ref_name)
    require(ref is not None, "segments.validate_result_context.1", "reference_revision")
    require(
        all(r["timestamp"] is not None for r in ref["records"]),
        "segments.validate_result_context.2",
        "timestamp,records",
    )
    ref_times = [seconds(r["timestamp"]) for r in ref["records"]]
    start, end = seconds(definition["start"]), seconds(definition["end"])
    ref_times = [t for t in ref_times if start <= t <= end]
    require(
        len(ref_times) >= 10 and ref_times[0] == start and ref_times[-1] == end,
        "segments.validate_result_context.3",
        "start,end",
    )
    require(
        all(0 < b - a <= 15 for a, b in zip(ref_times, ref_times[1:])),
        "segments.validate_result_context.4",
        "records.timestamp",
    )
    name = f"objects/{value['activity_id']}/{value['revision']}/streams.json"
    stream = store.read_json(name)
    require(stream is not None, "segments.validate_result_context.5", "revision")
    require(
        all(r["timestamp"] is not None for r in stream["records"]),
        "segments.validate_result_context.6",
        "timestamp,records",
    )
    all_times = [seconds(r["timestamp"]) for r in stream["records"]]
    require(
        all(0 < b - a <= 15 for a, b in zip(all_times, all_times[1:])),
        "segments.validate_result_context.7",
        "records.timestamp",
    )
    for effort in value["efforts"]:
        start, end = seconds(effort["start"]), seconds(effort["end"])
        rows = [
            r
            for r in stream["records"]
            if r["timestamp"] is not None and start <= seconds(r["timestamp"]) <= end
        ]
        require(len(rows) >= 2, "segments.validate_result_context.8", "records")
        times = [seconds(r["timestamp"]) for r in rows]
        require(
            times[0] == start and times[-1] == end,
            "segments.validate_result_context.9",
            "start,end",
        )
        require(
            all(0 < b - a <= 15 for a, b in zip(times, times[1:])),
            "segments.validate_result_context.10",
            "records.timestamp",
        )
        stats = observed_statistics(rows)
        require(
            canonical({k: effort[k] for k in stats}) == canonical(stats),
            "segments.validate_result_context.11",
            "heart_rate_mean,power_mean,heart_rate_present,power_present,sample_count",
        )
        require(
            effort["boundary_sampling_interval_seconds"]
            == 2 * max(b - a for a, b in zip(times, times[1:])),
            "segments.validate_result_context.12",
            "boundary_sampling_interval_seconds",
        )


@located("activities.sqlite", "segments")
def validate_segments(db, store):
    definitions = {}
    state = store.state()
    for ident, payload in db.execute("SELECT * FROM segments"):
        value = definition_valid(decode(payload))
        require(ident == value["id"], "segments.validate_segments.1", "id")
        require(
            value["reference_activity"] in state["activities"],
            "segments.validate_segments.2",
            "reference_activity,activities",
        )
        definitions[ident] = value
    for segment, activity, payload in db.execute("SELECT * FROM efforts"):
        value = result_valid(decode(payload))
        require(
            segment in definitions
            and segment == value["segment_id"]
            and activity == value["activity_id"],
            "segments.validate_segments.3",
            "segment_id,activity_id",
            table="efforts",
        )
        require(
            value["definition_version"] == definitions[segment]["version"],
            "segments.validate_segments.4",
            "definition_version,version",
            table="efforts",
        )
        # Cached historical activity revisions are allowed but must exist.
        require(
            db.execute(
                "SELECT 1 FROM documents WHERE name=?",
                (f"objects/{activity}/{value['revision']}/summary.json",),
            ).fetchone()
            is not None,
            "segments.validate_segments.5",
            "revision",
            table="efforts",
        )
        validate_result_context(store, definitions[segment], value)
        ref = definitions[segment]["reference_activity"]
        require(
            db.execute(
                "SELECT 1 FROM documents WHERE name=?",
                (f"objects/{ref}/{value['reference_revision']}/summary.json",),
            ).fetchone()
            is not None,
            "segments.validate_segments.6",
            "reference_revision",
            table="efforts",
        )


@located("activities.sqlite", "segments")
@diagnosed(source=True)
def define(store, reference, start, end, ident=None):
    state = store.state()
    require(reference in state["activities"], "segments.define.1", "activities")
    ident = ident or uuid.uuid4().hex
    require(c.JOB_ID(ident), "segments.define.2", "id")
    with store.database(write=True) as db:
        old = db.execute("SELECT payload FROM segments WHERE id=?", (ident,)).fetchone()
        previous = definition_valid(decode(old[0])) if old else None
        version = previous["version"] + 1 if previous else 1
        value = definition_valid(
            {
                "id": ident,
                "version": version,
                "algorithm_version": ALGORITHM,
                "reference_activity": reference,
                "start": start,
                "end": end,
            }
        )
        if previous and (
            previous["reference_activity"] == reference
            and seconds(previous["start"]) == seconds(start)
            and seconds(previous["end"]) == seconds(end)
        ):
            return previous
        db.execute("INSERT OR REPLACE INTO segments VALUES (?,?)", (ident, canonical(value)))
        db.execute("DELETE FROM efforts WHERE segment=?", (ident,))
    return value


def locations(store, entry):
    if entry["raw_ref"] is None:
        return None
    raw = store.read_bytes(entry["raw_ref"])
    if raw is None or sha256(raw) != entry["raw_sha256"]:
        return None
    fit = extract_fit(raw)
    if sha256(fit) != entry["summary"]["fit_sha256"]:
        return None
    points = []
    try:
        with fitdecode.FitReader(
            io.BytesIO(fit),
            check_crc=fitdecode.CrcCheck.RAISE,
            error_handling=fitdecode.ErrorHandling.RAISE,
        ) as reader:
            for frame in reader:
                if not isinstance(frame, fitdecode.FitDataMessage) or frame.name != "record":
                    continue
                values = {
                    f.name: f.value
                    for f in frame.fields
                    if not (f.field_def is not None and f.field_def.is_dev)
                    and f.name in {"timestamp", "position_lat", "position_long", "gps_accuracy"}
                }
                accuracy = values.get("gps_accuracy")
                if accuracy is not None and (
                    not c.numeric(accuracy) or accuracy < 0 or accuracy > 10
                ):
                    points.append(None)
                    continue
                t = timestamp(values.get("timestamp"), utc=True)
                lat, lon = values.get("position_lat"), values.get("position_long")
                if t is None or type(lat) is not int or type(lon) is not int:
                    points.append(None)
                elif not (-(2**30) <= lat <= 2**30 and -(2**31) <= lon < 2**31):
                    points.append(None)
                else:
                    points.append(
                        (t, math.radians(lat * 180 / 2**31), math.radians(lon * 180 / 2**31))
                    )
                if len(points) > 500_000:
                    return []
    except Exception:
        return []
    return points


def distance(a, b):
    dlat, dlon = b[1] - a[1], b[2] - a[2]
    h = math.sin(dlat / 2) ** 2 + math.cos(a[1]) * math.cos(b[1]) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(h)))


def usable(points):
    return (
        len(points) >= 10
        and all(p is not None for p in points)
        and all(
            0 < seconds(b[0]) - seconds(a[0]) <= 15 and distance(a, b) <= 50
            for a, b in zip(points, points[1:])
        )
    )


def length(points):
    return sum(distance(a, b) for a, b in zip(points, points[1:]))


def full_path(reference, candidate):
    """Discrete Frechet reachability at fixed 20m; order and both paths covered."""
    previous = [False] * len(candidate)
    for i, a in enumerate(reference):
        current = [False] * len(candidate)
        for j, b in enumerate(candidate):
            current[j] = distance(a, b) <= 20 and (
                (i == 0 and j == 0)
                or previous[j]
                or (j > 0 and (previous[j - 1] or current[j - 1]))
            )
        previous = current
    return previous[-1]


def clusters(points, anchor):
    """One nearest observed boundary per contiguous proximity visit."""
    result, group = [], []
    for i, p in enumerate(points):
        if p is not None and distance(p, anchor) <= 15:
            group.append(i)
        elif group:
            result.append(min(group, key=lambda k: distance(points[k], anchor)))
            group = []
    if group:
        result.append(min(group, key=lambda k: distance(points[k], anchor)))
    return result


def bracketed(points, index, anchor, tangent):
    """Require observed positions on both sides of the endpoint's normal plane.

    Endpoint proximity alone admits truncated rides. The 0.5m slack only covers
    coordinate quantization; larger uncertainty fails closed. No crossing time
    or interpolated/virtual position is produced.
    """

    def vector(point):
        longitude = (point[2] - anchor[2] + math.pi) % (2 * math.pi) - math.pi
        return longitude * math.cos(anchor[1]), point[1] - anchor[1]

    x, y = vector(tangent)
    norm = math.hypot(x, y)
    if norm == 0:
        return False
    along = [
        (u * x + v * y) / norm * 6371000
        for u, v in (vector(p) for p in points[max(0, index - 1) : index + 2])
    ]
    return min(along) <= 0.5 and max(along) >= -0.5


def match(reference, points):
    if not usable(reference) or not usable(points) or len(reference) > 4096:
        return "insufficient_data", []
    ref_length = length(reference)
    # Closed loops or very short reference paths cannot establish direction here.
    if ref_length < 100 or distance(reference[0], reference[-1]) < 50:
        return "insufficient_data", []
    starts, ends = clusters(points, reference[0]), clusters(points, reference[-1])
    found, budget, consumed = [], 5_000_000, -1
    first_tangent = next(p for p in reference if distance(p, reference[0]) >= 25)
    last_tangent = next(p for p in reversed(reference) if distance(p, reference[-1]) >= 25)
    uncertain = False
    for a in starts:
        if a <= consumed:
            continue
        for b in ends:
            if b <= a:
                continue
            candidate = points[a : b + 1]
            if seconds(candidate[-1][0]) - seconds(candidate[0][0]) > 7200:
                break
            size = len(reference) * len(candidate)
            budget -= size
            if budget < 0:
                return "insufficient_data", []
            if not 0.85 * ref_length <= length(candidate) <= 1.15 * ref_length:
                continue
            if full_path(reference, candidate):
                if not (
                    bracketed(points, a, reference[0], first_tangent)
                    and bracketed(points, b, reference[-1], last_tangent)
                ):
                    uncertain = True
                    continue
                found.append((a, b))
                consumed = b
                break
        if len(found) > 1000:
            return "insufficient_data", []
    if uncertain:
        return "insufficient_data", []
    return ("matched" if found else "no_match"), found


def calculate(store, definition, entry, reference_entry):
    base = {
        "segment_id": definition["id"],
        "definition_version": definition["version"],
        "algorithm_version": ALGORITHM,
        "activity_id": entry["summary"]["source_activity_id"],
        "revision": revision(entry),
        "reference_revision": revision(reference_entry),
        "status": "unavailable",
        "efforts": [],
    }
    ref, points = locations(store, reference_entry), locations(store, entry)
    if ref is None or points is None:
        return result_valid(base)
    # Do not silently drop gaps inside the reference window.
    if any(p is None for p in ref):
        base["status"] = "insufficient_data"
        return result_valid(base)
    ref = [
        p
        for p in ref
        if seconds(definition["start"]) <= seconds(p[0]) <= seconds(definition["end"])
    ]
    if (
        not ref
        or seconds(ref[0][0]) != seconds(definition["start"])
        or seconds(ref[-1][0]) != seconds(definition["end"])
    ):
        base["status"] = "insufficient_data"
        return result_valid(base)
    status, windows = match(ref, points)
    base["status"] = status
    records = store.read_json(entry["summary"]["files"]["streams"])["records"]
    for ordinal, (a, b) in enumerate(windows, 1):
        start, end = points[a][0], points[b][0]
        rows = [
            r
            for r in records
            if r["timestamp"] is not None
            and seconds(start) <= seconds(r["timestamp"]) <= seconds(end)
        ]
        effort = {
            "ordinal": ordinal,
            "start": start,
            "end": end,
            "elapsed_seconds": seconds(end) - seconds(start),
            "distance_m": length(points[a : b + 1]),
            "sample_count": len(rows),
            "aggregation": "arithmetic_mean_observed_samples_inclusive",
            "match_quality": "directed_full_path_within_20m",
            "boundary_sampling_interval_seconds": 2
            * max(
                seconds(y[0]) - seconds(x[0]) for x, y in zip(points[a:b], points[a + 1 : b + 1])
            ),
        }
        effort.update(observed_statistics(rows))
        base["efforts"].append(effort)
    return result_valid(base)


@located("activities.sqlite", "segments")
@diagnosed(source=True)
def refresh(store, ident):
    require(c.JOB_ID(ident), "segments.refresh.1", "id")
    state = store.state()
    with store.database() as db:
        row = db.execute("SELECT payload FROM segments WHERE id=?", (ident,)).fetchone()
    if row is None:
        raise BridgeError("not_found")
    definition = definition_valid(decode(row[0]))
    require(definition["id"] == ident, "segments.refresh.2", "id")
    if definition["algorithm_version"] != ALGORITHM:
        definition = {**definition, "algorithm_version": ALGORITHM}
        with store.database(write=True) as db:
            db.execute("UPDATE segments SET payload=? WHERE id=?", (canonical(definition), ident))
    reference = state["activities"][definition["reference_activity"]]
    changed = 0
    for activity, entry in state["activities"].items():
        with store.database() as db:
            row = db.execute(
                "SELECT payload FROM efforts WHERE segment=? AND activity=?", (ident, activity)
            ).fetchone()
        if row:
            prior = result_valid(decode(row[0]))
            require(
                prior["segment_id"] == ident and prior["activity_id"] == activity,
                "segments.refresh.3",
                "segment_id,activity_id",
                table="efforts",
            )
            require(
                prior["definition_version"] == definition["version"],
                "segments.refresh.4",
                "definition_version,version",
                table="efforts",
            )
            if (
                prior["revision"] == revision(entry)
                and prior["reference_revision"] == revision(reference)
                and prior["algorithm_version"] == ALGORITHM
                and prior["status"] not in {"unavailable", "insufficient_data"}
            ):
                validate_result_context(store, definition, prior)
                continue
        value = calculate(store, definition, entry, reference)
        validate_result_context(store, definition, value)
        with store.database(write=True) as db:
            db.execute(
                "INSERT OR REPLACE INTO efforts VALUES (?,?,?)", (ident, activity, canonical(value))
            )
        changed += 1
    return changed


@located("activities.sqlite", "segments")
@diagnosed(source=True)
def history(store, ident, offset=0, limit=20):
    require(
        c.JOB_ID(ident)
        and type(offset) is int
        and offset >= 0
        and type(limit) is int
        and 1 <= limit <= 50,
        "segments.history.1",
        "id,offset,limit",
    )
    state = store.state()
    with store.database() as db:
        row = db.execute("SELECT payload FROM segments WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise BridgeError("not_found")
        definition = definition_valid(decode(row[0]))
        require(definition["id"] == ident, "segments.history.2", "id")
        reference = state["activities"][definition["reference_activity"]]
        results = []
        for activity, payload in db.execute(
            "SELECT activity,payload FROM efforts WHERE segment=? ORDER BY activity", (ident,)
        ):
            value = result_valid(decode(payload))
            require(
                value["segment_id"] == ident and value["activity_id"] == activity,
                "segments.history.3",
                "segment_id,activity_id",
                table="efforts",
            )
            require(
                value["definition_version"] == definition["version"],
                "segments.history.4",
                "definition_version,version",
                table="efforts",
            )
            if (
                activity in state["activities"]
                and value["revision"] == revision(state["activities"][activity])
                and value["reference_revision"] == revision(reference)
                and value["algorithm_version"] == definition["algorithm_version"] == ALGORITHM
            ):
                validate_result_context(store, definition, value)
                # Flatten efforts for a real bounded response, even with many laps.
                for effort in value["efforts"] or [None]:
                    results.append(
                        {
                            **{k: v for k, v in value.items() if k != "efforts"},
                            "effort": effort,
                            "provenance": state["activities"][activity]["summary"]["provenance"],
                            "quality_flags": state["activities"][activity]["summary"][
                                "quality_flags"
                            ],
                            "normalizer_version": state["activities"][activity]["summary"][
                                "normalizer_version"
                            ],
                            "reference_normalizer_version": reference["summary"][
                                "normalizer_version"
                            ],
                        }
                    )
    page = results[offset : offset + limit]
    return {
        "definition": definition,
        "results": page,
        "next_offset": offset + len(page) if offset + len(page) < len(results) else None,
        "cached_activity_count": len({r["activity_id"] for r in results}),
        "activity_count": len(state["activities"]),
    }
