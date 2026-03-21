import asyncio
import logging
from typing import Callable, Awaitable, Any

from .bridge_client import BridgeMaxClient
from ..bridge.mirror_tracker import MirrorTracker
from ..config import ConfigLookup
from ..types import AppConfig, BridgeEvent, MediaInfo, UserMapping
from .session import MaxSession
from .media import download_media

log = logging.getLogger("bridge.max.listener")

RECONNECT_BASE_DELAY = 2
RECONNECT_MAX_DELAY = 60


class MaxListener:
    """Listens for messages in MAX using a user account via native TCP/SSL."""

    def __init__(
        self,
        config: AppConfig,
        lookup: ConfigLookup,
        mirror_tracker: MirrorTracker,
        on_event: Callable[[BridgeEvent], Awaitable[None]],
        user: UserMapping,
    ):
        self.config = config
        self.lookup = lookup
        self.mirrors = mirror_tracker
        self.on_event = on_event
        self.user = user
        self.client: BridgeMaxClient | None = None
        self._my_user_id: int = user.max_user_id
        self._login_token: str | None = None
        self._device_id: str | None = None
        self._stopped = False
        self._monitor_task: asyncio.Task | None = None
        self._name_cache: dict[int, str] = {}  # max_user_id -> display name

        # Pre-populate name cache with known bridge users.
        # For these users the bridge routes via their own TG account,
        # so the display name is rarely shown — but having it cached
        # avoids any network call and eliminates the delay entirely.
        for entry in config.bridges:
            u = entry.user
            self._name_cache[u.max_user_id] = u.name

    async def start(self) -> int:
        session = MaxSession(self.user.max_session, self.config.sessions_dir)
        if not session.exists():
            raise RuntimeError(
                f"MAX session not found ({self.user.max_session}) for user {self.user.name}. "
                f"Run 'python -m src.auth' first."
            )

        self._login_token = session.load()
        self._device_id = session.load_device_id()
        if not self._device_id:
            raise RuntimeError(
                f"No device_id in MAX session for {self.user.name}. "
                f"Re-authenticate with 'python -m src.auth'."
            )
        await self._connect()

        self._monitor_task = asyncio.create_task(self._reconnect_loop())

        log.info("Started for %s (User ID: %d)", self.user.name, self._my_user_id)
        return self._my_user_id

    async def _connect(self):
        self.client = BridgeMaxClient(token=self._login_token, device_id=self._device_id)
        self.client.set_raw_callback(self._handle_packet)
        await self.client.connect_and_login()
        # Pre-populate name cache with members of bridged MAX chats
        await self._preload_chat_members()

    async def _reconnect_loop(self):
        delay = RECONNECT_BASE_DELAY
        while not self._stopped:
            try:
                if self.client and self.client.recv_task:
                    await self.client.recv_task
            except Exception:
                pass

            if self._stopped:
                break

            log.warning("Connection lost. Reconnecting in %ds...", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)

            try:
                await self._connect()
                delay = RECONNECT_BASE_DELAY
                log.info("Reconnected (User ID: %d)", self._my_user_id)
            except Exception as e:
                log.error("Reconnect failed: %s", e)

    async def stop(self):
        self._stopped = True
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    async def _preload_chat_members(self):
        """Pre-load names of all members in bridged MAX chats into _name_cache.

        Called after connect — uses load_members() which goes through
        _send_and_wait (safe here because recv_loop handlers aren't active yet
        for message processing, and _send_and_wait uses seq-based futures).
        """
        max_chat_ids = set()
        for entry in self.config.bridges:
            max_chat_ids.add(entry.max_chat_id)

        for chat_id in max_chat_ids:
            try:
                members, _ = await self.client.inner.load_members(chat_id)
                for member in members:
                    c = member.contact
                    if c and c.id and c.id not in self._name_cache:
                        name = None
                        if c.names:
                            name = c.names[0].name or c.names[0].first_name or c.names[0].last_name
                        if name:
                            self._name_cache[c.id] = name
                log.info("Pre-loaded %d member names for MAX chat %s",
                         len(members) if members else 0, chat_id)
            except Exception as e:
                log.warning("Failed to preload members for chat %s: %s", chat_id, e)

    def _resolve_sender_name(self, sender_id: int) -> str:
        """Resolve display name for a MAX user ID — no network calls.

        Known bridge users are pre-populated from config.
        Other users are looked up in PyMax's internal cache (populated
        during _sync at login and updated as messages arrive).
        Falls back to 'User:<id>' only if no cached data exists.

        NOTE: This must be synchronous because it runs inside the PyMax
        raw_receive handler which is awaited by _recv_loop. Calling
        _send_and_wait (e.g. get_users) here would deadlock.
        """
        if sender_id in self._name_cache:
            return self._name_cache[sender_id]

        # Try PyMax's internal user cache (populated at login + on messages)
        name = self._try_pymax_cache(sender_id)
        if name:
            self._name_cache[sender_id] = name
            log.debug("Resolved MAX user %s → %s (from PyMax cache)", sender_id, name)
            return name

        # Schedule a background fetch for future messages (runs outside handler)
        asyncio.ensure_future(self._bg_resolve_name(sender_id))

        fallback = f"User:{sender_id}"
        self._name_cache[sender_id] = fallback
        return fallback

    def _try_pymax_cache(self, sender_id: int) -> str | None:
        """Try to extract name from PyMax's internal user/contact cache."""
        if not self.client:
            return None
        try:
            # PyMax caches users it has seen in self.inner._users
            u = self.client.inner.get_cached_user(sender_id)
            if u is not None:
                return self._extract_name(u)

            # Also check contacts loaded during _sync
            for contact in self.client.inner.contacts:
                if contact.id == sender_id:
                    return self._extract_name(contact)
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_name(u) -> str | None:
        """Extract display name from a PyMax User/Contact object."""
        if hasattr(u, 'names') and u.names:
            n = u.names[0]
            name = n.name or n.first_name or n.last_name
            if name:
                return name
        if hasattr(u, 'display_name') and u.display_name:
            return u.display_name
        if hasattr(u, 'first_name') and u.first_name:
            return u.first_name
        return None

    async def _bg_resolve_name(self, sender_id: int):
        """Background task: fetch real name via network, update cache.

        Runs as a separate task so it doesn't deadlock the recv loop.
        The current message uses fallback; future messages get the real name.
        """
        try:
            users = await asyncio.wait_for(
                self.client.get_users([sender_id]),
                timeout=5.0,
            )
            if users:
                name = self._extract_name(users[0])
                if name:
                    self._name_cache[sender_id] = name
                    log.debug("Resolved MAX user %s → %s (background)", sender_id, name)
        except Exception:
            pass

    async def _handle_packet(self, data: dict[str, Any]):
        try:
            opcode = data.get("opcode", 0)

            # PyMax uses Opcode enum values (ints) for opcodes
            if opcode == 128:  # NOTIF_MESSAGE
                await self._handle_message(data)
            elif opcode == 67:  # MSG_EDIT notification
                await self._handle_edited_message(data)
            elif opcode == 66:  # MSG_DELETE
                await self._handle_deleted_message(data)
            elif opcode == 142:  # NOTIF_MSG_DELETE
                await self._handle_deleted_message(data)
        except Exception as e:
            log.error("Error handling packet (opcode=%s): %s", data.get("opcode"), e, exc_info=True)

    async def _handle_message(self, packet: dict):
        payload = packet.get("payload", {})
        chat_id = payload.get("chatId")
        message = payload.get("message", {})
        msg_id = str(message.get("id", ""))
        # Sender ID lives in message.sender (opcode 128), NOT payload.fromUserId
        sender_id = message.get("sender") or payload.get("fromUserId")

        log.debug("MAX msg: chat=%s sender=%s msg_id=%r text=%r attaches=%s",
                  chat_id, sender_id, msg_id,
                  (message.get("text") or "")[:60],
                  [a.get("_type") for a in message.get("attaches", [])])

        if not chat_id or not sender_id:
            return

        # MirrorTracker: skip messages sent by the bridge via ANY user's
        # MAX account (MAX message IDs are global — same for all users).
        if msg_id and self.mirrors.is_max_mirror(msg_id):
            log.debug("MAX msg %s → is mirror, skipping", msg_id)
            return

        bridge_entry = self.lookup.get_primary_by_max(chat_id)
        if not bridge_entry:
            return

        sender_name = self._resolve_sender_name(sender_id)
        text = message.get("text")
        attaches = message.get("attaches", [])

        reply_to = None
        link = message.get("link")
        if link and link.get("type") == "REPLY":
            # Reply target ID can be in link.messageId or link.message.id
            reply_to = link.get("messageId")
            if not reply_to:
                link_msg = link.get("message")
                if isinstance(link_msg, dict):
                    reply_to = link_msg.get("id")
            if reply_to:
                reply_to = str(reply_to)
                log.debug("MAX reply_to: %s", reply_to)

        for att in attaches:
            att_type = att.get("_type", "")

            if att_type in ("PHOTO", "VIDEO", "FILE", "AUDIO"):
                media_url = att.get("url")
                # PHOTO attachments use baseUrl (full CDN link) instead of url
                if not media_url:
                    media_url = att.get("baseUrl")
                log.debug("MAX attachment: type=%s url=%s keys=%s", att_type, media_url, list(att.keys()))
                media = None
                if media_url:
                    try:
                        data = await download_media(media_url)
                        event_type_map = {
                            "PHOTO": ("photo", att.get("fileName", "photo.jpg"), "image/jpeg"),
                            "VIDEO": ("video", att.get("fileName", "video.mp4"), "video/mp4"),
                            "FILE": ("file", att.get("fileName", "file"), att.get("mimeType", "application/octet-stream")),
                            "AUDIO": ("audio", att.get("fileName", "audio.mp3"), "audio/mpeg"),
                        }
                        evt_type, fname, mime = event_type_map[att_type]
                        media = MediaInfo(data=data, filename=fname, mime_type=mime)
                    except Exception as e:
                        log.error("Failed to download %s from %s: %s", att_type, media_url, e, exc_info=True)
                        evt_type = "text"

                if media:
                    await self.on_event(BridgeEvent(
                        direction="max-to-tg",
                        bridge_entry=bridge_entry,
                        sender_display_name=sender_name,
                        sender_user_id=sender_id,
                        event_type=evt_type,
                        text=text,
                        media=media,
                        reply_to_source_msg_id=reply_to,
                        source_msg_id=msg_id,
                    ))
                else:
                    label = att_type.capitalize()
                    await self.on_event(BridgeEvent(
                        direction="max-to-tg",
                        bridge_entry=bridge_entry,
                        sender_display_name=sender_name,
                        sender_user_id=sender_id,
                        event_type="text",
                        text=f"{text or ''}\n[{label} — media download failed]".strip(),
                        reply_to_source_msg_id=reply_to,
                        source_msg_id=msg_id,
                    ))
                return

            if att_type == "STICKER":
                await self.on_event(BridgeEvent(
                    direction="max-to-tg",
                    bridge_entry=bridge_entry,
                    sender_display_name=sender_name,
                    sender_user_id=sender_id,
                    event_type="sticker",
                    text="[Sticker]",
                    reply_to_source_msg_id=reply_to,
                    source_msg_id=msg_id,
                ))
                return

        if text:
            await self.on_event(BridgeEvent(
                direction="max-to-tg",
                bridge_entry=bridge_entry,
                sender_display_name=sender_name,
                sender_user_id=sender_id,
                event_type="text",
                text=text,
                reply_to_source_msg_id=reply_to,
                source_msg_id=msg_id,
            ))

    async def _handle_edited_message(self, packet: dict):
        payload = packet.get("payload", {})
        chat_id = payload.get("chatId")
        sender_id = payload.get("fromUserId")
        msg_id = str(payload.get("messageId", ""))
        text = payload.get("text")

        if not chat_id or not msg_id:
            return

        if self.mirrors.is_max_mirror(msg_id):
            return

        bridge_entry = self.lookup.get_primary_by_max(chat_id)
        if not bridge_entry:
            return

        sender_name = payload.get("senderName", "Unknown")

        await self.on_event(BridgeEvent(
            direction="max-to-tg",
            bridge_entry=bridge_entry,
            sender_display_name=sender_name,
            sender_user_id=sender_id,
            event_type="edit",
            text=text,
            edit_source_msg_id=msg_id,
            source_msg_id=msg_id,
        ))

    async def _handle_deleted_message(self, packet: dict):
        """Handle MSG_DELETE (opcode 66) and NOTIF_MSG_DELETE (opcode 142)."""
        payload = packet.get("payload", {})
        chat_id = payload.get("chatId")
        message_ids = payload.get("messageIds", [])
        if not chat_id or not message_ids:
            return

        bridge_entry = self.lookup.get_primary_by_max(chat_id)
        if not bridge_entry:
            return

        for mid in message_ids:
            await self.on_event(BridgeEvent(
                direction="max-to-tg",
                bridge_entry=bridge_entry,
                sender_display_name="Unknown",
                event_type="delete",
                delete_source_msg_id=str(mid),
                source_msg_id=str(mid),
            ))
