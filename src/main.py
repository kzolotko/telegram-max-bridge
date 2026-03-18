"""
Main entry point for the Telegram ↔ MAX bridge.
Usage: python -m src
"""

import asyncio
import logging
import signal

from .config import load_config, ConfigLookup
from .message_store import MessageStore
from .bridge.mirror_tracker import MirrorTracker
from .bridge.bridge import Bridge
from .telegram.client_pool import TelegramClientPool
from .telegram.listener import TelegramListener
from .max.client_pool import MaxClientPool
from .max.listener import MaxListener

log = logging.getLogger("bridge")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("vkmax").setLevel(logging.WARNING)


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

    log.info("Starting Telegram listeners...")
    tg_listeners = []
    for user in users:
        client = tg_pool.get_client(user.telegram_user_id)
        listener = TelegramListener(config, lookup, mirror_tracker, bridge.handle_event, client, user)
        await listener.start()
        tg_listeners.append(listener)

    log.info("Starting MAX listeners...")
    max_listeners = []
    for user in users:
        listener = MaxListener(config, lookup, mirror_tracker, bridge.handle_event, user)
        await listener.start()
        max_listeners.append(listener)

    log.info("Bridge is active:")
    for entry in config.bridges:
        log.info("  [TG] %s  <->  [MAX] %s   (%s) via %s",
                 entry.telegram_chat_id, entry.max_chat_id, entry.name, entry.user.name)
    log.info("Users:")
    for user in users:
        log.info("  %-12s  TG:%-15s  MAX:%s", user.name, user.telegram_user_id, user.max_user_id)

    shutdown_event = asyncio.Event()

    def _shutdown():
        log.info("Shutting down...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    await shutdown_event.wait()

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
