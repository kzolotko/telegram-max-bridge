"""
Main entry point for the Telegram ↔ MAX bridge.
Usage: python -m src
"""

import asyncio
import logging
import signal

from pyrogram import Client as PyrogramClient

from .config import load_config, ConfigLookup
from .message_store import MessageStore
from .bridge.mirror_tracker import MirrorTracker
from .bridge.bridge import Bridge
from .telegram.client_pool import TelegramClientPool
from .telegram.listener import TelegramListener
from .max.client_pool import MaxClientPool
from .max.listener import MaxListener
from .dm_bridge import DmBridge
from .dm_store import DmStore

log = logging.getLogger("bridge")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("pymax").setLevel(logging.WARNING)


async def main():
    setup_logging()

    log.info("Loading configuration...")
    config = load_config()
    lookup = ConfigLookup(config)
    users = lookup.get_unique_users()

    message_store = MessageStore()
    message_store.start()

    mirror_tracker = MirrorTracker()

    log.info("Initializing Telegram user accounts...")
    tg_pool = TelegramClientPool(config)
    await tg_pool.init(users)

    log.info("Initializing MAX user accounts...")
    max_pool = MaxClientPool(config)
    await max_pool.init(users)

    bridge = Bridge(lookup, message_store, tg_pool, max_pool, mirror_tracker)

    # Warm up Pyrogram peer cache for configured chats only.
    # Previously we iterated ALL dialogs (~20s); now we resolve only the
    # chat IDs from config, falling back to full get_dialogs() if needed.
    log.info("Warming up Telegram peer cache...")
    chats_per_user: dict[int, set[int]] = {}
    for entry in config.bridges:
        chats_per_user.setdefault(entry.user.telegram_user_id, set()).add(
            entry.telegram_chat_id
        )
    for tg_user_id, chat_ids in chats_per_user.items():
        client = tg_pool.get_client(tg_user_id)
        if not client:
            continue
        failed: list[int] = []
        for chat_id in chat_ids:
            try:
                await client.get_chat(chat_id)
            except Exception:
                failed.append(chat_id)
        if failed:
            log.info("  Peer cache miss for %d chat(s), loading all dialogs...", len(failed))
            async for _ in client.get_dialogs():
                pass
        user_name = next(u.name for u in users if u.telegram_user_id == tg_user_id)
        log.info("  %s: resolved %d configured chat(s)", user_name, len(chat_ids))

    log.info("Starting Telegram listeners...")
    tg_listeners = []
    for user in users:
        client = tg_pool.get_client(user.telegram_user_id)
        listener = TelegramListener(config, lookup, mirror_tracker, bridge.handle_event, client, user)
        await listener.start()
        tg_listeners.append(listener)

    # ── Optional DM bridge ──────────────────────────────────────────────────
    dm_bridge = None
    if config.dm_bridge:
        log.info("Initializing DM bridge bot...")
        dm_store = DmStore()
        dm_store.start()

        bot_client = PyrogramClient(
            name="dm_bot",
            api_id=config.api_id,
            api_hash=config.api_hash,
            bot_token=config.dm_bridge.bot_token,
            workdir=config.sessions_dir,
        )
        dm_bridge = DmBridge(
            bot_client=bot_client,
            max_pool=max_pool,
            mirror_tracker=mirror_tracker,
            dm_store=dm_store,
            users=users,
        )
        await dm_bridge.start()

    log.info("Starting MAX listeners...")
    max_listeners = []
    for user in users:
        # If DM bridge is enabled, attach the on_dm callback to every user's listener
        on_dm = None
        if dm_bridge:
            async def _on_dm(sender_id, sender_name, chat_id, msg_id, text, **kwargs):
                await dm_bridge.handle_incoming(
                    sender_id=sender_id,
                    sender_name=sender_name,
                    chat_id=chat_id,
                    msg_id=msg_id,
                    text=text,
                    **kwargs,
                )
            on_dm = _on_dm
        listener = MaxListener(config, lookup, mirror_tracker, bridge.handle_event, user, on_dm=on_dm)
        await listener.start()
        max_listeners.append(listener)

    log.info("Bridge is active:")
    for entry in config.bridges:
        log.info("  [TG] %s  <->  [MAX] %s   (%s) via %s",
                 entry.telegram_chat_id, entry.max_chat_id, entry.name, entry.user.name)
    log.info("Users:")
    for user in users:
        log.info("  %-12s  TG:%-15s  MAX:%s", user.name, user.telegram_user_id, user.max_user_id)
    if dm_bridge:
        dm_names = ", ".join(u.name for u in users)
        log.info("DM bridge: enabled for %s (MAX DMs → TG bot)", dm_names)

    shutdown_event = asyncio.Event()

    def _shutdown():
        log.info("Shutting down...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    # Periodic health check — log connection status every 5 minutes
    async def _health_loop():
        while not shutdown_event.is_set():
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=300)
                break  # shutdown signalled
            except asyncio.TimeoutError:
                pass
            # Log MAX pool status
            for uid in max_pool.get_all_user_ids():
                client = max_pool.get_client(uid)
                status = "connected" if (client and client.is_connected) else "DISCONNECTED"
                log.info("Health: MAX pool user %s — %s", uid, status)
            # Log MAX listener status
            for listener in max_listeners:
                client = listener.client
                status = "connected" if (client and client.is_connected) else "DISCONNECTED"
                log.info("Health: MAX listener %s — %s", listener.user.name, status)

    health_task = asyncio.create_task(_health_loop())

    await shutdown_event.wait()
    health_task.cancel()
    try:
        await health_task
    except asyncio.CancelledError:
        pass

    if dm_bridge:
        await dm_bridge.stop()
        dm_store.stop()
    for listener in tg_listeners:
        await listener.stop()
    for listener in max_listeners:
        await listener.stop()
    await tg_pool.stop()
    await max_pool.stop()
    message_store.stop()

    log.info("Stopped.")


if __name__ == "__main__":
    asyncio.run(main())
