"""
Main entry point for the Telegram ↔ MAX bridge.
Usage: python -m src
"""

import asyncio
import logging
import os
import signal
import time

from pyrogram import Client as PyrogramClient

from .config import load_config, ConfigLookup
from .message_store import MessageStore
from .bridge.mirror_tracker import MirrorTracker
from .bridge.bridge import Bridge
from .bridge_state import BridgeState
from .log_buffer import LogRingBuffer
from .telegram.client_pool import TelegramClientPool
from .telegram.listener import TelegramListener
from .max.client_pool import MaxClientPool
from .max.listener import MaxListener
from .dm_bridge import DmBridge
from .dm_store import DmStore
from .admin_bot import AdminBot

log = logging.getLogger("bridge")


def setup_logging() -> LogRingBuffer:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("pymax").setLevel(logging.WARNING)

    log_buffer = LogRingBuffer(capacity=200)
    log_buffer.setLevel(logging.INFO)
    log_buffer.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    logging.getLogger("bridge").addHandler(log_buffer)
    return log_buffer


async def main(is_restart: bool = False):
    log_buffer = setup_logging()
    start_time = time.monotonic()

    log.info("Loading configuration...")
    config = load_config()
    lookup = ConfigLookup(config)
    users = lookup.get_unique_users()

    db_path = os.path.join(config.sessions_dir, "bridge.db")
    message_store = MessageStore(db_path=db_path)
    message_store.start()

    mirror_tracker = MirrorTracker()
    bridge_state = BridgeState()

    log.info("Initializing Telegram user accounts...")
    tg_pool = TelegramClientPool(config)
    tg_ok = await tg_pool.init(users)
    tg_ok_names = {u.name for u in tg_ok}

    log.info("Initializing MAX user accounts...")
    max_pool = MaxClientPool(config)
    max_ok = await max_pool.init(users)
    max_ok_names = {u.name for u in max_ok}

    # Only proceed with users that successfully initialized in both pools
    ok_names = tg_ok_names & max_ok_names
    skipped = {u.name for u in users} - ok_names
    if skipped:
        log.warning(
            "Skipping user(s) due to failed initialization: %s",
            ", ".join(sorted(skipped)),
        )
    users = [u for u in users if u.name in ok_names]
    if not users:
        raise RuntimeError("No users initialized successfully — cannot start bridge.")

    bridge = Bridge(lookup, message_store, tg_pool, max_pool, mirror_tracker, bridge_state)

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
        dm_store = DmStore(conn=message_store.connection)
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

    # ── Optional admin bot ──────────────────────────────────────────────────
    admin_bot = None
    if config.admin_bot:
        log.info("Initializing admin bot...")
        admin_bot = AdminBot(
            config=config,
            bridge_state=bridge_state,
            tg_pool=tg_pool,
            max_pool=max_pool,
            tg_listeners=tg_listeners,
            max_listeners=max_listeners,
            log_buffer=log_buffer,
            start_time=start_time,
        )

    shutdown_event = asyncio.Event()

    def _shutdown():
        log.info("Shutting down...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    if admin_bot:
        await admin_bot.start(shutdown_event=shutdown_event)
        log.info("Admin bot: enabled")
        elapsed = time.monotonic() - start_time
        bridge_count = len(config.bridges)
        user_count = len(users)
        label = "Restarted" if is_restart else "Started"
        await admin_bot.notify_admins(
            f"✅ Bridge {label.lower()} in {elapsed:.1f}s\n"
            f"  {bridge_count} bridge(s), {user_count} user(s)"
        )

    # Periodic health check — log connection status every 5 minutes
    # and write heartbeat file for Docker healthcheck
    health_file = os.path.join(config.sessions_dir, ".healthcheck")

    def _write_heartbeat():
        try:
            with open(health_file, "w") as f:
                f.write(str(time.time()))
        except OSError:
            log.warning("Failed to write healthcheck file")

    async def _health_loop():
        _write_heartbeat()  # initial heartbeat on startup
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
            _write_heartbeat()

    health_task = asyncio.create_task(_health_loop())

    await shutdown_event.wait()
    health_task.cancel()
    try:
        await health_task
    except asyncio.CancelledError:
        pass
    try:
        os.remove(health_file)
    except OSError:
        pass

    if admin_bot:
        await admin_bot.stop()
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

    restart = admin_bot.restart_requested if admin_bot else False
    log.info("Stopped.")
    return restart


if __name__ == "__main__":
    asyncio.run(main())
