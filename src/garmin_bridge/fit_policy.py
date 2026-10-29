"""Physical allowlist plus explicit context/reference checks, never name matching."""

from collections import Counter

from .errors import BridgeError
from .fit_fields import SPORT_LABELS, event_payload_allowed
from .fit_rules import (
    DEFERRED,
    ENUM_VALUES,
    EVENTS,
    EXPLICIT_REMOVALS,
    LEGACY_EVENTS,
    LEGACY_REMOVALS,
    RULE_VERSIONS,
    RULES_VERSION,
)
from .fit_wire import Message, WIDTHS, unsigned

REASONS = frozenset(
    {
        "approved",
        "explicit_private_field",
        "unreviewed_message",
        "unreviewed_field",
        "developer_field",
        "length_context",
        "wire_type",
        "array_shape",
        "event_context",
        "event_payload",
        "dependency_removed",
        "ambiguous_context",
        "sport_context",
        "enum_context",
    }
)


def field_reason(message, f, rules_version=RULES_VERSION):
    rules = RULE_VERSIONS[rules_version]
    removals = LEGACY_REMOVALS if rules_version == "1.0.0" else EXPLICIT_REMOVALS
    if f.developer is not None:
        return "developer_field"
    if f.number in removals.get(message.number, ()):
        return "explicit_private_field"
    if (message.number, f.number) in DEFERRED:
        return "length_context"
    if message.number not in rules:
        return "unreviewed_message"
    rule = rules[message.number].get(f.number)
    if rule is None:
        return "unreviewed_field"
    if f.base != rule.base:
        return "wire_type"
    width = WIDTHS[f.base]
    if f.size % width or not rule.minimum <= f.size // width <= rule.maximum:
        return "array_shape"
    return "approved"


def index_of(message):
    value = unsigned(message, 254, 0x84, 2)
    # FIT message_index: selected flag 0x8000, index 0x0FFF; no other flag accepted.
    return value & 0xFFF if value is not None and not value & 0x7000 else None


def project(document, rules_version=RULES_VERSION):
    """Return approved physical records and a value-free, bounded audit inventory."""
    messages = document.messages
    file_ids = [m for m in messages if m.number == 0]
    sessions = [m for m in messages if m.number == 18]
    if (
        len(file_ids) != 1
        or not messages
        or messages[0] is not file_ids[0]
        or unsigned(file_ids[0], 0, 0, 1) != 4
        or len(sessions) != 1
        or unsigned(sessions[0], 5, 0, 1) != 2
    ):
        raise BridgeError("sanitize_context")
    for message in messages:
        if message.number == 19 and unsigned(message, 25, 0, 1) not in (None, 2):
            raise BridgeError("sanitize_context")
    index_counts = Counter((m.number, index_of(m)) for m in messages if m.number in (8, 9, 18, 19))
    valid_indices = {
        key for key, count in index_counts.items() if key[1] is not None and count == 1
    }
    targets = sum(m.number == 7 for m in messages)
    inventory = Counter()
    kept = []
    removed_messages = 0
    expanded_timestamps = 0

    def count(message, f, reason, definition=False):
        action = "retained" if reason == "approved" else "removed"
        pending = reason in {
            "unreviewed_message",
            "unreviewed_field",
            "developer_field",
            "length_context",
            "wire_type",
            "array_shape",
            "event_context",
            "event_payload",
            "ambiguous_context",
            "enum_context",
        }
        # Developer index is deliberately not reported; only its field number.
        key = (message.number, f.number, f.base, f.developer is not None, action, reason, pending)
        inventory[(key, "definitions" if definition else "occurrences")] += 1
        if len(inventory) > 16_384:
            raise BridgeError("invalid_fit")

    # Even unused definitions are audited. 'eligible' never implies data survived
    # its context. They are not copied to the output unless an approved record uses them.
    for definition in document.definitions:
        for f in definition.fields:
            count(definition, f, field_reason(definition, f, rules_version), definition=True)

    for message in messages:
        overrides = {}
        whole = None
        mid = message.number
        if mid == 7 and targets != 1:
            whole = "ambiguous_context"
        if mid in (8, 9):
            idx = index_of(message)
            if (mid, idx) not in valid_indices or idx is None or idx >= 32:
                whole = "dependency_removed"
            if targets > 1:
                whole = "ambiguous_context"
        for (enum_message, number), allowed in ENUM_VALUES.items():
            if mid == enum_message:
                value = unsigned(message, number, 0, 1)
                if value is not None and value not in allowed:
                    overrides[number] = "enum_context"
        context_event = {18: (0, 1, 8), 19: (0, 1, 9), 34: (3, 4, 26)}.get(mid)
        if context_event:
            event_field, type_field, expected = context_event
            if unsigned(message, event_field, 0, 1) not in (None, expected):
                overrides.update({event_field: "event_context", type_field: "event_context"})
            elif unsigned(message, type_field, 0, 1) not in (*range(10), None):
                overrides[type_field] = "enum_context"
        if mid == 12 and unsigned(message, 0, 0, 1) != 2:
            whole = "sport_context"
        if mid in (18, 19) and (mid, index_of(message)) not in valid_indices:
            # Preserve an explicitly invalid index. It is never a reference target.
            raw_index = unsigned(message, 254, 0x84, 2)
            if raw_index is not None:
                overrides[254] = "dependency_removed"
        if mid == 18:
            first = unsigned(message, 25, 0x84, 2)
            number = unsigned(message, 26, 0x84, 2)
            lap_count = sum(m.number == 19 for m in messages)
            if number == 0 and lap_count == 0:
                # A real zero count is useful. There is no first lap to point at.
                if first is not None:
                    overrides[25] = "dependency_removed"
            elif (first is not None or number is not None) and (
                first is None
                or number is None
                or number > 4096
                or first + number > 4096
                or any((19, n) not in valid_indices for n in range(first, first + number))
                or number != lap_count
            ):
                overrides.update({25: "dependency_removed", 26: "dependency_removed"})
        if mid == 34:
            if unsigned(message, 1, 0x84, 2) not in (None, 1):
                overrides[1] = "dependency_removed"
        if mid == 216:
            target = unsigned(message, 0, 0x84, 2)
            idx = unsigned(message, 1, 0x84, 2)
            # References use plain index bits; no selected/reserved flags.
            if (
                target not in (18, 19)
                or idx is None
                or idx >= 4096
                or (target, idx) not in valid_indices
            ):
                whole = "dependency_removed"
        if mid == 21:
            event = unsigned(message, 0, 0, 1)
            kind = unsigned(message, 1, 0, 1)
            allowed_events = LEGACY_EVENTS if rules_version == "1.0.0" else EVENTS
            if event not in allowed_events or kind not in (*range(10), None):
                whole = "event_context"
            else:
                for number, base, size in ((3, 0x86, 4), (2, 0x84, 2)):
                    if not event_payload_allowed(
                        event, kind, unsigned(message, number, base, size), short=number == 2
                    ):
                        overrides[number] = "event_payload"
                if event not in (42, 43) or kind is None:
                    overrides.update({n: "event_payload" for n in (9, 10, 11, 12)})
                if rules_version == "1.0.0":
                    data = unsigned(message, 3, 0x86, 4)
                    if not (
                        kind is not None
                        and (
                            event == 0
                            and data in (None, 0, 1, 2)
                            or event == 44
                            and data in (None, 0, 1, 2, 3)
                            or event in (42, 43)
                        )
                    ):
                        overrides[3] = "event_payload"
        selected = []
        for f in message.fields:
            reason = field_reason(message, f, rules_version)
            if reason == "approved":
                reason = whole or overrides.get(f.number, "approved")
            if reason == "approved" and f.base == 7:
                # A string allowlist alone cannot exclude names/locations. Permit
                # exact reviewed sport labels and zero padding, no hidden suffix.
                approved = {label.encode("ascii") for label in SPORT_LABELS}
                if f.raw.rstrip(b"\0") not in approved:
                    reason = "explicit_private_field"
            count(message, f, reason)
            if reason == "approved":
                selected.append(f)
        if selected:
            kept.append(Message(mid, message.endian, tuple(selected)))
            if message.compressed and any(f.number == 253 for f in selected):
                expanded_timestamps += 1
        else:
            removed_messages += 1
    rows = []
    for key in sorted({key for key, _ in inventory}):
        mid, fid, base, developer, action, reason, pending = key
        rows.append(
            {
                "message": mid,
                "field": fid,
                "base_type": base,
                "developer": developer,
                "action": action,
                "reason": reason,
                "pending_review": pending,
                "definitions": inventory[(key, "definitions")],
                "occurrences": inventory[(key, "occurrences")],
            }
        )
    if not any(m.number == 20 for m in kept):
        raise BridgeError("sanitize_context")
    return kept, {
        "fields": rows,
        "input_messages": len(messages),
        "output_messages": len(kept),
        "removed_messages": removed_messages,
        "compressed_timestamps_expanded": expanded_timestamps,
    }


def assert_approved(document, rules_version=RULES_VERSION):
    """Independent verification also traverses unused definitions, not only values."""
    for message in document.definitions + document.messages:
        if not message.fields or any(
            field_reason(message, f, rules_version) != "approved" for f in message.fields
        ):
            raise BridgeError("sanitize_verification")
    projected, _ = project(document, rules_version)
    if len(projected) != len(document.messages):
        raise BridgeError("sanitize_verification")
    for before, after in zip(document.messages, projected, strict=True):
        if before != after:
            raise BridgeError("sanitize_verification")
