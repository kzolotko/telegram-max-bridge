import asyncio
import logging

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


log = logging.getLogger("bridge.max.pool")

# Number of automatic retry attempts after reconnecting on send failure.
_MAX_RETRIES = 2

# Only these exception types indicate a broken connection worth reconnecting.
# Server-side errors (rate limits, session state, etc.) are NOT connection
# problems — reconnecting on those just causes cascading failures.
_CONNECTION_ERRORS = (SocketNotConnectedError, SocketSendError)
_RECONNECT_DELAY = 1          # seconds before first reconnect attempt
_RECONNECT_MAX_DELAY = 120    # cap for exponential backoff


class MaxClientPool:
    """Manages multiple MAX user account clients with auto-reconnect.

    Each send/edit/delete operation is wrapped in a retry loop: if the
    underlying TCP connection has died, the pool transparently reconnects
    and retries the operation once before giving up.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self._clients: dict[int, BridgeMaxClient] = {}  # max_user_id -> client
        self._credentials: dict[int, tuple[str, str]] = {}  # max_user_id -> (token, device_id)
        self._user_ids: list[int] = []
        self._reconnect_locks: dict[int, asyncio.Lock] = {}  # prevent parallel reconnects
        self._reconnect_delays: dict[int, float] = {}        # exponential backoff state

    async def init(self, users: list[UserMapping]) -> list[UserMapping]:
        """Initialize clients for all users. Returns list of successfully started users."""
        started = []
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

            try:
                # Store credentials for reconnection
                self._credentials[user.max_user_id] = (login_token, device_id)
                self._reconnect_locks[user.max_user_id] = asyncio.Lock()
                self._reconnect_delays[user.max_user_id] = _RECONNECT_DELAY

                client = BridgeMaxClient(token=login_token, device_id=device_id)
                await client.connect_and_login()
            except Exception as e:
                log.warning(
                    "Failed to start MAX client for %s: %s — skipping user",
                    user.name, e,
                )
                self._credentials.pop(user.max_user_id, None)
                self._reconnect_locks.pop(user.max_user_id, None)
                self._reconnect_delays.pop(user.max_user_id, None)
                continue

            self._clients[user.max_user_id] = client
            self._user_ids.append(user.max_user_id)
            started.append(user)
            log.info("Started client for %s (MAX ID: %d)", user.name, user.max_user_id)

        return started

    def get_client(self, max_user_id: int) -> BridgeMaxClient | None:
        return self._clients.get(max_user_id)

    def get_any_client(self) -> BridgeMaxClient | None:
        for client in self._clients.values():
            return client
        return None

    def get_all_user_ids(self) -> list[int]:
        return list(self._user_ids)

    # ── Reconnection ──────────────────────────────────────────────────────────

    async def _reconnect(
        self, max_user_id: int, dead_client: "BridgeMaxClient | None" = None
    ) -> "BridgeMaxClient | None":
        """Reconnect a specific pool client. Returns new client or None.

        *dead_client* is the specific client instance we know is dead (from a
        failed ping).  If provided, the early-exit guard compares by identity so
        that a concurrently reconnected (new) client is returned as-is, while
        the known-dead client always triggers a fresh reconnect.
        """
        creds = self._credentials.get(max_user_id)
        if not creds:
            log.error("No credentials for MAX user %s — cannot reconnect", max_user_id)
            return None

        lock = self._reconnect_locks[max_user_id]
        async with lock:
            # Check if another coroutine already reconnected while we waited.
            existing = self._clients.get(max_user_id)
            if existing is not None and existing is not dead_client and existing.is_connected:
                return existing

            token, device_id = creds
            delay = self._reconnect_delays.get(max_user_id, _RECONNECT_DELAY)
            log.warning(
                "Pool client %s disconnected — reconnecting in %.0fs...",
                max_user_id, delay,
            )

            # Close old client gracefully
            old = self._clients.get(max_user_id)
            if old:
                try:
                    await old.disconnect()
                except Exception:
                    pass

            await asyncio.sleep(delay)

            try:
                client = BridgeMaxClient(token=token, device_id=device_id)
                await client.connect_and_login()
                self._clients[max_user_id] = client
                # Reset backoff on success
                self._reconnect_delays[max_user_id] = _RECONNECT_DELAY
                log.info("Pool client %s reconnected successfully", max_user_id)
                return client
            except Exception as e:
                # Exponential backoff: double the delay up to the cap
                self._reconnect_delays[max_user_id] = min(
                    delay * 2, _RECONNECT_MAX_DELAY
                )
                log.error("Pool client %s reconnect failed: %s", max_user_id, e)
                return None

    def _resolve_user_id(self, max_user_id: int | None) -> int | None:
        """Resolve max_user_id — if None, pick the first available."""
        if max_user_id and max_user_id in self._clients:
            return max_user_id
        if not max_user_id:
            for uid in self._clients:
                return uid
        return None

    async def _get_live_client(self, max_user_id: int) -> BridgeMaxClient | None:
        """Get the current pool client, reconnecting if obviously disconnected.

        Does NOT ping — the overhead of a 5 s ping timeout on dead connections
        is worse than letting the first send attempt fail fast and then
        reconnecting in the retry loop.  The retry loop handles actual send
        errors correctly now that _reconnect() tracks the dead client identity.
        """
        client = self._clients.get(max_user_id)
        if client is None or not client.is_connected:
            return await self._reconnect(max_user_id, dead_client=client)
        return client

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
                log.error("send_text: no live client for user %s", uid)
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
                    log.warning("send_text failed (attempt %d): %s — will reconnect and retry",
                                attempt + 1, e)
                    await self._reconnect(uid, dead_client=client)
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
                    log.warning("edit_text failed (attempt %d): %s — will reconnect and retry",
                                attempt + 1, e)
                    await self._reconnect(uid, dead_client=client)
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
                    log.warning("delete_msg failed (attempt %d): %s — will reconnect and retry",
                                attempt + 1, e)
                    await self._reconnect(uid, dead_client=client)
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
                log.error("send_photo: no live client for user %s", uid)
                return None
            try:
                upload_url = await get_upload_url(client)
                photo_token = await upload_photo_to_url(upload_url, photo_data, filename)
                response = await send_photo_message(client, chat_id, photo_token, caption, reply_to)
                return self._extract_msg_id(response)
            except _CONNECTION_ERRORS as e:
                if attempt < _MAX_RETRIES:
                    log.warning("send_photo failed (attempt %d): %s — will reconnect and retry",
                                attempt + 1, e)
                    await self._reconnect(uid, dead_client=client)
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
        # Let it run to completion (internal 20s timeout).  Returns None on
        # timeout — safe, no connection corruption.
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
                    log.warning("send_file failed (attempt %d): %s — will reconnect and retry",
                                attempt + 1, e)
                    await self._reconnect(uid, dead_client=client)
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
                log.error("send_media_multi: no live client for user %s", uid)
                return None
            try:
                # Upload each item and build the attaches list
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
                    log.warning("send_media_multi failed (attempt %d): %s — reconnecting",
                                attempt + 1, e)
                    await self._reconnect(uid, dead_client=client)
                else:
                    log.error("send_media_multi failed after %d attempts: %s", attempt + 1, e)
                    return None
            except Exception as e:
                log.warning("send_media_multi: non-retryable error: %s", e)
                return None

    async def reconnect_dead_clients(self) -> None:
        """Proactively reconnect any pool clients that have lost their connection.

        Called periodically from the health loop in main.py.  Uses an active
        ping check instead of relying on ``is_connected`` which can return
        True even when the socket is half-dead (recv blocked in executor,
        send already failing).
        """
        for uid in self._user_ids:
            client = self._clients.get(uid)
            if not client:
                alive = False
            elif not client.is_connected:
                alive = False
            else:
                alive = await client.ping(timeout=5.0)
            if alive:
                continue
            log.info("Proactive reconnect: pool client %s is dead, reconnecting...", uid)
            try:
                await self._reconnect(uid, dead_client=client)
            except Exception as e:
                log.error("Proactive reconnect failed for pool client %s: %s", uid, e)

    async def stop(self):
        for client in self._clients.values():
            try:
                await client.disconnect()
            except Exception as e:
                log.error("Error disconnecting client: %s", e)
