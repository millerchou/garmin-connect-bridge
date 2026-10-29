"""Typed FIT detail projection and validation; no implicit Profile permissions."""

from datetime import datetime, timedelta, timezone
from collections import Counter

from .errors import BridgeError
from .fit_fields import (
    ARRAY_LIMITS,
    EVENT_NAMES,
    FIELDS,
    SPORT_LABELS,
    STROKES_MESSAGES,
    event_payload_allowed,
)
from .fit_rules import TIMESTAMP_MESSAGES

EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)


def timestamp_value(value, *, local=False):
    if type(value) is not int or not 0x10000000 <= value < 0xFFFFFFFF:
        return None
    stamp = EPOCH + timedelta(seconds=value)
    return (
        stamp.replace(tzinfo=None).isoformat()
        if local
        else stamp.isoformat().replace("+00:00", "Z")
    )


class TimestampContext:
    """Do not trust fitdecode's accumulator: even unknown/developer fields seed it."""

    def __init__(self):
        self.anchor = None

    def read(self, frame):
        physical = [
            f
            for f in frame.fields
            if f.field_def is not None and not f.field_def.is_dev and f.field_def.def_num == 253
        ]
        if frame.time_offset is not None:
            if self.anchor is None or physical:
                raise BridgeError("invalid_fit")
            offset = frame.time_offset
            self.anchor = (self.anchor & ~31) + offset + (32 if offset < self.anchor & 31 else 0)
            if self.anchor >= 0xFFFFFFFF:
                raise BridgeError("invalid_fit")
            return self.anchor
        timestamp = None
        for field in physical:
            definition = field.field_def
            if frame.global_mesg_num not in TIMESTAMP_MESSAGES or (
                definition.base_type.identifier,
                definition.size,
            ) != (0x86, 4):
                self.anchor = None
            else:
                timestamp = self.anchor = field.raw_value
        return timestamp


def text_value(value):
    return type(value) is str and len(value) <= 64 and value.isprintable()


def field_value(raw, spec):
    from .contract import numeric

    def scalar(value):
        if value is None:
            return None
        if spec.kind in {"text", "label"}:
            return (
                value
                if (text_value(value) and (spec.kind == "text" or value in SPORT_LABELS))
                else None
            )
        if not numeric(value):
            return None
        if spec.kind in {"utc", "local"}:
            return timestamp_value(value, local=spec.kind == "local")
        return value / spec.scale - spec.offset if spec.scale != 1 or spec.offset else value

    if spec.shape != "scalar":
        values = list(raw) if isinstance(raw, (tuple, list)) else [raw]
        low, high = ARRAY_LIMITS[spec.shape]
        return [scalar(v) for v in values] if low <= len(values) <= high else None
    return scalar(raw)


def specs_for(message):
    specs = dict(FIELDS.get(message, {}))
    if message in STROKES_MESSAGES:
        specs["total_strokes"] = specs["total_cycles"]
    if message == "event":
        specs["gear_change_data"] = specs["data"]
    return specs


def project_message(frame, include_identity=False, *, timestamp_raw=None):
    specs = FIELDS.get(frame.name)
    if specs is None or frame.name == "user_profile" and not include_identity:
        return None
    by_number = {s.number: (n, s) for n, s in specs.items() if include_identity or not s.identity}
    fields, reported = {}, {}
    for field in frame.fields:
        definition = field.field_def
        # Only physical standard fields: component expansion cannot create values
        # from unknown/developer bytes. Check permission before any value access.
        if definition is None or definition.is_dev:
            continue
        pair = by_number.get(getattr(definition, "def_num", None))
        if pair is None:
            continue
        name, spec = pair
        if definition.base_type.identifier != spec.base:
            continue
        size = definition.size
        width = definition.base_type.size
        low, high = ARRAY_LIMITS.get(spec.shape, (1, 1))
        if spec.kind in {"text", "label"}:
            low, high = 1, 65
        if size % width or not low <= size // width <= high:
            continue
        value = field_value(field.raw_value, spec)
        # Reject unreviewed free text entirely, rather than retaining its presence.
        if spec.kind in {"text", "label"} and value is None:
            continue
        target = reported if spec.reported else fields
        target[name] = {"value": value, "unit": spec.unit}
    # Header timestamps have no physical field_def in fitdecode. Only the
    # independently checked clock may supply them, never arbitrary components.
    if timestamp_raw is not None and "timestamp" in specs:
        fields["timestamp"] = {"value": timestamp_value(timestamp_raw), "unit": "UTC"}
    if frame.name in {"session", "lap"}:
        for name in ("avg_speed", "max_speed"):
            enhanced = fields.get("enhanced_" + name)
            if enhanced is not None and enhanced["value"] is not None:
                fields[name] = dict(enhanced)
    if frame.name in STROKES_MESSAGES and get(fields, "sport") == 2 and "total_cycles" in fields:
        fields["total_strokes"] = {**fields.pop("total_cycles"), "unit": "strokes"}
    if frame.name == "event":
        event, kind = get(fields, "event"), get(fields, "event_type")
        if event not in EVENT_NAMES or kind is not None and not 0 <= kind <= 9:
            return None
        for name in ("data", "data16"):
            if name in fields and not event_payload_allowed(
                event, kind, get(fields, name), short=name == "data16"
            ):
                del fields[name]
        for name in ("front_gear", "front_gear_num", "rear_gear", "rear_gear_num"):
            if event not in (42, 43) or kind is None:
                fields.pop(name, None)
        if event in (42, 43) and "data" in fields:
            fields["gear_change_data"] = dict(fields["data"])
            data = get(fields, "data")
            if type(data) is int:
                for shift, name in enumerate(
                    ("rear_gear_num", "rear_gear", "front_gear_num", "front_gear")
                ):
                    component = (data >> (8 * shift)) & 255
                    fields.setdefault(name, {"value": component or None, "unit": specs[name].unit})
    if not fields and not reported and frame.name != "session":
        return None
    return {"message": frame.name, "fields": fields, "garmin_reported": reported}


def get(fields, name):
    return fields.get(name, {}).get("value")


def filter_message_context(session, laps, messages):
    """Zone references require a unique surviving session/lap index."""
    targets = Counter()
    for number, row in [(18, session), *((19, lap["fit"]) for lap in laps)]:
        index = get(row["fields"], "message_index")
        if type(index) is int and not index & 0x7000:
            targets[(number, index & 0xFFF)] += 1
    zones = sum(row["message"] == "zones_target" for row in messages)
    result = []
    for row in messages:
        fields = row["fields"]
        if row["message"] == "zones_target" and zones != 1:
            continue
        if row["message"] == "time_in_zone":
            ref, index = get(fields, "reference_mesg"), get(fields, "reference_index")
            if type(index) is not int or not 0 <= index < 4096 or targets[(ref, index)] != 1:
                continue
        result.append(row)
    return result


def valid_message(value, include_identity=False, allowed=None):
    from . import contract as c

    if type(value) is not dict or value.keys() != {"message", "fields", "garmin_reported"}:
        return False
    message = value["message"]
    if (
        type(message) is not str
        or message not in FIELDS
        or allowed is not None
        and message not in allowed
    ):
        return False
    if message == "user_profile" and not include_identity:
        return False
    specs = specs_for(message)
    for group in ("fields", "garmin_reported"):
        fields = value[group]
        if type(fields) is not dict or not fields.keys() <= specs.keys():
            return False
        for name, quantity in fields.items():
            spec = specs[name]
            if (
                spec.identity
                and not include_identity
                or spec.reported != (group == "garmin_reported")
            ):
                return False
            unit = "strokes" if name == "total_strokes" else spec.unit
            if not c.obj({"unit": c.literal(unit), "value": lambda _: True})(quantity):
                return False
            item = quantity["value"]
            check = c.numeric
            if spec.kind in {"utc", "local"}:

                def check(x):
                    return c.iso_time(x, spec.kind == "utc") and (
                        spec.kind != "local" or datetime.fromisoformat(x).tzinfo is None
                    )
            elif spec.kind == "text":
                check = text_value
            elif spec.kind == "label":

                def check(x):
                    return type(x) is str and x in SPORT_LABELS

            if spec.shape != "scalar":
                low, high = ARRAY_LIMITS[spec.shape]
                if not (
                    item is None
                    or type(item) is list
                    and low <= len(item) <= high
                    and all(c.nullable(check)(x) for x in item)
                ):
                    return False
            elif not c.nullable(check)(item):
                return False
    fields = value["fields"]
    if "total_strokes" in fields and get(fields, "sport") != 2:
        return False
    if message == "event":
        event, kind = get(fields, "event"), get(fields, "event_type")
        if (
            type(event) is not int
            or event not in EVENT_NAMES
            or kind is not None
            and (type(kind) is not int or not 0 <= kind <= 9)
        ):
            return False
        for name in ("data", "data16"):
            if name in fields and not event_payload_allowed(
                event, kind, get(fields, name), short=name == "data16"
            ):
                return False
        if any(
            n in fields
            for n in (
                "gear_change_data",
                "front_gear",
                "front_gear_num",
                "rear_gear",
                "rear_gear_num",
            )
        ):
            if event not in (42, 43) or kind is None:
                return False
        if "gear_change_data" in fields and fields["gear_change_data"] != fields.get("data"):
            return False
    return True


def fill_local_time(activity_time, messages):
    """Use only unambiguous file evidence; utc_offset is a system-clock correction."""
    candidates = set()
    for row in messages:
        fields = row["fields"]
        if row["message"] == "activity":
            utc, local = get(fields, "timestamp"), get(fields, "local_timestamp")
            if utc and local:
                delta = (
                    datetime.fromisoformat(local) - datetime.fromisoformat(utc).replace(tzinfo=None)
                ).total_seconds()
                if abs(delta) <= 14 * 3600:
                    candidates.add(delta)
        elif row["message"] == "device_settings":
            active = get(fields, "active_time_zone")
            for name in ("time_zone_offset", "time_offset"):
                offsets = get(fields, name)
                index = (
                    active
                    if type(active) is int
                    else 0
                    if "active_time_zone" not in fields and offsets and len(offsets) == 1
                    else None
                )
                if offsets and index is not None and 0 <= index < len(offsets):
                    delta = offsets[index]
                    if delta is None:
                        continue
                    if name == "time_zone_offset":
                        delta *= 3600
                    else:
                        utc_correction = get(fields, "utc_offset")
                        if utc_correction is None:
                            continue
                        delta = (delta - utc_correction) % 2**32
                        if delta >= 2**31:
                            delta -= 2**32
                    if abs(delta) <= 14 * 3600:
                        candidates.add(delta)
    if (
        len(candidates) == 1
        and activity_time["utc"]
        and activity_time["local"] is None
        and activity_time["utc_offset_seconds"] is None
    ):
        delta = candidates.pop()
        activity_time["utc_offset_seconds"] = delta
        activity_time["local"] = (
            datetime.fromisoformat(activity_time["utc"]).replace(tzinfo=None)
            + timedelta(seconds=delta)
        ).isoformat()
