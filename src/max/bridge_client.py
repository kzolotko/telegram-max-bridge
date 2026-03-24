"""
Bridge-friendly wrapper around PyMax's SocketMaxClient.

Uses native TCP/SSL protocol (api.oneme.ru:443) with binary msgpack framing.
Tokens obtained via NativeMaxAuth work here (unlike vkmax WebSocket).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from uuid import UUID

from pymax import SocketMaxClient
from pymax.payloads import UserAgentPayload
from pymax.static.enum import Opcode

# Apply runtime patch for pymax LZ4 buffer-size bug (see _pymax_patch.py).
from . import _pymax_patch as _  # noqa: F401

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
        elements: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Send a text message, optionally as a reply.

        If *elements* are provided, or *reply_to* is set, the message is sent
        via the raw protocol path so we control the exact msgpack types.
        PyMax's ReplyLink serialises messageId as a *string*, but the MAX
        server requires an *integer* — using _send_message_raw avoids this.
        """
        if elements is not None or reply_to is not None:
            return await self._send_message_raw(chat_id, text, reply_to, elements or [])

        msg = await self.inner.send_message(
            text=text,
            chat_id=chat_id,
            reply_to=reply_to,
        )
        # Convert PyMax Message to dict for compatibility
        if msg is None:
            return {}
        return {"payload": {"message": {"id": msg.id, "chatId": msg.chat_id}}}

    async def _send_message_raw(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None,
        elements: list[dict],
    ) -> dict[str, Any]:
        """Send message with explicit elements, bypassing PyMax markdown parsing."""
        link = None
        if reply_to:
            # MAX protocol requires messageId as integer, not string.
            link = {"type": "REPLY", "messageId": int(reply_to)}

        payload = {
            "chatId": chat_id,
            "message": {
                "text": text,
                "cid": int(time.time() * 1000),
                "elements": elements,
                "attaches": [],
                **({"link": link} if link else {}),
            },
            "notify": False,
        }

        data = await self.inner._send_and_wait(
            opcode=Opcode(64),  # MSG_SEND
            payload=payload,
        )
        if not data or not data.get("payload"):
            return {}
        msg = data["payload"].get("message", {})
        return {"payload": {"message": {"id": msg.get("id"), "chatId": msg.get("chatId")}}}

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        elements: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Edit an existing message.

        If *elements* are provided they are sent directly, bypassing PyMax's
        own markdown parser.
        """
        if elements is not None:
            return await self._edit_message_raw(chat_id, message_id, text, elements)

        msg = await self.inner.edit_message(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
        )
        if msg is None:
            return {}
        return {"payload": {"message": {"id": msg.id, "chatId": msg.chat_id}}}

    async def _edit_message_raw(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        elements: list[dict],
    ) -> dict[str, Any]:
        """Edit message with explicit elements, bypassing PyMax markdown parsing."""
        payload = {
            "chatId": chat_id,
            "messageId": message_id,
            "text": text,
            "elements": elements,
            "attaches": [],
        }

        data = await self.inner._send_and_wait(
            opcode=Opcode(67),  # MSG_EDIT
            payload=payload,
        )
        if not data or not data.get("payload"):
            return {}
        msg = data["payload"].get("message", {})
        return {"payload": {"message": {"id": msg.get("id"), "chatId": msg.get("chatId")}}}

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

    async def add_reaction(self, chat_id: int, message_id: str, emoji: str) -> None:
        """Add an emoji reaction to a MAX message."""
        # Bypass pymax.add_reaction which serializes messageId as string —
        # MAX server requires it as an integer (validation error otherwise).
        await self.invoke_method(opcode=178, payload={  # MSG_REACTION
            "chatId": chat_id,
            "messageId": int(message_id),
            "reaction": {"id": emoji},
        })

    async def remove_reaction(self, chat_id: int, message_id: str) -> None:
        """Remove our reaction from a MAX message."""
        await self.invoke_method(opcode=179, payload={  # MSG_CANCEL_REACTION
            "chatId": chat_id,
            "messageId": int(message_id),
        })

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
        """Clean shutdown — cancel all pymax background tasks then close socket."""
        if self._inner is not None:
            # _cleanup_client() cancels recv_task, outgoing_task and all
            # background tasks (ping etc.) started by _post_login_tasks().
            # The base close() is a no-op, so we must call this explicitly.
            try:
                await self._inner._cleanup_client()
            except Exception:
                pass
            try:
                await self._inner.close()
            except Exception:
                pass
            self._inner = None
            log.info("Disconnected")
