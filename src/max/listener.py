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

    async def _resolve_sender_name(self, sender_id: int) -> str:
        """Resolve display name for a MAX user ID.

        Known bridge users are pre-populated from config.
        Unknown users are resolved via get_users() API call (with timeout).
        Falls back to 'User:<id>' only if the API call fails.
        """
        if sender_id in self._name_cache:
            return self._name_cache[sender_id]
        # Try to resolve the real name before delivering the message.
        try:
            users = await asyncio.wait_for(
                self.client.get_users([sender_id]),
                timeout=5.0,
            )
            if users:
                u = users[0]
                name = None
                if hasattr(u, 'names') and u.names:
                    name = u.names[0].first_name or u.names[0].last_name
                if not name and hasattr(u, 'display_name'):
                    name = u.display_name
                if not name and hasattr(u, 'first_name'):
                    name = u.first_name
                if name:
                    self._name_cache[sender_id] = name
                    log.debug("Resolved MAX user %s → %s", sender_id, name)
                    return name
        except Exception:
            log.debug("Failed to resolve MAX user %s, using fallback", sender_id)
        # Fallback — cache it so we don't retry on every message.
        fallback = f"User:{sender_id}"
        self._name_cache[sender_id] = fallback
        return fallback

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

        sender_name = await self._resolve_sender_name(sender_id)
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
