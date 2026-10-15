"""Small adapter around the pinned library; no authentication implementation."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import http.client
import logging
import os
import warnings

from .errors import BridgeError


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@contextmanager
def quiet_upstream():
    """Provider exception bodies and payload debugging never reach the terminal."""
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    debug = http.client.HTTPConnection.debuglevel
    http.client.HTTPConnection.debuglevel = 0
    try:
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                yield
    finally:
        http.client.HTTPConnection.debuglevel = debug
        logging.disable(previous)


def invoke(operation, *args, auth_code="reauth_required", **kwargs):
    from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError
    from garminconnect.exceptions import GarminConnectNotFoundError

    try:
        with quiet_upstream():
            return operation(*args, **kwargs)
    except GarminConnectAuthenticationError:
        raise BridgeError(auth_code) from None
    except GarminConnectTooManyRequestsError:
        raise BridgeError("rate_limited") from None
    except GarminConnectNotFoundError:
        raise BridgeError("activity_unavailable") from None
    except Exception:
        raise BridgeError("network_failed") from None


def new_client(**kwargs):
    with quiet_upstream():
        from garminconnect import Garmin

        return Garmin(is_cn=False, retry_attempts=0, **kwargs)


def saved_session(store):
    saved = store.read_json("auth/session.json")
    if saved is not None and (
        type(saved) is not dict
        or saved.get("version") != 1
        or type(saved.get("tokenstore")) is not str
        or type(saved.get("rejected")) is not bool
    ):
        raise BridgeError("storage_failed")
    return saved


def auth_status(store):
    saved = saved_session(store)
    return {
        "state": ("reauth_required" if saved["rejected"] else "saved_unverified")
        if saved
        else "absent",
        "last_verified_at": saved["last_verified_at"] if saved else None,
    }


def save_session(store, client, verified_at):
    serialized = invoke(client.client.dumps)
    if type(serialized) is not str or not serialized.startswith("{"):
        raise BridgeError("auth_failed")
    store.write_json(
        "auth/session.json",
        {
            "version": 1,
            "tokenstore": serialized,
            "last_verified_at": verified_at,
            "rejected": False,
        },
    )


def mark_rejected(store):
    saved = saved_session(store)
    if saved:
        saved["rejected"] = True
        store.write_json("auth/session.json", saved)


def authenticate(store, prompt, factory=new_client):
    username = prompt("Garmin email (hidden): ")
    password = prompt("Garmin password: ")
    client = invoke(
        factory, email=username, password=password, return_on_mfa=True, auth_code="auth_failed"
    )
    del username, password
    try:
        # Explicit inline empty store prevents implicit GARMINTOKENS discovery.
        result, state = invoke(client.login, tokenstore="{}", auth_code="auth_failed")
        if result == "needs_mfa":
            for attempt in range(3):
                code = prompt("MFA code: " if attempt == 0 else "MFA code (retry): ")
                try:
                    result, _ = invoke(client.resume_login, state, code, auth_code="auth_failed")
                    break
                except BridgeError as error:
                    if error.code != "auth_failed" or attempt == 2:
                        raise
                finally:
                    del code
        if result is not None:
            raise BridgeError("auth_failed")
        # Upstream's return_on_mfa mode returns before loading the profile even
        # when MFA isn't needed. Its normal token login verifies the final session.
        tokenstore = invoke(client.client.dumps)
        invoke(client.login, tokenstore=tokenstore, auth_code="auth_failed")
        del tokenstore
        verified = now()
        save_session(store, client, verified)
        return verified
    finally:
        client.password = None
        client.username = None


class GarminSource:
    def __init__(self, store, factory=new_client):
        self.store = store
        saved = saved_session(store)
        if not saved:
            raise BridgeError("auth_required")
        self.client = invoke(factory)
        try:
            result, _ = invoke(self.client.login, tokenstore=saved["tokenstore"])
            if result is not None:
                raise BridgeError("reauth_required")
        except BridgeError as error:
            if error.code == "reauth_required":
                mark_rejected(store)
            raise
        self.verified_at = now()
        save_session(store, self.client, self.verified_at)

    def _request(self, method, *args, **kwargs):
        try:
            result = invoke(method, *args, **kwargs)
        except BridgeError as error:
            if error.code == "reauth_required":
                mark_rejected(self.store)
            else:
                save_session(self.store, self.client, self.verified_at)
            raise
        # Persist any upstream token rotation atomically, including rotations
        # during downloads. The library never receives a writable token path.
        save_session(self.store, self.client, self.verified_at)
        return result

    def discover(self, start, end):
        # The upstream convenience method treats null pages as end-of-history.
        # Validate each page here so an unexpected response can't advance coverage.
        items, seen, offset = [], set(), 0
        for _ in range(1000):
            page = self.page(start, end, offset, 100)
            if not page:
                return items
            from .normalize import sha256
            from .storage import canonical

            signature = sha256(canonical(page))
            if signature in seen:
                raise BridgeError("upstream_invalid")
            seen.add(signature)
            items.extend(page)
            # A server may cap page size below the requested limit.
            offset += len(page)
        raise BridgeError("upstream_invalid")

    def page(self, start, end, offset, limit):
        params = {
            "activityType": "cycling",
            "sortOrder": "asc",
            "start": str(offset),
            "limit": str(limit),
        }
        if end is not None:
            params["endDate"] = end
        if start is not None:
            params["startDate"] = start
        page = self._request(
            self.client.connectapi, self.client.garmin_connect_activities, params=params
        )
        if (
            type(page) is not list
            or len(page) > limit
            or any(type(item) is not dict for item in page)
        ):
            raise BridgeError("upstream_invalid")
        return page

    def download(self, source_id):
        return self._request(
            self.client.download_activity,
            source_id,
            dl_fmt=self.client.ActivityDownloadFormat.ORIGINAL,
        )
