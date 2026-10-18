"""Independent physical FIT constructor; every input is invented in test memory."""

import struct

from .synthetic import crc16

FORMATS = {
    0: "B",
    1: "b",
    2: "B",
    0x83: "h",
    0x84: "H",
    0x85: "i",
    0x86: "I",
    0x88: "f",
    0x8C: "I",
    7: "s",
    13: "B",
}


def raw(number, base, values, endian="little"):
    if isinstance(values, bytes):
        payload = values
    else:
        values = values if isinstance(values, (list, tuple)) else [values]
        payload = struct.pack(
            ("<" if endian == "little" else ">") + FORMATS[base] * len(values), *values
        )
    return number, base, payload


def definition(number, fields, endian="little", slot=0, developer=()):
    out = bytes((0x60 | slot if developer else 0x40 | slot, 0, endian == "big"))
    out += number.to_bytes(2, endian) + bytes((len(fields),))
    out += b"".join(bytes((n, len(payload), base)) for n, base, payload in fields)
    if developer:
        out += bytes((len(developer),)) + b"".join(
            bytes((n, len(payload), idx)) for n, idx, payload in developer
        )
    return out


def frame(number, fields, endian="little", slot=0, developer=()):
    return (
        definition(number, fields, endian, slot, developer)
        + bytes((slot,))
        + b"".join(payload for _, _, payload in (*fields, *developer))
    )


def finish(body, header_size=14):
    header = struct.pack("<BBHI4s", header_size, 0x20, 21218, len(body), b".FIT")
    if header_size == 14:
        header += struct.pack("<H", crc16(header))
    return header + body + struct.pack("<H", crc16(header + body))


def body(extra=b"", *, session=None, lap=None, records=None):
    if records is None:
        records = frame(20, [raw(253, 0x86, 100000), raw(7, 0x84, 0), raw(3, 2, 255)])
    if lap is None:
        lap = frame(19, [raw(254, 0x84, 0), raw(25, 0, 2), raw(8, 0x86, 1000)])
    if session is None:
        session = frame(
            18,
            [
                raw(254, 0x84, 0),
                raw(5, 0, 2),
                raw(25, 0x84, 0),
                raw(26, 0x84, 1),
                raw(8, 0x86, 1000),
            ],
        )
    return frame(0, [raw(0, 0, 4)]) + records + lap + extra + session


def minimal(extra=b"", **kwargs):
    return finish(body(extra, **kwargs))
