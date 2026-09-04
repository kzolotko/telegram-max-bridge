"""Device profile declared to MAX in the login handshake.

MAX binds an auth token to the device profile presented when the token was
issued: logging in with a different ``deviceType`` fails with
``FAIL_WRONG_PASSWORD [login.cred]`` (verified 2026-08-09).  So the profile
must be identical in ``src.auth`` and at runtime — hence one shared source
here, selected by the ``MAX_DEVICE_TYPE`` environment variable.

DESKTOP is the default and keeps the historical behaviour.
"""

import logging
import os
from random import choice, randint
from typing import Any

log = logging.getLogger("bridge.max.device")

OS_VERSIONS = [
    "Windows 10", "Windows 11",
    "macOS Monterey", "macOS Ventura", "macOS Sonoma",
    "Ubuntu 22.04", "Fedora 38",
]

TIMEZONES = [
    "Europe/Moscow", "Europe/Kaliningrad", "Europe/Samara",
    "Asia/Yekaterinburg", "Asia/Novosibirsk", "Asia/Krasnoyarsk",
    "Asia/Irkutsk", "Asia/Vladivostok",
]

# Per-device overrides on top of the common fields below.
_PROFILES: dict[str, dict[str, Any]] = {
    "DESKTOP": {
        "deviceType": "DESKTOP",
        "deviceName": "vkmax Python",
        "osVersion": choice(OS_VERSIONS),
        "screen": "1080x1920 1.0x",
        "headerUserAgent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/137.0.0.0 Safari/537.36"
        ),
    },
    "ANDROID": {
        "deviceType": "ANDROID",
        "deviceName": "Pixel 7",
        "osVersion": "13",
        "screen": "1080x2400 2.625x",
        "headerUserAgent": (
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36"
        ),
    },
    "IOS": {
        "deviceType": "IOS",
        "deviceName": "iPhone14,5",
        "osVersion": "17.5.1",
        "screen": "1170x2532 3.0x",
        "headerUserAgent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
            "Mobile/15E148 Safari/604.1"
        ),
    },
}


# The version claimed to MAX matters twice over:
#
#  * MAX refuses new authentications from clients it considers outdated
#    (``client.unsupported-version``), so /authmax fails on a stale version;
#  * more subtly, MAX signs OK CDN video URLs differently for a stale client.
#    Claiming pymax's default 25.12.14 produced ``MP4_480`` links the CDN
#    answered with 400/10 (valid signature, refused) — which is why MAX video
#    never forwarded.  Claiming a current version returns the very same URL
#    shape and the CDN serves it.  Verified 2026-08-09 on 26.8.2: 400 → 200,
#    1.88 MB of video/mp4.
#
# ⚠️ Do NOT read this from web.max.ru.  The web client and the native apps are
# separate version lines: on 2026-09-04 the web client was 26.9.3 while native
# auth still rejected anything below 26.16.1.  Native SMS auth (native_client)
# is what the version gate applies to, so the web number is the wrong one.
#
# Find the real minimum with the handshake oracle — it costs nothing, since the
# handshake requests no SMS and so does not touch the auth rate limit:
# ``app-update-type`` in the opcode-6 reply is 1 when the server considers the
# claimed version outdated, and absent (or 0) when it accepts it.  Bisect the
# version string against that.  Verified 2026-09-04: the gate reads the
# appVersion *string* only — buildNumber is not validated (26.9.3 with build
# 999999 is rejected; 99.9.9 with build 18144 is accepted).
#
# Both are overridable via the environment.
_DEFAULT_APP_VERSION = "26.16.1"   # minimum accepted by MAX on 2026-09-04
_DEFAULT_BUILD_NUMBER = 18144      # not version-gated; kept for the CDN signature


def app_version() -> str:
    return (os.getenv("MAX_APP_VERSION") or _DEFAULT_APP_VERSION).strip()


def build_number() -> int:
    raw = (os.getenv("MAX_BUILD_NUMBER") or "").strip()
    if not raw:
        return _DEFAULT_BUILD_NUMBER
    try:
        return int(raw, 0)
    except ValueError:
        log.warning("Invalid MAX_BUILD_NUMBER=%r, using default", raw)
        return _DEFAULT_BUILD_NUMBER


def current_device_type() -> str:
    """The configured device type, falling back to DESKTOP if unknown."""
    name = (os.getenv("MAX_DEVICE_TYPE") or "DESKTOP").strip().upper()
    if name not in _PROFILES:
        log.warning("Unknown MAX_DEVICE_TYPE=%r, using DESKTOP", name)
        return "DESKTOP"
    return name


def user_agent_dict() -> dict[str, Any]:
    """The ``userAgent`` object sent in handshake/login packets."""
    profile = _PROFILES[current_device_type()]
    return {
        "locale": "ru",
        "deviceLocale": "ru",
        "appVersion": app_version(),
        "timezone": choice(TIMEZONES),
        "clientSessionId": randint(1, 15),
        "buildNumber": build_number(),
        **profile,
    }
