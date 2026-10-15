"""Pure, reviewed projection of metadata and original FIT bytes."""

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
from .storage import canonical

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
    values = {}
    for field in frame.fields:
        if field.field_def is not None and field.field_def.is_dev:
            continue
        if field.name == "enhanced_respiration_rate" and field.units != "Breaths/min":
            continue  # Don't relabel legacy components with ambiguous units.
        values[field.name] = field.raw_value if field.name == "left_right_balance" else field.value
    return values


def _prefer(values, first, second):
    value = values.get(first)
    return value if value is not None else values.get(second)


def normalize(fit: bytes, meta: dict, acquired_at: str, source_digest: str):
    flags = {
        "non_location_projection",
        "unreviewed_fields_omitted",
        "source_reported_not_independently_validated",
    }
    records, laps, events, sessions = [], [], [], []
    device_count = 0

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
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue
                values = _values(frame)
                if frame.name == "record":
                    balance = values.get("left_right_balance")
                    balance_value = (balance & 0x7F) if type(balance) is int else None
                    if balance_value is not None and balance_value > 100:
                        balance_value = None
                        flags.add("invalid_values_omitted")
                    cadence = clean(values.get("cadence"))
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
                    records.append(row)
                    if len(records) > MAX_RECORDS:
                        raise BridgeError("invalid_fit")
                elif frame.name == "session":
                    if values.get("sport") != "cycling" or sessions:
                        raise BridgeError("invalid_fit")
                    sessions.append(values)
                elif frame.name == "lap":
                    laps.append(
                        {
                            "start": timestamp(values.get("start_time"), utc=True),
                            "end": timestamp(values.get("timestamp"), utc=True),
                            "duration": clean(values.get("total_timer_time")),
                            "distance": clean(values.get("total_distance")),
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
                elif frame.name == "device_info":
                    device_count += 1  # No serial numbers, device names or IDs.
    except BridgeError:
        raise
    except Exception:
        raise BridgeError("invalid_fit") from None
    if not sessions:
        raise BridgeError("invalid_fit")
    session = sessions[0]
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
    }
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
            "input": "garmin_original_fit",
            "parser": "fitdecode/0.11.0",
            "device_messages": device_count,
            "unique_identifiers_retained": "none",
        },
    }
    return summary, streams


def input_digest(meta, fit_hash):
    return sha256(canonical({"provider": "garmin", "metadata": meta, "fit_sha256": fit_hash}))
