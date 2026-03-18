import asyncio
import logging
from typing import Callable, Awaitable

from vkmax.client import MaxClient
from .patched_client import PatchedMaxClient

from ..bridge.mirror_tracker import MirrorTracker
from ..config import ConfigLookup
from ..types import AppConfig, BridgeEvent, MediaInfo, UserMapping
from .session import MaxSession
from .media import download_media

log = logging.getLogger("bridge.max.listener")

RECONNECT_BASE_DELAY = 2
RECONNECT_MAX_DELAY = 60


class MaxListener:
    """Listens for messages in MAX using a user account via vkmax WebSocket."""

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
        self.client: MaxClient | None = None
        self._my_user_id: int = user.max_user_id
        self._login_token: str | None = None
        self._stopped = False
        self._monitor_task: asyncio.Task | None = None

    async def start(self) -> int:
        session = MaxSession(self.user.max_session, self.config.sessions_dir)
        if not session.exists():
            raise RuntimeError(
                f"MAX session not found ({self.user.max_session}) for user {self.user.name}. "
                f"Run 'python -m src.auth' first."
            )

        self._login_token = session.load()
        await self._connect()

        self._monitor_task = asyncio.create_task(self._reconnect_loop())

        log.info("Started for %s (User ID: %d)", self.user.name, self._my_user_id)
        return self._my_user_id

    async def _connect(self):
        self.client = PatchedMaxClient()
        await self.client.connect()
        login_response = await self.client.login_by_token(self._login_token)

        await self.client.set_callback(self._handle_packet)

    async def _reconnect_loop(self):
        delay = RECONNECT_BASE_DELAY
        while not self._stopped:
            try:
                if self.client and self.client._recv_task:
                    await self.client._recv_task
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

    async def _handle_packet(self, client: MaxClient, packet: dict):
        try:
            opcode = packet.get("opcode", 0)

            if opcode == 128:
                await self._handle_message(packet)
            elif opcode == 67:
                await self._handle_edited_message(packet)
            elif opcode == 66:
                await self._handle_deleted_message(packet)
        except Exception as e:
            log.error("Error handling packet (opcode=%s): %s", packet.get("opcode"), e)

    async def _handle_message(self, packet: dict):
        payload = packet.get("payload", {})
        chat_id = payload.get("chatId")
        sender_id = payload.get("fromUserId")
        message = payload.get("message", {})
        msg_id = str(message.get("id", ""))

        log.debug("MAX msg: chat=%s sender=%s msg_id=%r text=%r attaches=%s",
                  chat_id, sender_id, msg_id,
                  (message.get("text") or "")[:60],
                  [a.get("_type") for a in message.get("attaches", [])])

        # sender_id is None when the MAX server delivers our own account's
        # outgoing messages back to us — skip to prevent echo loops.
        if not chat_id or not sender_id:
            return

        bridge_entry = self.lookup.get_bridge_by_max(chat_id, self._my_user_id)
        if not bridge_entry:
            return

        sender_name = payload.get("senderName", "Unknown")
        text = message.get("text")
        attaches = message.get("attaches", [])

        reply_to = None
        link = message.get("link")
        if link and link.get("type") == "REPLY":
            reply_to = link.get("messageId")

        for att in attaches:
            att_type = att.get("_type", "")

            if att_type in ("PHOTO", "VIDEO", "FILE", "AUDIO"):
                media_url = att.get("url")
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
                        log.error("Failed to download %s: %s", att_type, e)
                        evt_type = "text"

                if media:
                    await self.on_event(BridgeEvent(
                        direction="max-to-tg",
                        bridge_entry=bridge_entry,
                        sender_display_name=sender_name,
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

        bridge_entry = self.lookup.get_bridge_by_max(chat_id, self._my_user_id)
        if not bridge_entry:
            return

        sender_name = payload.get("senderName", "Unknown")

        await self.on_event(BridgeEvent(
            direction="max-to-tg",
            bridge_entry=bridge_entry,
            sender_display_name=sender_name,
            event_type="edit",
            text=text,
            edit_source_msg_id=msg_id,
            source_msg_id=msg_id,
        ))

    async def _handle_deleted_message(self, packet: dict):
        payload = packet.get("payload", {})
        chat_id = payload.get("chatId")
        message_ids = payload.get("messageIds", [])

        if not chat_id:
            return

        bridge_entry = self.lookup.get_bridge_by_max(chat_id, self._my_user_id)
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
