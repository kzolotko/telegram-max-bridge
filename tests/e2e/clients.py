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
from uuid import uuid4

from pyrogram import Client, filters
from pyrogram.handlers import (
    MessageHandler,
    EditedMessageHandler,
    DeletedMessagesHandler,
    RawUpdateHandler,
)
from pyrogram.raw.types import UpdateMessageReactions, PeerChannel, PeerChat, ReactionEmoji
from pyrogram.types import Message

from src.max.bridge_client import BridgeMaxClient
from src.max.media import (
    get_upload_url,
    upload_photo_to_url,
    get_file_upload_url,
    upload_file_to_url,
    send_photo_message,
    send_file_message,
    send_multi_media_message,
)

log = logging.getLogger("e2e.clients")

# Invisible zero-width marker used by the bridge to tag its own TG messages.
# Test client strips it so predicates can match on visible text only.
_MIRROR_MARKER = "\u200b"


# ── Unified event envelope ────────────────────────────────────────────────────

@dataclass
class ReceivedEvent:
    """A message/edit/delete captured by a test client."""

    kind: str  # "message", "edit", "delete", "reaction"
    chat_id: int | str | None = None
    msg_id: str | None = None
    text: str | None = None
    emoji: str | None = None  # for reaction events: the emoji, or None if removed
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

        # Warm up peer cache: the test session is fresh and may not know the
        # chat_id yet.  get_dialogs() fetches peers directly from Telegram and
        # stores them in the local SQLite, so resolve_peer() can find them
        # without falling back to get_peer_type() (which rejects non-standard
        # group IDs like -4845290322 that exceed MIN_CHAT_ID = -2147483647).
        try:
            async for _ in self._client.get_dialogs():
                pass
        except Exception as exc:
            log.warning("TG test client: get_dialogs failed (peer cache may be empty): %s", exc)

        chat_filter = filters.chat(self.chat_id)
        self._client.add_handler(MessageHandler(self._on_message, chat_filter))
        self._client.add_handler(
            EditedMessageHandler(self._on_edited, chat_filter)
        )
        # No filter for deletes (same reason as bridge — regular groups
        # don't include chat info in delete callbacks).
        self._client.add_handler(DeletedMessagesHandler(self._on_deleted))
        # Raw update handler for reactions (no chat-level filter available)
        self._client.add_handler(RawUpdateHandler(self._on_raw_update))

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

    async def send_video(self, path: str, caption: str = "") -> Message:
        return await self._client.send_video(self.chat_id, path, caption=caption)

    async def send_audio(self, path: str, caption: str = "") -> Message:
        return await self._client.send_audio(self.chat_id, path, caption=caption)

    async def send_voice(self, path: str, caption: str = "") -> Message:
        return await self._client.send_voice(self.chat_id, path, caption=caption)

    async def send_document(self, path: str, caption: str = "") -> Message:
        return await self._client.send_document(self.chat_id, path, caption=caption)

    async def send_poll(self, question: str, options: list[str]) -> Message:
        return await self._client.send_poll(self.chat_id, question, options)

    async def send_media_group(self, media: list) -> list[Message]:
        """Send an album (list of InputMediaPhoto / InputMediaVideo)."""
        return await self._client.send_media_group(self.chat_id, media)

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

    async def wait_for_album(
        self,
        predicate: Callable[[ReceivedEvent], bool],
        timeout: float = 15.0,
        collect_timeout: float = 2.0,
    ) -> list[ReceivedEvent]:
        """Wait for the first album message matching *predicate*, then collect
        remaining messages with the same ``media_group_id``.

        Returns a list of ReceivedEvents (one per album item), or [] on timeout.
        """
        first = await self.wait_for(predicate, timeout=timeout)
        if first is None:
            return []
        group_id = getattr(first.raw, "media_group_id", None)
        if not group_id:
            return [first]

        # Collect remaining album items
        album = [first]
        deadline = asyncio.get_event_loop().time() + collect_timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                evt = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                raw_gid = getattr(evt.raw, "media_group_id", None)
                if evt.kind == "message" and raw_gid == group_id:
                    album.append(evt)
                else:
                    # Put non-matching event back (best effort — prepend impossible,
                    # so we just re-queue; order may shift but it's acceptable for tests)
                    await self._queue.put(evt)
            except asyncio.TimeoutError:
                break
        return album

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

    async def _on_raw_update(self, _client: Client, update, users, chats) -> None:
        """Capture UpdateMessageReactions for reaction tests."""
        if not isinstance(update, UpdateMessageReactions):
            return

        # Resolve chat_id from peer
        peer = update.peer
        if isinstance(peer, PeerChannel):
            chat_id = -1000000000000 - peer.channel_id
        elif isinstance(peer, PeerChat):
            chat_id = -peer.chat_id
        else:
            return

        if chat_id != self.chat_id:
            return

        # Find the emoji with chosen_order set (our own reaction)
        our_emoji: str | None = None
        for rc in update.reactions.results:
            if rc.chosen_order is not None and isinstance(rc.reaction, ReactionEmoji):
                our_emoji = rc.reaction.emoticon
                break

        await self._queue.put(ReceivedEvent(
            kind="reaction",
            chat_id=chat_id,
            msg_id=str(update.msg_id),
            emoji=our_emoji,
            raw=update,
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
        # Use a fresh device_id every run so MAX treats this as a brand-new
        # device and skips the reconnect-state handshake (a session-resume
        # packet that pymax can't decode, causing the LOGIN to time out).
        fresh_device_id = str(uuid4())
        self._client = BridgeMaxClient(
            token=self._token, device_id=fresh_device_id,
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

    async def send_photo(
        self, data: bytes, filename: str = "photo.jpg", caption: str = "",
    ) -> str | None:
        """Upload a photo and send it. Returns MAX msg_id."""
        upload_url = await get_upload_url(self._client)
        token = await upload_photo_to_url(upload_url, data, filename)
        resp = await send_photo_message(self._client, self.chat_id, token, caption)
        payload = resp.get("payload", {})
        msg = payload.get("message", {})
        return str(msg.get("id")) if msg.get("id") else None

    async def send_file(
        self, data: bytes, filename: str, content_type: str = "application/octet-stream",
        caption: str = "",
    ) -> str | None:
        """Upload a file/video and send it. Returns MAX msg_id."""
        upload_url = await get_file_upload_url(self._client)
        file_info = await upload_file_to_url(upload_url, data, filename, content_type)
        resp = await send_file_message(self._client, self.chat_id, file_info, caption)
        payload = resp.get("payload", {})
        msg = payload.get("message", {})
        return str(msg.get("id")) if msg.get("id") else None

    async def send_media_multi(
        self, items: list[tuple[bytes, str, str]], caption: str = "",
    ) -> str | None:
        """Upload multiple media items and send as one message.

        *items*: list of (data, filename, mime_type) tuples.
        Returns MAX msg_id.
        """
        attaches: list[dict] = []
        for data, fname, mime in items:
            if mime.startswith("image/"):
                url = await get_upload_url(self._client)
                token = await upload_photo_to_url(url, data, fname)
                attaches.append({"_type": "PHOTO", "photoToken": token})
            else:
                url = await get_file_upload_url(self._client)
                file_info = await upload_file_to_url(url, data, fname, mime)
                attaches.append(file_info)
        resp = await send_multi_media_message(
            self._client, self.chat_id, attaches, caption,
        )
        payload = resp.get("payload", {})
        msg = payload.get("message", {})
        return str(msg.get("id")) if msg.get("id") else None

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
        elif opcode == 155:  # NOTIF_MSG_REACTIONS_CHANGED
            await self._on_reaction(data)

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

    async def _on_reaction(self, data: dict) -> None:
        """Handle NOTIF_MSG_REACTIONS_CHANGED (opcode 155)."""
        payload = data.get("payload", {})
        chat_id = payload.get("chatId")
        if chat_id != self.chat_id:
            return
        msg_id = payload.get("messageId")
        # yourReaction: the emoji string for this account, or absent/None if removed
        your_reaction: str | None = payload.get("yourReaction") or None
        # Also check the reactions array for any reaction that was set
        reactions = payload.get("reactions", [])
        top_emoji: str | None = reactions[0].get("reaction") if reactions else None

        await self._queue.put(ReceivedEvent(
            kind="reaction",
            chat_id=chat_id,
            msg_id=str(msg_id) if msg_id else None,
            emoji=your_reaction or top_emoji,
            raw=data,
        ))
