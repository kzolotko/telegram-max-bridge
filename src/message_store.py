import logging
import sqlite3
import time
import asyncio
from pathlib import Path

log = logging.getLogger("bridge.store")


class MessageStore:
    """SQLite-backed bidirectional message ID mapping with TTL."""

    TTL_SECONDS = 24 * 60 * 60  # 24 hours
    CLEANUP_INTERVAL = 10 * 60  # 10 minutes

    def __init__(self, db_path: str = "sessions/bridge.db"):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._cleanup_task: asyncio.Task | None = None

    def start(self):
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS message_map (
                tg_chat_id   INTEGER NOT NULL,
                tg_msg_id    INTEGER NOT NULL,
                max_chat_id  INTEGER NOT NULL,
                max_msg_id   TEXT    NOT NULL,
                is_primary   INTEGER NOT NULL DEFAULT 1,
                created_at   REAL    NOT NULL,
                PRIMARY KEY (tg_chat_id, tg_msg_id, max_chat_id)
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_mm_reverse
                ON message_map (max_chat_id, max_msg_id, tg_chat_id)
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_mm_created
                ON message_map (created_at)
        """)
        self._conn.commit()
        # Purge entries that expired while the process was down.
        cutoff = time.time() - self.TTL_SECONDS
        cur = self._conn.execute("DELETE FROM message_map WHERE created_at < ?", (cutoff,))
        if cur.rowcount:
            self._conn.commit()
            log.info("Startup: purged %d expired message mappings", cur.rowcount)
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def stop(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()
        if self._conn:
            self._conn.close()
            self._conn = None

    def store(
        self,
        tg_chat_id: int,
        tg_msg_id: int,
        max_chat_id: int,
        max_msg_id: str,
        tg_msg_ids: list[int] | None = None,
    ):
        now = time.time()
        max_msg_id = str(max_msg_id)
        all_ids = [int(mid) for mid in (tg_msg_ids or [tg_msg_id])]
        primary = int(tg_msg_id)
        rows = [
            (tg_chat_id, mid, max_chat_id, max_msg_id, 1 if mid == primary else 0, now)
            for mid in all_ids
        ]
        self._conn.executemany(
            "INSERT OR REPLACE INTO message_map "
            "(tg_chat_id, tg_msg_id, max_chat_id, max_msg_id, is_primary, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

    def get_max_msg_id(self, tg_chat_id: int, tg_msg_id: int, max_chat_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT max_msg_id FROM message_map "
            "WHERE tg_chat_id=? AND tg_msg_id=? AND max_chat_id=?",
            (tg_chat_id, tg_msg_id, max_chat_id),
        ).fetchone()
        return row[0] if row else None

    def get_tg_msg_id(self, max_chat_id: int, max_msg_id: str, tg_chat_id: int) -> int | None:
        row = self._conn.execute(
            "SELECT tg_msg_id FROM message_map "
            "WHERE max_chat_id=? AND max_msg_id=? AND tg_chat_id=? AND is_primary=1 "
            "LIMIT 1",
            (max_chat_id, str(max_msg_id), tg_chat_id),
        ).fetchone()
        return row[0] if row else None

    def get_tg_msg_ids(self, max_chat_id: int, max_msg_id: str, tg_chat_id: int) -> list[int]:
        rows = self._conn.execute(
            "SELECT tg_msg_id FROM message_map "
            "WHERE max_chat_id=? AND max_msg_id=? AND tg_chat_id=?",
            (max_chat_id, str(max_msg_id), tg_chat_id),
        ).fetchall()
        if rows:
            return [r[0] for r in rows]
        first = self.get_tg_msg_id(max_chat_id, max_msg_id, tg_chat_id)
        return [first] if first is not None else []

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(self.CLEANUP_INTERVAL)
            cutoff = time.time() - self.TTL_SECONDS
            cur = self._conn.execute(
                "DELETE FROM message_map WHERE created_at < ?", (cutoff,)
            )
            if cur.rowcount:
                self._conn.commit()
                log.debug("Cleanup: removed %d expired message mappings", cur.rowcount)
