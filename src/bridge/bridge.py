import io
import logging

from ..config import ConfigLookup
from ..message_store import MessageStore
from ..telegram.client_pool import TelegramClientPool
from ..max.client_pool import MaxClientPool
from ..types import BridgeEvent
from .formatting import prepend_sender_name
from .mirror_tracker import MirrorTracker

log = logging.getLogger("bridge.core")


class Bridge:
    def __init__(
        self,
        lookup: ConfigLookup,
        message_store: MessageStore,
        tg_pool: TelegramClientPool,
        max_pool: MaxClientPool,
        mirror_tracker: MirrorTracker,
    ):
        self.lookup = lookup
        self.store = message_store
        self.tg_pool = tg_pool
        self.max_pool = max_pool
        self.mirrors = mirror_tracker

    async def handle_event(self, event: BridgeEvent):
        try:
            if event.direction == "tg-to-max":
                await self._tg_to_max(event)
            else:
                await self._max_to_tg(event)
        except Exception as e:
            log.error("Error %s %s: %s", event.direction, event.event_type, e)

    async def _tg_to_max(self, event: BridgeEvent):
        pair = event.chat_pair
        max_chat_id = pair.max_chat_id
        max_user_id = event.user.max_user_id if event.user else None

        text = event.text or ""
        if not event.user and text:
            text = prepend_sender_name(event.sender_display_name, text)

        # Resolve reply target
        reply_to = None
        if event.reply_to_source_msg_id is not None:
            reply_to = self.store.get_max_msg_id(pair.name, int(event.reply_to_source_msg_id))

        if event.event_type == "text":
            max_msg_id = await self.max_pool.send_text(max_user_id, max_chat_id, text, reply_to)
            if max_msg_id and event.source_msg_id is not None:
                self.store.store(pair.name, int(event.source_msg_id), max_msg_id)
                self.mirrors.mark_max(max_msg_id)

        elif event.event_type == "photo" and event.media:
            max_msg_id = await self.max_pool.send_photo(
                max_user_id, max_chat_id, event.media.data,
                event.media.filename, text, reply_to,
            )
            if max_msg_id and event.source_msg_id is not None:
                self.store.store(pair.name, int(event.source_msg_id), max_msg_id)
                self.mirrors.mark_max(max_msg_id)

        elif event.event_type in ("video", "file", "audio") and event.media:
            max_msg_id = await self.max_pool.send_file(
                max_user_id, max_chat_id, event.media.data,
                event.media.filename, event.media.mime_type, text, reply_to,
            )
            if max_msg_id and event.source_msg_id is not None:
                self.store.store(pair.name, int(event.source_msg_id), max_msg_id)
                self.mirrors.mark_max(max_msg_id)

        elif event.event_type in ("photo", "video", "file", "audio"):
            fallback_text = f"{text}\n[{event.event_type.capitalize()} — media download failed]".strip()
            if not event.user:
                fallback_text = prepend_sender_name(event.sender_display_name, fallback_text)
            max_msg_id = await self.max_pool.send_text(max_user_id, max_chat_id, fallback_text, reply_to)
            if max_msg_id and event.source_msg_id is not None:
                self.store.store(pair.name, int(event.source_msg_id), max_msg_id)
                self.mirrors.mark_max(max_msg_id)

        elif event.event_type == "sticker":
            sticker_text = text or "[Sticker]"
            if not event.user:
                sticker_text = prepend_sender_name(event.sender_display_name, sticker_text)
            max_msg_id = await self.max_pool.send_text(max_user_id, max_chat_id, sticker_text, reply_to)
            if max_msg_id and event.source_msg_id is not None:
                self.store.store(pair.name, int(event.source_msg_id), max_msg_id)
                self.mirrors.mark_max(max_msg_id)

        elif event.event_type == "edit":
            if event.edit_source_msg_id is not None:
                max_msg_id = self.store.get_max_msg_id(pair.name, int(event.edit_source_msg_id))
                if max_msg_id:
                    edit_text = text
                    if not event.user:
                        edit_text = prepend_sender_name(event.sender_display_name, text)
                    await self.max_pool.edit_text(max_user_id, max_chat_id, max_msg_id, edit_text)

        elif event.event_type == "delete":
            if event.delete_source_msg_id is not None:
                max_msg_id = self.store.get_max_msg_id(pair.name, int(event.delete_source_msg_id))
                if max_msg_id:
                    await self.max_pool.delete_msg(max_user_id, max_chat_id, max_msg_id)

    async def _max_to_tg(self, event: BridgeEvent):
        pair = event.chat_pair
        tg_chat_id = pair.telegram_chat_id
        tg_user_id = event.user.telegram_user_id if event.user else None

        text = event.text or ""
        if not event.user and text:
            text = prepend_sender_name(event.sender_display_name, text)

        # Get the appropriate Pyrogram client
        client = None
        if tg_user_id:
            client = self.tg_pool.get_client(tg_user_id)
        if not client:
            client = self.tg_pool.get_any_client()
            # If using fallback client for a mapped user, add prefix
            if event.user and text == (event.text or ""):
                text = prepend_sender_name(event.sender_display_name, text)

        if not client:
            log.warning("No TG client available for user %s", tg_user_id)
            return

        # Resolve reply target
        reply_to = None
        if event.reply_to_source_msg_id is not None:
            reply_to = self.store.get_tg_msg_id(pair.name, str(event.reply_to_source_msg_id))

        if event.event_type == "text":
            msg = await client.send_message(
                tg_chat_id, text,
                reply_to_message_id=reply_to,
            )
            if event.source_msg_id is not None:
                self.store.store(pair.name, msg.id, str(event.source_msg_id))
            self.mirrors.mark_tg(msg.id)

        elif event.event_type == "photo" and event.media:
            caption = text if text else None
            buf = io.BytesIO(event.media.data)
            buf.name = event.media.filename
            msg = await client.send_photo(
                tg_chat_id, buf, caption=caption,
                reply_to_message_id=reply_to,
            )
            if event.source_msg_id is not None:
                self.store.store(pair.name, msg.id, str(event.source_msg_id))
            self.mirrors.mark_tg(msg.id)

        elif event.event_type in ("video", "audio", "file") and event.media:
            caption = text if text else None
            buf = io.BytesIO(event.media.data)
            buf.name = event.media.filename
            send_fn = {
                "video": client.send_video,
                "audio": client.send_audio,
                "file": client.send_document,
            }[event.event_type]
            msg = await send_fn(
                tg_chat_id, buf, caption=caption,
                reply_to_message_id=reply_to,
            )
            if event.source_msg_id is not None:
                self.store.store(pair.name, msg.id, str(event.source_msg_id))
            self.mirrors.mark_tg(msg.id)

        elif event.event_type in ("photo", "video", "audio", "file"):
            fallback = f"{text}\n[{event.event_type.capitalize()} — media unavailable]".strip()
            msg = await client.send_message(tg_chat_id, fallback, reply_to_message_id=reply_to)
            if event.source_msg_id is not None:
                self.store.store(pair.name, msg.id, str(event.source_msg_id))
            self.mirrors.mark_tg(msg.id)

        elif event.event_type == "sticker":
            sticker_text = text or "[Sticker]"
            msg = await client.send_message(tg_chat_id, sticker_text, reply_to_message_id=reply_to)
            if event.source_msg_id is not None:
                self.store.store(pair.name, msg.id, str(event.source_msg_id))
            self.mirrors.mark_tg(msg.id)

        elif event.event_type == "edit":
            if event.edit_source_msg_id is not None:
                tg_msg_id = self.store.get_tg_msg_id(pair.name, str(event.edit_source_msg_id))
                if tg_msg_id:
                    edit_text = text
                    if not event.user:
                        edit_text = prepend_sender_name(event.sender_display_name, event.text or "")
                    await client.edit_message_text(tg_chat_id, tg_msg_id, edit_text)

        elif event.event_type == "delete":
            if event.delete_source_msg_id is not None:
                tg_msg_id = self.store.get_tg_msg_id(pair.name, str(event.delete_source_msg_id))
                if tg_msg_id:
                    await client.delete_messages(tg_chat_id, tg_msg_id)
