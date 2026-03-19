import logging
from typing import Callable, Awaitable

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.handlers import MessageHandler, EditedMessageHandler, DeletedMessagesHandler

from ..bridge.formatting import MIRROR_MARKER
from ..bridge.mirror_tracker import MirrorTracker
from ..config import ConfigLookup
from ..types import AppConfig, BridgeEvent, MediaInfo, UserMapping

log = logging.getLogger("bridge.tg.listener")


class TelegramListener:
    """Listens for messages in Telegram using a user account.

    Reuses an already-started Pyrogram *client* from the pool — the pool
    manages its lifecycle, so start/stop here are no-ops for the client.
    """

    def __init__(
        self,
        config: AppConfig,
        lookup: ConfigLookup,
        mirror_tracker: MirrorTracker,
        on_event: Callable[[BridgeEvent], Awaitable[None]],
        client: Client,
        user: UserMapping,
    ):
        self.config = config
        self.lookup = lookup
        self.mirrors = mirror_tracker
        self.on_event = on_event
        self.client = client
        self.user = user

    async def start(self) -> int:
        chat_ids = self.lookup.get_tg_chat_ids_for_user(self.user.telegram_user_id)
        chat_filter = filters.chat(chat_ids)

        self.client.add_handler(MessageHandler(self._handle_message, chat_filter))
        self.client.add_handler(EditedMessageHandler(self._handle_edited_message, chat_filter))
        self.client.add_handler(DeletedMessagesHandler(self._handle_deleted_messages, chat_filter))

        me = await self.client.get_me()
        log.info("Started for %s as @%s (ID: %d)", self.user.name, me.username, me.id)
        return me.id

    async def stop(self):
        pass  # client lifecycle is owned by TelegramClientPool

    async def _handle_message(self, client: Client, message: Message):
        try:
            # MirrorTracker check (works reliably for supergroups)
            if self.mirrors.is_tg_mirror(message.id):
                log.debug("TG msg %s → is mirror (tracker), skipping", message.id)
                return

            # MIRROR_MARKER check (works for regular groups where msg IDs
            # differ per user — the marker is embedded in the text itself)
            msg_text = message.text or message.caption or ""
            if msg_text.startswith(MIRROR_MARKER):
                log.debug("TG msg %s → has mirror marker, skipping", message.id)
                return

            bridge_entry = self.lookup.get_primary_by_tg(message.chat.id)
            if not bridge_entry:
                return

            sender_name = self._get_sender_name(message)
            sender_id = message.from_user.id if message.from_user else None

            reply_to = None
            if message.reply_to_message:
                reply_to = message.reply_to_message.id

            if message.photo:
                media = await self._download_media(message)
                await self.on_event(BridgeEvent(
                    direction="tg-to-max",
                    bridge_entry=bridge_entry,
                    sender_display_name=sender_name,
                    sender_user_id=sender_id,
                    event_type="photo",
                    text=message.caption,
                    media=media,
                    reply_to_source_msg_id=reply_to,
                    source_msg_id=message.id,
                ))
            elif message.video:
                media = await self._download_media(message)
                await self.on_event(BridgeEvent(
                    direction="tg-to-max",
                    bridge_entry=bridge_entry,
                    sender_display_name=sender_name,
                    sender_user_id=sender_id,
                    event_type="video",
                    text=message.caption,
                    media=media,
                    reply_to_source_msg_id=reply_to,
                    source_msg_id=message.id,
                ))
            elif message.document:
                media = await self._download_media(message)
                if media:
                    media.filename = message.document.file_name or media.filename
                await self.on_event(BridgeEvent(
                    direction="tg-to-max",
                    bridge_entry=bridge_entry,
                    sender_display_name=sender_name,
                    sender_user_id=sender_id,
                    event_type="file",
                    text=message.caption,
                    media=media,
                    reply_to_source_msg_id=reply_to,
                    source_msg_id=message.id,
                ))
            elif message.audio or message.voice:
                media = await self._download_media(message)
                await self.on_event(BridgeEvent(
                    direction="tg-to-max",
                    bridge_entry=bridge_entry,
                    sender_display_name=sender_name,
                    sender_user_id=sender_id,
                    event_type="audio",
                    text=message.caption,
                    media=media,
                    reply_to_source_msg_id=reply_to,
                    source_msg_id=message.id,
                ))
            elif message.sticker:
                await self.on_event(BridgeEvent(
                    direction="tg-to-max",
                    bridge_entry=bridge_entry,
                    sender_display_name=sender_name,
                    sender_user_id=sender_id,
                    event_type="sticker",
                    text=f"[Sticker: {message.sticker.emoji or ''}]",
                    reply_to_source_msg_id=reply_to,
                    source_msg_id=message.id,
                ))
            elif message.text:
                await self.on_event(BridgeEvent(
                    direction="tg-to-max",
                    bridge_entry=bridge_entry,
                    sender_display_name=sender_name,
                    sender_user_id=sender_id,
                    event_type="text",
                    text=message.text,
                    reply_to_source_msg_id=reply_to,
                    source_msg_id=message.id,
                ))
        except Exception as e:
            log.error("Error handling message: %s", e)

    async def _handle_edited_message(self, client: Client, message: Message):
        try:
            if self.mirrors.is_tg_mirror(message.id):
                return

            msg_text = message.text or message.caption or ""
            if msg_text.startswith(MIRROR_MARKER):
                return

            bridge_entry = self.lookup.get_primary_by_tg(message.chat.id)
            if not bridge_entry:
                return

            sender_name = self._get_sender_name(message)
            sender_id = message.from_user.id if message.from_user else None

            await self.on_event(BridgeEvent(
                direction="tg-to-max",
                bridge_entry=bridge_entry,
                sender_display_name=sender_name,
                sender_user_id=sender_id,
                event_type="edit",
                text=message.text or message.caption,
                edit_source_msg_id=message.id,
                source_msg_id=message.id,
            ))
        except Exception as e:
            log.error("Error handling edited message: %s", e)

    async def _handle_deleted_messages(self, client: Client, messages: list[Message]):
        try:
            for message in messages:
                if not message.chat:
                    continue
                bridge_entry = self.lookup.get_primary_by_tg(message.chat.id)
                if not bridge_entry:
                    continue

                await self.on_event(BridgeEvent(
                    direction="tg-to-max",
                    bridge_entry=bridge_entry,
                    sender_display_name="Unknown",
                    event_type="delete",
                    delete_source_msg_id=message.id,
                    source_msg_id=message.id,
                ))
        except Exception as e:
            log.error("Error handling deleted messages: %s", e)

    def _get_sender_name(self, message: Message) -> str:
        if message.from_user:
            parts = [message.from_user.first_name]
            if message.from_user.last_name:
                parts.append(message.from_user.last_name)
            return " ".join(parts)
        return "Unknown"

    async def _download_media(self, message: Message) -> MediaInfo | None:
        try:
            # download_media with in_memory=True returns a BytesIO object directly.
            result = await self.client.download_media(message, in_memory=True)
            if not result:
                return None
            data = result.getvalue()
            if not data:
                return None

            filename = "file"
            mime_type = "application/octet-stream"

            if message.photo:
                filename = f"photo_{message.photo.file_unique_id}.jpg"
                mime_type = "image/jpeg"
            elif message.video:
                filename = message.video.file_name or f"video_{message.video.file_unique_id}.mp4"
                mime_type = message.video.mime_type or "video/mp4"
            elif message.document:
                filename = message.document.file_name or "document"
                mime_type = message.document.mime_type or "application/octet-stream"
            elif message.audio:
                filename = message.audio.file_name or "audio.mp3"
                mime_type = message.audio.mime_type or "audio/mpeg"
            elif message.voice:
                filename = "voice.ogg"
                mime_type = "audio/ogg"

            return MediaInfo(data=data, filename=filename, mime_type=mime_type)
        except Exception as e:
            log.error("Failed to download media: %s", e)
            return None
