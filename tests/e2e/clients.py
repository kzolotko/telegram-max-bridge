"""
Test clients for Telegram and MAX.

Each client connects independently (separate sessions) and provides:
  - send_text() — send a message into the test chat
  - wait_for()  — wait for a message matching a predicate (via asyncio.Queue)

These clients are designed to run alongside the bridge (which uses its own
sessions) so that tests can observe bridge-forwarded messages.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from pyrogram import Client, filters
from pyrogram.handlers import (
    MessageHandler,
    EditedMessageHandler,
    DeletedMessagesHandler,
)
from pyrogram.types import Message

from src.max.bridge_client import BridgeMaxClient

log = logging.getLogger("e2e.clients")

# Invisible zero-width marker used by the bridge to tag its own TG messages.
# Test client strips it so predicates can match on visible text only.
_MIRROR_MARKER = "\u200b"


# ── Unified event envelope ────────────────────────────────────────────────────

@dataclass
class ReceivedEvent:
    """A message/edit/delete captured by a test client."""

    kind: str  # "message", "edit", "delete"
    chat_id: int | str | None = None
    msg_id: str | None = None
    text: str | None = None
    sender_id: int | None = None
    raw: Any = None  # original object (Pyrogram Message or MAX dict)

    def get_reply_to_id(self) -> str | None:
        """Return the reply-to message ID (works for both TG and MAX raw objects)."""
        if self.raw is None:
            return None
        # Pyrogram Message
        if hasattr(self.raw, 'reply_to_message_id') and self.raw.reply_to_message_id:
            return str(self.raw.reply_to_message_id)
        # MAX message dict
        if isinstance(self.raw, dict):
            link = self.raw.get("link", {})
            if link and link.get("type") == "REPLY":
                msg_id = link.get("messageId")
                if msg_id:
                    return str(msg_id)
        return None


# ── Telegram test client ─────────────────────────────────────────────────────

class TgTestClient:
    """Pyrogram-based test client that captures incoming messages via a Queue."""

    def __init__(
        self,
        session_name: str,
        api_id: int,
        api_hash: str,
        chat_id: int,
        sessions_dir: str = "sessions",
    ):
        self._client = Client(
            name=session_name,
            api_id=api_id,
            api_hash=api_hash,
            workdir=sessions_dir,
        )
        self.chat_id = chat_id
        self._queue: asyncio.Queue[ReceivedEvent] = asyncio.Queue()

    async def start(self) -> None:
        await self._client.start()

        chat_filter = filters.chat(self.chat_id)
        self._client.add_handler(MessageHandler(self._on_message, chat_filter))
        self._client.add_handler(
            EditedMessageHandler(self._on_edited, chat_filter)
        )
        # No filter for deletes (same reason as bridge — regular groups
        # don't include chat info in delete callbacks).
        self._client.add_handler(DeletedMessagesHandler(self._on_deleted))

        me = await self._client.get_me()
        log.info(
            "TG test client started: @%s (ID: %d) → chat %s",
            me.username, me.id, self.chat_id,
        )

    async def stop(self) -> None:
        try:
            await self._client.stop()
        except Exception:
            pass

    # ── Send ──────────────────────────────────────────────────────────────────

    async def send_text(self, text: str, reply_to: int | None = None) -> Message:
        return await self._client.send_message(
            self.chat_id, text,
            reply_to_message_id=reply_to,
        )

    async def send_photo(self, path: str, caption: str = "") -> Message:
        return await self._client.send_photo(self.chat_id, path, caption=caption)

    async def send_reaction(self, msg_id: int, emoji: str | None) -> None:
        await self._client.send_reaction(
            self.chat_id, msg_id, [emoji] if emoji else []
        )

    async def delete_messages(self, msg_ids: list[int]) -> None:
        await self._client.delete_messages(self.chat_id, msg_ids)

    async def edit_message(self, msg_id: int, new_text: str) -> Message:
        return await self._client.edit_message_text(self.chat_id, msg_id, new_text)

    # ── Receive ───────────────────────────────────────────────────────────────

    async def wait_for(
        self,
        predicate: Callable[[ReceivedEvent], bool],
        timeout: float = 15.0,
    ) -> ReceivedEvent | None:
        """Wait for an event matching *predicate*. Returns None on timeout."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return None
            try:
                evt = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                if predicate(evt):
                    return evt
            except asyncio.TimeoutError:
                return None

    def drain(self) -> list[ReceivedEvent]:
        """Drain all currently queued events (non-blocking). Useful for cleanup."""
        events = []
        while not self._queue.empty():
            try:
                events.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return events

    # ── Handlers ──────────────────────────────────────────────────────────────

    async def _on_message(self, _client: Client, message: Message) -> None:
        text = message.text or message.caption or ""
        if text.startswith(_MIRROR_MARKER):
            text = text[len(_MIRROR_MARKER):]
        await self._queue.put(ReceivedEvent(
            kind="message",
            chat_id=message.chat.id,
            msg_id=str(message.id),
            text=text or None,
            sender_id=message.from_user.id if message.from_user else None,
            raw=message,
        ))

    async def _on_edited(self, _client: Client, message: Message) -> None:
        text = message.text or message.caption or ""
        if text.startswith(_MIRROR_MARKER):
            text = text[len(_MIRROR_MARKER):]
        await self._queue.put(ReceivedEvent(
            kind="edit",
            chat_id=message.chat.id,
            msg_id=str(message.id),
            text=text or None,
            sender_id=message.from_user.id if message.from_user else None,
            raw=message,
        ))

    async def _on_deleted(self, _client: Client, messages: list) -> None:
        for msg in messages:
            # Pyrogram's deleted message may have .id but no .chat
            chat_id = getattr(msg, "chat", None)
            if chat_id and hasattr(chat_id, "id"):
                chat_id = chat_id.id
            else:
                chat_id = None
            await self._queue.put(ReceivedEvent(
                kind="delete",
                chat_id=chat_id,
                msg_id=str(msg.id) if hasattr(msg, "id") else None,
                raw=msg,
            ))


# ── MAX test client ──────────────────────────────────────────────────────────

class MaxTestClient:
    """PyMax-based test client that captures incoming messages via a Queue.

    Uses a separate device_id from the bridge so both can run simultaneously.
    """

    def __init__(
        self,
        login_token: str,
        device_id: str,
        chat_id: int,
    ):
        self.chat_id = chat_id
        self._token = login_token
        self._device_id = device_id
        self._client: BridgeMaxClient | None = None
        self._queue: asyncio.Queue[ReceivedEvent] = asyncio.Queue()

    async def start(self) -> None:
        self._client = BridgeMaxClient(
            token=self._token, device_id=self._device_id,
        )
        self._client.set_raw_callback(self._on_packet)
        await self._client.connect_and_login()

        user_id = None
        if self._client.inner.me:
            user_id = self._client.inner.me.id
        log.info("MAX test client started: user_id=%s → chat %s", user_id, self.chat_id)

    async def stop(self) -> None:
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass

    # ── Send ──────────────────────────────────────────────────────────────────

    async def send_text(self, text: str, reply_to: str | None = None) -> str | None:
        """Send text message, return MAX msg_id as string (or None)."""
        resp = await self._client.send_message(
            self.chat_id, text,
            reply_to=int(reply_to) if reply_to else None,
        )
        payload = resp.get("payload", {})
        msg = payload.get("message", {})
        return str(msg.get("id")) if msg.get("id") else None

    async def send_text_with_elements(
        self, text: str, elements: list[dict], reply_to: str | None = None,
    ) -> str | None:
        """Send text with explicit formatting elements (for formatting tests)."""
        resp = await self._client.send_message(
            self.chat_id, text,
            reply_to=int(reply_to) if reply_to else None,
            elements=elements,
        )
        payload = resp.get("payload", {})
        msg = payload.get("message", {})
        return str(msg.get("id")) if msg.get("id") else None

    async def edit_message(self, msg_id: str, new_text: str) -> None:
        await self._client.edit_message(self.chat_id, int(msg_id), new_text)

    async def delete_messages(self, msg_ids: list[str]) -> None:
        await self._client.delete_message(self.chat_id, [int(m) for m in msg_ids])

    async def add_reaction(self, msg_id: str, emoji: str) -> None:
        await self._client.add_reaction(self.chat_id, msg_id, emoji)

    async def remove_reaction(self, msg_id: str) -> None:
        await self._client.remove_reaction(self.chat_id, msg_id)

    # ── Receive ───────────────────────────────────────────────────────────────

    async def wait_for(
        self,
        predicate: Callable[[ReceivedEvent], bool],
        timeout: float = 15.0,
    ) -> ReceivedEvent | None:
        """Wait for an event matching *predicate*. Returns None on timeout."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return None
            try:
                evt = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                if predicate(evt):
                    return evt
            except asyncio.TimeoutError:
                return None

    def drain(self) -> list[ReceivedEvent]:
        """Drain all currently queued events (non-blocking)."""
        events = []
        while not self._queue.empty():
            try:
                events.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return events

    # ── Raw packet handler ────────────────────────────────────────────────────

    async def _on_packet(self, data: dict[str, Any]) -> None:
        opcode = data.get("opcode", 0)

        if opcode == 128:  # NOTIF_MESSAGE
            await self._on_notif_message(data)
        elif opcode in (66, 142):  # MSG_DELETE / NOTIF_MSG_DELETE
            await self._on_delete(data)

    async def _on_notif_message(self, data: dict) -> None:
        payload = data.get("payload", {})
        chat_id = payload.get("chatId")
        if chat_id != self.chat_id:
            return

        message = payload.get("message", {})
        msg_id = message.get("id")
        status = message.get("status")
        text = message.get("text")
        sender_id = message.get("sender") or payload.get("fromUserId")

        if status == "EDITED":
            await self._queue.put(ReceivedEvent(
                kind="edit",
                chat_id=chat_id,
                msg_id=str(msg_id) if msg_id else None,
                text=text,
                sender_id=sender_id,
                raw=message,
            ))
        elif status == "REMOVED":
            await self._queue.put(ReceivedEvent(
                kind="delete",
                chat_id=chat_id,
                msg_id=str(msg_id) if msg_id else None,
                raw=message,
            ))
        elif status is None:
            # New message
            await self._queue.put(ReceivedEvent(
                kind="message",
                chat_id=chat_id,
                msg_id=str(msg_id) if msg_id else None,
                text=text,
                sender_id=sender_id,
                raw=message,
            ))

    async def _on_delete(self, data: dict) -> None:
        payload = data.get("payload", {})
        chat_id = payload.get("chatId")
        if chat_id != self.chat_id:
            return
        for mid in payload.get("messageIds", []):
            await self._queue.put(ReceivedEvent(
                kind="delete",
                chat_id=chat_id,
                msg_id=str(mid) if mid else None,
                raw=data,
            ))
