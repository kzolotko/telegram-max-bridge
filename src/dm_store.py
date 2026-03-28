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

import sqlite3
import time
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("bridge.dm_store")


@dataclass
class DmContext:
    max_user_id: int
    max_chat_id: int
    max_msg_id: str
    sender_name: str


class DmStore:
    """SQLite-backed mapping: (tg_owner_id, bot_msg_id) → DmContext with TTL cleanup."""

    TTL_SECONDS = 24 * 60 * 60   # 24 hours
    CLEANUP_INTERVAL = 10 * 60   # 10 minutes
    MAX_SIZE = 50_000

    def __init__(self, db_path: str = "sessions/bridge.db"):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._cleanup_task: asyncio.Task | None = None

    def start(self):
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS dm_map (
                tg_owner_id  INTEGER NOT NULL,
                bot_msg_id   INTEGER NOT NULL,
                max_user_id  INTEGER NOT NULL,
                max_chat_id  INTEGER NOT NULL,
                max_msg_id   TEXT    NOT NULL,
                sender_name  TEXT    NOT NULL,
                created_at   REAL    NOT NULL,
                PRIMARY KEY (tg_owner_id, bot_msg_id)
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_dm_bot_msg
                ON dm_map (bot_msg_id)
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_dm_created
                ON dm_map (created_at)
        """)
        self._conn.commit()
        # Purge entries that expired while the process was down.
        cutoff = time.time() - self.TTL_SECONDS
        cur = self._conn.execute("DELETE FROM dm_map WHERE created_at < ?", (cutoff,))
        if cur.rowcount:
            self._conn.commit()
            log.info("Startup: purged %d expired DM mappings", cur.rowcount)
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def stop(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()
        if self._conn:
            self._conn.close()
            self._conn = None

    def store(self, bot_msg_id: int, context: DmContext, tg_owner_id: int):
        """Store mapping from bot message ID to MAX DM context."""
        now = time.time()
        self._conn.execute(
            "INSERT OR REPLACE INTO dm_map "
            "(tg_owner_id, bot_msg_id, max_user_id, max_chat_id, max_msg_id, sender_name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tg_owner_id, bot_msg_id, context.max_user_id,
             context.max_chat_id, context.max_msg_id, context.sender_name, now),
        )
        self._conn.commit()
        self._evict_if_needed()

    def get(self, bot_msg_id: int, tg_owner_id: int | None = None) -> DmContext | None:
        """Look up MAX DM context by bot message ID.

        If tg_owner_id is provided, uses precise key. Otherwise scans all
        owners (slower, but works when owner is unknown).
        """
        if tg_owner_id is not None:
            row = self._conn.execute(
                "SELECT max_user_id, max_chat_id, max_msg_id, sender_name "
                "FROM dm_map WHERE tg_owner_id=? AND bot_msg_id=?",
                (tg_owner_id, bot_msg_id),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT max_user_id, max_chat_id, max_msg_id, sender_name "
                "FROM dm_map WHERE bot_msg_id=? LIMIT 1",
                (bot_msg_id,),
            ).fetchone()
        if row:
            return DmContext(
                max_user_id=row[0],
                max_chat_id=row[1],
                max_msg_id=row[2],
                sender_name=row[3],
            )
        return None

    def _evict_if_needed(self):
        count = self._conn.execute("SELECT COUNT(*) FROM dm_map").fetchone()[0]
        if count > self.MAX_SIZE:
            evict_count = self.MAX_SIZE // 10
            self._conn.execute("""
                DELETE FROM dm_map WHERE rowid IN (
                    SELECT rowid FROM dm_map ORDER BY created_at ASC LIMIT ?
                )
            """, (evict_count,))
            self._conn.commit()

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(self.CLEANUP_INTERVAL)
            cutoff = time.time() - self.TTL_SECONDS
            cur = self._conn.execute("DELETE FROM dm_map WHERE created_at < ?", (cutoff,))
            if cur.rowcount:
                self._conn.commit()
                log.debug("Cleanup: removed %d expired DM mappings", cur.rowcount)
