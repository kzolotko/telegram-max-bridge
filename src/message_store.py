import logging
import time
import asyncio

log = logging.getLogger("bridge.store")


class MessageStore:
    """In-memory bidirectional message ID mapping with TTL."""

    TTL_SECONDS = 24 * 60 * 60  # 24 hours
    CLEANUP_INTERVAL = 10 * 60  # 10 minutes

    def __init__(self):
        # key: "pair_name:tg_msg_id" -> max_msg_id
        self._tg_to_max: dict[str, str] = {}
        # key: "pair_name:max_msg_id" -> tg_msg_id
        self._max_to_tg: dict[str, int] = {}
        # timestamps for cleanup
        self._timestamps: list[tuple[str, str, float]] = []  # (tg_key, max_key, timestamp)
        self._cleanup_task: asyncio.Task | None = None

    def start(self):
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def stop(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()

    def store(self, pair_name: str, tg_msg_id: int, max_msg_id: str):
        tg_key = f"{pair_name}:{tg_msg_id}"
        max_key = f"{pair_name}:{max_msg_id}"
        self._tg_to_max[tg_key] = max_msg_id
        self._max_to_tg[max_key] = tg_msg_id
        self._timestamps.append((tg_key, max_key, time.time()))

    def get_max_msg_id(self, pair_name: str, tg_msg_id: int) -> str | None:
        return self._tg_to_max.get(f"{pair_name}:{tg_msg_id}")

    def get_tg_msg_id(self, pair_name: str, max_msg_id: str) -> int | None:
        return self._max_to_tg.get(f"{pair_name}:{max_msg_id}")

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(self.CLEANUP_INTERVAL)
            cutoff = time.time() - self.TTL_SECONDS
            remaining = []
            removed = 0
            for tg_key, max_key, ts in self._timestamps:
                if ts < cutoff:
                    self._tg_to_max.pop(tg_key, None)
                    self._max_to_tg.pop(max_key, None)
                    removed += 1
                else:
                    remaining.append((tg_key, max_key, ts))
            self._timestamps = remaining
            log.debug("Cleanup: removed %d, kept %d", removed, len(remaining))
