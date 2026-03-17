from vkmax.client import MaxClient
from vkmax.functions.messages import send_message, reply_message, edit_message, delete_message

from ..types import AppConfig, UserMapping
from .session import MaxSession


class MaxClientPool:
    """Manages multiple MAX user account clients."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._clients: dict[int, MaxClient] = {}  # max_user_id -> MaxClient
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
            client = MaxClient()
            await client.connect()
            await client.login_by_token(login_token)

            self._clients[user.max_user_id] = client
            user_ids.append(user.max_user_id)
            self._user_ids.append(user.max_user_id)
            print(f"[MAX Pool] Started client for {user.name} (MAX ID: {user.max_user_id})")

        return user_ids

    def get_client(self, max_user_id: int) -> MaxClient | None:
        return self._clients.get(max_user_id)

    def get_any_client(self) -> MaxClient | None:
        for client in self._clients.values():
            return client
        return None

    def get_all_user_ids(self) -> list[int]:
        return list(self._user_ids)

    async def send_text(
        self,
        max_user_id: int | None,
        chat_id: int,
        text: str,
        reply_to: str | None = None,
    ) -> str | None:
        client = self._clients.get(max_user_id) if max_user_id else self.get_any_client()
        if not client:
            return None

        if reply_to:
            response = await reply_message(
                client=client,
                chat_id=chat_id,
                text=text,
                reply_to_message_id=int(reply_to),
            )
        else:
            response = await send_message(
                client=client,
                chat_id=chat_id,
                text=text,
            )

        if response and "payload" in response:
            msg_id = response["payload"].get("messageId")
            if msg_id:
                return str(msg_id)
        return None

    async def edit_text(
        self,
        max_user_id: int | None,
        chat_id: int,
        message_id: str,
        text: str,
    ):
        client = self._clients.get(max_user_id) if max_user_id else self.get_any_client()
        if not client:
            return

        await edit_message(
            client=client,
            chat_id=chat_id,
            message_id=int(message_id),
            text=text,
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

        await delete_message(
            client=client,
            chat_id=chat_id,
            message_ids=[message_id],
        )

    async def stop(self):
        for client in self._clients.values():
            try:
                await client.disconnect()
            except Exception as e:
                print(f"[MAX Pool] Error disconnecting client: {e}")
