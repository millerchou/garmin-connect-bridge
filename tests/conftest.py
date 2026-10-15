import socket

import pytest

from garmin_bridge.storage import Store


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Network calls are forbidden in offline tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    # curl_cffi uses native sockets, so patch it and requests explicitly too.
    import curl_cffi.requests
    import requests

    monkeypatch.setattr(curl_cffi.requests.Session, "request", denied)
    monkeypatch.setattr(requests.Session, "request", denied)
    monkeypatch.setattr("garmin_bridge.jobs.time.sleep", lambda _: None)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "private")
