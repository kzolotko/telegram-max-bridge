"""
MAX media upload/download helpers.

Upload flow (user account via native protocol + HTTP):
1. Request upload URL via opcode 80
2. HTTP POST multipart to that URL
3. Use returned token in message attaches
"""

import aiohttp
import logging as _logging
from random import randint
from typing import TYPE_CHECKING

_log = _logging.getLogger("bridge.max.media")

if TYPE_CHECKING:
    from .bridge_client import BridgeMaxClient


UPLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",
    "Origin": "https://web.max.ru",
    "Referer": "https://web.max.ru/",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-Mode": "cors",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


async def get_upload_url(client: "BridgeMaxClient") -> str:
    """Request a photo upload URL via opcode 80."""
    response = await client.invoke_method(opcode=80, payload={"count": 1})
    return response["payload"]["url"]


async def upload_photo_to_url(upload_url: str, data: bytes, filename: str = "photo.jpg") -> str:
    """Upload image bytes to the MAX upload URL, return photo token."""
    api_token = upload_url.split("apiToken=")[1].split("&")[0]

    form = aiohttp.FormData()
    form.add_field("file", data, filename=filename, content_type="image/jpeg")

    async with aiohttp.ClientSession() as session:
        async with session.post(
            upload_url,
            params={"apiToken": api_token},
            data=form,
            headers=UPLOAD_HEADERS,
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()

    photos = result.get("photos", {})
    first_photo = next(iter(photos.values()), None)
    if not first_photo:
        raise RuntimeError(f"No photo token in upload response: {result}")
    return first_photo["token"]


async def send_photo_message(
    client: "BridgeMaxClient",
    chat_id: int,
    photo_token: str,
    caption: str = "",
    reply_to: str | None = None,
) -> dict:
    """Send a message with a photo attachment."""
    message = {
        "text": caption,
        "cid": randint(1750000000000, 2000000000000),
        "elements": [],
        "attaches": [{"_type": "PHOTO", "photoToken": photo_token}],
    }
    if reply_to:
        # MAX requires messageId as integer, not string.
        message["link"] = {"type": "REPLY", "messageId": int(reply_to)}

    return await client.invoke_method(
        opcode=64,
        payload={"chatId": chat_id, "message": message, "notify": True},
    )


async def get_file_upload_url(client: "BridgeMaxClient") -> str:
    """Request a file upload URL via opcode 80 with type FILE."""
    response = await client.invoke_method(opcode=80, payload={"count": 1, "type": "FILE"})
    return response["payload"]["url"]


async def _do_upload(upload_url: str, data: bytes, filename: str, content_type: str) -> dict:
    """HTTP multipart upload, returns raw server response."""
    api_token = upload_url.split("apiToken=")[1].split("&")[0]
    form = aiohttp.FormData()
    form.add_field("file", data, filename=filename, content_type=content_type)
    async with aiohttp.ClientSession() as session:
        async with session.post(
            upload_url,
            params={"apiToken": api_token},
            data=form,
            headers=UPLOAD_HEADERS,
        ) as resp:
            resp.raise_for_status()
            return await resp.json()


async def upload_file_to_url(
    upload_url: str, data: bytes, filename: str, content_type: str = "application/octet-stream"
) -> dict:
    """Upload file bytes to the MAX upload URL, return file info dict.

    MAX runs video validation when the Content-Type is ``video/*`` or the
    filename has a video extension.  If validation fails (e.g. for a synthetic
    test file), the function automatically retries with
    ``application/octet-stream`` and a ``.bin`` extension so the file is
    stored as a generic binary attachment instead of a video.  If the retry
    also fails a ``RuntimeError`` is raised.

    The function always uses the FILE upload endpoint (opcode 80 with
    type=FILE), so the response contains FILE-type fields (fileId, size, …)
    rather than videoId/audioId.  We inject ``_type="FILE"`` when MAX omits it.
    """
    result = await _do_upload(upload_url, data, filename, content_type)
    _log.debug("upload_file_to_url response for %s (ct=%s): %r", filename, content_type, result)

    # If MAX rejected the upload due to video validation, retry as a generic
    # binary blob so that the file is at least delivered.  We also change the
    # file extension to strip video-related hints that MAX may use for detection.
    if result.get("error_data") == "VIDEO_VALIDATION_FAILED":
        import os as _os
        base, ext = _os.path.splitext(filename)
        fallback_filename = base + ".bin" if ext else filename
        _log.warning(
            "upload_file_to_url: VIDEO_VALIDATION_FAILED for %s — retrying as %s (octet-stream)",
            filename,
            fallback_filename,
        )
        result = await _do_upload(upload_url, data, fallback_filename, "application/octet-stream")
        _log.debug("upload_file_to_url retry response for %s: %r", fallback_filename, result)
        # If the retry also failed (e.g. MAX still detects video by magic bytes),
        # raise so the caller can decide how to handle it rather than silently
        # sending a message with a broken attachment.
        if result.get("error_data"):
            raise RuntimeError(
                f"upload_file_to_url: upload failed for {filename!r}: {result['error_data']!r}"
            )

    # Ensure _type is set — MAX omits it for some file types.
    if not result.get("_type"):
        result["_type"] = "FILE"

    return result


async def send_file_message(
    client: "BridgeMaxClient",
    chat_id: int,
    file_info: dict,
    caption: str = "",
    reply_to: str | None = None,
) -> dict:
    """Send a message with a file/video/audio attachment."""
    message = {
        "text": caption,
        "cid": randint(1750000000000, 2000000000000),
        "elements": [],
        "attaches": [file_info],
    }
    if reply_to:
        message["link"] = {"type": "REPLY", "messageId": int(reply_to)}

    return await client.invoke_method(
        opcode=64,
        payload={"chatId": chat_id, "message": message, "notify": True},
    )


async def send_multi_media_message(
    client: "BridgeMaxClient",
    chat_id: int,
    attaches: list[dict],
    caption: str = "",
    elements: list[dict] | None = None,
    reply_to: str | None = None,
) -> dict:
    """Send a single message with multiple attachments (photo/file/video mix)."""
    message = {
        "text": caption,
        "cid": randint(1750000000000, 2000000000000),
        "elements": elements or [],
        "attaches": attaches,
    }
    if reply_to:
        message["link"] = {"type": "REPLY", "messageId": int(reply_to)}

    return await client.invoke_method(
        opcode=64,
        payload={"chatId": chat_id, "message": message, "notify": True},
    )


async def download_media(url: str) -> bytes:
    """Download media from a MAX CDN URL.

    OK CDN signs URLs against a User-Agent family encoded in the URL itself
    (``srcAg=CHROME_ANDROID``/``CHROME``/...). The default UPLOAD_HEADERS use
    a desktop Chrome UA that works for photo CDN endpoints, but the video
    CDN (maxvdNNN.okcdn.ru) returns 400 when the UA family doesn't match.
    Pick a UA that matches the ``srcAg`` parameter when present.
    """
    headers = dict(UPLOAD_HEADERS)
    src_ag = ""
    try:
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(url).query)
        src_ag = (qs.get("srcAg", [""])[0] or "").upper()
    except Exception:
        pass

    UA_MAP = {
        "CHROME_ANDROID": (
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/137.0.0.0 Mobile Safari/537.36"
        ),
        "CHROME_MAC": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/137.0.0.0 Safari/537.36"
        ),
        "CHROME_WIN": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/137.0.0.0 Safari/537.36"
        ),
        "CHROME_LINUX": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/137.0.0.0 Safari/537.36"
        ),
        "GECKO": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) "
            "Gecko/20100101 Firefox/128.0"
        ),
        "GECKO_ANDROID": (
            "Mozilla/5.0 (Android 13; Mobile; rv:128.0) "
            "Gecko/128.0 Firefox/128.0"
        ),
        "SAFARI": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Safari/605.1.15"
        ),
        "SAFARI_IOS": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 Safari/604.1"
        ),
    }
    # Fallback prefix matching: GECKO* → GECKO, CHROME* → CHROME_MAC.
    ua = UA_MAP.get(src_ag)
    if not ua:
        if src_ag.startswith("GECKO"):
            ua = UA_MAP["GECKO"]
        elif src_ag.startswith("CHROME"):
            ua = UA_MAP["CHROME_MAC"]
        elif src_ag.startswith("SAFARI"):
            ua = UA_MAP["SAFARI"]
    if ua:
        headers["User-Agent"] = ua

    # OK CDN (okcdn.ru) rejects CORS-like requests with foreign Origin —
    # strip the MAX-specific CORS/Origin/Referer/Sec-Fetch-* headers for
    # video CDN hosts. Keep them only for the MAX upload endpoints where
    # they're needed.
    host = ""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        pass
    if "okcdn.ru" in host or "ok.ru" in host:
        for h in ("Origin", "Referer", "Sec-Fetch-Site", "Sec-Fetch-Mode",
                  "Accept-Language", "Accept-Encoding"):
            headers.pop(h, None)
        headers["Accept"] = "*/*"
        # Some CDN endpoints require a Range to start streaming.
        headers.setdefault("Range", "bytes=0-")

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status >= 400:
                body = await resp.read()
                _log.warning("download_media %s -> %d host=%s srcAg=%s body[:200]=%r",
                             url[:120], resp.status, host, src_ag, body[:200])
            resp.raise_for_status()
            return await resp.read()


async def try_download_media(urls: list[str]) -> bytes:
    """Try to download from a list of candidate URLs, returning the first
    successful payload. Raises the last error if all candidates fail."""
    last_exc: Exception | None = None
    for url in urls:
        try:
            return await download_media(url)
        except Exception as e:
            _log.debug("Candidate URL failed: %s: %s", url, e)
            last_exc = e
    if last_exc:
        raise last_exc
    raise RuntimeError("try_download_media: no URLs provided")
