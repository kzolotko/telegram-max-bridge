import logging

from pyrogram import Client

from ..types import AppConfig, UserMapping

log = logging.getLogger("bridge.tg.pool")


class TelegramClientPool:
    """Manages multiple Pyrogram user account clients."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._clients: dict[int, Client] = {}  # tg_user_id -> Client
        self._user_ids: dict[str, int] = {}  # session_name -> tg_user_id

    async def init(self, users: list[UserMapping]) -> list[int]:
        """Initialize clients for all users. Returns list of TG user IDs."""
        user_ids = []
        for user in users:
            client = Client(
                name=user.telegram_session,
                api_id=self.config.api_id,
                api_hash=self.config.api_hash,
                workdir=self.config.sessions_dir,
            )
            await client.start()
            me = await client.get_me()
            self._clients[user.telegram_user_id] = client
            self._user_ids[user.telegram_session] = me.id
            user_ids.append(me.id)
            log.info("Started client for %s (@%s, ID: %d)", user.name, me.username, me.id)
        return user_ids

    def get_client(self, tg_user_id: int) -> Client | None:
        return self._clients.get(tg_user_id)

    def get_any_client(self) -> Client | None:
        """Get any available client (for fallback operations)."""
        for client in self._clients.values():
            return client
        return None

    def get_all_user_ids(self) -> list[int]:
        return list(self._user_ids.values())

    async def stop(self):
        for client in self._clients.values():
            try:
                await client.stop()
            except Exception as e:
                log.error("Error stopping client: %s", e)
