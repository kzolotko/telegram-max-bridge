"""
Admin bot: Telegram bot for remote bridge management.

Separate bot (own token) that provides commands for monitoring,
configuration, auth, and control of the bridge.
"""

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pyrogram import Client as PyrogramClient, enums, filters
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

from .bridge_state import BridgeState
from .log_buffer import LogRingBuffer
from .max.bridge_client import BridgeMaxClient
from .max.native_client import NativeMaxAuth
from .max.session import MaxSession
from .setup import _extract_max_user_id, parse_max_chat_id
from .types import AppConfig

log = logging.getLogger("bridge.admin")

CONFIG_FILE = "config/config.yaml"


@dataclass
class ConversationState:
    flow: str       # "auth_max", "auth_tg", "add_bridge", "add_user", etc.
    step: str
    data: dict = field(default_factory=dict)
    client: Any = None  # NativeMaxAuth or Pyrogram Client during auth


class AdminBot:
    """Telegram bot for remote bridge management."""

    def __init__(
        self,
        config: AppConfig,
        bridge_state: BridgeState,
        tg_pool: Any,          # TelegramClientPool
        max_pool: Any,          # MaxClientPool
        tg_listeners: list,
        max_listeners: list,
        log_buffer: LogRingBuffer,
        start_time: float,
    ):
        self.config = config
        self.state = bridge_state
        self.tg_pool = tg_pool
        self.max_pool = max_pool
        self.tg_listeners = tg_listeners
        self.max_listeners = max_listeners
        self.log_buffer = log_buffer
        self.start_time = start_time

        self._admin_ids: set[int] = set(config.admin_bot.admin_ids)
        self._conversations: dict[int, ConversationState] = {}
        self._shutdown_event = None  # set from main.py for /restart
        self.restart_requested = False

        self.bot = PyrogramClient(
            name="admin_bot",
            api_id=config.api_id,
            api_hash=config.api_hash,
            bot_token=config.admin_bot.bot_token,
            workdir=config.sessions_dir,
        )

    async def start(self, shutdown_event=None):
        self._shutdown_event = shutdown_event
        admin_filter = filters.user(list(self._admin_ids)) & filters.private

        commands = [
            ("help", self._cmd_help),
            ("status", self._cmd_status),
            ("bridges", self._cmd_bridges),
            ("users", self._cmd_users),
            ("logs", self._cmd_logs),
            ("pause", self._cmd_pause),
            ("resume", self._cmd_resume),
            ("addbridge", self._cmd_addbridge),
            ("rmbridge", self._cmd_rmbridge),
            ("addbridgeuser", self._cmd_addbridgeuser),
            ("rmbridgeuser", self._cmd_rmbridgeuser),
            ("adduser", self._cmd_adduser),
            ("rmuser", self._cmd_rmuser),
            ("authmax", self._cmd_authmax),
            ("authtg", self._cmd_authtg),
            ("config", self._cmd_config),
            ("restart", self._cmd_restart),
            ("cancel", self._cmd_cancel),
        ]
        for name, handler in commands:
            self.bot.add_handler(
                MessageHandler(handler, admin_filter & filters.command(name)),
            )

        # Catch-all for conversation continuation (non-command private messages)
        self.bot.add_handler(
            MessageHandler(self._handle_conversation, admin_filter & ~filters.command(
                [name for name, _ in commands]
            )),
        )

        await self.bot.start()
        me = await self.bot.get_me()
        log.info("Admin bot started: @%s (ID: %d)", me.username, me.id)

    async def notify_admins(self, text: str):
        """Send a message to all admin users."""
        for uid in self._admin_ids:
            try:
                await self.bot.send_message(chat_id=uid, text=text)
            except Exception as e:
                log.warning("Failed to notify admin %s: %s", uid, e)

    async def stop(self):
        # Clean up active conversations
        for uid, conv in list(self._conversations.items()):
            await self._cleanup_conversation(conv)
        self._conversations.clear()
        try:
            await self.bot.stop()
        except Exception:
            pass

    async def _cleanup_conversation(self, conv: ConversationState):
        if conv.client:
            try:
                if isinstance(conv.client, NativeMaxAuth):
                    await conv.client.close()
                elif isinstance(conv.client, PyrogramClient):
                    await conv.client.stop()
            except Exception:
                pass
            conv.client = None

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _truncate(text: str, limit: int = 4096) -> str:
        if len(text) <= limit:
            return text
        return text[:limit - 20] + "\n\n... (truncated)"

    def _format_uptime(self) -> str:
        secs = int(time.monotonic() - self.start_time)
        days, secs = divmod(secs, 86400)
        hours, secs = divmod(secs, 3600)
        mins, secs = divmod(secs, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        parts.append(f"{mins}m")
        return " ".join(parts)

    def _read_config_raw(self) -> dict:
        path = Path(CONFIG_FILE)
        if not path.exists():
            return {}
        with open(path) as f:
            return yaml.safe_load(f) or {}

    def _write_config_raw(self, data: dict):
        path = Path(CONFIG_FILE)
        path.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False))

    def _get_bridge_names(self) -> list[str]:
        raw = self._read_config_raw()
        return [b.get("name", "") for b in raw.get("bridges", []) if b.get("name")]

    def _get_user_names(self) -> list[str]:
        raw = self._read_config_raw()
        # New format: top-level users section
        if isinstance(raw.get("users"), list) and raw["users"]:
            return [u.get("name", "") for u in raw["users"] if u.get("name")]
        # Old format: extract from bridges
        seen = []
        for b in raw.get("bridges", []):
            name = b.get("user", {}).get("name", "")
            if name and name not in seen:
                seen.append(name)
        return seen

    def _get_user_data(self, user_name: str) -> dict | None:
        """Get user data dict by name from config."""
        raw = self._read_config_raw()
        # New format
        if isinstance(raw.get("users"), list):
            for u in raw["users"]:
                if u.get("name") == user_name:
                    return u
        # Old format fallback
        for b in raw.get("bridges", []):
            u = b.get("user", {})
            if u.get("name") == user_name:
                return u
        return None

    # ── Status & Monitoring Commands ─────────────────────────────────────────

    async def _cmd_help(self, client: PyrogramClient, message: Message):
        text = (
            "Bridge Admin Bot\n\n"
            "Monitoring:\n"
            "  /status — uptime, connections, forwarding state\n"
            "  /bridges — list configured bridges\n"
            "  /users — list configured users\n"
            "  /logs [count] [level] — recent log entries\n"
            "\n"
            "Control:\n"
            "  /pause [bridge] — pause forwarding\n"
            "  /resume [bridge] — resume forwarding\n"
            "\n"
            "Configuration:\n"
            "  /addbridge — add a new bridge (interactive)\n"
            "  /rmbridge — remove a bridge\n"
            "  /addbridgeuser — add user to existing bridge\n"
            "  /rmbridgeuser — remove user from bridge\n"
            "  /adduser — add a new user to config\n"
            "  /rmuser — remove a user\n"
            "  /config — show current config\n"
            "\n"
            "Authentication:\n"
            "  /authmax <username> — authenticate MAX account\n"
            "  /authtg <username> — authenticate Telegram account\n"
            "\n"
            "System:\n"
            "  /restart — restart the bridge process\n"
            "  /cancel — cancel current interactive flow\n"
        )
        await message.reply_text(text)

    async def _cmd_status(self, client: PyrogramClient, message: Message):
        lines = [f"Uptime: {self._format_uptime()}"]

        # TG pool
        tg_ok = tg_total = 0
        for user_id in self.tg_pool._clients:
            tg_total += 1
            c = self.tg_pool.get_client(user_id)
            if c and c.is_connected:
                tg_ok += 1
        lines.append(f"Telegram: {tg_ok}/{tg_total} connected")

        # MAX pool
        max_ok = max_total = 0
        for uid in self.max_pool.get_all_user_ids():
            max_total += 1
            c = self.max_pool.get_client(uid)
            if c and c.is_connected:
                max_ok += 1
        lines.append(f"MAX: {max_ok}/{max_total} connected")

        # MAX listeners
        for listener in self.max_listeners:
            c = listener.client
            status = "connected" if (c and c.is_connected) else "DISCONNECTED"
            lines.append(f"  MAX listener {listener.user.name}: {status}")

        # Pause state
        if self.state.is_globally_paused:
            lines.append("\nForwarding: PAUSED (global)")
        else:
            paused = self.state.get_paused_bridges()
            if paused:
                lines.append(f"\nForwarding: active (paused: {', '.join(paused)})")
            else:
                lines.append("\nForwarding: active")

        await message.reply_text("\n".join(lines))

    async def _cmd_bridges(self, client: PyrogramClient, message: Message):
        raw = self._read_config_raw()
        bridges = raw.get("bridges", [])
        if not bridges:
            await message.reply_text("No bridges configured.")
            return

        lines = []
        for i, b in enumerate(bridges, 1):
            name = b.get("name", "?")
            tg = b.get("telegram_chat_id", "?")
            mx = b.get("max_chat_id", "?")
            # Support both new and old format
            if "users" in b:
                users_str = ", ".join(b["users"])
            else:
                users_str = b.get("user", {}).get("name", "?")
            paused = not self.state.should_forward(name)
            status = " [PAUSED]" if paused else ""
            lines.append(f"{i}. {name}{status}\n   TG: {tg} <-> MAX: {mx}\n   users: {users_str}")

        await message.reply_text(self._truncate("\n\n".join(lines)))

    async def _cmd_users(self, client: PyrogramClient, message: Message):
        raw = self._read_config_raw()

        # New format: top-level users
        if isinstance(raw.get("users"), list) and raw["users"]:
            user_list = raw["users"]
        else:
            # Old format: extract from bridges
            seen: dict[str, dict] = {}
            for b in raw.get("bridges", []):
                u = b.get("user", {})
                name = u.get("name", "")
                if name and name not in seen:
                    seen[name] = u
            user_list = list(seen.values())

        if not user_list:
            await message.reply_text("No users configured.")
            return

        lines = []
        for u in user_list:
            name = u.get("name", "?")
            tg_id = u.get("telegram_user_id", "?")
            max_id = u.get("max_user_id", "?")
            lines.append(f"{name}: TG={tg_id} MAX={max_id}")

        await message.reply_text("\n".join(lines))

    async def _cmd_logs(self, client: PyrogramClient, message: Message):
        parts = message.text.split()
        count = 20
        level = None
        for p in parts[1:]:
            if p.isdigit():
                count = int(p)
            elif p.upper() in ("ERROR", "WARNING", "INFO", "DEBUG"):
                level = p

        entries = self.log_buffer.get_recent(count, level)
        if not entries:
            await message.reply_text("No log entries.")
            return

        text = "\n".join(entries)
        await message.reply_text(self._truncate(f"<pre>{text}</pre>", 4096),
                                 parse_mode=enums.ParseMode.HTML)

    # ── Forwarding Control ───────────────────────────────────────────────────

    async def _cmd_pause(self, client: PyrogramClient, message: Message):
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            name = parts[1].strip()
            if name not in self._get_bridge_names():
                await message.reply_text(f"Bridge '{name}' not found.")
                return
            self.state.pause_bridge(name)
            await message.reply_text(f"Paused: {name}")
        else:
            self.state.pause_global()
            await message.reply_text("All forwarding paused.")

    async def _cmd_resume(self, client: PyrogramClient, message: Message):
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            name = parts[1].strip()
            self.state.resume_bridge(name)
            await message.reply_text(f"Resumed: {name}")
        else:
            self.state.resume_global()
            await message.reply_text("All forwarding resumed.")

    # ── Configuration Management ─────────────────────────────────────────────

    async def _cmd_config(self, client: PyrogramClient, message: Message):
        raw = self._read_config_raw()
        # Sanitize tokens
        sanitized = dict(raw)
        for section in ("dm_bridge", "admin_bot"):
            if section in sanitized and "bot_token" in sanitized[section]:
                token = sanitized[section]["bot_token"]
                sanitized[section] = dict(sanitized[section])
                sanitized[section]["bot_token"] = token[:10] + "..."
        text = yaml.dump(sanitized, allow_unicode=True, default_flow_style=False)
        await message.reply_text(self._truncate(f"<pre>{text}</pre>", 4096),
                                 parse_mode=enums.ParseMode.HTML)

    async def _cmd_addbridge(self, client: PyrogramClient, message: Message):
        users = self._get_user_names()
        if not users:
            await message.reply_text(
                "No users configured. Use /adduser first to register a user."
            )
            return
        conv = ConversationState(flow="add_bridge", step="name")
        conv.data["_users"] = users
        self._conversations[message.from_user.id] = conv
        await message.reply_text("Enter bridge name (e.g. team-general):")

    async def _cmd_rmbridge(self, client: PyrogramClient, message: Message):
        bridge_names = self._get_bridge_names()
        if not bridge_names:
            await message.reply_text("No bridges configured.")
            return

        raw = self._read_config_raw()
        bridges = raw.get("bridges", [])

        lines = ["Select bridge to remove (enter number):"]
        for i, b in enumerate(bridges, 1):
            name = b.get("name", "?")
            if "users" in b:
                users_str = ", ".join(b["users"])
            else:
                users_str = b.get("user", {}).get("name", "?")
            lines.append(f"  {i}. {name} ({users_str})")

        conv = ConversationState(flow="rm_bridge", step="select")
        conv.data["_bridges"] = bridges
        self._conversations[message.from_user.id] = conv
        await message.reply_text("\n".join(lines))

    async def _conv_rm_bridge(self, message: Message, conv: ConversationState, text: str):
        uid = message.from_user.id
        bridges = conv.data.get("_bridges", [])

        if conv.step == "select":
            try:
                idx = int(text) - 1
                if not (0 <= idx < len(bridges)):
                    raise ValueError
            except ValueError:
                await message.reply_text(f"Enter a number between 1 and {len(bridges)}:")
                return

            target = bridges[idx]
            name = target.get("name", "?")
            bridge_users = target.get("users", [])

            if len(bridge_users) <= 1:
                # Single or no users — remove entire bridge
                self._conversations.pop(uid, None)
                await self._do_rmbridge(message, name)
            else:
                # Multiple users — ask what to remove
                lines = [f"Bridge '{name}' has {len(bridge_users)} user(s):"]
                lines.append(f"  1. Remove entire bridge")
                for i, u in enumerate(bridge_users, 2):
                    lines.append(f"  {i}. Remove only user '{u}'")
                conv.data["_name"] = name
                conv.data["_bridge_users"] = bridge_users
                conv.step = "confirm_user"
                await message.reply_text("\n".join(lines))

        elif conv.step == "confirm_user":
            name = conv.data["_name"]
            bridge_users = conv.data.get("_bridge_users", [])
            total_options = 1 + len(bridge_users)
            try:
                choice = int(text)
                if not (1 <= choice <= total_options):
                    raise ValueError
            except ValueError:
                await message.reply_text(f"Enter a number between 1 and {total_options}:")
                return

            self._conversations.pop(uid, None)

            if choice == 1:
                await self._do_rmbridge(message, name)
            else:
                user_name = bridge_users[choice - 2]
                await self._do_rmbridge_user(message, name, user_name)

    async def _do_rmbridge(self, message: Message, name: str):
        raw = self._read_config_raw()
        bridges = raw.get("bridges", [])
        new_bridges = [b for b in bridges if b.get("name") != name]

        if len(new_bridges) == len(bridges):
            await message.reply_text(f"Bridge '{name}' not found.")
            return

        raw["bridges"] = new_bridges
        self._write_config_raw(raw)
        await message.reply_text(
            f"Bridge '{name}' removed.\n"
            f"Use /restart to apply changes."
        )

    async def _do_rmbridge_user(self, message: Message, bridge_name: str, user_name: str):
        raw = self._read_config_raw()
        bridges = raw.get("bridges", [])

        for b in bridges:
            if b.get("name") == bridge_name and "users" in b:
                if user_name in b["users"]:
                    b["users"].remove(user_name)
                    if not b["users"]:
                        bridges.remove(b)
                        self._write_config_raw(raw)
                        await message.reply_text(
                            f"Removed last user '{user_name}' from '{bridge_name}'. "
                            f"Bridge removed.\nUse /restart to apply changes."
                        )
                    else:
                        self._write_config_raw(raw)
                        await message.reply_text(
                            f"Removed user '{user_name}' from bridge '{bridge_name}'.\n"
                            f"Use /restart to apply changes."
                        )
                    return

        # Old format fallback
        new_bridges = [
            b for b in bridges
            if not (b.get("name") == bridge_name and b.get("user", {}).get("name") == user_name)
        ]
        if len(new_bridges) == len(bridges):
            await message.reply_text(f"Entry for '{bridge_name}' / '{user_name}' not found.")
            return

        raw["bridges"] = new_bridges
        self._write_config_raw(raw)
        await message.reply_text(
            f"Removed '{bridge_name}' for user '{user_name}'.\n"
            f"Use /restart to apply changes."
        )

    async def _cmd_adduser(self, client: PyrogramClient, message: Message):
        self._conversations[message.from_user.id] = ConversationState(
            flow="add_user", step="name",
        )
        await message.reply_text("Enter username (lowercase letters, digits, underscores):")

    async def _cmd_rmuser(self, client: PyrogramClient, message: Message):
        user_names = self._get_user_names()
        if not user_names:
            await message.reply_text("No users configured.")
            return

        # Show which bridges reference each user
        raw = self._read_config_raw()
        lines = ["Select user to remove (enter number):"]
        for i, name in enumerate(user_names, 1):
            bridges_with_user = []
            for b in raw.get("bridges", []):
                if name in b.get("users", []):
                    bridges_with_user.append(b.get("name", "?"))
            bridge_info = f"  (in: {', '.join(bridges_with_user)})" if bridges_with_user else ""
            lines.append(f"  {i}. {name}{bridge_info}")
        lines.append("\nThis will remove the user from all bridges.")
        conv = ConversationState(flow="rm_user", step="select")
        conv.data["_users"] = user_names
        self._conversations[message.from_user.id] = conv
        await message.reply_text("\n".join(lines))

    async def _conv_rm_user(self, message: Message, conv: ConversationState, text: str):
        uid = message.from_user.id
        users = conv.data.get("_users", [])
        try:
            idx = int(text) - 1
            if not (0 <= idx < len(users)):
                raise ValueError
        except ValueError:
            await message.reply_text(f"Enter a number between 1 and {len(users)}:")
            return

        name = users[idx]
        self._conversations.pop(uid, None)

        raw = self._read_config_raw()

        # New format: remove from users section and from all bridges
        if isinstance(raw.get("users"), list):
            raw["users"] = [u for u in raw["users"] if u.get("name") != name]
            # Remove from all bridges' users lists
            for b in raw.get("bridges", []):
                if "users" in b and name in b["users"]:
                    b["users"].remove(name)
            # Remove empty bridges
            raw["bridges"] = [b for b in raw.get("bridges", []) if b.get("users")]
        else:
            # Old format: remove bridge entries with this user
            raw["bridges"] = [
                b for b in raw.get("bridges", [])
                if b.get("user", {}).get("name") != name
            ]

        self._write_config_raw(raw)
        await message.reply_text(
            f"User '{name}' removed.\n"
            f"Use /restart to apply changes."
        )

    # ── Authentication Commands ──────────────────────────────────────────────

    async def _cmd_authmax(self, client: PyrogramClient, message: Message):
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply_text("Usage: /authmax <username>")
            return

        username = parts[1].strip()
        if not re.match(r'^[a-z0-9_]+$', username):
            await message.reply_text("Username must be lowercase letters, digits, underscores.")
            return

        # Check if session already exists
        session = MaxSession(f"max_{username}", self.config.sessions_dir)
        if session.exists():
            try:
                token = session.load()
                device_id = session.load_device_id()
                if token and device_id:
                    test_client = BridgeMaxClient(token=token, device_id=device_id)
                    await test_client.connect_and_login()
                    uid = test_client.inner.me.id if test_client.inner.me else session.load_user_id()
                    await test_client.disconnect()
                    await message.reply_text(
                        f"MAX session for '{username}' is already valid (ID: {uid}).\n"
                        f"To re-authenticate, delete the session file first."
                    )
                    return
            except Exception:
                await message.reply_text(
                    f"Existing MAX session for '{username}' is invalid. Re-authenticating..."
                )

        self._conversations[message.from_user.id] = ConversationState(
            flow="auth_max", step="phone",
            data={"username": username},
        )
        await message.reply_text(f"Enter phone number for MAX auth (e.g. +79991234567):")

    async def _cmd_authtg(self, client: PyrogramClient, message: Message):
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply_text("Usage: /authtg <username>")
            return

        username = parts[1].strip()
        if not re.match(r'^[a-z0-9_]+$', username):
            await message.reply_text("Username must be lowercase letters, digits, underscores.")
            return

        # Check if session already exists
        session_path = Path(self.config.sessions_dir) / f"tg_{username}.session"
        if session_path.exists():
            try:
                test_client = PyrogramClient(
                    name=f"tg_{username}",
                    api_id=self.config.api_id,
                    api_hash=self.config.api_hash,
                    workdir=self.config.sessions_dir,
                )
                await test_client.start()
                me = await test_client.get_me()
                await test_client.stop()
                await message.reply_text(
                    f"Telegram session for '{username}' is already valid: "
                    f"@{me.username or me.first_name} (ID: {me.id}).\n"
                    f"To re-authenticate, delete the session file first."
                )
                return
            except Exception:
                await message.reply_text(
                    f"Existing Telegram session for '{username}' is invalid. Re-authenticating..."
                )

        self._conversations[message.from_user.id] = ConversationState(
            flow="auth_tg", step="phone",
            data={"username": username},
        )
        await message.reply_text(f"Enter phone number for Telegram auth (e.g. +79991234567):")

    # ── System Commands ──────────────────────────────────────────────────────

    async def _cmd_restart(self, client: PyrogramClient, message: Message):
        await message.reply_text("Restarting bridge...")
        self.restart_requested = True
        if self._shutdown_event:
            self._shutdown_event.set()
        else:
            os._exit(42)

    async def _cmd_cancel(self, client: PyrogramClient, message: Message):
        uid = message.from_user.id
        conv = self._conversations.pop(uid, None)
        if conv:
            await self._cleanup_conversation(conv)
            await message.reply_text("Cancelled.")
        else:
            await message.reply_text("Nothing to cancel.")

    # ── Conversation Handler ─────────────────────────────────────────────────

    async def _handle_conversation(self, client: PyrogramClient, message: Message):
        uid = message.from_user.id
        conv = self._conversations.get(uid)
        if not conv:
            await message.reply_text("Unknown command. Send /help for available commands.")
            return

        text = (message.text or "").strip()
        if not text:
            return

        try:
            if conv.flow == "add_bridge":
                await self._conv_add_bridge(message, conv, text)
            elif conv.flow == "add_bridge_user":
                await self._conv_add_bridge_user(message, conv, text)
            elif conv.flow == "rm_bridge_user":
                await self._conv_rm_bridge_user(message, conv, text)
            elif conv.flow == "add_user":
                await self._conv_add_user(message, conv, text)
            elif conv.flow == "auth_max":
                await self._conv_auth_max(message, conv, text)
            elif conv.flow == "auth_tg":
                await self._conv_auth_tg(message, conv, text)
            elif conv.flow == "rm_bridge":
                await self._conv_rm_bridge(message, conv, text)
            elif conv.flow == "rm_user":
                await self._conv_rm_user(message, conv, text)
        except Exception as e:
            log.error("Conversation error (%s/%s): %s", conv.flow, conv.step, e, exc_info=True)
            await self._cleanup_conversation(conv)
            self._conversations.pop(uid, None)
            await message.reply_text(f"Error: {e}\nConversation cancelled.")

    # ── Add Bridge Conversation ──────────────────────────────────────────────

    async def _load_tg_chats(self, conv: ConversationState) -> bool:
        """Load and cache TG chats. Returns False if unavailable."""
        if "_tg_chats" in conv.data:
            return True
        user_data = conv.data["_user"]
        tg_client = self.tg_pool.get_client(user_data["telegram_user_id"])
        if not tg_client:
            return False
        try:
            chats = []
            async for dialog in tg_client.get_dialogs():
                if dialog.chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
                    chats.append(dialog.chat)
            chats.sort(key=lambda c: (c.title or "").lower())
            conv.data["_tg_chats"] = [(c.id, c.title or f"Chat {c.id}") for c in chats]
            return bool(chats)
        except Exception as e:
            log.warning("Failed to load TG chats: %s", e)
            return False

    def _load_max_chats(self, conv: ConversationState) -> bool:
        """Load and cache MAX chats. Returns False if unavailable."""
        if "_max_chats" in conv.data:
            return True
        user_data = conv.data["_user"]
        max_client = self.max_pool.get_client(user_data["max_user_id"])
        if not max_client:
            return False
        try:
            chats = []
            for chat in max_client.inner.chats:
                chats.append({
                    "id": chat.id,
                    "title": chat.title or f"Chat {chat.id}",
                    "type": "CHAT",
                    "members": getattr(chat, "participants_count", None),
                })
            for ch in max_client.inner.channels:
                chats.append({
                    "id": ch.id,
                    "title": ch.title or f"Channel {ch.id}",
                    "type": "CHANNEL",
                    "members": getattr(ch, "participants_count", None),
                })
            chats.sort(key=lambda c: c["title"].lower())
            conv.data["_max_chats"] = chats
            return bool(chats)
        except Exception as e:
            log.warning("Failed to load MAX chats: %s", e)
            return False

    def _search_tg(self, chats: list, query: str) -> list:
        q = query.lower()
        return [(cid, title) for cid, title in chats if q in title.lower()]

    def _search_max(self, chats: list, query: str) -> list:
        q = query.lower()
        return [c for c in chats if q in c["title"].lower()]

    async def _finish_max_selection(self, message: Message, conv: ConversationState):
        """Transition after MAX chat is selected — save bridge."""
        uid = message.from_user.id
        max_title = conv.data.get("max_chat_title", str(conv.data["max_chat_id"]))
        await message.reply_text(f"✅ MAX: {max_title} ({conv.data['max_chat_id']})")

        bridge_name = conv.data["_bridge_name"]
        primary_user = conv.data.get("_primary_user", conv.data["_user"]["name"])
        raw = self._read_config_raw()

        new_bridge = {
            "name": bridge_name,
            "telegram_chat_id": conv.data["tg_chat_id"],
            "max_chat_id": conv.data["max_chat_id"],
            "users": [primary_user],
        }

        # Ensure users section exists
        if not isinstance(raw.get("users"), list):
            seen_users: dict[str, dict] = {}
            for b in raw.get("bridges", []):
                u = b.get("user", {})
                uname = u.get("name", "")
                if uname and uname not in seen_users:
                    seen_users[uname] = u
            raw["users"] = list(seen_users.values())

        raw.setdefault("bridges", []).append(new_bridge)
        self._write_config_raw(raw)
        self._conversations.pop(uid, None)

        await message.reply_text(
            f"Bridge '{bridge_name}' added.\n"
            f"  TG: {conv.data.get('tg_chat_title', conv.data['tg_chat_id'])} ({conv.data['tg_chat_id']})\n"
            f"  MAX: {max_title} ({conv.data['max_chat_id']})\n"
            f"  users: {primary_user}\n\n"
            f"Use /addbridgeuser to add more users.\n"
            f"Use /restart to apply changes."
        )

    async def _conv_add_bridge(self, message: Message, conv: ConversationState, text: str):
        uid = message.from_user.id

        if conv.step == "name":
            bridge_name = text.strip()
            if bridge_name in self._get_bridge_names():
                await message.reply_text(
                    f"Bridge '{bridge_name}' already exists. Enter a different name:"
                )
                return
            conv.data["_bridge_name"] = bridge_name

            users = conv.data.get("_users", [])
            lines = [f"Bridge: {bridge_name}\n\nSelect primary user (enter number):"]
            for i, name in enumerate(users, 1):
                lines.append(f"  {i}. {name}")
            conv.step = "user"
            await message.reply_text("\n".join(lines))

        elif conv.step == "user":
            users = conv.data.get("_users", [])
            try:
                idx = int(text) - 1
                if not (0 <= idx < len(users)):
                    raise ValueError
            except ValueError:
                await message.reply_text(f"Enter a number between 1 and {len(users)}:")
                return

            user_name = users[idx]
            user_data = self._get_user_data(user_name)
            if not user_data:
                await message.reply_text(f"User '{user_name}' details not found.")
                self._conversations.pop(uid, None)
                return

            conv.data["_user"] = user_data
            conv.data["_primary_user"] = user_name
            await message.reply_text(
                f"User: {user_name}\n\nEnter Telegram chat name (or part of it):"
            )
            conv.step = "tg_search"

        elif conv.step == "tg_search":
            ok = await self._load_tg_chats(conv)
            if not ok:
                await message.reply_text(
                    "Could not load Telegram chats.\n"
                    "Enter Telegram chat ID manually (negative number):"
                )
                conv.step = "tg_chat_manual"
                return
            matches = self._search_tg(conv.data["_tg_chats"], text)
            if not matches:
                await message.reply_text(
                    f'No Telegram chats matching "{text}". Try again:'
                )
                return
            if len(matches) == 1:
                cid, title = matches[0]
                conv.data["tg_chat_id"] = cid
                conv.data["tg_chat_title"] = title
                await message.reply_text(
                    f"✅ TG: {title} ({cid})\n\nEnter MAX chat name (or part of it):"
                )
                conv.step = "max_search"
            else:
                lines = [f'Found {len(matches)} Telegram chats matching "{text}":']
                for i, (cid, title) in enumerate(matches, 1):
                    lines.append(f"  {i}. {title} ({cid})")
                lines.append("\nEnter number to select, or search again:")
                conv.data["_tg_matches"] = matches
                conv.step = "tg_select"
                await message.reply_text("\n".join(lines))

        elif conv.step == "tg_select":
            matches = conv.data.get("_tg_matches", [])
            try:
                idx = int(text) - 1
                if not (0 <= idx < len(matches)):
                    raise ValueError
                cid, title = matches[idx]
                conv.data["tg_chat_id"] = cid
                conv.data["tg_chat_title"] = title
                conv.data.pop("_tg_matches", None)
                await message.reply_text(
                    f"✅ TG: {title} ({cid})\n\nEnter MAX chat name (or part of it):"
                )
                conv.step = "max_search"
            except ValueError:
                # Treat as new search
                conv.data.pop("_tg_matches", None)
                conv.step = "tg_search"
                await self._conv_add_bridge(message, conv, text)

        elif conv.step == "tg_chat_manual":
            try:
                conv.data["tg_chat_id"] = int(text)
                conv.data["tg_chat_title"] = str(conv.data["tg_chat_id"])
            except ValueError:
                await message.reply_text("Must be a number. Try again:")
                return
            await message.reply_text(
                f"✅ TG: {conv.data['tg_chat_id']}\n\nEnter MAX chat name (or part of it):"
            )
            conv.step = "max_search"

        elif conv.step == "max_search":
            # Allow manual URL/ID entry directly
            try:
                conv.data["max_chat_id"] = parse_max_chat_id(text)
                conv.data["max_chat_title"] = str(conv.data["max_chat_id"])
                await self._finish_max_selection(message, conv)
                return
            except ValueError:
                pass

            ok = self._load_max_chats(conv)
            if not ok:
                await message.reply_text(
                    "Could not load MAX chats.\n"
                    "Enter MAX chat ID or URL manually:"
                )
                conv.step = "max_chat_manual"
                return
            matches = self._search_max(conv.data["_max_chats"], text)
            if not matches:
                await message.reply_text(
                    f'No MAX chats matching "{text}". Try again, or send a MAX chat URL / ID:'
                )
                return
            if len(matches) == 1:
                c = matches[0]
                conv.data["max_chat_id"] = c["id"]
                conv.data["max_chat_title"] = c["title"]
                await self._finish_max_selection(message, conv)
            else:
                lines = [f'Found {len(matches)} MAX chats matching "{text}":']
                for i, c in enumerate(matches, 1):
                    members = f", {c['members']} members" if c["members"] else ""
                    lines.append(f"  {i}. [{c['type']}] {c['title']}{members}")
                lines.append("\nEnter number to select, or search again:")
                conv.data["_max_matches"] = matches
                conv.step = "max_select"
                await message.reply_text("\n".join(lines))

        elif conv.step == "max_select":
            matches = conv.data.get("_max_matches", [])
            try:
                idx = int(text) - 1
                if not (0 <= idx < len(matches)):
                    raise ValueError
                c = matches[idx]
                conv.data["max_chat_id"] = c["id"]
                conv.data["max_chat_title"] = c["title"]
                conv.data.pop("_max_matches", None)
                await self._finish_max_selection(message, conv)
            except ValueError:
                # Treat as new search
                conv.data.pop("_max_matches", None)
                conv.step = "max_search"
                await self._conv_add_bridge(message, conv, text)

        elif conv.step == "max_chat_manual":
            try:
                conv.data["max_chat_id"] = parse_max_chat_id(text)
                conv.data["max_chat_title"] = str(conv.data["max_chat_id"])
            except ValueError as e:
                await message.reply_text(f"{e}\nTry again:")
                return
            await self._finish_max_selection(message, conv)

    # ── Add Bridge User Conversation ─────────────────────────────────────────

    async def _cmd_addbridgeuser(self, client: PyrogramClient, message: Message):
        """Add an existing user to an existing bridge."""
        raw = self._read_config_raw()
        bridges = raw.get("bridges", [])
        if not bridges:
            await message.reply_text("No bridges configured.")
            return

        lines = ["Select bridge (enter number):"]
        for i, b in enumerate(bridges, 1):
            name = b.get("name", "?")
            if "users" in b:
                users_str = ", ".join(b["users"])
            else:
                users_str = b.get("user", {}).get("name", "?")
            lines.append(f"  {i}. {name} ({users_str})")

        conv = ConversationState(flow="add_bridge_user", step="select_bridge")
        conv.data["_bridges"] = bridges
        self._conversations[message.from_user.id] = conv
        await message.reply_text("\n".join(lines))

    async def _conv_add_bridge_user(self, message: Message, conv: ConversationState, text: str):
        uid = message.from_user.id

        if conv.step == "select_bridge":
            bridges = conv.data.get("_bridges", [])
            try:
                idx = int(text) - 1
                if not (0 <= idx < len(bridges)):
                    raise ValueError
            except ValueError:
                await message.reply_text(f"Enter a number between 1 and {len(bridges)}:")
                return

            target = bridges[idx]
            conv.data["_target_bridge"] = target
            bridge_user_names = set(target.get("users", []))

            # Show available users not already in this bridge
            all_users = self._get_user_names()
            available = [u for u in all_users if u not in bridge_user_names]

            if not available:
                self._conversations.pop(uid, None)
                await message.reply_text("All users are already in this bridge.")
                return

            lines = [f"Bridge: {target.get('name', '?')}\n\nSelect user to add (enter number):"]
            for i, name in enumerate(available, 1):
                lines.append(f"  {i}. {name}")
            conv.data["_available"] = available
            conv.step = "select_user"
            await message.reply_text("\n".join(lines))

        elif conv.step == "select_user":
            available = conv.data.get("_available", [])
            try:
                idx = int(text) - 1
                if not (0 <= idx < len(available)):
                    raise ValueError
            except ValueError:
                await message.reply_text(f"Enter a number between 1 and {len(available)}:")
                return

            user_name = available[idx]
            target = conv.data["_target_bridge"]
            bridge_name = target.get("name", "?")
            self._conversations.pop(uid, None)

            # Write to config
            raw = self._read_config_raw()
            for b in raw.get("bridges", []):
                if b.get("name") == bridge_name and "users" in b:
                    if user_name not in b["users"]:
                        b["users"].append(user_name)
                    break

            self._write_config_raw(raw)
            await message.reply_text(
                f"User '{user_name}' added to bridge '{bridge_name}'.\n"
                f"Use /restart to apply changes."
            )

    # ── Remove Bridge User Conversation ──────────────────────────────────────

    async def _cmd_rmbridgeuser(self, client: PyrogramClient, message: Message):
        """Remove a user from an existing bridge."""
        raw = self._read_config_raw()
        bridges = [b for b in raw.get("bridges", []) if len(b.get("users", [])) > 0]
        if not bridges:
            await message.reply_text("No bridges configured.")
            return

        lines = ["Select bridge (enter number):"]
        for i, b in enumerate(bridges, 1):
            name = b.get("name", "?")
            users_str = ", ".join(b.get("users", []))
            lines.append(f"  {i}. {name} ({users_str})")

        conv = ConversationState(flow="rm_bridge_user", step="select_bridge")
        conv.data["_bridges"] = bridges
        self._conversations[message.from_user.id] = conv
        await message.reply_text("\n".join(lines))

    async def _conv_rm_bridge_user(self, message: Message, conv: ConversationState, text: str):
        uid = message.from_user.id

        if conv.step == "select_bridge":
            bridges = conv.data.get("_bridges", [])
            try:
                idx = int(text) - 1
                if not (0 <= idx < len(bridges)):
                    raise ValueError
            except ValueError:
                await message.reply_text(f"Enter a number between 1 and {len(bridges)}:")
                return

            target = bridges[idx]
            bridge_users = target.get("users", [])

            if len(bridge_users) < 1:
                self._conversations.pop(uid, None)
                await message.reply_text("Bridge has no users.")
                return

            conv.data["_target_bridge_name"] = target.get("name", "?")
            conv.data["_bridge_users"] = bridge_users

            lines = [f"Bridge: {target.get('name', '?')}\n\nSelect user to remove (enter number):"]
            for i, uname in enumerate(bridge_users, 1):
                role = " (primary)" if i == 1 else ""
                lines.append(f"  {i}. {uname}{role}")
            conv.step = "select_user"
            await message.reply_text("\n".join(lines))

        elif conv.step == "select_user":
            bridge_users = conv.data.get("_bridge_users", [])
            try:
                idx = int(text) - 1
                if not (0 <= idx < len(bridge_users)):
                    raise ValueError
            except ValueError:
                await message.reply_text(f"Enter a number between 1 and {len(bridge_users)}:")
                return

            user_name = bridge_users[idx]
            bridge_name = conv.data["_target_bridge_name"]
            self._conversations.pop(uid, None)

            # Warnings for primary / last user
            if idx == 0 and len(bridge_users) > 1:
                next_primary = bridge_users[1]
                warning = f"'{next_primary}' will become the new primary.\n"
            elif len(bridge_users) == 1:
                warning = "This is the last user — bridge will be removed.\n"
            else:
                warning = ""

            raw = self._read_config_raw()
            for b in raw.get("bridges", []):
                if b.get("name") == bridge_name and "users" in b:
                    if user_name in b["users"]:
                        b["users"].remove(user_name)
                    if not b["users"]:
                        raw["bridges"].remove(b)
                        self._write_config_raw(raw)
                        await message.reply_text(
                            f"{warning}Bridge '{bridge_name}' removed (no users left).\n"
                            f"Use /restart to apply changes."
                        )
                        return
                    break

            self._write_config_raw(raw)
            await message.reply_text(
                f"{warning}User '{user_name}' removed from bridge '{bridge_name}'.\n"
                f"Use /restart to apply changes."
            )

    # ── Add User Conversation ────────────────────────────────────────────────

    async def _conv_add_user(self, message: Message, conv: ConversationState, text: str):
        uid = message.from_user.id

        if conv.step == "name":
            if not re.match(r'^[a-z0-9_]+$', text):
                await message.reply_text("Use only lowercase letters, digits, underscores. Try again:")
                return
            # Uniqueness check
            if text in self._get_user_names():
                await message.reply_text(f"User '{text}' already exists. Enter a different name:")
                return
            conv.data["name"] = text
            conv.step = "tg_user_id"
            await message.reply_text("Enter Telegram user ID (number):")

        elif conv.step == "tg_user_id":
            try:
                conv.data["tg_user_id"] = int(text)
            except ValueError:
                await message.reply_text("Must be a number. Try again:")
                return
            conv.step = "max_user_id"
            await message.reply_text("Enter MAX user ID (number):")

        elif conv.step == "max_user_id":
            try:
                conv.data["max_user_id"] = int(text)
            except ValueError:
                await message.reply_text("Must be a number. Try again:")
                return

            # Save to config
            raw = self._read_config_raw()
            if not isinstance(raw.get("users"), list):
                raw["users"] = []
            raw["users"].append({
                "name": conv.data["name"],
                "telegram_user_id": conv.data["tg_user_id"],
                "max_user_id": conv.data["max_user_id"],
            })
            self._write_config_raw(raw)

            self._conversations.pop(uid, None)
            await message.reply_text(
                f"User '{conv.data['name']}' added to config.\n"
                f"  TG: {conv.data['tg_user_id']}\n"
                f"  MAX: {conv.data['max_user_id']}\n\n"
                f"Next steps:\n"
                f"  /authmax {conv.data['name']} — authenticate MAX account\n"
                f"  /authtg {conv.data['name']} — authenticate Telegram account\n"
                f"  /addbridge — create a bridge using this user"
            )

    # ── Auth MAX Conversation ────────────────────────────────────────────────

    async def _conv_auth_max(self, message: Message, conv: ConversationState, text: str):
        uid = message.from_user.id
        username = conv.data["username"]

        if conv.step == "phone":
            phone = text.strip()
            await message.reply_text("Connecting to MAX...")

            native = NativeMaxAuth()
            await native.connect()
            await native.handshake()

            try:
                sms_token = await native.send_code(phone)
            except RuntimeError as e:
                await native.close()
                self._conversations.pop(uid, None)
                if "limit.violate" in str(e):
                    await message.reply_text(
                        "Too many auth attempts. Server blocked SMS.\n"
                        "Wait 1-2 hours and try again."
                    )
                else:
                    await message.reply_text(f"Error: {e}")
                return

            conv.data["phone"] = phone
            conv.data["sms_token"] = sms_token
            conv.client = native
            conv.step = "code"
            await message.reply_text("SMS code sent. Enter the code:")

        elif conv.step == "code":
            sms_token = conv.data["sms_token"]
            native = conv.client

            account_data = await native.sign_in(sms_token, int(text))

            password_challenge = account_data.get("passwordChallenge")
            login_attrs = account_data.get("tokenAttrs", {}).get("LOGIN", {})

            if password_challenge and not login_attrs:
                await native.close()
                self._conversations.pop(uid, None)
                await message.reply_text("MAX account has 2FA enabled. Not supported yet.")
                return

            login_token = login_attrs.get("token")
            if not login_token:
                await native.close()
                self._conversations.pop(uid, None)
                await message.reply_text(
                    f"No login token in response. Keys: {list(account_data.keys())}"
                )
                return

            session = MaxSession(f"max_{username}", self.config.sessions_dir)
            max_user_id = _extract_max_user_id(account_data, session)
            device_id = native.device_id

            # If no user_id from sign_in, try login_by_token
            if not max_user_id:
                try:
                    session.save(login_token, user_id=None, device_id=device_id)
                    login_native = NativeMaxAuth()
                    try:
                        login_resp = await login_native.login_by_token(login_token)
                        login_payload = login_resp.get("payload", {}) or {}
                        max_user_id = _extract_max_user_id(login_payload, session)
                    finally:
                        await login_native.close()
                except Exception as e:
                    log.warning("login_by_token for user_id failed: %s", e)

            session.save(login_token, user_id=max_user_id, device_id=device_id)
            await native.close()
            conv.client = None
            self._conversations.pop(uid, None)

            uid_str = str(max_user_id) if max_user_id else "unknown"
            await message.reply_text(
                f"MAX auth successful for '{username}'.\n"
                f"  User ID: {uid_str}\n"
                f"  Session saved."
            )

    # ── Auth TG Conversation ─────────────────────────────────────────────────

    async def _conv_auth_tg(self, message: Message, conv: ConversationState, text: str):
        uid = message.from_user.id
        username = conv.data["username"]

        if conv.step == "phone":
            phone = text.strip()
            await message.reply_text("Sending verification code...")

            tg_client = PyrogramClient(
                name=f"tg_{username}",
                api_id=self.config.api_id,
                api_hash=self.config.api_hash,
                workdir=self.config.sessions_dir,
            )
            await tg_client.connect()
            sent_code = await tg_client.send_code(phone)

            conv.data["phone"] = phone
            conv.data["phone_code_hash"] = sent_code.phone_code_hash
            conv.client = tg_client
            conv.step = "code"
            await message.reply_text("Verification code sent. Enter the code:")

        elif conv.step == "code":
            phone = conv.data["phone"]
            phone_code_hash = conv.data["phone_code_hash"]
            tg_client = conv.client

            try:
                await tg_client.sign_in(phone, phone_code_hash, text.strip())
            except Exception as e:
                err = str(e)
                if "SESSION_PASSWORD_NEEDED" in err or "Two-step" in err or "PASSWORD_HASH_INVALID" in err:
                    conv.step = "2fa"
                    await message.reply_text("2FA is enabled. Enter your password:")
                    return
                raise

            me = await tg_client.get_me()
            await tg_client.disconnect()
            conv.client = None
            self._conversations.pop(uid, None)

            await message.reply_text(
                f"Telegram auth successful for '{username}'.\n"
                f"  @{me.username or me.first_name} (ID: {me.id})\n"
                f"  Session saved."
            )

        elif conv.step == "2fa":
            tg_client = conv.client
            await tg_client.check_password(text.strip())

            me = await tg_client.get_me()
            await tg_client.disconnect()
            conv.client = None
            self._conversations.pop(uid, None)

            await message.reply_text(
                f"Telegram auth successful for '{username}'.\n"
                f"  @{me.username or me.first_name} (ID: {me.id})\n"
                f"  Session saved."
            )
