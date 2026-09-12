"""Device profile declared to MAX in the login handshake.

MAX binds an auth token to the device profile presented when the token was
issued: logging in with a different ``deviceType`` fails with
``FAIL_WRONG_PASSWORD [login.cred]`` (verified 2026-08-09).  So the profile
must be identical in ``src.auth`` and at runtime — hence one shared source
here, selected by the ``MAX_DEVICE_TYPE`` environment variable.

WEB is the default since 2026-09-12: MAX stopped accepting phone
authentication from the DESKTOP profile at all — the handshake answers with
``app-update-type`` and no ``phone-auth-enabled`` flag, and ``START_AUTH``
fails with ``client.unsupported-version`` whatever ``appVersion`` is claimed
(the desktop app evidently has its own version line).  The WEB profile with
the live web client's ``appVersion`` is accepted.  Existing DESKTOP tokens keep
working only with ``MAX_DEVICE_TYPE=DESKTOP`` set explicitly.
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
    # Mirrors the SESSION_INIT userAgent of web.max.ru (2026-09-12).  Note the
    # web client sends neither buildNumber nor clientSessionId — see
    # user_agent_dict().
    "WEB": {
        "deviceType": "WEB",
        "pushDeviceType": "WEBPUSH",
        "isPwa": False,
        "deviceName": "Chrome",
        "osVersion": "macOS 10.15.7",
        "screen": "1117x1728 2.0x",
        "headerUserAgent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
    },
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
#    never forwarded.  Claiming 26.8.2 returns the very same URL shape and the
#    CDN serves it.  Verified 2026-08-09: 400 → 200, 1.88 MB of video/mp4.
#
# Taken from the live web client (``window.APP_VERSION`` on web.max.ru).
# Raise these when MAX moves on; both are overridable via the environment.
_DEFAULT_APP_VERSION = "26.9.6"
_DEFAULT_BUILD_NUMBER = 18433


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


DEFAULT_DEVICE_TYPE = "WEB"


def current_device_type() -> str:
    """The configured device type, falling back to the default if unknown."""
    name = (os.getenv("MAX_DEVICE_TYPE") or DEFAULT_DEVICE_TYPE).strip().upper()
    if name not in _PROFILES:
        log.warning("Unknown MAX_DEVICE_TYPE=%r, using %s", name, DEFAULT_DEVICE_TYPE)
        return DEFAULT_DEVICE_TYPE
    return name


def user_agent_dict() -> dict[str, Any]:
    """The ``userAgent`` object sent in handshake/login packets.

    Must be byte-for-byte the same shape at auth time (``NativeMaxAuth``) and
    at runtime (pymax login): MAX binds the token to the declared profile.
    """
    device_type = current_device_type()
    profile = _PROFILES[device_type]
    ua: dict[str, Any] = {
        "locale": "ru",
        "deviceLocale": "ru",
        "appVersion": app_version(),
        "timezone": choice(TIMEZONES),
    }
    if device_type != "WEB":
        # Native apps report a build; the web client does not have one.
        ua["clientSessionId"] = randint(1, 15)
        ua["buildNumber"] = build_number()
    return {**ua, **profile}
