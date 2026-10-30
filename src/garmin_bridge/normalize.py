"""Pure, reviewed projection of metadata and original FIT bytes."""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import io
import re
import stat
import struct
import zipfile

import fitdecode

from .contract import (
    METRIC_UNITS,
    NORMALIZER_VERSION,
    REPORTED_UNITS,
    SCHEMA_VERSION,
    STREAM_UNITS,
    identifier,
    numeric,
    timezone_name,
)
from .errors import BridgeError
from .fit_details import (
    TimestampContext,
    fill_local_time,
    filter_message_context,
    project_message,
    timestamp_value,
)
from .fit_fields import AUXILIARY_MESSAGES, FIT_FIELDS_VERSION, RECORD_UNITS
from .storage import canonical
from .fit_unknown import FIT_PRIVACY_VERSION, RawPrivacy, project_unknown

MAX_ARCHIVE = 64 * 1024 * 1024
MAX_FIT = 64 * 1024 * 1024
MAX_RECORDS = 500_000
CYCLING_TYPES = frozenset(
    {
        "cycling",
        "road_biking",
        "mountain_biking",
        "gravel_cycling",
        "indoor_cycling",
        "virtual_ride",
        "track_cycling",
        "recumbent_cycling",
        "hand_cycling",
        "bmx",
        "e_bike_fitness",
        "e_bike_mountain",
        "cyclocross",
        "downhill_biking",
    }
)
NON_CYCLING_TYPES = frozenset(
    {
        "running",
        "trail_running",
        "swimming",
        "lap_swimming",
        "open_water_swimming",
        "walking",
        "hiking",
        "multi_sport",
        "fitness_equipment",
        "other",
    }
)
SUMMARY_FIELDS = {
    "duration": ("total_timer_time", "duration"),
    "distance": ("total_distance", "distance"),
    "avg_heart_rate": ("avg_heart_rate", "averageHR"),
    "max_heart_rate": ("max_heart_rate", "maxHR"),
    "avg_power": ("avg_power", "avgPower"),
    "max_power": ("max_power", "maxPower"),
    "avg_cadence": ("avg_cadence", "averageBikingCadenceInRevPerMinute"),
    "max_cadence": ("max_cadence", "maxBikingCadenceInRevPerMinute"),
    "avg_speed": ("enhanced_avg_speed", "averageSpeed"),
    "max_speed": ("enhanced_max_speed", "maxSpeed"),
    "ascent": ("total_ascent", "elevationGain"),
    "descent": ("total_descent", "elevationLoss"),
}
REPORTED_FIELDS = {
    "normalized_power": ("normalized_power", "normPower"),
    "training_stress_score": ("training_stress_score", "trainingStressScore"),
    "aerobic_training_effect": ("total_training_effect", "aerobicTrainingEffect"),
    "anaerobic_training_effect": ("total_anaerobic_training_effect", "anaerobicTrainingEffect"),
    "threshold_power": ("threshold_power", "functionalThresholdPower"),
}
LOCAL_FLAGS = frozenset({"local_file_import", "missing_api_metadata", "unverified_activity_id"})
# Legacy summary/record projection. Additional physical FIT detail has its own
# reviewed, typed allowlist in fit_fields.py, selected before value access.
MESSAGE_FIELDS = {
    "record": frozenset(
        {
            "timestamp",
            "heart_rate",
            "power",
            "cadence",
            "fractional_cadence",
            "enhanced_speed",
            "speed",
            "enhanced_altitude",
            "altitude",
            "distance",
            "temperature",
            "enhanced_respiration_rate",
            "left_right_balance",
        }
    ),
    "session": frozenset(
        {"sport", "start_time", "timestamp", "avg_speed", "max_speed"}
        | {names[0] for names in SUMMARY_FIELDS.values()}
        | {names[0] for names in REPORTED_FIELDS.values()}
    ),
    "lap": frozenset({"start_time", "timestamp", "total_timer_time", "total_distance"}),
    "event": frozenset({"timestamp", "event", "event_type"}),
}


def sha256(data: bytes):
    return hashlib.sha256(data).hexdigest()


def number(value):
    return value if numeric(value) else None


def timestamp(value, utc=False):
    if type(value) is str and len(value) <= 35:
        if len(value) <= 10 or value[10] not in ("T", " "):
            return None
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if utc:
        value = (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )
        return value.isoformat().replace("+00:00", "Z")
    return value.isoformat()


def metadata(raw):
    if type(raw) is not dict:
        raise BridgeError("upstream_invalid")
    kind = raw.get("activityType")
    if type(kind) is not dict:
        raise BridgeError("upstream_invalid")
    if type(kind.get("typeKey")) is not str:
        raise BridgeError("upstream_invalid")
    if kind["typeKey"] in NON_CYCLING_TYPES:
        return None
    if kind["typeKey"] not in CYCLING_TYPES:
        raise BridgeError("unsupported_activity_type")
    source_id = raw.get("activityId")
    if type(source_id) is int:
        source_id = str(source_id)
    if not identifier(source_id):
        raise BridgeError("upstream_invalid")
    zone = raw.get("timeZoneUnitDTO")
    zone = zone.get("timeZone") if type(zone) is dict else None
    utc = timestamp(raw.get("startTimeGMT"), utc=True)
    local = timestamp(raw.get("startTimeLocal"))
    offset = None
    if utc and local:
        # The observed local offset is distinct from an IANA time-zone name.
        delta = (
            datetime.fromisoformat(local).replace(tzinfo=None)
            - datetime.fromisoformat(utc).replace(tzinfo=None)
        ).total_seconds()
        offset = delta if abs(delta) <= 14 * 3600 else None
    return {
        "source_activity_id": source_id,
        "activity_time": {
            "utc": utc,
            "local": local,
            "timezone": zone if timezone_name(zone) else None,
            "utc_offset_seconds": offset,
        },
        "metrics": {k: number(raw.get(v[1])) for k, v in SUMMARY_FIELDS.items()},
        "garmin_reported": {k: number(raw.get(v[1])) for k, v in REPORTED_FIELDS.items()},
    }


def local_metadata(source_id):
    """No API metadata can be inferred from an offline file or the host."""
    if not identifier(source_id):
        raise BridgeError("usage")
    return {
        "source_activity_id": source_id,
        "activity_time": dict.fromkeys(("utc", "local", "timezone", "utc_offset_seconds")),
        "metrics": dict.fromkeys(METRIC_UNITS),
        "garmin_reported": dict.fromkeys(REPORTED_UNITS),
    }


def extract_fit(original: bytes) -> bytes:
    """Never extract archive paths to disk; accept one unambiguous FIT only."""
    if type(original) is not bytes or not 0 < len(original) <= MAX_ARCHIVE:
        raise BridgeError("unsafe_archive")
    data = original
    if original[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(original)) as archive:
                entries = archive.infolist()
                if not 1 <= len(entries) <= 32 or sum(e.file_size for e in entries) > MAX_FIT:
                    raise BridgeError("unsafe_archive")
                fits = []
                names = set()
                for entry in entries:
                    name = entry.filename
                    mode = entry.external_attr >> 16
                    if (
                        not re.fullmatch(r"[A-Za-z0-9_.\-/]+", name)
                        or name.startswith("/")
                        or any(p in ("", ".", "..") for p in name.rstrip("/").split("/"))
                        or name in names
                        or entry.flag_bits & 1
                        or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                        or entry.file_size > MAX_FIT
                        or entry.file_size > max(entry.compress_size, 1) * 200
                    ):
                        raise BridgeError("unsafe_archive")
                    names.add(name)
                    if not entry.is_dir() and name.lower().endswith(".fit"):
                        fits.append(entry)
                if len(fits) != 1:
                    raise BridgeError("unsafe_archive")
                with archive.open(fits[0]) as handle:
                    data = handle.read(MAX_FIT + 1)
        except (ValueError, RuntimeError, OSError, zipfile.BadZipFile, NotImplementedError):
            raise BridgeError("unsafe_archive") from None
    if (
        len(data) < 14
        or len(data) > MAX_FIT
        or data[0] not in (12, 14)
        or data[8:12] != b".FIT"
        or data[0] + struct.unpack_from("<I", data, 4)[0] + 2 != len(data)
    ):
        raise BridgeError("invalid_fit")
    return data


def _values(frame):
    # FIT developer field names are arbitrary free text and may spoof standard
    # names. Only fields from the standard profile (including components) pass.
    allowed = MESSAGE_FIELDS.get(frame.name)
    if allowed is None:
        return {}
    values = {}
    for field in frame.fields:
        if field.field_def is not None and field.field_def.is_dev:
            continue
        if field.name not in allowed:
            continue
        if field.name == "timestamp":
            continue  # Supplied only by the independently checked FIT clock.
        if field.name == "enhanced_respiration_rate" and field.units != "Breaths/min":
            continue  # Don't relabel legacy components with ambiguous units.
        values[field.name] = field.raw_value if field.name == "left_right_balance" else field.value
    return values


def _prefer(values, first, second):
    value = values.get(first)
    return value if value is not None else values.get(second)


def normalize(
    fit: bytes,
    meta: dict,
    acquired_at: str,
    source_digest: str,
    *,
    local_import=False,
    include_identity=False,
    blocked_fields=None,
):
    if type(include_identity) is not bool:
        raise BridgeError("usage")
    flags = {
        "non_location_projection",
        "unreviewed_fields_omitted",
        "source_reported_not_independently_validated",
    }
    if local_import:
        if meta != local_metadata(meta["source_activity_id"]):
            raise BridgeError("import_conflict")
        flags.update(LOCAL_FLAGS)
    records, laps, events, sessions = [], [], [], []
    detailed_events, messages, raw_messages = [], [], []
    blocked = Counter() if blocked_fields is None else blocked_fields
    session_detail = None
    device_count = 0
    cadence_integers = []
    clock = TimestampContext()
    raw_privacy = RawPrivacy()

    def clean(value):
        if value is not None and not numeric(value):
            flags.add("invalid_values_omitted")
        return number(value)

    try:
        with fitdecode.FitReader(
            io.BytesIO(fit),
            check_crc=fitdecode.CrcCheck.RAISE,
            error_handling=fitdecode.ErrorHandling.RAISE,
        ) as reader:
            for frame in reader:
                if isinstance(frame, fitdecode.FitDefinitionMessage):
                    # A keyed projection cannot represent duplicate physical
                    # fields safely (including conflicting wire types).
                    numbers = [field.def_num for field in frame.field_defs]
                    if len(set(numbers)) != len(numbers):
                        raise BridgeError("invalid_fit")
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue
                timestamp_raw = clock.read(frame)
                values = _values(frame)
                if timestamp_raw is not None and "timestamp" in MESSAGE_FIELDS.get(frame.name, ()):
                    values["timestamp"] = timestamp_value(timestamp_raw)
                detail = project_message(frame, include_identity, timestamp_raw=timestamp_raw)
                unknown, raw_message = project_unknown(frame, blocked, include_identity)
                raw_privacy.observe(frame, unknown)
                if raw_message is not None:
                    raw_messages.append(raw_message)
                    if len(raw_messages) > MAX_RECORDS:
                        raise BridgeError("invalid_fit")
                if unknown and frame.name != "record":
                    detail = detail or {"message": frame.name, "fields": {}, "garmin_reported": {}}
                    detail["unknown"] = unknown
                if frame.name == "record":
                    balance = values.get("left_right_balance")
                    balance_value = (balance & 0x7F) if type(balance) is int else None
                    if balance_value is not None and balance_value > 100:
                        balance_value = None
                        flags.add("invalid_values_omitted")
                    cadence = clean(values.get("cadence"))
                    cadence_integers.append(cadence)
                    fractional = clean(values.get("fractional_cadence"))
                    if cadence is not None and fractional is not None:
                        cadence += fractional
                    row = {
                        "timestamp": timestamp(values.get("timestamp"), utc=True),
                        "heart_rate": clean(values.get("heart_rate")),
                        "power": clean(values.get("power")),
                        "cadence": cadence,
                        "speed": clean(_prefer(values, "enhanced_speed", "speed")),
                        "altitude": clean(_prefer(values, "enhanced_altitude", "altitude")),
                        "distance": clean(values.get("distance")),
                        "temperature": clean(values.get("temperature")),
                        # Standard enhanced field has an unambiguous breaths/min unit.
                        "respiration": clean(values.get("enhanced_respiration_rate")),
                        "balance_percent": balance_value,
                        "balance_side": ("right" if balance & 0x80 else "unknown")
                        if balance_value is not None
                        else None,
                    }
                    row.update(
                        {
                            key: detail["fields"].get(key, {}).get("value") if detail else None
                            for key in RECORD_UNITS
                        }
                    )
                    row.update(unknown)
                    records.append(row)
                    if len(records) > MAX_RECORDS:
                        raise BridgeError("invalid_fit")
                elif frame.name == "session":
                    if values.get("sport") != "cycling" or sessions:
                        raise BridgeError("invalid_fit")
                    sessions.append(values)
                    session_detail = detail
                elif frame.name == "lap":
                    laps.append(
                        {
                            "start": timestamp(values.get("start_time"), utc=True),
                            "end": timestamp(values.get("timestamp"), utc=True),
                            "duration": clean(values.get("total_timer_time")),
                            "distance": clean(values.get("total_distance")),
                            "fit": detail
                            or {"message": "lap", "fields": {}, "garmin_reported": {}},
                        }
                    )
                    if len(laps) > 10_000:
                        raise BridgeError("invalid_fit")
                elif frame.name == "event" and values.get("event") == "timer":
                    kind = values.get("event_type")
                    if kind in {"start", "stop", "stop_all", "stop_disable", "stop_disable_all"}:
                        events.append(
                            {
                                "timestamp": timestamp(values.get("timestamp"), utc=True),
                                "type": kind,
                            }
                        )
                    if len(events) > 100_000:
                        raise BridgeError("invalid_fit")
                if frame.name == "event" and detail:
                    detailed_events.append(detail)
                    if len(detailed_events) > 100_000:
                        raise BridgeError("invalid_fit")
                if frame.name in AUXILIARY_MESSAGES and detail:
                    messages.append(detail)
                    if len(messages) > 100_000:
                        raise BridgeError("invalid_fit")
                if frame.name == "device_info":
                    device_count += 1
    except BridgeError:
        raise
    except Exception:
        raise BridgeError("invalid_fit") from None
    if not sessions:
        raise BridgeError("invalid_fit")
    session = sessions[0]
    messages = filter_message_context(session_detail, laps, messages)
    for key in ("avg_speed", "max_speed"):
        session["enhanced_" + key] = _prefer(session, "enhanced_" + key, key)

    def metrics(fields, units, fallback):
        result = {}
        for key, (fit_name, _) in fields.items():
            value = clean(session.get(fit_name))
            source = "fit_session"
            if value is None:
                value, source = fallback[key], "garmin_activity"
            result[key] = {
                "value": value,
                "unit": units[key],
                "source": source if value is not None else None,
            }
        return result

    activity_time = dict(meta["activity_time"])
    activity_time["utc"] = activity_time["utc"] or timestamp(session.get("start_time"), utc=True)
    fill_local_time(activity_time, messages)
    if activity_time["timezone"] is None:
        flags.add("missing_timezone")
    if activity_time["utc"] is None:
        flags.add("missing_start_time")
    if not records:
        flags.add("missing_samples")
    times = [row["timestamp"] for row in records if row["timestamp"] is not None]
    if len(times) != len(records):
        flags.add("missing_timestamps")
    if any(b <= a for a, b in zip(times, times[1:])):
        flags.add("nonmonotonic_timestamps")
    coverage = {
        key: {
            "present": sum(row[key] is not None for row in records),
            "missing": sum(row[key] is None for row in records),
            "total": len(records),
        }
        for key in STREAM_UNITS
    }
    streams = {
        "schema_version": SCHEMA_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "provider": "garmin",
        "source_activity_id": meta["source_activity_id"],
        "source_digest": source_digest,
        "units": dict(STREAM_UNITS),
        "lap_units": {"start": "UTC", "end": "UTC", "duration": "s", "distance": "m"},
        "records": records,
        "laps": laps,
        "timer_events": events,
        "fit_fields_version": FIT_FIELDS_VERSION,
        "include_identity": include_identity,
        "session": session_detail,
        "events": detailed_events,
        "messages": messages,
        "raw_messages": raw_messages,
    }
    raw_privacy.filter(streams, blocked)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "provider": "garmin",
        "source_activity_id": meta["source_activity_id"],
        "source_digest": source_digest,
        "fit_sha256": sha256(fit),
        "acquired_at": acquired_at,
        "activity_time": activity_time,
        "sport": "cycling",
        "metrics": metrics(SUMMARY_FIELDS, METRIC_UNITS, meta["metrics"]),
        "garmin_reported": metrics(REPORTED_FIELDS, REPORTED_UNITS, meta["garmin_reported"]),
        "coverage": coverage,
        "quality_flags": sorted(flags),
        "sample_count": len(records),
        "lap_count": len(laps),
        "timer_event_count": len(events),
        "provenance": {
            "input": "local_fit_file" if local_import else "garmin_original_fit",
            "parser": "fitdecode/0.11.0",
            "device_messages": device_count,
            "unique_identifiers_retained": "explicit_opt_in" if include_identity else "none",
        },
    }
    summary["quality_flags"] = sorted(
        flags | _cadence_quality(summary["metrics"], session, records, cadence_integers)
    )
    if local_import:
        summary["provenance"].update(activity_id_origin="cli_argument", garmin_api_verified=False)
    return summary, streams


def _cadence_quality(metrics, session, records, integers):
    """Non-fatal consistency evidence, never a sensor diagnosis or value repair."""
    flags = set()
    avg, maximum = (metrics[key] for key in ("avg_cadence", "max_cadence"))
    if (
        avg["value"] is not None
        and maximum["value"] is not None
        and avg["source"] == maximum["source"]
        and avg["unit"] == maximum["unit"] == "rpm"
        and 0 <= maximum["value"] < avg["value"]
    ):
        flags.add("cadence_summary_max_below_avg")

    # FIT session timestamp is its explicit end, not start + active timer time.
    # API maxima may describe a different range and cannot substitute here.
    start = timestamp(session.get("start_time"), utc=True)
    end = timestamp(session.get("timestamp"), utc=True)
    if (
        not start
        or not end
        or maximum["source"] != "fit_session"
        or maximum["value"] is None
        or maximum["value"] < 0
    ):
        return flags
    start, end = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if end <= start:
        return flags
    for row, integer in zip(records, integers):
        if integer is None or integer < 0 or row["timestamp"] is None:
            continue
        observed = datetime.fromisoformat(row["timestamp"])
        # Compare like precision: the current session extraction is integer-only.
        # Missing samples cannot refute a maximum; one in-range exceedance can.
        if start <= observed <= end and integer > maximum["value"]:
            flags.add("cadence_record_max_exceeds_session")
            break
    return flags


def input_digest(meta, fit_hash, *, local_import=False, include_identity=False, legacy=False):
    value = {"provider": "garmin", "metadata": meta, "fit_sha256": fit_hash}
    if not legacy:
        value["fit_privacy_version"] = FIT_PRIVACY_VERSION
    if local_import:
        # Do not let a later API acquisition reuse an unverified local result,
        # even when the API happens to supply no optional metadata either.
        value["input"] = "local_fit_file"
    if include_identity:
        value["include_fit_identity"] = True
    return sha256(canonical(value))


def input_digest_matches(digest, meta, fit_hash, *, local_import=False, include_identity=False):
    """Validate historical provenance without making old revisions cache hits."""
    return any(
        digest
        == input_digest(
            meta,
            fit_hash,
            local_import=local_import,
            include_identity=include_identity,
            legacy=legacy,
        )
        for legacy in (False, True)
    )
