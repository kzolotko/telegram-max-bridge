from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from pymax.exceptions import SocketNotConnectedError, SocketSendError
from pymax.files import File as PyMaxFile

from .bridge_client import BridgeMaxClient
from ..types import AppConfig, MediaInfo, UserMapping
from .session import MaxSession
from .media import (
    get_upload_url, upload_photo_to_url, send_photo_message,
    get_file_upload_url, upload_file_to_url,
    send_file_message,
    send_multi_media_message,
)

if TYPE_CHECKING:
    from .listener import MaxListener


log = logging.getLogger("bridge.max.pool")

# Number of automatic retry attempts on send failure.
_MAX_RETRIES = 2

# Seconds to wait for the listener's reconnect loop to recover
# before retrying a failed send.  The listener detects dead connections
# instantly (via ``await recv_task``) and reconnects with ~3-4 s total
# (2 s delay + 1-2 s connect + 1 s post-login).
_LISTENER_RECONNECT_WAIT = 4

# Only these exception types indicate a broken connection worth retrying.
# Server-side errors (rate limits, session state, etc.) are NOT connection
# problems — retrying on those just causes cascading failures.
#
# TimeoutError is included because a send that times out waiting for a response
# almost always means a dead socket (half-open TCP connection where the server
# stopped responding without sending FIN/RST).
_CONNECTION_ERRORS = (SocketNotConnectedError, SocketSendError, TimeoutError)


class MaxClientPool:
    """Send-side interface to MAX, borrowing connections from MaxListener.

    The pool does NOT own connections.  Each user's MaxListener creates and
    maintains a single BridgeMaxClient (with its own reconnect loop).  The
    pool borrows that client for every send/edit/delete operation.

    This avoids the "session-kick cascade" that occurred when pool and
    listener each opened a separate connection with the same token — MAX
    server would kick the older session after ~4 minutes.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self._listeners: dict[int, MaxListener] = {}  # max_user_id -> listener
        self._user_ids: list[int] = []

    async def init(self, users: list[UserMapping]) -> list[UserMapping]:
        """Validate MAX credentials for all users.

        Returns users whose session files exist and are loadable.
        Actual connections are created later by MaxListener.start().
        """
        valid = []
        for user in users:
            session = MaxSession(user.max_session, self.config.sessions_dir)
            if not session.exists():
                log.warning(
                    "MAX session not found for %s (%s) — skipping user",
                    user.name, user.max_session,
                )
                continue

            login_token = session.load()
            device_id = session.load_device_id()
            if not device_id:
                log.warning(
                    "No device_id in MAX session for %s — skipping user",
                    user.name,
                )
                continue

            self._user_ids.append(user.max_user_id)
            valid.append(user)
            log.info("Validated MAX credentials for %s (MAX ID: %d)", user.name, user.max_user_id)

        return valid

    def set_listener(self, max_user_id: int, listener: MaxListener) -> None:
        """Register a MaxListener whose client the pool will borrow for sends."""
        self._listeners[max_user_id] = listener

    def get_client(self, max_user_id: int) -> BridgeMaxClient | None:
        listener = self._listeners.get(max_user_id)
        return listener.client if listener else None

    def get_any_client(self) -> BridgeMaxClient | None:
        for listener in self._listeners.values():
            if listener.client:
                return listener.client
        return None

    def get_all_user_ids(self) -> list[int]:
        return list(self._user_ids)

    # ── Client access ─────────────────────────────────────────────────────────

    def _resolve_user_id(self, max_user_id: int | None) -> int | None:
        """Resolve max_user_id — if None, pick the first available."""
        if max_user_id and max_user_id in self._listeners:
            return max_user_id
        if not max_user_id:
            for uid in self._listeners:
                return uid
        return None

    async def _get_live_client(self, max_user_id: int) -> BridgeMaxClient | None:
        """Get the listener's client if it's connected, else None.

        The pool does NOT reconnect — the listener's own _reconnect_loop
        handles that.  The retry loop in each send method waits for the
        listener to recover.
        """
        listener = self._listeners.get(max_user_id)
        if not listener or not listener.client:
            return None
        if not listener.client.is_connected:
            return None
        return listener.client

    # ── Message ID extraction ─────────────────────────────────────────────────

    @staticmethod
    def _extract_msg_id(response: dict | None) -> str | None:
        """Extract message ID from MAX API response.

        The ID can appear in two places depending on the endpoint:
          - payload.messageId  (some endpoints)
          - payload.message.id (send_message / reply_message)
        """
        if not response or "payload" not in response:
            return None
        payload = response["payload"]
        msg_id = payload.get("messageId")
        if not msg_id:
            msg = payload.get("message")
            if isinstance(msg, dict):
                msg_id = msg.get("id")
        return str(msg_id) if msg_id else None

    # ── Send operations with retry ────────────────────────────────────────────

    async def send_text(
        self,
        max_user_id: int | None,
        chat_id: int,
        text: str,
        reply_to: str | None = None,
        elements: list[dict] | None = None,
    ) -> str | None:
        uid = self._resolve_user_id(max_user_id)
        if uid is None:
            return None

        for attempt in range(_MAX_RETRIES + 1):
            client = await self._get_live_client(uid)
            if not client:
                if attempt < _MAX_RETRIES:
                    log.warning("send_text: no live client for user %s — waiting for reconnect", uid)
                    await asyncio.sleep(_LISTENER_RECONNECT_WAIT)
                    continue
                log.error("send_text: no live client for user %s after %d attempts", uid, attempt + 1)
                return None
            try:
                response = await client.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_to=int(reply_to) if reply_to else None,
                    elements=elements,
                )
                return self._extract_msg_id(response)
            except _CONNECTION_ERRORS as e:
                if attempt < _MAX_RETRIES:
                    log.warning("send_text failed (attempt %d): %s — waiting for reconnect",
                                attempt + 1, e)
                    await asyncio.sleep(_LISTENER_RECONNECT_WAIT)
                else:
                    log.error("send_text failed after %d attempts: %s", attempt + 1, e)
                    return None
            except Exception as e:
                log.warning("send_text: non-retryable error: %s", e)
                return None

    async def edit_text(
        self,
        max_user_id: int | None,
        chat_id: int,
        message_id: str,
        text: str,
        elements: list[dict] | None = None,
    ):
        uid = self._resolve_user_id(max_user_id)
        if uid is None:
            return

        for attempt in range(_MAX_RETRIES + 1):
            client = await self._get_live_client(uid)
            if not client:
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_LISTENER_RECONNECT_WAIT)
                    continue
                log.error("edit_text: no live client for user %s", uid)
                return
            try:
                await client.edit_message(
                    chat_id=chat_id,
                    message_id=int(message_id),
                    text=text,
                    elements=elements,
                )
                return
            except _CONNECTION_ERRORS as e:
                if attempt < _MAX_RETRIES:
                    log.warning("edit_text failed (attempt %d): %s — waiting for reconnect",
                                attempt + 1, e)
                    await asyncio.sleep(_LISTENER_RECONNECT_WAIT)
                else:
                    log.error("edit_text failed after %d attempts: %s", attempt + 1, e)
            except Exception as e:
                log.warning("edit_text: non-retryable error: %s", e)
                return

    async def delete_msg(
        self,
        max_user_id: int | None,
        chat_id: int,
        message_id: str,
    ):
        uid = self._resolve_user_id(max_user_id)
        if uid is None:
            return

        for attempt in range(_MAX_RETRIES + 1):
            client = await self._get_live_client(uid)
            if not client:
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_LISTENER_RECONNECT_WAIT)
                    continue
                log.error("delete_msg: no live client for user %s", uid)
                return
            try:
                await client.delete_message(
                    chat_id=chat_id,
                    message_ids=[int(message_id)],
                )
                return
            except _CONNECTION_ERRORS as e:
                if attempt < _MAX_RETRIES:
                    log.warning("delete_msg failed (attempt %d): %s — waiting for reconnect",
                                attempt + 1, e)
                    await asyncio.sleep(_LISTENER_RECONNECT_WAIT)
                else:
                    log.error("delete_msg failed after %d attempts: %s", attempt + 1, e)
            except Exception as e:
                log.warning("delete_msg: non-retryable error: %s", e)
                return

    async def send_photo(
        self,
        max_user_id: int | None,
        chat_id: int,
        photo_data: bytes,
        filename: str = "photo.jpg",
        caption: str = "",
        reply_to: str | None = None,
    ) -> str | None:
        uid = self._resolve_user_id(max_user_id)
        if uid is None:
            return None

        for attempt in range(_MAX_RETRIES + 1):
            client = await self._get_live_client(uid)
            if not client:
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_LISTENER_RECONNECT_WAIT)
                    continue
                log.error("send_photo: no live client for user %s", uid)
                return None
            try:
                upload_url = await get_upload_url(client)
                photo_token = await upload_photo_to_url(upload_url, photo_data, filename)
                response = await send_photo_message(client, chat_id, photo_token, caption, reply_to)
                return self._extract_msg_id(response)
            except _CONNECTION_ERRORS as e:
                if attempt < _MAX_RETRIES:
                    log.warning("send_photo failed (attempt %d): %s — waiting for reconnect",
                                attempt + 1, e)
                    await asyncio.sleep(_LISTENER_RECONNECT_WAIT)
                else:
                    log.error("send_photo failed after %d attempts: %s", attempt + 1, e)
                    return None
            except Exception as e:
                log.warning("send_photo: non-retryable error: %s", e)
                return None

    async def _upload_file_with_fallback(
        self, client: BridgeMaxClient, file_data: bytes,
        filename: str, content_type: str,
    ) -> dict:
        """Upload a file to MAX, trying pymax opcode 87 first, then HTTP multipart.

        Returns an attaches-ready dict like ``{"_type": "FILE", "fileId": ...}``.
        Raises RuntimeError if both methods fail.

        NOTE: Do NOT use asyncio.wait_for to cap pymax's timeout — cancelling
        ``_upload_file`` mid-flight corrupts pymax's connection state.
        """
        # ── Attempt 1: pymax FILE_UPLOAD (opcode 87) ──────────────────────
        try:
            pymax_file = PyMaxFile(raw=file_data, url=filename)
            attach = await client.inner._upload_file(pymax_file)
            if attach and attach.file_id:
                return {"_type": "FILE", "fileId": attach.file_id}
            log.warning("_upload_file returned no file_id for %r — trying HTTP fallback", filename)
        except Exception as e:
            log.warning("_upload_file failed for %r (%s) — trying HTTP fallback", filename, e)

        # ── Attempt 2: HTTP multipart via opcode 80 (FILE type) ───────────
        upload_url = await get_file_upload_url(client)
        file_info = await upload_file_to_url(upload_url, file_data, filename, content_type)
        if not file_info or not file_info.get("fileId"):
            raise RuntimeError(f"Both upload methods failed for {filename!r}")
        return file_info

    async def send_file(
        self,
        max_user_id: int | None,
        chat_id: int,
        file_data: bytes,
        filename: str,
        content_type: str = "application/octet-stream",
        caption: str = "",
        reply_to: str | None = None,
    ) -> str | None:
        uid = self._resolve_user_id(max_user_id)
        if uid is None:
            return None

        for attempt in range(_MAX_RETRIES + 1):
            client = await self._get_live_client(uid)
            if not client:
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_LISTENER_RECONNECT_WAIT)
                    continue
                log.error("send_file: no live client for user %s", uid)
                return None
            try:
                file_info = await self._upload_file_with_fallback(
                    client, file_data, filename, content_type,
                )
                response = await send_file_message(client, chat_id, file_info, caption, reply_to)
                return self._extract_msg_id(response)
            except _CONNECTION_ERRORS as e:
                if attempt < _MAX_RETRIES:
                    log.warning("send_file failed (attempt %d): %s — waiting for reconnect",
                                attempt + 1, e)
                    await asyncio.sleep(_LISTENER_RECONNECT_WAIT)
                else:
                    log.error("send_file failed after %d attempts: %s", attempt + 1, e)
                    return None
            except Exception as e:
                log.warning("send_file: non-retryable error: %s", e)
                return None

    async def react(
        self,
        max_user_id: int | None,
        chat_id: int,
        message_id: str,
        emoji: str | None,
    ) -> None:
        """Add or remove a reaction on a MAX message.

        Pass *emoji=None* (or empty string) to remove the current reaction.
        """
        uid = self._resolve_user_id(max_user_id)
        if uid is None:
            return
        client = await self._get_live_client(uid)
        if not client:
            return
        try:
            if emoji:
                await client.add_reaction(chat_id, message_id, emoji)
            else:
                await client.remove_reaction(chat_id, message_id)
        except Exception as e:
            log.warning("react(%s, %s, %r) failed: %s", message_id, uid, emoji, e)

    async def send_media_multi(
        self,
        max_user_id: int | None,
        chat_id: int,
        media_items: list[MediaInfo],
        caption: str = "",
        reply_to: str | None = None,
        elements: list[dict] | None = None,
    ) -> str | None:
        """Send a single MAX message with multiple attachments (photo/video/file mix)."""
        uid = self._resolve_user_id(max_user_id)
        if uid is None:
            return None

        for attempt in range(_MAX_RETRIES + 1):
            client = await self._get_live_client(uid)
            if not client:
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_LISTENER_RECONNECT_WAIT)
                    continue
                log.error("send_media_multi: no live client for user %s", uid)
                return None
            try:
                attaches: list[dict] = []
                for mi in media_items:
                    if mi.mime_type.startswith("image/"):
                        upload_url = await get_upload_url(client)
                        token = await upload_photo_to_url(upload_url, mi.data, mi.filename)
                        attaches.append({"_type": "PHOTO", "photoToken": token})
                    else:
                        info = await self._upload_file_with_fallback(
                            client, mi.data, mi.filename, mi.mime_type,
                        )
                        attaches.append(info)

                response = await send_multi_media_message(
                    client, chat_id, attaches, caption, elements, reply_to
                )
                return self._extract_msg_id(response)
            except _CONNECTION_ERRORS as e:
                if attempt < _MAX_RETRIES:
                    log.warning("send_media_multi failed (attempt %d): %s — waiting for reconnect",
                                attempt + 1, e)
                    await asyncio.sleep(_LISTENER_RECONNECT_WAIT)
                else:
                    log.error("send_media_multi failed after %d attempts: %s", attempt + 1, e)
                    return None
            except Exception as e:
                log.warning("send_media_multi: non-retryable error: %s", e)
                return None

    async def stop(self):
        # Pool doesn't own clients — listeners handle lifecycle.
        pass
