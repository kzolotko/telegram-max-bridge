import logging
from typing import Callable, Awaitable

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.handlers import MessageHandler, EditedMessageHandler, DeletedMessagesHandler

from ..bridge.formatting import MIRROR_MARKER, tg_entities_to_internal
from ..bridge.mirror_tracker import MirrorTracker
from ..config import ConfigLookup
from ..types import AppConfig, BridgeEvent, MediaInfo, UserMapping

# Delay (seconds) to wait for more album messages after the last received one.
_ALBUM_FLUSH_DELAY = 0.8

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
        # Cache: tg_msg_id → chat_id, for delete events in regular groups
        # where Pyrogram does not include chat info in the callback.
        self._msg_chat_cache: dict[int, int] = {}
        self._MSG_CACHE_MAX = 10_000
        # Album (media group) buffering: group_id → buffered state
        self._album_buffer: dict[str, dict] = {}
        self._album_tasks: dict[str, asyncio.Task] = {}

    async def start(self) -> int:
        chat_ids = self.lookup.get_tg_chat_ids_for_user(self.user.telegram_user_id)
        chat_filter = filters.chat(chat_ids)

        self.client.add_handler(MessageHandler(self._handle_message, chat_filter))
        self.client.add_handler(EditedMessageHandler(self._handle_edited_message, chat_filter))
        # DeletedMessagesHandler registered WITHOUT chat filter: in regular
        # (non-super) groups Pyrogram does not attach chat info to deleted
        # messages, so filters.chat() would never match and the handler
        # would never fire. We filter manually inside using _msg_chat_cache.
        self.client.add_handler(DeletedMessagesHandler(self._handle_deleted_messages))

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

            # Album (media group): buffer and flush after a short delay
            if message.media_group_id:
                await self._buffer_album(message, bridge_entry, sender_name, sender_id, reply_to)
                self._cache_msg_chat(message.id, message.chat.id)
                return

            if message.photo:
                media = await self._download_media(message)
                cap_fmt = tg_entities_to_internal(
                    message.caption or "", message.caption_entities,
                )
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
                    formatting=cap_fmt or None,
                ))
            elif message.video:
                media = await self._download_media(message)
                cap_fmt = tg_entities_to_internal(
                    message.caption or "", message.caption_entities,
                )
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
                    formatting=cap_fmt or None,
                ))
            elif message.document:
                media = await self._download_media(message)
                if media:
                    media.filename = message.document.file_name or media.filename
                cap_fmt = tg_entities_to_internal(
                    message.caption or "", message.caption_entities,
                )
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
                    formatting=cap_fmt or None,
                ))
            elif message.audio or message.voice:
                media = await self._download_media(message)
                cap_fmt = tg_entities_to_internal(
                    message.caption or "", message.caption_entities,
                )
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
                    formatting=cap_fmt or None,
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
                text_fmt = tg_entities_to_internal(message.text, message.entities)
                await self.on_event(BridgeEvent(
                    direction="tg-to-max",
                    bridge_entry=bridge_entry,
                    sender_display_name=sender_name,
                    sender_user_id=sender_id,
                    event_type="text",
                    text=message.text,
                    reply_to_source_msg_id=reply_to,
                    source_msg_id=message.id,
                    formatting=text_fmt or None,
                ))
            # Cache msg_id → chat_id for delete lookup in regular groups
            self._cache_msg_chat(message.id, message.chat.id)
        except Exception as e:
            log.error("Error handling message: %s", e)

    def _cache_msg_chat(self, msg_id: int, chat_id: int):
        """Store msg_id → chat_id mapping for delete resolution in regular groups."""
        self._msg_chat_cache[msg_id] = chat_id
        if len(self._msg_chat_cache) > self._MSG_CACHE_MAX:
            # Evict oldest 10% of entries (dict preserves insertion order)
            evict = self._MSG_CACHE_MAX // 10
            for key in list(self._msg_chat_cache.keys())[:evict]:
                del self._msg_chat_cache[key]

    # ── Album buffering ───────────────────────────────────────────────────────

    async def _buffer_album(
        self,
        message: Message,
        bridge_entry,
        sender_name: str,
        sender_id: int | None,
        reply_to: int | None,
    ):
        """Buffer an album message and schedule a flush after _ALBUM_FLUSH_DELAY."""
        group_id = message.media_group_id
        if group_id not in self._album_buffer:
            self._album_buffer[group_id] = {
                "messages": [],
                "bridge_entry": bridge_entry,
                "sender_name": sender_name,
                "sender_id": sender_id,
                "reply_to": reply_to,
            }
        self._album_buffer[group_id]["messages"].append(message)

        # Cancel previous flush task and reschedule so the timer resets
        # each time a new album message arrives.
        old_task = self._album_tasks.get(group_id)
        if old_task and not old_task.done():
            old_task.cancel()
        self._album_tasks[group_id] = asyncio.ensure_future(
            self._flush_album(group_id)
        )

    async def _flush_album(self, group_id: str):
        """Wait _ALBUM_FLUSH_DELAY, then emit a single media_group BridgeEvent."""
        try:
            await asyncio.sleep(_ALBUM_FLUSH_DELAY)
        except asyncio.CancelledError:
            return  # another message arrived; rescheduled above

        data = self._album_buffer.pop(group_id, None)
        self._album_tasks.pop(group_id, None)
        if not data:
            return

        messages = sorted(data["messages"], key=lambda m: m.id)
        bridge_entry = data["bridge_entry"]
        sender_name  = data["sender_name"]
        sender_id    = data["sender_id"]
        reply_to     = data["reply_to"]

        # Download all media and pick caption from whichever message has it
        media_list: list[MediaInfo] = []
        caption: str | None = None
        caption_entities = None

        for msg in messages:
            if caption is None and (msg.caption or msg.text):
                caption = msg.caption or msg.text
                caption_entities = msg.caption_entities or msg.entities

            media = await self._download_media(msg)
            if media:
                media_list.append(media)

        if not media_list:
            log.warning("Album %s: all downloads failed, skipping", group_id)
            return

        fmt = tg_entities_to_internal(caption or "", caption_entities)

        await self.on_event(BridgeEvent(
            direction="tg-to-max",
            bridge_entry=bridge_entry,
            sender_display_name=sender_name,
            sender_user_id=sender_id,
            event_type="media_group",
            text=caption,
            media_list=media_list,
            reply_to_source_msg_id=reply_to,
            source_msg_id=messages[0].id,  # first message ID for store mapping
            formatting=fmt or None,
        ))
        log.debug("Flushed album %s: %d media items", group_id, len(media_list))

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

            edit_text = message.text or message.caption or ""
            edit_entities = message.entities if message.text else message.caption_entities
            edit_fmt = tg_entities_to_internal(edit_text, edit_entities)

            await self.on_event(BridgeEvent(
                direction="tg-to-max",
                bridge_entry=bridge_entry,
                sender_display_name=sender_name,
                sender_user_id=sender_id,
                event_type="edit",
                text=edit_text or None,
                edit_source_msg_id=message.id,
                source_msg_id=message.id,
                formatting=edit_fmt or None,
            ))
        except Exception as e:
            log.error("Error handling edited message: %s", e)

    async def _handle_deleted_messages(self, client: Client, messages: list[Message]):
        try:
            for message in messages:
                # Supergroups provide chat in the event; regular groups don't.
                # Fall back to the local msg→chat cache populated on new messages.
                chat_id = message.chat.id if message.chat else \
                          self._msg_chat_cache.get(message.id)
                if not chat_id:
                    log.debug("TG delete: no chat_id for msg %s — not in cache, skipping", message.id)
                    continue
                bridge_entry = self.lookup.get_primary_by_tg(chat_id)
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
