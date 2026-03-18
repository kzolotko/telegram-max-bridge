"""
Main entry point for the Telegram ↔ MAX bridge.
Usage: python -m src
"""

import asyncio
import logging
import signal

from .config import load_config, ConfigLookup
from .message_store import MessageStore
from .bridge.echo_guard import EchoGuard
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


async def main():
    setup_logging()

    log.info("Loading configuration...")
    config = load_config()
    lookup = ConfigLookup(config)

    log.info("%d chat pair(s), %d user mapping(s)", len(config.chat_pairs), len(config.users))

    message_store = MessageStore()
    message_store.start()

    echo_guard = EchoGuard()

    log.info("Initializing Telegram user accounts...")
    tg_pool = TelegramClientPool(config)
    tg_user_ids = await tg_pool.init(config.users)
    for uid in tg_user_ids:
        echo_guard.add_tg_user_id(uid)

    log.info("Initializing MAX user accounts...")
    max_pool = MaxClientPool(config)
    max_user_ids = await max_pool.init(config.users)
    for uid in max_user_ids:
        echo_guard.add_max_user_id(uid)

    bridge = Bridge(lookup, message_store, tg_pool, max_pool)

    log.info("Starting Telegram listener...")
    tg_listener = TelegramListener(config, lookup, echo_guard, bridge.handle_event)
    tg_listener_id = await tg_listener.start()
    echo_guard.add_tg_user_id(tg_listener_id)

    log.info("Starting MAX listener...")
    max_listener = MaxListener(config, lookup, echo_guard, bridge.handle_event)
    max_listener_id = await max_listener.start()
    echo_guard.add_max_user_id(max_listener_id)

    log.info("Ready! Bridging messages between Telegram and MAX.")

    shutdown_event = asyncio.Event()

    def _shutdown():
        log.info("Shutting down...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    await shutdown_event.wait()

    await tg_listener.stop()
    await max_listener.stop()
    await tg_pool.stop()
    await max_pool.stop()
    message_store.stop()

    log.info("Stopped.")


if __name__ == "__main__":
    asyncio.run(main())
