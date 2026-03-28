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
from pyrogram import Client as PyrogramClient, filters
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

CONFIG_FILE = "config.yaml"


@dataclass
class ConversationState:
    flow: str       # "auth_max", "auth_tg", "add_bridge", "add_user"
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
        seen = []
        for b in raw.get("bridges", []):
            name = b.get("name", "")
            if name and name not in seen:
                seen.append(name)
        return seen

    def _get_user_names(self) -> list[str]:
        raw = self._read_config_raw()
        seen = []
        for b in raw.get("bridges", []):
            name = b.get("user", {}).get("name", "")
            if name and name not in seen:
                seen.append(name)
        return seen

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
            "  /rmbridge <name> — remove a bridge\n"
            "  /adduser — add a new user (interactive)\n"
            "  /rmuser <name> — remove a user\n"
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
            user = b.get("user", {}).get("name", "?")
            paused = not self.state.should_forward(name)
            status = " [PAUSED]" if paused else ""
            lines.append(f"{i}. {name}{status}\n   TG: {tg} <-> MAX: {mx}\n   user: {user}")

        await message.reply_text(self._truncate("\n\n".join(lines)))

    async def _cmd_users(self, client: PyrogramClient, message: Message):
        raw = self._read_config_raw()
        seen: dict[str, dict] = {}
        for b in raw.get("bridges", []):
            u = b.get("user", {})
            name = u.get("name", "")
            if name and name not in seen:
                seen[name] = u

        if not seen:
            await message.reply_text("No users configured.")
            return

        lines = []
        for name, u in seen.items():
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
                                 parse_mode="html")

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
                                 parse_mode="html")

    async def _cmd_addbridge(self, client: PyrogramClient, message: Message):
        self._conversations[message.from_user.id] = ConversationState(
            flow="add_bridge", step="name",
        )
        await message.reply_text("Enter bridge name (e.g. team-general):")

    async def _cmd_rmbridge(self, client: PyrogramClient, message: Message):
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply_text("Usage: /rmbridge <name>")
            return

        name = parts[1].strip()
        raw = self._read_config_raw()
        bridges = raw.get("bridges", [])
        new_bridges = [b for b in bridges if b.get("name") != name]

        if len(new_bridges) == len(bridges):
            await message.reply_text(f"Bridge '{name}' not found.")
            return

        removed = len(bridges) - len(new_bridges)
        raw["bridges"] = new_bridges
        self._write_config_raw(raw)
        await message.reply_text(
            f"Removed {removed} entry(ies) for '{name}'.\n"
            f"Use /restart to apply changes."
        )

    async def _cmd_adduser(self, client: PyrogramClient, message: Message):
        self._conversations[message.from_user.id] = ConversationState(
            flow="add_user", step="name",
        )
        await message.reply_text("Enter username (lowercase letters, digits, underscores):")

    async def _cmd_rmuser(self, client: PyrogramClient, message: Message):
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply_text("Usage: /rmuser <name>")
            return

        name = parts[1].strip()
        raw = self._read_config_raw()
        bridges = raw.get("bridges", [])
        new_bridges = [b for b in bridges if b.get("user", {}).get("name") != name]

        if len(new_bridges) == len(bridges):
            await message.reply_text(f"User '{name}' not found in any bridge.")
            return

        removed = len(bridges) - len(new_bridges)
        raw["bridges"] = new_bridges
        self._write_config_raw(raw)
        await message.reply_text(
            f"Removed {removed} bridge(s) for user '{name}'.\n"
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
            elif conv.flow == "add_user":
                await self._conv_add_user(message, conv, text)
            elif conv.flow == "auth_max":
                await self._conv_auth_max(message, conv, text)
            elif conv.flow == "auth_tg":
                await self._conv_auth_tg(message, conv, text)
        except Exception as e:
            log.error("Conversation error (%s/%s): %s", conv.flow, conv.step, e, exc_info=True)
            await self._cleanup_conversation(conv)
            self._conversations.pop(uid, None)
            await message.reply_text(f"Error: {e}\nConversation cancelled.")

    # ── Add Bridge Conversation ──────────────────────────────────────────────

    async def _conv_add_bridge(self, message: Message, conv: ConversationState, text: str):
        uid = message.from_user.id

        if conv.step == "name":
            conv.data["name"] = text
            conv.step = "tg_chat_id"
            await message.reply_text("Enter Telegram chat ID (negative number):")

        elif conv.step == "tg_chat_id":
            try:
                conv.data["tg_chat_id"] = int(text)
            except ValueError:
                await message.reply_text("Must be a number. Try again:")
                return
            conv.step = "max_chat_id"
            await message.reply_text(
                "Enter MAX chat ID (number or web.max.ru URL):"
            )

        elif conv.step == "max_chat_id":
            try:
                conv.data["max_chat_id"] = parse_max_chat_id(text)
            except ValueError as e:
                await message.reply_text(f"{e}\nTry again:")
                return
            # Show users to pick from
            users = self._get_user_names()
            if not users:
                await message.reply_text("No users configured. Add a user first with /adduser.")
                self._conversations.pop(uid, None)
                return
            lines = ["Select user (enter number):"]
            for i, name in enumerate(users, 1):
                lines.append(f"  {i}. {name}")
            conv.data["_users"] = users
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
            # Find user details from config
            raw = self._read_config_raw()
            user_data = None
            for b in raw.get("bridges", []):
                u = b.get("user", {})
                if u.get("name") == user_name:
                    user_data = u
                    break

            if not user_data:
                await message.reply_text(f"User '{user_name}' details not found.")
                self._conversations.pop(uid, None)
                return

            # Write to config
            new_bridge = {
                "name": conv.data["name"],
                "telegram_chat_id": conv.data["tg_chat_id"],
                "max_chat_id": conv.data["max_chat_id"],
                "user": {
                    "name": user_data["name"],
                    "telegram_user_id": user_data["telegram_user_id"],
                    "max_user_id": user_data["max_user_id"],
                },
            }
            raw.setdefault("bridges", []).append(new_bridge)
            self._write_config_raw(raw)
            self._conversations.pop(uid, None)

            await message.reply_text(
                f"Bridge '{conv.data['name']}' added.\n"
                f"  TG: {conv.data['tg_chat_id']} <-> MAX: {conv.data['max_chat_id']}\n"
                f"  user: {user_name}\n\n"
                f"Use /restart to apply changes."
            )

    # ── Add User Conversation ────────────────────────────────────────────────

    async def _conv_add_user(self, message: Message, conv: ConversationState, text: str):
        uid = message.from_user.id

        if conv.step == "name":
            if not re.match(r'^[a-z0-9_]+$', text):
                await message.reply_text("Use only lowercase letters, digits, underscores. Try again:")
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

            self._conversations.pop(uid, None)
            await message.reply_text(
                f"User '{conv.data['name']}' registered.\n"
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
                # Could be 2FA
                if "Two-step" in str(e) or "PASSWORD_HASH_INVALID" in str(e):
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
