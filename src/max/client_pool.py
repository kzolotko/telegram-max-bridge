import asyncio
import logging

from .bridge_client import BridgeMaxClient
from ..types import AppConfig, MediaInfo, UserMapping
from .session import MaxSession
from .media import (
    get_upload_url, upload_photo_to_url, send_photo_message,
    get_file_upload_url, upload_file_to_url, send_file_message,
    send_multi_media_message,
)


log = logging.getLogger("bridge.max.pool")

# Number of automatic retry attempts after reconnecting on send failure.
_MAX_RETRIES = 1
_RECONNECT_DELAY = 2  # seconds before reconnect attempt


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

    async def init(self, users: list[UserMapping]) -> list[int]:
        """Initialize clients for all users. Returns list of MAX user IDs."""
        user_ids = []
        for user in users:
            session = MaxSession(user.max_session, self.config.sessions_dir)
            if not session.exists():
                raise RuntimeError(
                    f"MAX session not found for {user.name} ({user.max_session}). "
                    f"Run 'python -m src.auth' first to authenticate."
                )

            login_token = session.load()
            device_id = session.load_device_id()
            if not device_id:
                raise RuntimeError(
                    f"No device_id in MAX session for {user.name}. "
                    f"Re-authenticate with 'python -m src.auth'."
                )

            # Store credentials for reconnection
            self._credentials[user.max_user_id] = (login_token, device_id)
            self._reconnect_locks[user.max_user_id] = asyncio.Lock()

            client = BridgeMaxClient(token=login_token, device_id=device_id)
            await client.connect_and_login()

            self._clients[user.max_user_id] = client
            user_ids.append(user.max_user_id)
            self._user_ids.append(user.max_user_id)
            log.info("Started client for %s (MAX ID: %d)", user.name, user.max_user_id)

        return user_ids

    def get_client(self, max_user_id: int) -> BridgeMaxClient | None:
        return self._clients.get(max_user_id)

    def get_any_client(self) -> BridgeMaxClient | None:
        for client in self._clients.values():
            return client
        return None

    def get_all_user_ids(self) -> list[int]:
        return list(self._user_ids)

    # ── Reconnection ──────────────────────────────────────────────────────────

    async def _reconnect(self, max_user_id: int) -> BridgeMaxClient | None:
        """Reconnect a specific pool client. Returns new client or None."""
        creds = self._credentials.get(max_user_id)
        if not creds:
            log.error("No credentials for MAX user %s — cannot reconnect", max_user_id)
            return None

        lock = self._reconnect_locks[max_user_id]
        async with lock:
            # Check if another coroutine already reconnected while we waited
            existing = self._clients.get(max_user_id)
            if existing and existing.is_connected:
                return existing

            token, device_id = creds
            log.warning("Pool client %s disconnected — reconnecting...", max_user_id)

            # Close old client gracefully
            old = self._clients.get(max_user_id)
            if old:
                try:
                    await old.disconnect()
                except Exception:
                    pass

            await asyncio.sleep(_RECONNECT_DELAY)

            try:
                client = BridgeMaxClient(token=token, device_id=device_id)
                await client.connect_and_login()
                self._clients[max_user_id] = client
                log.info("Pool client %s reconnected successfully", max_user_id)
                return client
            except Exception as e:
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
        """Get a connected client, reconnecting if necessary."""
        client = self._clients.get(max_user_id)
        if client and client.is_connected:
            return client
        # Connection dead — try to reconnect
        return await self._reconnect(max_user_id)

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
            except Exception as e:
                if attempt < _MAX_RETRIES:
                    log.warning("send_text failed (attempt %d): %s — will reconnect and retry",
                                attempt + 1, e)
                    # Force reconnect on next _get_live_client call
                    self._clients[uid] = client  # keep ref for disconnect
                    await self._reconnect(uid)
                else:
                    log.error("send_text failed after %d attempts: %s", attempt + 1, e)
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
            except Exception as e:
                if attempt < _MAX_RETRIES:
                    log.warning("edit_text failed (attempt %d): %s — will reconnect and retry",
                                attempt + 1, e)
                    await self._reconnect(uid)
                else:
                    log.error("edit_text failed after %d attempts: %s", attempt + 1, e)

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
            except Exception as e:
                if attempt < _MAX_RETRIES:
                    log.warning("delete_msg failed (attempt %d): %s — will reconnect and retry",
                                attempt + 1, e)
                    await self._reconnect(uid)
                else:
                    log.error("delete_msg failed after %d attempts: %s", attempt + 1, e)

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
            except Exception as e:
                if attempt < _MAX_RETRIES:
                    log.warning("send_photo failed (attempt %d): %s — will reconnect and retry",
                                attempt + 1, e)
                    await self._reconnect(uid)
                else:
                    log.error("send_photo failed after %d attempts: %s", attempt + 1, e)
                    return None

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
                upload_url = await get_file_upload_url(client)
                file_info = await upload_file_to_url(upload_url, file_data, filename, content_type)
                response = await send_file_message(client, chat_id, file_info, caption, reply_to)
                return self._extract_msg_id(response)
            except Exception as e:
                if attempt < _MAX_RETRIES:
                    log.warning("send_file failed (attempt %d): %s — will reconnect and retry",
                                attempt + 1, e)
                    await self._reconnect(uid)
                else:
                    log.error("send_file failed after %d attempts: %s", attempt + 1, e)
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
                        upload_url = await get_file_upload_url(client)
                        file_info = await upload_file_to_url(
                            upload_url, mi.data, mi.filename, mi.mime_type
                        )
                        attaches.append(file_info)

                response = await send_multi_media_message(
                    client, chat_id, attaches, caption, elements, reply_to
                )
                return self._extract_msg_id(response)
            except Exception as e:
                if attempt < _MAX_RETRIES:
                    log.warning("send_media_multi failed (attempt %d): %s — reconnecting",
                                attempt + 1, e)
                    await self._reconnect(uid)
                else:
                    log.error("send_media_multi failed after %d attempts: %s", attempt + 1, e)
                    return None

    async def stop(self):
        for client in self._clients.values():
            try:
                await client.disconnect()
            except Exception as e:
                log.error("Error disconnecting client: %s", e)
