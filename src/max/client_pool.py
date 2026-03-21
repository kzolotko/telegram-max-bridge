import logging

from .bridge_client import BridgeMaxClient
from ..types import AppConfig, UserMapping
from .session import MaxSession
from .media import (
    get_upload_url, upload_photo_to_url, send_photo_message,
    get_file_upload_url, upload_file_to_url, send_file_message,
)


log = logging.getLogger("bridge.max.pool")


class MaxClientPool:
    """Manages multiple MAX user account clients."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._clients: dict[int, BridgeMaxClient] = {}  # max_user_id -> client
        self._user_ids: list[int] = []

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

    async def send_text(
        self,
        max_user_id: int | None,
        chat_id: int,
        text: str,
        reply_to: str | None = None,
        elements: list[dict] | None = None,
    ) -> str | None:
        client = self._clients.get(max_user_id) if max_user_id else self.get_any_client()
        if not client:
            return None

        response = await client.send_message(
            chat_id=chat_id,
            text=text,
            reply_to=int(reply_to) if reply_to else None,
            elements=elements,
        )

        return self._extract_msg_id(response)

    async def edit_text(
        self,
        max_user_id: int | None,
        chat_id: int,
        message_id: str,
        text: str,
        elements: list[dict] | None = None,
    ):
        client = self._clients.get(max_user_id) if max_user_id else self.get_any_client()
        if not client:
            return

        await client.edit_message(
            chat_id=chat_id,
            message_id=int(message_id),
            text=text,
            elements=elements,
        )

    async def delete_msg(
        self,
        max_user_id: int | None,
        chat_id: int,
        message_id: str,
    ):
        client = self._clients.get(max_user_id) if max_user_id else self.get_any_client()
        if not client:
            return

        await client.delete_message(
            chat_id=chat_id,
            message_ids=[int(message_id)],
        )

    async def send_photo(
        self,
        max_user_id: int | None,
        chat_id: int,
        photo_data: bytes,
        filename: str = "photo.jpg",
        caption: str = "",
        reply_to: str | None = None,
    ) -> str | None:
        client = self._clients.get(max_user_id) if max_user_id else self.get_any_client()
        if not client:
            return None

        upload_url = await get_upload_url(client)
        photo_token = await upload_photo_to_url(upload_url, photo_data, filename)
        response = await send_photo_message(client, chat_id, photo_token, caption, reply_to)
        return self._extract_msg_id(response)

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
        client = self._clients.get(max_user_id) if max_user_id else self.get_any_client()
        if not client:
            return None

        upload_url = await get_file_upload_url(client)
        file_info = await upload_file_to_url(upload_url, file_data, filename, content_type)
        response = await send_file_message(client, chat_id, file_info, caption, reply_to)
        return self._extract_msg_id(response)

    async def stop(self):
        for client in self._clients.values():
            try:
                await client.disconnect()
            except Exception as e:
                log.error("Error disconnecting client: %s", e)
