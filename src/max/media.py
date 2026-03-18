"""
MAX media upload/download helpers.

Upload flow (user account via WebSocket + HTTP):
1. Request upload URL via opcode 80
2. HTTP POST multipart to that URL
3. Use returned token in message attaches
"""

import aiohttp
from random import randint

from vkmax.client import MaxClient


UPLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Origin": "https://web.max.ru",
    "Referer": "https://web.max.ru/",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-Mode": "cors",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


async def get_upload_url(client: MaxClient) -> str:
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
    client: MaxClient,
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
        message["link"] = {"type": "REPLY", "messageId": str(reply_to)}

    return await client.invoke_method(
        opcode=64,
        payload={"chatId": chat_id, "message": message, "notify": True},
    )


async def get_file_upload_url(client: MaxClient) -> str:
    """Request a file upload URL via opcode 80 with type FILE."""
    response = await client.invoke_method(opcode=80, payload={"count": 1, "type": "FILE"})
    return response["payload"]["url"]


async def upload_file_to_url(
    upload_url: str, data: bytes, filename: str, content_type: str = "application/octet-stream"
) -> dict:
    """Upload file bytes to the MAX upload URL, return file info dict."""
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


async def send_file_message(
    client: MaxClient,
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
        message["link"] = {"type": "REPLY", "messageId": str(reply_to)}

    return await client.invoke_method(
        opcode=64,
        payload={"chatId": chat_id, "message": message, "notify": True},
    )


async def download_media(url: str) -> bytes:
    """Download media from a MAX CDN URL."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=UPLOAD_HEADERS) as resp:
            resp.raise_for_status()
            return await resp.read()
