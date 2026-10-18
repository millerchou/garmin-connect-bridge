"""Independent parsers see only the physically approved output, never raw secrets."""

import io
from importlib.metadata import version
import math
import struct
import warnings

import fitdecode
from garmin_fit_sdk import Decoder, Stream

from .errors import BridgeError
from .fit_policy import assert_approved
from .fit_rules import RULES, SDK_VERSION
from .fit_wire import WIDTHS, read_wire, write_wire


def require(condition):
    if not condition:
        raise BridgeError("sanitize_verification")


def values(message, field):
    """Interpret only approved bytes, without scale/offset/component expansion."""
    formats = {0: "B", 1: "b", 2: "B", 0x83: "h", 0x84: "H", 0x85: "i", 0x86: "I", 0x88: "f"}
    invalids = {
        0: 255,
        1: 127,
        2: 255,
        0x83: 32767,
        0x84: 65535,
        0x85: 2147483647,
        0x86: 4294967295,
    }
    width = WIDTHS[field.base]
    result = []
    for offset in range(0, field.size, width):
        raw = field.raw[offset : offset + width]
        value = struct.unpack(
            ("<" if message.endian == "little" else ">") + formats[field.base], raw
        )[0]
        invalid = raw == b"\xff" * 4 if field.base == 0x88 else value == invalids[field.base]
        result.append(None if invalid else value)
    return tuple(result)


def same_values(expected, actual, base):
    actual = tuple(actual) if isinstance(actual, (list, tuple)) else (actual,)
    if actual == (None,) and all(v is None for v in expected):
        actual = (None,) * len(expected)  # SDK omits all-invalid arrays.
    require(len(expected) == len(actual))
    for left, right in zip(expected, actual, strict=True):
        if base == 0x88 and isinstance(right, float) and math.isnan(right):
            # SDK exposes FIT's all-ones float sentinel as NaN. Physical bytes
            # (including the exact sentinel/NaN payload) were compared separately.
            require(left is None or isinstance(left, float) and math.isnan(left))
        elif base == 0x88 and left is not None and right is not None:
            require(struct.pack("<f", left) == struct.pack("<f", right))
        else:
            require(left == right)


def independent_readers(data, physical):
    require(version("garmin-fit-sdk") == SDK_VERSION and version("fitdecode") == "0.11.0")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        # Strict policy/CRC framing has already rejected all unapproved fields.
        source = io.BytesIO(data)
        position = 0
        with fitdecode.FitReader(
            source,
            processor=None,
            check_crc=fitdecode.CrcCheck.RAISE,
            error_handling=fitdecode.ErrorHandling.RAISE,
        ) as reader:
            for frame in reader:
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue
                require(position < len(physical))
                expected = physical[position]
                position += 1
                require(frame.global_mesg_num == expected.number)
                # Components/subfields must never be treated as physical fields.
                fields = [f for f in frame.fields if f.field_def is not None]
                require(
                    [f.field_def.def_num for f in fields] == [f.number for f in expected.fields]
                )
                for field, decoded in zip(expected.fields, fields, strict=True):
                    same_values(values(expected, field), decoded.raw_value, field.base)
            require(source.tell() == len(data) and position == len(physical))

        position = 0

        def on_message(number, decoded):
            nonlocal position
            require(position < len(physical))
            expected = physical[position]
            position += 1
            require(number == expected.number)
            names = {RULES[number][f.number].name for f in expected.fields}
            require(set(decoded) <= names)
            for field in expected.fields:
                same_values(
                    values(expected, field),
                    decoded.get(RULES[number][field.number].name),
                    field.base,
                )

        stream = Stream.from_byte_array(data)
        try:
            decoder = Decoder(stream)
            _, errors = decoder.read(
                apply_scale_and_offset=False,
                convert_datetimes_to_dates=False,
                convert_types_to_strings=False,
                enable_crc_check=True,
                expand_sub_fields=False,
                expand_components=False,
                merge_heart_rates=False,
                mesg_listener=on_message,
            )
            require(not errors and position == len(physical) and stream.position() == len(data))
        finally:
            stream.close()


def validate_archive_fit(data):
    """Check the current FIT alone; this proves no fidelity to an absent source."""
    try:
        document = read_wire(data)
        assert_approved(document)
        # Canonical framing excludes unused/redundant definitions and alternate
        # headers. Re-encoding observed fields is NOT a source-fidelity comparison.
        require(data == write_wire(document.messages))
        independent_readers(data, document.messages)
        return document
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        # Neither upstream diagnostics nor raw fields cross this boundary.
        raise BridgeError("sanitize_verification") from None


def validate_derived(data, expected):
    try:
        document = validate_archive_fit(data)
        # Only the separately projected source supplies the preservation evidence.
        require(data == write_wire(expected))
        require(document.messages == expected)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise BridgeError("sanitize_verification") from None
    return {
        "strict_crc": True,
        "complete_byte_consumption": True,
        "all_definitions_approved": True,
        "all_data_fields_approved": True,
        "physical_bytes_equal": True,
        "message_order_equal": True,
        "presence_invalid_arrays_precision_equal": True,
        "references_valid": True,
        "fitdecode_reread": True,
        "garmin_sdk_reread": True,
        "no_extra_payload": True,
    }
