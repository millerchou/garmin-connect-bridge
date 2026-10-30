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
    {"identity_message", "possible_semicircle", "possible_identity", "non_integer_raw"}
)
RAW_BASES = frozenset({0, 1, 2, 0x83, 0x84, 0x86, 0xA, 0x8B, 0x8C, 0x8E, 0x8F, 0x90})


def raw_value(value):
    def scalar(x):
        return x is None or type(x) is int and -(2**63) <= x < 2**64

    return scalar(value) or (
        type(value) is list and 1 <= len(value) <= 255 and all(scalar(x) for x in value)
    )


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
        unknown_number(k) is not None and unknown_number(k) not in known and raw_value(v)
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
            for k, v in value["fields"].items()
        )
    )


def project_unknown(frame, blocked, include_identity=False):
    """Return (unknown object, raw message); counters contain no input values."""
    number = frame.global_mesg_num
    private = number == 79 or number == 3 and not include_identity
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
            elif (
                base in {2, 0xA}
                and type(value) is list
                and len(value) >= 4
                and all(x is None or x == 0 or 32 <= x <= 126 for x in value)
            ):
                reason = "possible_identity"
            else:
                fields[str(index) if generic else f"unknown_{index}"] = value
                continue
        denied.add((number, index, reason))
    blocked.update(denied)  # one count per physical message row, not array element
    return ({} if generic else fields), (
        {"mesg_num": number, "fields": fields} if generic else None
    )


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
