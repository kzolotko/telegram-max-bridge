"""
Bridge-friendly wrapper around PyMax's SocketMaxClient.

Uses native TCP/SSL protocol (api.oneme.ru:443) with binary msgpack framing.
Tokens obtained via NativeMaxAuth work here (unlike vkmax WebSocket).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from pymax import SocketMaxClient
from pymax.payloads import UserAgentPayload
from pymax.static.enum import Opcode

log = logging.getLogger("bridge.max.client")

# Dummy phone — required by PyMax constructor but unused for token auth.
_DUMMY_PHONE = "+70000000000"


class BridgeMaxClient:
    """Thin wrapper around PyMax SocketMaxClient for bridge use.

    Provides:
    - Token-based connect + login (no phone auth at runtime)
    - invoke_method(opcode, payload) for compatibility with media.py
    - Raw packet callback for listener.py
    - Clean disconnect
    """

    def __init__(self, token: str, device_id: str) -> None:
        self._token = token
        self._device_id = device_id
        self._inner: SocketMaxClient | None = None
        self._raw_callback: Any = None

    async def connect_and_login(self) -> dict[str, Any]:
        """Connect via TCP/SSL, handshake, and login with stored token."""
        self._inner = SocketMaxClient(
            phone=_DUMMY_PHONE,
            token=self._token,
            device_id=UUID(self._device_id),
            send_fake_telemetry=False,
            reconnect=False,
            work_dir="/tmp/pymax_bridge",
        )
        # Silence PyMax's own logger to avoid duplicate output
        self._inner.logger.setLevel(logging.WARNING)

        # If we registered a raw callback before connect, attach it now
        if self._raw_callback is not None:
            self._inner.add_raw_receive_handler(self._raw_callback)

        # connect() does TCP/SSL + handshake (opcode 6)
        await self._inner.connect(self._inner.user_agent)

        # _sync() does login (opcode 19) with token
        await self._inner._sync(self._inner.user_agent)

        # Start ping and background tasks
        await self._inner._post_login_tasks(sync=False)

        log.info("Connected and logged in via native protocol (device_id=%s)", self._device_id)
        return {}

    def set_raw_callback(self, callback: Any) -> None:
        """Register a callback for all incoming raw packets.

        Must be called before connect_and_login().
        Signature: async def callback(data: dict) -> None
        """
        self._raw_callback = callback
        if self._inner is not None:
            self._inner.add_raw_receive_handler(callback)

    @property
    def inner(self) -> SocketMaxClient:
        if self._inner is None:
            raise RuntimeError("Not connected")
        return self._inner

    # ── Message operations (delegate to PyMax) ─────────────────

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
    ) -> dict[str, Any]:
        """Send a text message, optionally as a reply."""
        msg = await self.inner.send_message(
            text=text,
            chat_id=chat_id,
            reply_to=reply_to,
        )
        # Convert PyMax Message to dict for compatibility
        if msg is None:
            return {}
        return {"payload": {"message": {"id": msg.id, "chatId": msg.chat_id}}}

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> dict[str, Any]:
        """Edit an existing message."""
        msg = await self.inner.edit_message(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
        )
        if msg is None:
            return {}
        return {"payload": {"message": {"id": msg.id, "chatId": msg.chat_id}}}

    async def delete_message(
        self,
        chat_id: int,
        message_ids: list[int],
    ) -> bool:
        """Delete messages."""
        return await self.inner.delete_message(
            chat_id=chat_id,
            message_ids=message_ids,
            for_me=False,
        )

    async def get_users(self, user_ids: list[int]) -> list[Any]:
        """Resolve user info by IDs."""
        return await self.inner.get_users(user_ids)

    # ── Raw protocol access (for media.py compatibility) ───────

    async def invoke_method(self, opcode: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a raw opcode+payload and wait for response.

        Compatibility layer for media.py which uses raw opcodes.
        """
        data = await self.inner._send_and_wait(
            opcode=Opcode(opcode),
            payload=payload,
        )
        return data

    # ── Lifecycle ──────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._inner is not None and self._inner.is_connected

    @property
    def recv_task(self) -> asyncio.Task | None:
        """Expose recv_task for reconnect monitoring."""
        if self._inner is None:
            return None
        return self._inner._recv_task

    async def disconnect(self) -> None:
        """Clean shutdown."""
        if self._inner is not None:
            try:
                await self._inner.close()
            except Exception:
                pass
            self._inner = None
            log.info("Disconnected")
