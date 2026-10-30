"""Conservative, raw-only interception for undocumented physical FIT fields.

All undocumented sint32 fields are withheld: every valid sint32 can describe a
semicircle coordinate, including zero and small values near the equator/meridian.
This also blocks position-correlated sint32 streams without persisting positions
or running a statistical test that could miss short/constant tracks. Strings,
byte blobs and text-like uint8 arrays are withheld as possible device identities.
Developer fields remain excluded. No scale, offset or semantic guess is applied.

High-frequency unknown messages use their own ordered samples partition, exported
as a dedicated raw_messages table. Timestamp alignment would lose duplicate or
missing timestamps and incorrectly imply a one-to-one relationship with records.
"""

import re
from collections import Counter
from datetime import datetime
from itertools import chain

from fitdecode import profile

from .errors import BridgeError
from .fit_fields import FIELDS, MESSAGE_NUMBERS

UNKNOWN_MESSAGES = frozenset(
    {
        "record",
        "session",
        "lap",
        "split",
        "split_summary",
        "sport",
        "training_settings",
        "device_settings",
        "zones_target",
        "device_info",
        "segment_lap",
        "timestamp_correlation",
    }
)
KNOWN_MESSAGES = frozenset(profile.MESSAGE_TYPES) | {79} | frozenset(MESSAGE_NUMBERS.values())
KNOWN_FIELDS = {
    message: frozenset(profile.MESSAGE_TYPES[MESSAGE_NUMBERS[message]].fields)
    | {spec.number for spec in FIELDS[message].values()}
    for message in UNKNOWN_MESSAGES
}
REPORT_PATH = "reports/fit-blocked-fields.json"
REASONS = frozenset(
    {
        "identity_message",
        "possible_semicircle",
        "possible_identity",
        "non_integer_raw",
        "position_match",
        "position_correlated",
        "identity_match",
        "suspected_identity",
    }
)
RAW_BASES = frozenset({0, 1, 2, 0x83, 0x84, 0x86, 0xA, 0x8B, 0x8C, 0x8E, 0x8F, 0x90})
# Small settings, enums and short device numbers must not cause file-wide matches.
IDENTITY_MATCH_MIN = 2**16
SUSPECTED_IDENTITY_MIN = 2**24
IDENTITY_DISTINCT_LIMIT = 3
# Cache discriminator independent of the public normalizer version. Restoring
# version 1.4.0 must not reuse artifacts produced by its earlier privacy rules.
FIT_PRIVACY_VERSION = "1"
# These Profile lookups only identify data to withhold, never grant permissions.
POSITION_FIELDS = {
    number: frozenset(
        index
        for index, field in message.fields.items()
        if re.search(r"(?:^|_)(?:lat|long|latitude|longitude)$", field.name)
    )
    for number, message in profile.MESSAGE_TYPES.items()
}
IDENTITY_FIELDS = {
    number: frozenset(
        index
        for index, field in message.fields.items()
        if field.name
        in {
            "serial_number",
            "ant_device_number",
            "device_number",
            "device_id",
            "local_id",
            "global_id",
        }
    )
    for number, message in profile.MESSAGE_TYPES.items()
}


def raw_value(value):
    def scalar(x):
        return x is None or type(x) is int and -(2**63) <= x < 2**64

    return scalar(value) or (
        type(value) is list and 1 <= len(value) <= 255 and all(scalar(x) for x in value)
    )


def text_array(value):
    """ASCII/UTF-8 text can use any integer wire type, not just uint8."""
    if type(value) is not list:
        return False
    if sum(type(x) is int and 32 <= x <= 126 for x in value) >= 4 and all(
        x is None or x == 0 or 32 <= x <= 126 for x in value
    ):
        return True
    if not all(x is None or 0 <= x <= 255 for x in value):
        return False
    try:
        text = bytes(x or 0 for x in value).strip(b"\0").decode("utf-8")
    except UnicodeError:
        return False
    return len(text) >= 4 and text.isprintable()


def undocumented_message(number):
    return type(number) is int and 0 <= number < 65535 and number not in KNOWN_MESSAGES


def unknown_number(key):
    if type(key) is str and re.fullmatch(r"unknown_(0|[1-9][0-9]{0,2})", key):
        number = int(key[8:])
        if number <= 255:
            return number
    return None


def valid_unknown(value, message):
    if type(value) is not dict or message not in UNKNOWN_MESSAGES:
        return False
    known = KNOWN_FIELDS[message]
    return all(
        unknown_number(k) is not None
        and unknown_number(k) not in known
        and raw_value(v)
        and not text_array(v)
        for k, v in value.items()
    )


def valid_raw_message(value):
    return (
        type(value) is dict
        and value.keys() == {"mesg_num", "fields"}
        and undocumented_message(value["mesg_num"])
        and type(value["fields"]) is dict
        and all(
            type(k) is str
            and re.fullmatch(r"0|[1-9][0-9]{0,2}", k)
            and int(k) <= 255
            and raw_value(v)
            and not text_array(v)
            for k, v in value["fields"].items()
        )
    )


def project_unknown(frame, blocked, include_identity=False):
    """Return (unknown object, raw message); counters contain no input values."""
    number = frame.global_mesg_num
    private = number in {3, 79}
    generic = undocumented_message(number)
    eligible = frame.name in UNKNOWN_MESSAGES
    if not private and not generic and not eligible:
        return {}, None
    fields = {}
    denied = set()
    for field in frame.fields:
        definition = field.field_def
        if definition is None or definition.is_dev:
            continue
        index = definition.def_num
        if private:
            if (
                number == 3
                and include_identity
                and index in {spec.number for spec in FIELDS["user_profile"].values()}
            ):
                continue
            denied.add((number, index, "identity_message"))
            continue
        if not generic and not (eligible and field.field is None):
            continue
        # A newer reviewed literal must not also enter the raw unknown object.
        if not generic and index in KNOWN_FIELDS[frame.name]:
            continue
        base = definition.base_type.identifier
        if base == 0x85:
            reason = "possible_semicircle"
        elif base in {7, 13}:
            reason = "possible_identity"
        elif base not in RAW_BASES:
            reason = "non_integer_raw"
        else:
            value = field.raw_value
            if isinstance(value, tuple):
                value = list(value)
            if not raw_value(value):
                reason = "non_integer_raw"
            elif text_array(value):
                reason = "possible_identity"
            else:
                fields[str(index) if generic else f"unknown_{index}"] = value
                continue
        denied.add((number, index, reason))
    blocked.update(denied)  # one count per physical message row, not array element
    return ({} if generic else fields), (
        {"mesg_num": number, "fields": fields} if generic else None
    )


def raw_objects(streams):
    """Mutable raw containers, with no copies of the high-frequency rows."""
    for row in streams["records"]:
        yield 20, row, "unknown_"
    for detail in chain(
        (streams["session"],),
        (lap["fit"] for lap in streams["laps"]),
        streams["messages"],
    ):
        if "unknown" in detail:
            yield MESSAGE_NUMBERS[detail["message"]], detail["unknown"], "unknown_"
    for row in streams["raw_messages"]:
        yield row["mesg_num"], row["fields"], ""


class RawIdentityCheck:
    """Bounded per-slot evidence shared by normalization and artifact validation."""

    def __init__(self):
        self.distinct = {}
        self.timestamps = set()

    def observe_timestamp(self, value):
        from .fit_details import EPOCH

        if value is None:
            return
        delta = datetime.fromisoformat(value) - EPOCH
        seconds = delta.days * 86400 + delta.seconds
        if not delta.microseconds and 0x10000000 <= seconds < 0xFFFFFFFF:
            self.timestamps.add(seconds)

    def observe_detail(self, detail):
        for name, spec in FIELDS[detail["message"]].items():
            if spec.kind == "utc":
                group = "garmin_reported" if spec.reported else "fields"
                self.observe_timestamp(detail[group].get(name, {}).get("value"))

    def observe_raw(self, number, fields, prefix=""):
        for key, value in fields.items():
            if not key.startswith(prefix):
                continue
            values = value if type(value) is list else [value]
            for slot, item in enumerate(values):
                if type(item) is int:
                    seen = self.distinct.setdefault((number, int(key[len(prefix) :]), slot), set())
                    if len(seen) <= IDENTITY_DISTINCT_LIMIT:
                        seen.add(item)

    def blocked_fields(self):
        return {
            (number, field): "suspected_identity"
            for (number, field, _), seen in self.distinct.items()
            if len(seen) <= IDENTITY_DISTINCT_LIMIT
            and any(abs(value) >= SUSPECTED_IDENTITY_MIN for value in seen)
            # Proximity alone also exempts constant IDs. Require variation and
            # exact reviewed time evidence for every value in the slot. Sparse
            # or constant unknown times are intentionally withheld as ambiguous.
            and not (len(seen) >= 3 and seen <= self.timestamps)
        }


def suspected_identity_fields(streams):
    check = RawIdentityCheck()
    for row in streams["records"]:
        check.observe_timestamp(row["timestamp"])
    for detail in chain(
        (streams["session"],),
        (lap["fit"] for lap in streams["laps"]),
        streams["events"],
        streams["messages"],
    ):
        check.observe_detail(detail)
    for number, fields, prefix in raw_objects(streams):
        check.observe_raw(number, fields, prefix)
    return check.blocked_fields()


class RawPrivacy:
    """Transient cross-field checks; never serialize coordinates or statistics.

    Block exact copies of observed locations/identifiers across all integer types,
    including unsigned semicircle representations. For co-observed record fields,
    also block near-perfect linear position correlations (three or more pairs).
    Entire field families are withheld, including earlier/unpaired rows. The
    accumulator uses integer sums, so uint64 values cannot lose precision.
    """

    def __init__(self):
        self.positions = set()
        self.identities = set()
        self.correlations = {}

    def observe(self, frame, unknown):
        position_fields = POSITION_FIELDS.get(frame.global_mesg_num, ())
        identity_fields = IDENTITY_FIELDS.get(frame.global_mesg_num, ())
        private = frame.global_mesg_num in {3, 79}
        if not position_fields and not identity_fields and not private:
            return
        coordinates = []
        for field in frame.fields:
            definition = field.field_def
            if definition is None or definition.is_dev:
                continue
            location = definition.def_num in position_fields
            # Known profile settings (for example seconds into the day) are not
            # identifiers, even when their magnitude exceeds the match threshold.
            identity = definition.def_num in identity_fields or private and field.field is None
            if not location and not identity:
                continue
            value = field.raw_value
            values = value if isinstance(value, tuple) else (value,)
            for value in values:
                if type(value) is not int:
                    continue
                if location:
                    self.positions.add(value)
                    if -(2**31) <= value < 2**32:
                        self.positions.update((value & 0xFFFFFFFF, (value + 2**31) % 2**32 - 2**31))
                    if frame.global_mesg_num == 20:
                        coordinates.append((definition.def_num, value))
                if identity and abs(value) >= IDENTITY_MATCH_MIN:
                    self.identities.add(value)
        # Other messages have no reviewed alignment to records. Exact matching
        # above is file-wide; never invent timestamps or joins for raw messages.
        if not coordinates:
            return
        for key, value in unknown.items():
            values = value if type(value) is list else [value]
            for slot, value in enumerate(values):
                if type(value) is not int:
                    continue
                for position, coordinate in coordinates:
                    key_pair = int(key[8:]), slot, position
                    stats = self.correlations.setdefault(key_pair, [0, 0, 0, 0, 0, 0])
                    stats[0] += 1
                    stats[1] += value
                    stats[2] += coordinate
                    stats[3] += value * value
                    stats[4] += coordinate * coordinate
                    stats[5] += value * coordinate

    def filter(self, streams, blocked):
        reasons = {}
        for (field, _, _), (n, x, y, xx, yy, xy) in self.correlations.items():
            variance = (n * xx - x * x) * (n * yy - y * y)
            if n >= 3 and variance > 0 and (n * xy - x * y) ** 2 * 10000 >= variance * 9999:
                reasons[(20, field)] = "position_correlated"
        for number, fields, prefix in raw_objects(streams):
            for key, value in fields.items():
                if not key.startswith(prefix):
                    continue
                values = value if type(value) is list else [value]
                field = number, int(key[len(prefix) :])
                if any(v in self.identities for v in values):
                    reasons[field] = "identity_match"
                elif any(v in self.positions for v in values):
                    reasons.setdefault(field, "position_match")
        for field, reason in suspected_identity_fields(streams).items():
            reasons.setdefault(field, reason)
        for number, fields, prefix in raw_objects(streams):
            for key in list(fields):
                if key.startswith(prefix) and (
                    reason := reasons.get((number, int(key[len(prefix) :])))
                ):
                    del fields[key]
                    blocked[(number, int(key[len(prefix) :]), reason)] += 1
        # Keep source order among surviving rows, with no empty storage/export rows.
        streams["raw_messages"][:] = [row for row in streams["raw_messages"] if row["fields"]]


def validate_report(value):
    if type(value) is not list or len(value) > 100_000:
        raise BridgeError("privacy_failed")
    seen = set()
    for row in value:
        if not (
            type(row) is dict
            and row.keys() == {"field", "reason", "row_count"}
            and type(row["field"]) is str
            and re.fullmatch(r"(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,2})", row["field"])
            and int(row["field"].split(".")[0]) < 65535
            and int(row["field"].split(".")[1]) <= 255
            and type(row["reason"]) is str
            and row["reason"] in REASONS
            and type(row["row_count"]) is int
            and 0 < row["row_count"] <= 10**12
        ):
            raise BridgeError("privacy_failed")
        key = row["field"], row["reason"]
        if key in seen:
            raise BridgeError("privacy_failed")
        seen.add(key)
    return value


def read_report(store):
    value = store.read_json(REPORT_PATH)
    return validate_report([] if value is None else value)


def save_report(store, blocked):
    """Cumulative normalization-attempt counts; cache hits do not increment them."""
    if not blocked:
        return
    counts = Counter({(r["field"], r["reason"]): r["row_count"] for r in read_report(store)})
    for (message, field, reason), count in blocked.items():
        counts[(f"{message}.{field}", reason)] += count
    rows = [{"field": f, "reason": r, "row_count": n} for (f, r), n in sorted(counts.items())]
    store.write_json(REPORT_PATH, validate_report(rows))
