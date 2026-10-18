"""Only reviewed, constant diagnostics may cross the CLI boundary."""

MESSAGES = {
    "usage": "Invalid arguments. Use --help; never pass credentials as arguments.",
    "interactive_required": "Run auth in a private interactive terminal.",
    "auth_required": "No saved session. Run auth in a private terminal.",
    "auth_failed": "Authentication or MFA was rejected. Run auth again.",
    "reauth_required": "Saved session was rejected. Run auth again.",
    "network_failed": "Garmin request failed. Retry the explicit auth or sync command.",
    "rate_limited": "Garmin rate limit reached. Wait before retrying.",
    "upstream_invalid": "Unexpected Garmin response. Sync remains incomplete.",
    "invalid_fit": "Original activity is not a supported, complete FIT input.",
    "sanitize_context": "Sanitization requires one cycling session and an activity FIT with records.",
    "sanitize_verification": "Derived FIT archive failed policy, preservation, parser, or integrity checks.",
    "sanitize_publish": "Atomic publication is unavailable; no archive was published.",
    "unsafe_archive": "Original archive failed size, path, or format validation.",
    "unsafe_input": "Use one private regular FIT or ZIP file outside Git and authentication directories.",
    "import_conflict": "Local input conflicts with saved API data or scan evidence. Existing results were preserved.",
    "unsafe_store": "Use an owner-only private directory outside any Git checkout.",
    "storage_failed": "Private storage could not be read or written safely.",
    "busy": "Private store is in use by another command. Retry later.",
    "invalid_bundle": "Backup failed format, checksum, privacy, or consistency validation.",
    "destination_exists": "Destination is not empty. Use a new private path; existing data was preserved.",
    "restore_pending": "Restore is interrupted. Repeat backup restore with the same verified package.",
    "privacy_failed": "Output failed privacy validation; no data was returned.",
    "not_found": "Activity is not in the local index. Run an explicit sync if needed.",
    "activity_unavailable": "Garmin cannot provide this original activity. The gap remains recorded.",
    "unsupported_activity_type": "An unreviewed activity type was returned by the cycling catalog; the gap remains recorded.",
    "initial_import_required": "Initialize this new store with sync --full before daily sync.",
    "resume_required": "An unfinished scan exists. Use sync --resume, or --full --new-scan to retain gaps and scan again.",
    "no_pending_sync": "There is no unfinished scan to resume.",
    "enumeration_limit": "The page limit was reached. Import is incomplete; resume with a larger --max-pages.",
    "catalog_unstable": "The visible activity catalog did not stabilize. Resume to reconcile it.",
    "interrupted": "Operation interrupted. Previously saved results remain available.",
    "internal_error": "Operation failed. Previous committed results remain available.",
}


class BridgeError(Exception):
    def __init__(self, code: str):
        self.code = code if code in MESSAGES else "internal_error"
        super().__init__(MESSAGES[self.code])
