"""
Patched MaxClient that adds the required Origin header to the WebSocket connection.

The MAX WebSocket server (wss://ws-api.oneme.ru/websocket) rejects connections
without a valid Origin header (HTTP 403). The vkmax library's MaxClient.connect()
doesn't pass any headers, so we override connect() here.
"""

import asyncio
import websockets

from vkmax.client import MaxClient, WS_HOST


class PatchedMaxClient(MaxClient):
    """MaxClient with Origin header fix for the MAX WebSocket server."""

    async def connect(self):
        if self._connection:
            raise Exception("Already connected")

        self._connection = await websockets.connect(
            WS_HOST,
            origin="https://web.max.ru",
            additional_headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/137.0.0.0 Safari/537.36"
                )
            },
        )

        self._recv_task = asyncio.create_task(self._recv_loop())
        return self._connection
