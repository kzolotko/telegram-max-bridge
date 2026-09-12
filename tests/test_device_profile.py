"""Unit tests for the MAX device profile and its pymax serialisation.

Regression cover for 2026-09-12: MAX refused phone auth from the DESKTOP
profile with ``client.unsupported-version`` regardless of the claimed
version, while the WEB profile (what web.max.ru itself declares) passed.  The
token is bound to the declared profile, so the runtime handshake sent through
pymax must reproduce the auth-time dict exactly — including the *absence* of
``buildNumber`` / ``clientSessionId`` for WEB.
"""

import pytest

from src.max import device_profile
from src.max.bridge_client import build_user_agent
from pymax.payloads import SyncPayload


@pytest.fixture
def env(monkeypatch):
    def _set(device_type=None, app_version="26.9.6"):
        if device_type is None:
            monkeypatch.delenv("MAX_DEVICE_TYPE", raising=False)
        else:
            monkeypatch.setenv("MAX_DEVICE_TYPE", device_type)
        monkeypatch.setenv("MAX_APP_VERSION", app_version)
        monkeypatch.delenv("MAX_BUILD_NUMBER", raising=False)
    return _set


def test_default_is_web(env):
    env(None)
    ua = device_profile.user_agent_dict()
    assert ua["deviceType"] == "WEB"
    assert ua["pushDeviceType"] == "WEBPUSH"
    assert ua["isPwa"] is False
    assert ua["appVersion"] == "26.9.6"
    assert "buildNumber" not in ua
    assert "clientSessionId" not in ua


def test_desktop_still_reports_build(env):
    env("DESKTOP")
    ua = device_profile.user_agent_dict()
    assert ua["deviceType"] == "DESKTOP"
    assert ua["buildNumber"] == device_profile._DEFAULT_BUILD_NUMBER
    assert "pushDeviceType" not in ua


def test_runtime_model_matches_auth_dict_for_web(env, monkeypatch):
    env(None)
    fixed = dict(device_profile.user_agent_dict())
    monkeypatch.setattr(device_profile, "user_agent_dict", lambda: dict(fixed))
    monkeypatch.setattr("src.max.bridge_client.user_agent_dict", lambda: dict(fixed))

    wire = build_user_agent().model_dump(by_alias=True)
    assert wire == fixed

    # Nested inside pymax's LOGIN payload the same shape must survive.
    login = SyncPayload(token="t", user_agent=build_user_agent()).model_dump(by_alias=True)
    assert login["userAgent"] == fixed


def test_socket_client_accepts_web_profile(env, tmp_path):
    """pymax reserves WEB for its websocket client; the bridge lifts that gate."""
    from uuid import uuid4
    from pymax import SocketMaxClient

    env(None)
    client = SocketMaxClient(
        phone="+70000000000", token="t", device_id=uuid4(),
        send_fake_telemetry=False, reconnect=False,
        work_dir=str(tmp_path), headers=build_user_agent(),
    )
    assert client.user_agent.device_type == "WEB"


def test_runtime_model_matches_auth_dict_for_desktop(env, monkeypatch):
    env("DESKTOP")
    fixed = dict(device_profile.user_agent_dict())
    monkeypatch.setattr("src.max.bridge_client.user_agent_dict", lambda: dict(fixed))

    wire = build_user_agent().model_dump(by_alias=True)
    assert wire == fixed
    assert wire["buildNumber"] == fixed["buildNumber"]
