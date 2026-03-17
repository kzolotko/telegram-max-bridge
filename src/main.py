"""
Main entry point for the Telegram ↔ MAX bridge.
Usage: python -m src.main
"""

import asyncio
import signal

from .config import load_config, ConfigLookup
from .message_store import MessageStore
from .bridge.echo_guard import EchoGuard
from .bridge.bridge import Bridge
from .telegram.client_pool import TelegramClientPool
from .telegram.listener import TelegramListener
from .max.client_pool import MaxClientPool
from .max.listener import MaxListener


async def main():
    print("[Bridge] Loading configuration...")
    config = load_config()
    lookup = ConfigLookup(config)

    print(f"[Bridge] {len(config.chat_pairs)} chat pair(s), {len(config.users)} user mapping(s)")

    # Initialize message store
    message_store = MessageStore()
    message_store.start()

    # Initialize echo guard
    echo_guard = EchoGuard()

    # Initialize Telegram sender pool (user accounts)
    print("[Bridge] Initializing Telegram user accounts...")
    tg_pool = TelegramClientPool(config)
    tg_user_ids = await tg_pool.init(config.users)
    for uid in tg_user_ids:
        echo_guard.add_tg_user_id(uid)

    # Initialize MAX sender pool (user accounts)
    print("[Bridge] Initializing MAX user accounts...")
    max_pool = MaxClientPool(config)
    max_user_ids = await max_pool.init(config.users)
    for uid in max_user_ids:
        echo_guard.add_max_user_id(uid)

    # Create bridge
    bridge = Bridge(lookup, message_store, tg_pool, max_pool)

    # Start Telegram listener
    print("[Bridge] Starting Telegram listener...")
    tg_listener = TelegramListener(config, lookup, echo_guard, bridge.handle_event)
    tg_listener_id = await tg_listener.start()
    echo_guard.add_tg_user_id(tg_listener_id)

    # Start MAX listener
    print("[Bridge] Starting MAX listener...")
    max_listener = MaxListener(config, lookup, echo_guard, bridge.handle_event)
    max_listener_id = await max_listener.start()
    echo_guard.add_max_user_id(max_listener_id)

    print("[Bridge] Ready! Bridging messages between Telegram and MAX.")

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    def _shutdown():
        print("\n[Bridge] Shutting down...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    await shutdown_event.wait()

    # Cleanup
    await tg_listener.stop()
    await max_listener.stop()
    await tg_pool.stop()
    await max_pool.stop()
    message_store.stop()

    print("[Bridge] Stopped.")


if __name__ == "__main__":
    asyncio.run(main())
