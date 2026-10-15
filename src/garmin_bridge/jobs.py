"""Durable page/activity journals for a new, independent Garmin data store.

Two equal, complete passes reconcile offset pagination without claiming a server
snapshot. Every observed required ID is retained, even if a later pass omits it.
"""

from datetime import date, datetime, timedelta
import time
import uuid

from . import normalize as parser
from .contract import SCHEMA_VERSION, identifier
from .errors import BridgeError
from .storage import canonical
from .sync import acquire, artifacts_intact, freshness, merge_ranges, subtract_range
from .upstream import now

TRANSIENT = {"network_failed", "rate_limited"}
GAP_STATES = {"discovered", "retry", "unavailable"}
STOP_ERRORS = {"reauth_required", "auth_required", "rate_limited", "storage_failed", "unsafe_store"}
JOB_FILE = "sync-job.json"


def load_job(store):
    from .persistence import validate_job

    job = store.read_json(JOB_FILE)
    return validate_job(job) if job is not None else None


def gaps(job):
    return [
        {
            "source_activity_id": source_id,
            "state": item["state"],
            "reason": item["reason"] or "pending",
            "attempts": item["attempts"],
        }
        for source_id, item in sorted(job["items"].items(), key=lambda pair: int(pair[0]))
        if item["state"] in GAP_STATES
    ]


def observed_range(job):
    days = []
    for item in job["items"].values():
        meta = item["metadata"]
        if meta and item["reason"] not in {"outside_scope", "after_cutoff"}:
            stamp = meta["activity_time"]["local"] or meta["activity_time"]["utc"]
            if stamp:
                days.append(stamp[:10])
    return {"start": min(days), "end": max(days)} if days else None


def report(job, state):
    missing = gaps(job)
    items = list(job["items"].values())
    counts = {
        name: sum(i["state"] == status for i in items)
        for name, status in (
            ("completed", "completed"),
            ("skipped", "skipped"),
            ("pending", "discovered"),
            ("retry", "retry"),
            ("unavailable", "unavailable"),
        )
    }
    counts["discovered"] = len(items)
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": job["id"],
        "mode": job["mode"],
        "status": job["status"],
        "complete": job["status"] == "complete",
        "cutoff_at": job["cutoff_at"],
        "filter": {
            **job["filter"],
            "activity_type": "cycling",
            "region": "international",
            "cutoff_basis": "activity_start_utc",
        },
        "observed_range": observed_range(job),
        "enumeration": {
            "complete": job["enumeration_complete"],
            "passes": job["passes"],
            "next_offset": job["offset"],
            "limit_reached": job["limit_reached"],
            "basis": "two_matching_visible_catalog_passes",
            "server_snapshot": False,
        },
        "counts": counts,
        "updated": sum(i["outcome"] == "updated" for i in items),
        "unchanged": sum(i["outcome"] == "unchanged" for i in items),
        "failed": len(missing) or int(not job["enumeration_complete"]),
        "gaps": missing[:20],
        "gap_count": len(missing),
        "gap_report": f"reports/{job['id']}/gaps.json" if job["report_valid"] else None,
        "freshness": freshness(state),
        "last_failure": job["last_failure"],
    }


class Runner:
    def __init__(
        self,
        store,
        source_factory,
        interval,
        mode,
        lookback,
        new_scan,
        page_size,
        max_pages,
        max_passes,
        attempts,
        sleep,
    ):
        self.store, self.source_factory = store, source_factory
        self.state = store.state()
        self.max_pages, self.max_passes, self.attempts = max_pages, max_passes, attempts
        self.sleep = sleep
        self.attempted = set()
        previous = load_job(store)
        unfinished = previous and previous["status"] != "complete"
        if mode == "resume":
            if not unfinished:
                raise BridgeError("no_pending_sync")
            self.job = previous
        elif unfinished and not new_scan:
            if mode == "incremental" or (
                mode == previous["mode"] and (interval is None or interval == previous["filter"])
            ):
                self.job = previous
            else:
                raise BridgeError("resume_required")
        else:
            if mode == "incremental" and not self.state["history_initialized"]:
                raise BridgeError("initial_import_required")
            cutoff = now()
            if interval is None:
                # An unbounded catalog avoids assuming the account's time zone
                # matches this machine. The activity UTC cutoff is applied locally.
                interval = {"start": None, "end": None}
            carried = {}
            if unfinished:
                # A new cutoff never makes an old unresolved ID disappear.
                carried = {
                    key: value
                    for key, value in previous["items"].items()
                    if value["state"] in GAP_STATES
                }
                store.write_json(f"jobs/{previous['id']}.json", previous)
            self.job = {
                "version": 1,
                "schema_version": SCHEMA_VERSION,
                "normalizer_version": parser.NORMALIZER_VERSION,
                "id": uuid.uuid4().hex,
                "mode": mode,
                "filter": interval,
                "cutoff_at": cutoff,
                "lookback": lookback,
                "status": "incomplete",
                "enumeration_complete": False,
                "passes": 0,
                "offset": 0,
                "pages_in_pass": 0,
                "page_size": page_size,
                "seen": {},
                "page_signatures": [],
                "previous_fingerprint": None,
                "pass_conflicted": False,
                "limit_reached": False,
                "items": carried,
                "report_valid": False,
                "last_failure": "interrupted",
            }
        if (
            self.job["normalizer_version"] != parser.NORMALIZER_VERSION
            or self.job["schema_version"] != SCHEMA_VERSION
        ):
            self.job["normalizer_version"] = parser.NORMALIZER_VERSION
            self.job["schema_version"] = SCHEMA_VERSION
            self.job["enumeration_complete"] = False
            self.job["previous_fingerprint"] = None
            self.reset_pass()
            for item in self.job["items"].values():
                if item["metadata"] is not None and item["reason"] not in {
                    "outside_scope",
                    "after_cutoff",
                }:
                    item.update(state="discovered", reason=None, outcome=None)
        # A checkpoint is not proof that its referenced files survived. Check
        # completed work on resume, including when enumeration already ended.
        for source_id, item in self.job["items"].items():
            if item["state"] == "completed" or item["reason"] == "outside_recheck_window":
                entry = self.state["activities"].get(source_id)
                if entry is None or not artifacts_intact(store, entry):
                    item.update(state="discovered", reason=None, outcome=None)

    def checkpoint(self):
        self.job["report_valid"] = False
        self.store.write_json(JOB_FILE, self.job)

    def reset_pass(self):
        self.job.update(
            offset=0, pages_in_pass=0, seen={}, page_signatures=[], pass_conflicted=False
        )

    def retry(self, operation, item=None):
        for attempt in range(self.attempts):
            if item is not None:
                item["attempts"] += 1
                self.checkpoint()
            try:
                return operation()
            except BridgeError as error:
                if error.code not in TRANSIENT or attempt == self.attempts - 1:
                    raise
                self.sleep(min(2**attempt, 30))

    def ingest(self, page):
        projected = {}
        for raw in page:
            try:
                meta = parser.metadata(raw)
                reason = "outside_scope" if meta is None else None
            except BridgeError as error:
                if error.code != "unsupported_activity_type":
                    raise
                meta, reason = None, error.code
            source_id = str(raw.get("activityId", ""))
            if not identifier(source_id):
                raise BridgeError("upstream_invalid")
            if meta:
                stamp = meta["activity_time"]["utc"]
                local = meta["activity_time"]["local"]
                if stamp and datetime.fromisoformat(stamp) > datetime.fromisoformat(
                    self.job["cutoff_at"]
                ):
                    reason = "after_cutoff"
                elif local and (
                    (self.job["filter"]["end"] and local[:10] > self.job["filter"]["end"])
                    or (self.job["filter"]["start"] and local[:10] < self.job["filter"]["start"])
                ):
                    reason = "outside_scope"
            value = {"metadata": meta, "reason": reason}
            if source_id in projected and projected[source_id] != value:
                raise BridgeError("upstream_invalid")
            projected[source_id] = value
        signature = parser.sha256(canonical(projected))
        if signature in self.job["page_signatures"]:
            # Offset progress would be fictitious; restart reconciliation on resume.
            self.reset_pass()
            self.job["previous_fingerprint"] = None
            self.checkpoint()
            raise BridgeError("catalog_unstable")
        self.job["page_signatures"].append(signature)
        for source_id, value in projected.items():
            fingerprint = parser.sha256(canonical(value))
            if value["reason"] not in {"outside_scope", "after_cutoff"}:
                seen = self.job["seen"].get(source_id)
                if seen and seen != fingerprint:
                    self.job["pass_conflicted"] = True
                self.job["seen"][source_id] = fingerprint
            previous = self.job["items"].get(source_id)
            if previous is None or previous["fingerprint"] != fingerprint:
                self.job["items"][source_id] = {
                    **value,
                    "fingerprint": fingerprint,
                    "state": (
                        "unavailable"
                        if value["reason"] == "unsupported_activity_type"
                        else "skipped"
                        if value["reason"]
                        else "discovered"
                    ),
                    "attempts": previous["attempts"] if previous else 0,
                    "outcome": None,
                }

    def outside_recheck(self, source_id, item):
        if self.job["mode"] != "incremental":
            return False
        previous = self.state["activities"].get(source_id)
        meta = item["metadata"]
        stamp = meta["activity_time"]["local"] or meta["activity_time"]["utc"]
        recent = date.fromisoformat(self.job["cutoff_at"][:10]) - timedelta(
            days=self.job["lookback"] - 1
        )
        return bool(
            previous
            and stamp
            and date.fromisoformat(stamp[:10]) < recent
            and previous.get("metadata_digest") == parser.sha256(canonical(meta))
            and previous["summary"]["normalizer_version"] == parser.NORMALIZER_VERSION
            and previous["summary"]["schema_version"] == SCHEMA_VERSION
            and artifacts_intact(self.store, previous)
        )

    def process(self):
        for source_id, item in self.job["items"].items():
            key = (source_id, item["fingerprint"])
            if item["state"] not in GAP_STATES or key in self.attempted:
                continue
            if item["metadata"] is None:
                continue  # explicit unsupported-type gap; never pretend it was excluded
            self.attempted.add(key)
            if self.outside_recheck(source_id, item):
                item.update(state="skipped", reason="outside_recheck_window", outcome=None)
                self.checkpoint()
                continue
            try:
                updated = self.retry(
                    lambda: acquire(
                        self.store,
                        self.source,
                        item["metadata"],
                        self.state,
                        cutoff_at=self.job["cutoff_at"],
                    ),
                    item,
                )
                if updated is None:
                    item.update(state="skipped", reason="after_cutoff", outcome=None)
                else:
                    item.update(
                        state="completed",
                        reason=None,
                        outcome="updated" if updated else "unchanged",
                    )
                self.checkpoint()
            except BridgeError as error:
                item.update(
                    state="retry" if error.code in TRANSIENT | STOP_ERRORS else "unavailable",
                    reason=error.code,
                    outcome=None,
                )
                self.checkpoint()
                if error.code in STOP_ERRORS:
                    raise

    def enumerate(self):
        passes_this_run = 0
        while not self.job["enumeration_complete"]:
            self.process()  # resume queued work before fetching the next page
            if self.job["pages_in_pass"] >= self.max_pages:
                self.job["limit_reached"] = True
                raise BridgeError("enumeration_limit")
            page = self.retry(
                lambda: self.source.page(
                    self.job["filter"]["start"],
                    self.job["filter"]["end"],
                    self.job["offset"],
                    self.job["page_size"],
                )
            )
            if type(page) is not list or len(page) > self.job["page_size"]:
                raise BridgeError("upstream_invalid")
            self.job["pages_in_pass"] += 1
            self.job["limit_reached"] = False
            if page:
                self.ingest(page)
                self.job["offset"] += len(page)
                self.checkpoint()  # page receipt is durable before any downloads
                continue
            self.job["passes"] += 1
            passes_this_run += 1
            fingerprint = parser.sha256(canonical(self.job["seen"]))
            if not self.job["pass_conflicted"] and self.job["previous_fingerprint"] == fingerprint:
                self.job["enumeration_complete"] = True
                self.checkpoint()
                return
            self.job["previous_fingerprint"] = None if self.job["pass_conflicted"] else fingerprint
            self.reset_pass()
            self.checkpoint()
            if passes_this_run >= self.max_passes:
                raise BridgeError("catalog_unstable")

    def finish(self, error=None):
        missing = gaps(self.job)
        complete = self.job["enumeration_complete"] and not missing and error is None
        self.job["last_failure"] = error or (missing[0]["reason"] if missing else None)
        if self.job["last_failure"] == "pending":
            self.job["last_failure"] = "interrupted"
        self.state["last_failure"] = self.job["last_failure"]
        if complete:
            interval = self.job["filter"]
            if interval["start"] is None:
                observed = observed_range(self.job)
                interval = (
                    {
                        "start": observed["start"],
                        "end": max(observed["end"], self.job["cutoff_at"][:10]),
                    }
                    if observed
                    else None
                )
            if interval:
                self.state["covered_ranges"] = merge_ranges(
                    [*self.state["covered_ranges"], interval]
                )
                self.state["pending_ranges"] = subtract_range(
                    self.state["pending_ranges"], interval
                )
            self.state["last_success_at"] = now()
            if self.job["mode"] == "full":
                self.state["history_initialized"] = True
                self.state["full_completed_at"] = self.state["last_success_at"]
                self.state["pending_ranges"] = []
        self.store.write_json("index.json", self.state)
        self.job["status"] = "complete" if complete else "incomplete"
        gap_report = {
            "schema_version": SCHEMA_VERSION,
            "job_id": self.job["id"],
            "cutoff_at": self.job["cutoff_at"],
            "gaps": missing,
        }
        from .contract import validate_gap_report

        validate_gap_report(gap_report)
        self.store.write_json(f"reports/{self.job['id']}/gaps.json", gap_report)
        self.job["report_valid"] = True
        self.store.write_json(JOB_FILE, self.job)
        return report(self.job, self.state)

    def run(self):
        self.state["last_attempt_at"] = now()
        self.state["last_failure"] = "interrupted"
        self.job["last_failure"] = "interrupted"
        interval = self.job["filter"]
        if interval["start"]:
            self.state["pending_ranges"] = merge_ranges([*self.state["pending_ranges"], interval])
        self.checkpoint()
        self.store.write_json("index.json", self.state)
        try:
            self.source = self.retry(self.source_factory)
            self.process()
            self.enumerate()
            self.process()
        except BridgeError as error:
            return self.finish(error.code)
        # A killed process leaves the last page and item checkpoint intact.
        # Unexpected exceptions cannot commit completion or erase prior results.
        return self.finish()


def run_job(
    store,
    source_factory,
    interval=None,
    *,
    mode="range",
    lookback=14,
    new_scan=False,
    page_size=100,
    max_pages=1000,
    max_passes=3,
    attempts=3,
    sleep=None,
):
    return Runner(
        store,
        source_factory,
        interval,
        mode,
        lookback,
        new_scan,
        page_size,
        max_pages,
        max_passes,
        attempts,
        sleep or time.sleep,
    ).run()
