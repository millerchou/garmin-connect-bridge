"""JSON-only CLI. Only explicit auth and sync may contact Garmin."""

import argparse
from datetime import date
import getpass
import json
import logging
from pathlib import Path
import sys
import warnings

from .contract import SCHEMA_VERSION, day, identifier, project_activity, validate_output
from .errors import BridgeError, MESSAGES
from .storage import Store
from .sync import choose_range, files_available, freshness, synchronize
from .upstream import GarminSource, authenticate, auth_status


class PrivateParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes rejected values; those might be secrets.
        raise BridgeError("usage")


def date_arg(value):
    if not day(value):
        raise BridgeError("usage")
    return value


def positive_id(value):
    if not identifier(value):
        raise BridgeError("usage")
    return value


def bounded_int(minimum, maximum):
    def parse(value):
        try:
            result = int(value)
        except ValueError:
            raise BridgeError("usage") from None
        if not minimum <= result <= maximum:
            raise BridgeError("usage")
        return result

    return parse


def build_parser():
    parser = PrivateParser(
        description="Read-only Garmin cycling bridge. Queries use private local state."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Private store outside Git; defaults to the OS user data directory",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", help="Interactive sign-in and MFA; save a protected session")
    auth.add_argument(
        "--refresh",
        action="store_true",
        help="Explicitly verify/refresh the saved session without a password",
    )
    commands.add_parser("status", help="Offline authentication state, coverage and freshness")
    importing = commands.add_parser(
        "import-fit", help="Offline import of one private FIT or single-FIT ZIP; no authentication"
    )
    importing.add_argument(
        "--file", type=Path, required=True, help="Private FIT or ZIP outside Git"
    )
    importing.add_argument(
        "--activity-id",
        type=positive_id,
        required=True,
        help="Explicit Garmin activity ID from the website",
    )
    sync = commands.add_parser(
        "sync", help="Explicitly discover cycling activities and store private original FIT data"
    )
    sync.add_argument(
        "--start", type=date_arg, help="First Garmin local calendar date (YYYY-MM-DD), inclusive"
    )
    sync.add_argument(
        "--end", type=date_arg, help="Last Garmin local calendar date, inclusive; default today"
    )
    sync.add_argument(
        "--lookback-days",
        type=bounded_int(1, 365),
        default=14,
        help="Recent correction window for incremental sync (default 14)",
    )
    modes = sync.add_mutually_exclusive_group()
    modes.add_argument(
        "--full",
        action="store_true",
        help="Import all visible historical cycling; required for a new store",
    )
    modes.add_argument(
        "--resume", action="store_true", help="Resume the durable scan with its original cutoff"
    )
    sync.add_argument(
        "--new-scan",
        action="store_true",
        help="With --full, start a new cutoff while retaining unresolved gaps",
    )
    sync.add_argument(
        "--max-pages",
        type=bounded_int(2, 10_000),
        default=1000,
        help="Page budget per catalog pass; exhaustion is incomplete (default 1000)",
    )
    listing = commands.add_parser("list", help="Offline, bounded activity index")
    listing.add_argument("--start", type=date_arg)
    listing.add_argument("--end", type=date_arg)
    listing.add_argument("--limit", type=bounded_int(1, 50), default=20)
    listing.add_argument("--offset", type=bounded_int(0, 10**9), default=0)
    activity = commands.add_parser(
        "activity", help="Offline summary, coverage and approved file references"
    )
    activity.add_argument("activity_id", type=positive_id)
    backup = commands.add_parser(
        "backup", help="Offline non-location backup, validation and restore"
    )
    actions = backup.add_subparsers(dest="backup_action", required=True)
    for action, help_text in (
        ("export", "Export reviewed current data and recovery state; omit auth and originals"),
        ("verify", "Validate every package member without changing any store"),
        ("restore", "Restore a verified package into an empty private store"),
    ):
        command = actions.add_parser(action, help=help_text)
        command.add_argument(
            "--file", type=Path, required=True, help="Private package path outside Git"
        )
    return parser


def private_prompt(label):
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise BridgeError("interactive_required")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(label)
        except getpass.GetPassWarning:
            raise BridgeError("interactive_required") from None


def query(store, args):
    state = store.state()
    base = {"schema_version": SCHEMA_VERSION, "freshness": freshness(state)}
    if args.command == "status":
        from .jobs import load_job, report

        job = load_job(store)
        return {
            **base,
            "authentication": auth_status(store),
            "activity_count": len(state["activities"]),
            "covered_ranges": state["covered_ranges"],
            "last_failure": state["last_failure"],
            "full_completed_at": state["full_completed_at"],
            "sync": report(job, state) if job else None,
        }
    if args.command == "activity":
        entry = state["activities"].get(args.activity_id)
        if entry is None:
            raise BridgeError("not_found")
        summary = project_activity(entry["summary"])
        return {**base, "activity": summary, "files_available": files_available(store, summary)}
    if args.start and args.end and args.start > args.end:
        raise BridgeError("usage")
    summaries = [project_activity(entry["summary"]) for entry in state["activities"].values()]

    def local_day(summary):
        time = summary["activity_time"]
        return (time["local"] or time["utc"] or "")[:10]

    selected = sorted(
        (
            s
            for s in summaries
            if (not args.start or local_day(s) >= args.start)
            and (not args.end or local_day(s) and local_day(s) <= args.end)
        ),
        key=lambda s: (local_day(s), int(s["source_activity_id"])),
        reverse=True,
    )
    page = selected[args.offset : args.offset + args.limit]
    cards = [
        {
            **{
                k: s[k]
                for k in (
                    "provider",
                    "source_activity_id",
                    "activity_time",
                    "quality_flags",
                    "files",
                )
            },
            "metrics": {k: s["metrics"][k] for k in ("duration", "distance")},
        }
        for s in page
    ]
    next_offset = args.offset + len(page)
    return {
        **base,
        "activities": cards,
        "next_offset": next_offset if next_offset < len(selected) else None,
    }


def execute(args):
    if args.command == "backup":
        from .backup import export_bundle, restore_bundle, verify_bundle

        if args.backup_action == "verify":
            return verify_bundle(args.file)
        operation = export_bundle if args.backup_action == "export" else restore_bundle
        return operation(Store(args.data_dir), args.file)
    store = Store(args.data_dir)
    write = args.command in {"auth", "sync", "import-fit"}
    with store.lock(write=write):
        if args.command == "import-fit":
            from .import_fit import import_file

            return import_file(store, args.file, args.activity_id)
        if args.command == "auth":
            verified = (
                GarminSource(store).verified_at
                if args.refresh
                else authenticate(store, private_prompt)
            )
            return {"schema_version": SCHEMA_VERSION, "state": "saved", "verified_at": verified}
        if args.command == "sync":
            if ((args.full or args.resume) and (args.start or args.end)) or (
                args.new_scan and not args.full
            ):
                raise BridgeError("usage")
            if args.end and not args.start:
                raise BridgeError("usage")
            interval = (
                choose_range(store.state(), args.start, args.end, args.lookback_days, date.today())
                if args.start
                else None
            )
            mode = (
                "full"
                if args.full
                else "resume"
                if args.resume
                else "range"
                if args.start
                else "incremental"
            )
            return synchronize(
                store,
                lambda: GarminSource(store),
                interval,
                mode=mode,
                lookback=args.lookback_days,
                new_scan=args.new_scan,
                max_pages=args.max_pages,
            )
        return query(store, args)


def main(argv=None):
    logging.disable(logging.CRITICAL)
    try:
        args = build_parser().parse_args(argv)
        result = validate_output(args.command, execute(args))
        serialized = json.dumps(result, allow_nan=False, separators=(",", ":"))
        print(serialized)
        return 1 if args.command == "sync" and not result["complete"] else 0
    except (KeyboardInterrupt, EOFError):
        code = "interrupted"
    except BridgeError as error:
        code = error.code
    except OSError:
        code = "storage_failed"
    except Exception:
        # No repr, traceback, dynamic exception message or provider response.
        code = "internal_error"
    result = {"schema_version": SCHEMA_VERSION, "error": code, "message": MESSAGES[code]}
    print(json.dumps(validate_output("error", result)), file=sys.stderr)
    return 130 if code == "interrupted" else 2
