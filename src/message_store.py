import logging
import time
import asyncio

log = logging.getLogger("bridge.store")


class MessageStore:
    """In-memory bidirectional message ID mapping with TTL."""

    TTL_SECONDS = 24 * 60 * 60  # 24 hours
    CLEANUP_INTERVAL = 10 * 60  # 10 minutes

    def __init__(self):
        # key: (tg_chat_id, tg_msg_id, max_chat_id) -> max_msg_id
        self._tg_to_max: dict[tuple[int, int, int], str] = {}
        # key: (max_chat_id, max_msg_id, tg_chat_id) -> primary tg_msg_id
        self._max_to_tg: dict[tuple[int, str, int], int] = {}
        # key: (max_chat_id, max_msg_id, tg_chat_id) -> all mirrored tg_msg_ids
        # (used for album delete, where one MAX message maps to multiple TG IDs)
        self._max_to_tg_all: dict[tuple[int, str, int], list[int]] = {}
        # timestamps for cleanup
        self._timestamps: list[tuple[tuple[int, int, int], tuple[int, str, int], float]] = []
        self._cleanup_task: asyncio.Task | None = None

    def start(self):
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def stop(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()

    def store(
        self,
        tg_chat_id: int,
        tg_msg_id: int,
        max_chat_id: int,
        max_msg_id: str,
        tg_msg_ids: list[int] | None = None,
    ):
        max_key = (max_chat_id, str(max_msg_id), tg_chat_id)
        all_ids = [int(mid) for mid in (tg_msg_ids or [tg_msg_id])]
        # Forward map: every TG message in an album should resolve to the same MAX msg.
        now = time.time()
        for mid in all_ids:
            tg_key = (tg_chat_id, mid, max_chat_id)
            self._tg_to_max[tg_key] = str(max_msg_id)
            self._timestamps.append((tg_key, max_key, now))

        # Reverse map: keep primary TG id for edit/reaction/reply.
        primary_tg_msg_id = int(tg_msg_id)
        self._max_to_tg[max_key] = primary_tg_msg_id
        self._max_to_tg_all[max_key] = all_ids

    def get_max_msg_id(self, tg_chat_id: int, tg_msg_id: int, max_chat_id: int) -> str | None:
        return self._tg_to_max.get((tg_chat_id, tg_msg_id, max_chat_id))

    def get_tg_msg_id(self, max_chat_id: int, max_msg_id: str, tg_chat_id: int) -> int | None:
        return self._max_to_tg.get((max_chat_id, str(max_msg_id), tg_chat_id))

    def get_tg_msg_ids(self, max_chat_id: int, max_msg_id: str, tg_chat_id: int) -> list[int]:
        key = (max_chat_id, str(max_msg_id), tg_chat_id)
        all_ids = self._max_to_tg_all.get(key)
        if all_ids:
            return list(all_ids)
        first = self._max_to_tg.get(key)
        return [first] if first is not None else []

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(self.CLEANUP_INTERVAL)
            cutoff = time.time() - self.TTL_SECONDS
            remaining = []
            active_tg_keys: set[tuple[int, int, int]] = set()
            active_max_keys: set[tuple[int, str, int]] = set()
            removed = 0
            for tg_key, max_key, ts in self._timestamps:
                if ts < cutoff:
                    removed += 1
                else:
                    remaining.append((tg_key, max_key, ts))
                    active_tg_keys.add(tg_key)
                    active_max_keys.add(max_key)
            self._timestamps = remaining
            # Prune maps using active key sets to avoid stale entries.
            self._tg_to_max = {
                k: v for k, v in self._tg_to_max.items() if k in active_tg_keys
            }
            self._max_to_tg = {
                k: v for k, v in self._max_to_tg.items() if k in active_max_keys
            }
            self._max_to_tg_all = {
                k: v for k, v in self._max_to_tg_all.items() if k in active_max_keys
            }
            log.debug("Cleanup: removed %d, kept %d", removed, len(remaining))
