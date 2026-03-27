"""
Maps TG bot message IDs → MAX DM context for reply routing.

When a MAX DM arrives, the bridge sends it via TG bot to the user.
DmStore remembers which bot message corresponds to which MAX user/chat,
so that when the user replies to the bot message, the bridge can route
the reply back to the correct MAX DM.

Keys are (tg_owner_id, bot_msg_id) — because multiple users share the
same bot, and message IDs in different private chats could theoretically
overlap.
"""

import time
import asyncio
import logging
from dataclasses import dataclass

log = logging.getLogger("bridge.dm_store")


@dataclass
class DmContext:
    max_user_id: int
    max_chat_id: int
    max_msg_id: str
    sender_name: str


class DmStore:
    """In-memory mapping: (tg_owner_id, bot_msg_id) → DmContext with TTL cleanup."""

    TTL_SECONDS = 24 * 60 * 60   # 24 hours
    CLEANUP_INTERVAL = 10 * 60   # 10 minutes
    MAX_SIZE = 50_000

    def __init__(self):
        # (tg_owner_id, bot_msg_id) → context
        self._store: dict[tuple[int, int], DmContext] = {}
        self._timestamps: dict[tuple[int, int], float] = {}
        self._cleanup_task: asyncio.Task | None = None

    def start(self):
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def stop(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()

    def store(self, bot_msg_id: int, context: DmContext, tg_owner_id: int):
        """Store mapping from bot message ID to MAX DM context."""
        key = (tg_owner_id, bot_msg_id)
        self._store[key] = context
        self._timestamps[key] = time.time()
        self._evict_if_needed()

    def get(self, bot_msg_id: int, tg_owner_id: int | None = None) -> DmContext | None:
        """Look up MAX DM context by bot message ID.

        If tg_owner_id is provided, uses precise key. Otherwise scans all
        owners (slower, but works when owner is unknown).
        """
        if tg_owner_id is not None:
            return self._store.get((tg_owner_id, bot_msg_id))
        # Fallback: scan all owners for this msg_id
        for (uid, mid), ctx in self._store.items():
            if mid == bot_msg_id:
                return ctx
        return None

    def _evict_if_needed(self):
        if len(self._store) > self.MAX_SIZE:
            sorted_keys = sorted(self._timestamps, key=self._timestamps.get)
            evict_count = self.MAX_SIZE // 10
            for k in sorted_keys[:evict_count]:
                self._store.pop(k, None)
                self._timestamps.pop(k, None)

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(self.CLEANUP_INTERVAL)
            cutoff = time.time() - self.TTL_SECONDS
            expired = [k for k, ts in self._timestamps.items() if ts < cutoff]
            for k in expired:
                self._store.pop(k, None)
                self._timestamps.pop(k, None)
            if expired:
                log.debug("Cleanup: removed %d expired DM mappings", len(expired))
