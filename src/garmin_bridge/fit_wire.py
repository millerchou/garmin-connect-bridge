"""Bounded FIT framing, without Profile expansion or decoding private values.

Only the sanitizer's closed policy can select fields for transcription. Unknown
payloads are opaque byte slices, never names, strings or exception diagnostics.
This is not a general FIT encoder: approved physical fields keep their original
endianness and bytes; only record/definition headers and CRCs are rebuilt.
"""

from dataclasses import dataclass, field
import struct

from fitdecode.utils import compute_crc

from .errors import BridgeError
from .fit_rules import RULE_VERSIONS, RULES_VERSION
from .normalize import MAX_FIT

MAX_MESSAGES = 500_000
MAX_FIELDS = 2_000_000
WIDTHS = {0: 1, 1: 1, 2: 1, 7: 1, 10: 1, 0x83: 2, 0x84: 2, 0x85: 4, 0x86: 4, 0x88: 4}


@dataclass(frozen=True, repr=False)
class Field:
    number: int
    size: int
    base: int
    raw: bytes = field(default=b"", repr=False)
    developer: int | None = None


@dataclass(frozen=True, repr=False)
class Message:
    number: int
    endian: str
    fields: tuple[Field, ...]
    compressed: bool = False


@dataclass(repr=False)
class Document:
    messages: list[Message]
    definitions: list[Message]
    protocol: int
    profile: int


def invalid():
    raise BridgeError("invalid_fit")


def read_wire(data: bytes, rules_version=RULES_VERSION) -> Document:
    """Consume exactly one file, including every unused/redefined definition."""
    if not 14 <= len(data) <= MAX_FIT:
        invalid()
    header = data[0]
    if header not in (12, 14) or data[8:12] != b".FIT" or data[1] not in (0x10, 0x20):
        invalid()
    end = header + int.from_bytes(data[4:8], "little")
    if end + 2 != len(data):
        invalid()
    if header == 14 and compute_crc(data[:12]) != int.from_bytes(data[12:14], "little"):
        invalid()
    if compute_crc(data[:end]) != int.from_bytes(data[end:], "little"):
        invalid()
    position = header

    def take(size):
        nonlocal position
        if size < 0 or position + size > end:
            invalid()
        result = data[position : position + size]
        position += size
        return result

    local = {}
    definitions = []
    messages = []
    timestamp = None
    fields_seen = 0
    while position < end:
        tag = take(1)[0]
        compressed = bool(tag & 0x80)
        slot = (tag >> 5) & 3 if compressed else tag & 15
        if not compressed and (tag & 0x10 or (tag & 0x20 and not tag & 0x40)):
            invalid()
        if not compressed and tag & 0x40:
            reserved, architecture = take(2)
            if reserved != 0 or architecture not in (0, 1):
                invalid()
            endian = "little" if architecture == 0 else "big"
            number = int.from_bytes(take(2), endian)
            count = take(1)[0]
            fields = []
            keys = set()
            for _ in range(count):
                n, size, base = take(3)
                if size == 0 or n in keys:
                    invalid()
                keys.add(n)
                fields.append(Field(n, size, base))
            if tag & 0x20:
                for _ in range(take(1)[0]):
                    n, size, developer = take(3)
                    key = (developer, n)
                    if size == 0 or key in keys:
                        invalid()
                    keys.add(key)
                    fields.append(Field(n, size, -1, developer=developer))
            definition = Message(number, endian, tuple(fields))
            definitions.append(definition)
            local[slot] = definition
            fields_seen += len(fields)
        else:
            if slot not in local:
                invalid()
            definition = local[slot]
            fields = []
            for f in definition.fields:
                fields.append(Field(f.number, f.size, f.base, take(f.size), f.developer))
            if compressed:
                # This supported encoding has timestamp omitted from the local
                # definition. Ambiguous encodings with BOTH forms are rejected.
                if timestamp is None or any(
                    f.number == 253 and f.developer is None for f in fields
                ):
                    invalid()
                offset = tag & 31
                timestamp = (timestamp & ~31) + offset + (32 if offset < timestamp & 31 else 0)
                if timestamp >= 0xFFFFFFFF:
                    invalid()
                fields.append(Field(253, 4, 0x86, timestamp.to_bytes(4, definition.endian)))
            else:
                for f in fields:
                    if f.developer is None and f.number == 253:
                        # Unknown payloads must not enter the approved output
                        # indirectly as an anchor for later compressed records.
                        if 253 not in RULE_VERSIONS[rules_version].get(definition.number, {}) or (
                            f.base,
                            f.size,
                        ) != (
                            0x86,
                            4,
                        ):
                            timestamp = None
                        else:
                            value = int.from_bytes(f.raw, definition.endian)
                            timestamp = value if value != 0xFFFFFFFF else None
            messages.append(
                Message(definition.number, definition.endian, tuple(fields), compressed)
            )
            fields_seen += len(fields)
        if fields_seen > MAX_FIELDS or len(messages) + len(definitions) > MAX_MESSAGES:
            invalid()
    return Document(messages, definitions, data[1], int.from_bytes(data[2:4], "little"))


def write_wire(messages: list[Message]) -> bytes:
    """No physical value conversion. Coalesce only identical adjacent definitions."""
    body = bytearray()
    previous = None
    for message in messages:
        descriptor = (
            message.number,
            message.endian,
            tuple((f.number, f.size, f.base) for f in message.fields),
        )
        if descriptor != previous:
            body.extend(bytes((0x40, 0, message.endian == "big")))
            body.extend(message.number.to_bytes(2, message.endian))
            body.append(len(message.fields))
            for f in message.fields:
                if f.developer is not None or f.base not in WIDTHS or len(f.raw) != f.size:
                    invalid()
                body.extend(bytes((f.number, f.size, f.base)))
            previous = descriptor
        body.append(0)
        for f in message.fields:
            body.extend(f.raw)
        if len(body) > MAX_FIT - 16:
            invalid()
    header = struct.pack("<BBHI4s", 14, 0x20, 21218, len(body), b".FIT")
    content = header + struct.pack("<H", compute_crc(header)) + body
    return content + struct.pack("<H", compute_crc(content))


def unsigned(message: Message, number: int, base: int, size: int):
    """Read only a caller-reviewed scalar context; invalid and absent return None."""
    for f in message.fields:
        if f.developer is None and (f.number, f.base, f.size) == (number, base, size):
            value = int.from_bytes(f.raw, message.endian)
            return value if value != (1 << (8 * size)) - 1 else None
    return None
