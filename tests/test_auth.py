import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)
import pytest

from garmin_bridge.errors import BridgeError
from garmin_bridge.upstream import GarminSource, authenticate, auth_status, invoke, new_client
from .synthetic import SECRET, activity, archive


def sdk():
    client = create_autospec(Garmin, instance=True)
    client.login.return_value = (None, None)
    client.resume_login.return_value = (None, None)
    client.client = SimpleNamespace(
        dumps=Mock(return_value=json.dumps({"di_token": SECRET, "di_refresh_token": SECRET}))
    )
    client.garmin_connect_activities = "/activitylist-service/activities/search/activities"
    return client


def sign_in(store, client=None):
    client = client or sdk()
    with store.lock(write=True):
        authenticate(store, lambda _: SECRET, factory=lambda **_: client)
    return client


def test_first_login_permissions_explicit_store_and_no_secret_output(
    store, capsys, caplog, monkeypatch
):
    monkeypatch.setenv("GARMINTOKENS", "/never-read-an-ambient-token-store")
    client = sdk()

    def login(*args, **kwargs):
        print(SECRET)
        logging.error(SECRET)
        return None, None

    client.login.side_effect = login
    sign_in(store, client)
    assert client.login.call_args_list[0].kwargs == {"tokenstore": "{}"}
    assert client.password is None and client.username is None
    assert auth_status(store)["state"] == "saved_unverified"
    assert os.stat(store.root).st_mode & 0o777 == 0o700
    assert os.stat(store.path("auth")).st_mode & 0o777 == 0o700
    assert os.stat(store.path("auth/session.json")).st_mode & 0o777 == 0o600
    assert SECRET not in capsys.readouterr().out + caplog.text


def test_mfa_continuation_retries_on_same_client(store):
    client = sdk()
    client.login.side_effect = [("needs_mfa", None), (None, None)]
    client.resume_login.side_effect = [GarminConnectAuthenticationError(SECRET), (None, None)]
    prompts = []

    def prompt(label):
        prompts.append(label)
        return SECRET

    with store.lock(write=True):
        authenticate(store, prompt, factory=lambda **_: client)
    assert len(prompts) == 4
    assert client.resume_login.call_count == 2
    assert client.resume_login.call_args.args == (None, SECRET)
    assert auth_status(store)["last_verified_at"] is not None


@pytest.mark.parametrize("mfa", [False, True])
def test_failed_auth_preserves_previous_session(store, mfa):
    sign_in(store)
    original = store.read_bytes("auth/session.json")
    client = sdk()
    if mfa:
        client.login.return_value = ("needs_mfa", None)
        client.resume_login.side_effect = GarminConnectAuthenticationError(SECRET)
    else:
        client.login.side_effect = GarminConnectAuthenticationError(SECRET)
    with store.lock(write=True), pytest.raises(BridgeError) as error:
        authenticate(store, lambda _: SECRET, factory=lambda **_: client)
    assert error.value.code == "auth_failed" and SECRET not in str(error.value)
    assert store.read_bytes("auth/session.json") == original


def test_interrupted_mfa_does_not_persist_challenge(store):
    client = sdk()
    client.login.return_value = ("needs_mfa", None)
    prompts = iter([SECRET, SECRET])

    def prompt(_):
        try:
            return next(prompts)
        except StopIteration:
            raise KeyboardInterrupt

    with store.lock(write=True), pytest.raises(KeyboardInterrupt):
        authenticate(store, prompt, factory=lambda **_: client)
    assert store.read_bytes("auth/session.json") is None
    assert client.password is None


def test_rejected_saved_session_and_token_rotation(store):
    sign_in(store)
    old_payload = store.read_json("auth/session.json")["tokenstore"]
    client = sdk()
    client.login.side_effect = GarminConnectAuthenticationError(SECRET)
    with store.lock(write=True), pytest.raises(BridgeError) as error:
        GarminSource(store, factory=lambda: client)
    assert error.value.code == "reauth_required"
    assert auth_status(store)["state"] == "reauth_required"
    assert store.read_json("auth/session.json")["tokenstore"] == old_payload
    fresh = sdk()
    fresh.client.dumps.return_value = '{"di_token":"synthetic-rotation"}'
    fresh.download_activity.return_value = archive()
    with store.lock(write=True):
        source = GarminSource(store, factory=lambda: fresh)
        fresh.client.dumps.return_value = '{"di_token":"synthetic-second-rotation"}'
        source.download("1001")
    assert "synthetic-second-rotation" in store.read_json("auth/session.json")["tokenstore"]
    fresh.download_activity.assert_called_once_with(
        "1001", dl_fmt=fresh.ActivityDownloadFormat.ORIGINAL
    )
    assert auth_status(store)["state"] == "saved_unverified"


def test_saved_session_never_falls_back_to_credentials(store):
    sign_in(store)
    factory = Mock(return_value=sdk())
    with store.lock(write=True):
        GarminSource(store, factory=factory)
    factory.assert_called_once_with()


def test_adapter_requests_only_international_region(monkeypatch):
    factory = Mock(return_value=sdk())
    monkeypatch.setattr("garminconnect.Garmin", factory)
    new_client()
    factory.assert_called_once_with(is_cn=False, retry_attempts=0)


@pytest.mark.parametrize("pages", [[None], [{"token": SECRET}], [[activity()], [activity()]]])
def test_null_malformed_and_repeated_pages_fail_closed(store, pages):
    sign_in(store)
    client = sdk()
    client.connectapi.side_effect = pages
    with store.lock(write=True):
        source = GarminSource(store, factory=lambda: client)
        with pytest.raises(BridgeError) as error:
            source.discover("2025-01-01", "2025-01-03")
    assert error.value.code == "upstream_invalid"


def test_validated_pagination_and_request_parameters(store):
    sign_in(store)
    client = sdk()
    client.connectapi.side_effect = [[activity(1)], [activity(2)], []]
    with store.lock(write=True):
        source = GarminSource(store, factory=lambda: client)
        items = source.discover("2025-01-01", "2025-01-03")
    assert [item["activityId"] for item in items] == [1, 2]
    calls = client.connectapi.call_args_list
    assert [c.kwargs["params"]["start"] for c in calls] == ["0", "1", "2"]
    assert calls[0].kwargs["params"]["activityType"] == "cycling"
    assert calls[0].kwargs["params"]["startDate"] == "2025-01-01"
    assert calls[0].kwargs["params"]["endDate"] == "2025-01-03"


@pytest.mark.parametrize(
    "exception,code",
    [
        (RuntimeError(SECRET), "network_failed"),
        (GarminConnectTooManyRequestsError(SECRET), "rate_limited"),
    ],
)
def test_exception_messages_are_not_exposed(exception, code, capsys):
    with pytest.raises(BridgeError) as error:
        invoke(Mock(side_effect=exception))
    assert error.value.code == code
    assert SECRET not in str(error.value) + capsys.readouterr().out
