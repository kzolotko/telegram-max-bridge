"""Regression tests for MAX upload URLs without the legacy apiToken query."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web

from src.bridge.bridge import Bridge
from src.config import ConfigLookup
from src.max.client_pool import MaxClientPool
from src.max.media import upload_file_to_url, upload_photo_to_url
from src.types import AppConfig, BridgeEntry, BridgeEvent, MediaInfo, UserMapping


@asynccontextmanager
async def upload_server(result):
    requests = []

    async def receive(request):
        multipart = await request.multipart()
        part = await multipart.next()
        requests.append({
            "query": list(request.query.items()),
            "field": part.name,
            "filename": part.filename,
            "content_type": part.headers["Content-Type"],
            "data": bytes(await part.read()),
        })
        return web.json_response(result)

    app = web.Application()
    app.router.add_post("/upload", receive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/upload", requests
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("query,expected", [
    ("r=signed%2Btoken%2Fpart%3D", [("r", "signed+token/part=")]),
    ("apiToken=legacy%2Btoken&count=1", [("apiToken", "legacy+token"), ("count", "1")]),
])
@pytest.mark.parametrize("kind", ["photo", "file"])
async def test_upload_uses_the_supplied_url_without_adding_auth_params(kind, query, expected):
    result = {"photos": {"0": {"token": "photo-token"}}} if kind == "photo" else {"fileId": 42}
    async with upload_server(result) as (url, requests):
        if kind == "photo":
            uploaded = await upload_photo_to_url(f"{url}?{query}", b"media", "photo.jpg")
            assert uploaded == "photo-token"
            content_type = "image/jpeg"
            filename = "photo.jpg"
        else:
            uploaded = await upload_file_to_url(f"{url}?{query}", b"media", "file.txt", "text/plain")
            assert uploaded == {"fileId": 42, "_type": "FILE"}
            content_type = "text/plain"
            filename = "file.txt"

    assert requests == [{
        "query": expected, "field": "file", "filename": filename,
        "content_type": content_type, "data": b"media",
    }]


@pytest.mark.parametrize("kind", ["photo", "media_group"])
@pytest.mark.parametrize("message_id", [None, "12345"])
async def test_photo_forward_is_logged_only_after_max_confirms_send(caplog, kind, message_id):
    user = UserMapping("alice", 555, 777)
    entry = BridgeEntry("test", -1001, 2002, user)
    config = AppConfig(api_id=1, api_hash="h", users=[user], bridges=[entry])
    pool = AsyncMock(spec=MaxClientPool)
    pool.send_photo.return_value = message_id
    pool.send_media_multi.return_value = message_id
    store = Mock()
    mirrors = Mock()
    bridge = Bridge(ConfigLookup(config), store, None, pool, mirrors)
    media = MediaInfo(b"jpeg", "photo.jpg", "image/jpeg")
    event = BridgeEvent(
        direction="tg-to-max", bridge_entry=entry, sender_display_name="Alice",
        sender_user_id=555, event_type=kind, source_msg_id=10,
        media=media if kind == "photo" else None,
        media_list=[media, media] if kind == "media_group" else None,
    )

    with caplog.at_level("INFO", logger="bridge.core"):
        await bridge.handle_event(event)

    if message_id:
        assert f"Forwarded tg-to-max {kind}" in caplog.text
        assert "Error tg-to-max" not in caplog.text
        store.store.assert_called_once()
        mirrors.mark_max.assert_called_once_with(message_id)
    else:
        assert f"Error tg-to-max {kind}" in caplog.text
        assert "Forwarded" not in caplog.text
        store.store.assert_not_called()
        mirrors.mark_max.assert_not_called()
