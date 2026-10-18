"""Standalone offline derived-FIT archives; no activity/auth/sync state access."""

import ctypes
from collections import Counter
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import tempfile

from .contract import (
    FIT_ARCHIVE_CHECK_NAMES,
    SCHEMA_VERSION,
    count,
    digest,
    literal,
    obj,
    sequence,
    validate_output,
)
from .errors import BridgeError
from .fit_policy import REASONS, project
from .fit_rules import PROFILE_VERSION, RULES_VERSION, SDK_VERSION
from .fit_verify import require, validate_archive_fit, validate_derived
from .fit_wire import read_wire, write_wire
from .import_fit import read_input
from .normalize import MAX_FIT, extract_fit
from .storage import Store, canonical, default_root

FORMAT = "garmin-bridge-derived-training-fit"
FORMAT_VERSION = 1
FILES = ("derived.fit", "manifest.json", "report.json")
DESCRIPTION = "去除显式位置的派生训练FIT"
ARCHIVE_LIMITS = {"derived.fit": MAX_FIT, "manifest.json": 4 * 1024 * 1024, "report.json": 65536}

VALIDATION_NAMES = (
    "strict_crc",
    "complete_byte_consumption",
    "all_definitions_approved",
    "all_data_fields_approved",
    "physical_bytes_equal",
    "message_order_equal",
    "presence_invalid_arrays_precision_equal",
    "references_valid",
    "fitdecode_reread",
    "garmin_sdk_reread",
    "no_extra_payload",
)
SUMMARY_NAMES = (
    "input_messages",
    "output_messages",
    "removed_messages",
    "retained_fields",
    "removed_fields",
    "pending_review_fields",
    "compressed_timestamps_expanded",
)
LIMITATIONS = [
    "not_anonymous",
    "removed_fields_not_recoverable",
    "historical_configuration_only",
    "single_cycling_session",
]
SUMMARY = obj({name: count for name in SUMMARY_NAMES})
VALIDATIONS = obj({name: lambda x: x is True for name in VALIDATION_NAMES})
COMMON = {
    "format_version": literal(FORMAT_VERSION),
    "rules_version": literal(RULES_VERSION),
    "description": literal(DESCRIPTION),
    "summary": SUMMARY,
    "validations": VALIDATIONS,
}
REPORT = obj({**COMMON, "status": literal("verified"), "limitations": lambda x: x == LIMITATIONS})
MANIFEST = obj(
    {
        **COMMON,
        "format": literal(FORMAT),
        "profile_version": literal(PROFILE_VERSION),
        "sdk_version": literal(SDK_VERSION),
        "fitdecode_version": literal("0.11.0"),
        "input_sha256": digest,
        "input_fit_sha256": digest,
        "output_sha256": digest,
        "report_sha256": digest,
        "input_protocol_version": literal(0x10, 0x20),
        "input_profile_number": lambda x: type(x) is int and 0 <= x <= 65535,
        "files": lambda x: x == list(FILES),
        "fields": sequence(
            obj(
                {
                    "message": lambda x: type(x) is int and 0 <= x <= 65535,
                    "field": lambda x: type(x) is int and 0 <= x <= 255,
                    "base_type": lambda x: type(x) is int and -1 <= x <= 255,
                    "developer": lambda x: type(x) is bool,
                    "action": literal("retained", "removed"),
                    "reason": literal(*REASONS),
                    "pending_review": lambda x: type(x) is bool,
                    "definitions": count,
                    "occurrences": count,
                }
            ),
            8192,
        ),
    }
)


def validate_documents(manifest, report):
    # Recursive closed contracts apply to private JSON as well as CLI output.
    if not MANIFEST(manifest) or not REPORT(report):
        raise BridgeError("privacy_failed")


def checked_path(value):
    path = Path(value).expanduser()
    if ".." in path.parts or path.name.startswith(".fit-pending-"):
        raise BridgeError("unsafe_input")
    return path


def archive_store(destination, data_root):
    destination = checked_path(destination)
    protected = Path(data_root or default_root()).expanduser().resolve()
    # Archives are separate from the Bridge store, including all auth and jobs.
    if destination.resolve().is_relative_to(protected):
        raise BridgeError("unsafe_store")
    return Store(destination)


def paths(source, destination, data_root):
    source, destination = checked_path(source), checked_path(destination)
    protected = Path(data_root or default_root()).expanduser().resolve()
    if source.resolve().is_relative_to(protected / "auth"):
        raise BridgeError("unsafe_input")
    archive = archive_store(destination, data_root)
    if destination.resolve() == source.resolve() or source.resolve().is_relative_to(
        destination.resolve()
    ):
        raise BridgeError("unsafe_input")
    return source, archive


def prepare(original):
    fit = extract_fit(original)
    document = read_wire(fit)
    selected, inventory = project(document)
    derived = write_wire(selected)
    validations = validate_derived(derived, selected)
    fields = inventory["fields"]
    summary = {
        "input_messages": inventory["input_messages"],
        "output_messages": inventory["output_messages"],
        "removed_messages": inventory["removed_messages"],
        "retained_fields": sum(r["occurrences"] for r in fields if r["action"] == "retained"),
        "removed_fields": sum(r["occurrences"] for r in fields if r["action"] == "removed"),
        "pending_review_fields": sum(r["occurrences"] for r in fields if r["pending_review"]),
        "compressed_timestamps_expanded": inventory["compressed_timestamps_expanded"],
    }
    report = {
        "format_version": FORMAT_VERSION,
        "rules_version": RULES_VERSION,
        "description": DESCRIPTION,
        "status": "verified",
        "summary": summary,
        "validations": validations,
        "limitations": LIMITATIONS.copy(),
    }
    report_bytes = canonical(report)
    manifest = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "rules_version": RULES_VERSION,
        "profile_version": PROFILE_VERSION,
        "sdk_version": SDK_VERSION,
        "fitdecode_version": "0.11.0",
        "description": DESCRIPTION,
        "input_sha256": sha256(original).hexdigest(),
        "input_fit_sha256": sha256(fit).hexdigest(),
        "output_sha256": sha256(derived).hexdigest(),
        "report_sha256": sha256(report_bytes).hexdigest(),
        "input_protocol_version": document.protocol,
        "input_profile_number": document.profile,
        "fields": fields,
        "summary": summary,
        "validations": validations,
        "files": list(FILES),
    }
    validate_documents(manifest, report)
    return {
        "derived.fit": derived,
        "report.json": report_bytes,
        "manifest.json": canonical(manifest),
    }, summary


def receipt(action, summary, *, archive_only=False):
    # Only constants and bounded counts. Never expose names, paths, hashes or values.
    result = {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "rules_version": RULES_VERSION,
        "description": DESCRIPTION,
        "status": "verified",
        "summary": summary,
        "files": list(FILES),
        "activity_store_changed": False,
    }
    if action == "verify":
        result.update(
            mode="archive_only" if archive_only else "with_source",
            checks={name: "passed" for name in FIT_ARCHIVE_CHECK_NAMES},
            source_comparison=(
                {"status": "not_checked", "reason": "source_not_provided"}
                if archive_only
                else {"status": "passed"}
            ),
        )
    return validate_output("verify-fit" if action == "verify" else "sanitize-fit", result)


def strict_json(data):
    """Read only canonical JSON; duplicates, non-finite numbers and extras fail."""

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result

    def invalid_constant(_):
        raise BridgeError("sanitize_verification")

    value = json.loads(data, object_pairs_hook=unique, parse_constant=invalid_constant)
    require(canonical(value) == data)
    return value


def check_inventory(manifest, report, document):
    """Check consistency, not the truth of source-only historical inventory rows."""
    require(all(manifest[key] == report[key] for key in COMMON))
    summary = manifest["summary"]
    rows = manifest["fields"]
    keys = [
        tuple(
            row[key]
            for key in (
                "message",
                "field",
                "base_type",
                "developer",
                "action",
                "reason",
                "pending_review",
            )
        )
        for row in rows
    ]
    require(keys == sorted(set(keys)))
    retained = Counter()
    for row in rows:
        require(row["definitions"] + row["occurrences"] > 0)
        require((row["action"] == "retained") == (row["reason"] == "approved"))
        if row["action"] == "retained":
            require(not row["developer"] and not row["pending_review"])
            retained[(row["message"], row["field"], row["base_type"])] += row["occurrences"]
    actual = Counter(
        (message.number, field.number, field.base)
        for message in document.messages
        for field in message.fields
    )
    require(retained == actual)
    observed = {"output_messages": len(document.messages), "retained_fields": actual.total()}
    require(all(summary[key] == value for key, value in observed.items()))
    require(summary["input_messages"] == summary["output_messages"] + summary["removed_messages"])
    require(
        summary["removed_fields"] == sum(r["occurrences"] for r in rows if r["action"] == "removed")
    )
    require(
        summary["pending_review_fields"]
        == sum(r["occurrences"] for r in rows if r["pending_review"])
    )
    require(
        summary["compressed_timestamps_expanded"]
        <= sum(n for (_, field, _), n in actual.items() if field == 253)
    )
    return observed


def exact_members(archive):
    require(archive.root.is_dir() and {p.name for p in archive.root.iterdir()} == set(FILES))


def verify_archive_only(directory, data_root=None):
    """Recheck a bounded, read-only archive snapshot without opening any source."""
    archive = archive_store(directory, data_root)
    try:
        exact_members(archive)
        observed = {name: archive.read_bytes(name, limit=ARCHIVE_LIMITS[name]) for name in FILES}
        manifest = strict_json(observed["manifest.json"])
        report = strict_json(observed["report.json"])
        validate_documents(manifest, report)
        require(sha256(observed["derived.fit"]).hexdigest() == manifest["output_sha256"])
        require(sha256(observed["report.json"]).hexdigest() == manifest["report_sha256"])
        # Historical pass flags are format metadata, never verification evidence.
        document = validate_archive_fit(observed["derived.fit"])
        summary = check_inventory(manifest, report, document)
        exact_members(archive)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise BridgeError("sanitize_verification") from None
    return receipt("verify", summary, archive_only=True)


def publish_directory(source: Path, destination: Path):
    """Atomic no-replace rename on both supported operating systems.

    A check followed by os.rename can overwrite a concurrently created empty
    directory. These kernel primitives fail even for an empty destination.
    There is deliberately no unsafe fallback on unsupported filesystems.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "linux" and hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        function = libc.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(os.fsencode(source), os.fsencode(destination), 4)
    else:
        raise BridgeError("sanitize_publish")
    if result != 0:
        code = ctypes.get_errno()
        raise BridgeError(
            "destination_exists" if code in (errno.EEXIST, errno.ENOTEMPTY) else "sanitize_publish"
        )


def sanitize_file(source, destination, data_root=None):
    source, target = paths(source, destination, data_root)
    if target.root.exists():
        raise BridgeError("destination_exists")
    files, summary = prepare(read_input(None, source))
    parent = Store(target.root.parent)
    parent.ensure()
    # Temporary directory names are explicitly rejected by the verify command.
    # A hard kill can leave only a hidden, unpublishable staging directory.
    with tempfile.TemporaryDirectory(prefix=".fit-pending-", dir=parent.root) as temporary:
        staged = Store(Path(temporary))
        for name in ("derived.fit", "report.json", "manifest.json"):
            staged.write_bytes(name, files[name])
            if staged.read_bytes(name) != files[name]:
                raise BridgeError("sanitize_verification")
        publish_directory(staged.root, target.root)
        descriptor = os.open(parent.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return receipt("sanitize", summary)


def verify_archive(source, directory, data_root=None):
    """Recompute from the read-only source; never trust stored pass flags or hashes."""
    source, archive = paths(source, directory, data_root)
    try:
        if not archive.root.is_dir() or {p.name for p in archive.root.iterdir()} != set(FILES):
            raise BridgeError("sanitize_verification")
        original = read_input(None, source)
        expected, summary = prepare(original)
        observed = {}
        for name in FILES:
            observed[name] = archive.read_bytes(name, limit=len(expected[name]))
            if observed[name] != expected[name]:
                raise BridgeError("sanitize_verification")
        # Validate the actual stored FIT through every physical field and both
        # independent parsers, even if its manifest makes a validity claim.
        selected, _ = project(read_wire(extract_fit(original)))
        validate_derived(observed["derived.fit"], selected)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise BridgeError("sanitize_verification") from None
    return receipt("verify", summary)
