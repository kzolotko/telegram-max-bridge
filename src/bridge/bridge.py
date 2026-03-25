import io
import logging

from pyrogram import enums as tg_enums

from ..config import ConfigLookup
from ..message_store import MessageStore
from ..telegram.client_pool import TelegramClientPool
from ..max.client_pool import MaxClientPool
from ..types import BridgeEvent
from .formatting import (
    MIRROR_MARKER,
    prepend_sender_name,
    prepend_sender_name_fmt,
    internal_to_max_elements,
    internal_to_tg_html,
)
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
            log.error("Error %s %s: %s", event.direction, event.event_type, e,
                      exc_info=True)

    # ── TG → MAX ─────────────────────────────────────────────────────────────

    async def _tg_to_max(self, event: BridgeEvent):
        entry = event.bridge_entry  # primary entry (from listener)
        tg_chat_id = entry.telegram_chat_id

        # Try to route via the sender's own MAX account (authorship match).
        sender_entry = None
        if event.sender_user_id is not None:
            sender_entry = self.lookup.get_bridge_by_tg(
                entry.telegram_chat_id, event.sender_user_id
            )

        fmt = event.formatting

        if sender_entry:
            # Sender has a bridge account — use their MAX account, no prefix.
            max_user_id = sender_entry.user.max_user_id
            text = event.text or ""
            log.debug("tg→max (sender=%s) type=%s text=%r",
                      sender_entry.user.name, event.event_type, text[:50])
        else:
            # Unknown sender — use primary's MAX account with [Name]: prefix.
            max_user_id = entry.user.max_user_id
            text, fmt = prepend_sender_name_fmt(
                event.sender_display_name, event.text or "", fmt,
            )
            log.debug("tg→max (primary=%s) type=%s text=%r",
                      entry.user.name, event.event_type, text[:50])

        max_chat_id = entry.max_chat_id
        max_elements = internal_to_max_elements(fmt) or None

        # Resolve reply target
        reply_to = None
        if event.reply_to_source_msg_id is not None:
            reply_to = self.store.get_max_msg_id(
                tg_chat_id=tg_chat_id,
                tg_msg_id=int(event.reply_to_source_msg_id),
                max_chat_id=max_chat_id,
            )

        if event.event_type == "media_group" and event.media_list:
            max_msg_id = await self.max_pool.send_media_multi(
                max_user_id, max_chat_id, event.media_list,
                text, reply_to, elements=max_elements,
            )
            if max_msg_id and event.source_msg_id is not None:
                tg_msg_ids: list[int] | None = None
                if event.source_msg_ids:
                    tg_msg_ids = [int(mid) for mid in event.source_msg_ids]
                self.store.store(
                    tg_chat_id=tg_chat_id,
                    tg_msg_id=int(event.source_msg_id),
                    max_chat_id=max_chat_id,
                    max_msg_id=max_msg_id,
                    tg_msg_ids=tg_msg_ids,
                )
                self.mirrors.mark_max(max_msg_id)

        elif event.event_type == "text":
            max_msg_id = await self.max_pool.send_text(
                max_user_id, max_chat_id, text, reply_to, elements=max_elements,
            )
            if max_msg_id and event.source_msg_id is not None:
                self.store.store(tg_chat_id, int(event.source_msg_id), max_chat_id, max_msg_id)
                self.mirrors.mark_max(max_msg_id)

        elif event.event_type == "photo" and event.media:
            max_msg_id = await self.max_pool.send_photo(
                max_user_id, max_chat_id, event.media.data,
                event.media.filename, text, reply_to,
            )
            if max_msg_id and event.source_msg_id is not None:
                self.store.store(tg_chat_id, int(event.source_msg_id), max_chat_id, max_msg_id)
                self.mirrors.mark_max(max_msg_id)

        elif event.event_type in ("video", "file", "audio") and event.media:
            max_msg_id = await self.max_pool.send_file(
                max_user_id, max_chat_id, event.media.data,
                event.media.filename, event.media.mime_type, text, reply_to,
            )
            if max_msg_id and event.source_msg_id is not None:
                self.store.store(tg_chat_id, int(event.source_msg_id), max_chat_id, max_msg_id)
                self.mirrors.mark_max(max_msg_id)

        elif event.event_type in ("photo", "video", "file", "audio"):
            fallback_text = f"{text}\n[{event.event_type.capitalize()} — media download failed]".strip()
            max_msg_id = await self.max_pool.send_text(max_user_id, max_chat_id, fallback_text, reply_to)
            if max_msg_id and event.source_msg_id is not None:
                self.store.store(tg_chat_id, int(event.source_msg_id), max_chat_id, max_msg_id)
                self.mirrors.mark_max(max_msg_id)

        elif event.event_type == "sticker":
            sticker_text = text or "[Sticker]"
            max_msg_id = await self.max_pool.send_text(max_user_id, max_chat_id, sticker_text, reply_to)
            if max_msg_id and event.source_msg_id is not None:
                self.store.store(tg_chat_id, int(event.source_msg_id), max_chat_id, max_msg_id)
                self.mirrors.mark_max(max_msg_id)

        elif event.event_type == "edit":
            if event.edit_source_msg_id is not None:
                max_msg_id = self.store.get_max_msg_id(
                    tg_chat_id=tg_chat_id,
                    tg_msg_id=int(event.edit_source_msg_id),
                    max_chat_id=max_chat_id,
                )
                if max_msg_id:
                    edit_text = event.text or ""
                    edit_fmt = fmt
                    if not sender_entry:
                        edit_text, edit_fmt = prepend_sender_name_fmt(
                            event.sender_display_name, edit_text, edit_fmt,
                        )
                    edit_elements = internal_to_max_elements(edit_fmt) or None
                    await self.max_pool.edit_text(
                        max_user_id, max_chat_id, max_msg_id, edit_text,
                        elements=edit_elements,
                    )

        elif event.event_type == "reaction":
            if event.source_msg_id is not None:
                max_msg_id = self.store.get_max_msg_id(
                    tg_chat_id=tg_chat_id,
                    tg_msg_id=int(event.source_msg_id),
                    max_chat_id=max_chat_id,
                )
                log.info("tg→max reaction: tg_msg=%s → max_msg=%s emoji=%r",
                         event.source_msg_id, max_msg_id, event.reaction_emoji)
                if max_msg_id:
                    self.mirrors.mark_max_reaction(max_msg_id, event.reaction_emoji)
                    await self.max_pool.react(max_user_id, max_chat_id, max_msg_id,
                                              event.reaction_emoji)

        elif event.event_type == "delete":
            if event.delete_source_msg_id is not None:
                max_msg_id = self.store.get_max_msg_id(
                    tg_chat_id=tg_chat_id,
                    tg_msg_id=int(event.delete_source_msg_id),
                    max_chat_id=max_chat_id,
                )
                log.debug("tg→max delete: tg_msg=%s → max_msg=%s (bridge=%s)",
                          event.delete_source_msg_id, max_msg_id, entry.name)
                if max_msg_id:
                    await self.max_pool.delete_msg(max_user_id, max_chat_id, max_msg_id)

    # ── MAX → TG ─────────────────────────────────────────────────────────────

    async def _max_to_tg(self, event: BridgeEvent):
        entry = event.bridge_entry  # primary entry (from listener)
        max_chat_id = entry.max_chat_id
        fmt = event.formatting

        # Try to route via the sender's own TG account (authorship match).
        sender_entry = None
        if event.sender_user_id is not None:
            sender_entry = self.lookup.get_bridge_by_max(
                entry.max_chat_id, event.sender_user_id
            )

        if sender_entry:
            # Sender has a bridge account — use their TG account, no prefix.
            client = self.tg_pool.get_client(sender_entry.user.telegram_user_id)
            text = event.text or ""
            log.debug("max→tg (sender=%s) type=%s text=%r",
                      sender_entry.user.name, event.event_type, text[:50])
        else:
            # Unknown sender — use primary's TG account with [Name]: prefix.
            client = self.tg_pool.get_client(entry.user.telegram_user_id)
            text, fmt = prepend_sender_name_fmt(
                event.sender_display_name, event.text or "", fmt,
            )
            log.info("max→tg via primary=%s (max_sender_id=%s not matched in config) "
                     "type=%s text=%r",
                     entry.user.name, event.sender_user_id, event.event_type, text[:50])

        if not client:
            log.warning("No TG client for event, dropping")
            return

        tg_chat_id = entry.telegram_chat_id
        # Convert formatting to HTML if present
        has_html = bool(fmt)
        html_text = internal_to_tg_html(text, fmt) if has_html else text

        # Resolve reply target
        reply_to = None
        if event.reply_to_source_msg_id is not None:
            reply_to = self.store.get_tg_msg_id(
                max_chat_id=max_chat_id,
                max_msg_id=str(event.reply_to_source_msg_id),
                tg_chat_id=tg_chat_id,
            )

        
        if event.event_type == "media_group" and event.media_list:
            from pyrogram.types import (
                InputMediaPhoto, InputMediaVideo,
                InputMediaAudio, InputMediaDocument,
            )
            media_inputs = []
            for i, mi in enumerate(event.media_list):
                buf = io.BytesIO(mi.data)
                buf.name = mi.filename
                # Caption and formatting only on the first item
                cap = (MIRROR_MARKER + (html_text if has_html else text)) if i == 0 else None
                pm = (tg_enums.ParseMode.HTML if has_html else tg_enums.ParseMode.DISABLED) if i == 0 else tg_enums.ParseMode.DISABLED
                if mi.mime_type.startswith("image/"):
                    media_inputs.append(InputMediaPhoto(buf, caption=cap, parse_mode=pm))
                elif mi.mime_type.startswith("video/"):
                    media_inputs.append(InputMediaVideo(buf, caption=cap, parse_mode=pm))
                elif mi.mime_type.startswith("audio/"):
                    media_inputs.append(InputMediaAudio(buf, caption=cap, parse_mode=pm))
                else:
                    media_inputs.append(InputMediaDocument(buf, caption=cap, parse_mode=pm))

            if media_inputs:
                msgs = await client.send_media_group(
                    tg_chat_id, media_inputs,
                    reply_to_message_id=reply_to,
                )
                if msgs and event.source_msg_id is not None:
                    self.store.store(
                        tg_chat_id=tg_chat_id,
                        tg_msg_id=msgs[0].id,
                        max_chat_id=max_chat_id,
                        max_msg_id=str(event.source_msg_id),
                        tg_msg_ids=[m.id for m in msgs],
                    )
                for msg in (msgs or []):
                    self.mirrors.mark_tg(msg.id)

        elif event.event_type == "text":
            send_text = MIRROR_MARKER + (html_text if has_html else text)
            msg = await client.send_message(
                tg_chat_id, send_text,
                reply_to_message_id=reply_to,
                parse_mode=tg_enums.ParseMode.HTML if has_html else tg_enums.ParseMode.DISABLED,
            )
            if event.source_msg_id is not None:
                self.store.store(tg_chat_id, msg.id, max_chat_id, str(event.source_msg_id))
            self.mirrors.mark_tg(msg.id)

        elif event.event_type == "photo" and event.media:
            caption = MIRROR_MARKER + (html_text if has_html else text) if text else MIRROR_MARKER
            buf = io.BytesIO(event.media.data)
            buf.name = event.media.filename
            msg = await client.send_photo(
                tg_chat_id, buf, caption=caption,
                reply_to_message_id=reply_to,
                parse_mode=tg_enums.ParseMode.HTML if has_html else tg_enums.ParseMode.DISABLED,
            )
            if event.source_msg_id is not None:
                self.store.store(tg_chat_id, msg.id, max_chat_id, str(event.source_msg_id))
            self.mirrors.mark_tg(msg.id)

        elif event.event_type in ("video", "audio", "file") and event.media:
            caption = MIRROR_MARKER + (html_text if has_html else text) if text else MIRROR_MARKER
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
                parse_mode=tg_enums.ParseMode.HTML if has_html else tg_enums.ParseMode.DISABLED,
            )
            if event.source_msg_id is not None:
                self.store.store(tg_chat_id, msg.id, max_chat_id, str(event.source_msg_id))
            self.mirrors.mark_tg(msg.id)

        elif event.event_type in ("photo", "video", "audio", "file"):
            fallback = f"{text}\n[{event.event_type.capitalize()} — media unavailable]".strip()
            msg = await client.send_message(
                tg_chat_id, MIRROR_MARKER + fallback,
                reply_to_message_id=reply_to,
                parse_mode=tg_enums.ParseMode.DISABLED,
            )
            if event.source_msg_id is not None:
                self.store.store(tg_chat_id, msg.id, max_chat_id, str(event.source_msg_id))
            self.mirrors.mark_tg(msg.id)

        elif event.event_type == "sticker":
            sticker_text = event.text or "[Sticker]"
            if not sender_entry:
                sticker_text = prepend_sender_name(event.sender_display_name, sticker_text)
            msg = await client.send_message(
                tg_chat_id, MIRROR_MARKER + sticker_text,
                reply_to_message_id=reply_to,
                parse_mode=tg_enums.ParseMode.DISABLED,
            )
            if event.source_msg_id is not None:
                self.store.store(tg_chat_id, msg.id, max_chat_id, str(event.source_msg_id))
            self.mirrors.mark_tg(msg.id)

        elif event.event_type == "edit":
            if event.edit_source_msg_id is not None:
                tg_msg_id = self.store.get_tg_msg_id(
                    max_chat_id=max_chat_id,
                    max_msg_id=str(event.edit_source_msg_id),
                    tg_chat_id=tg_chat_id,
                )
                if tg_msg_id:
                    edit_text = event.text or ""
                    edit_fmt = fmt
                    if not sender_entry:
                        edit_text, edit_fmt = prepend_sender_name_fmt(
                            event.sender_display_name, edit_text, edit_fmt,
                        )
                    edit_has_html = bool(edit_fmt)
                    edit_html = internal_to_tg_html(edit_text, edit_fmt) if edit_has_html else edit_text
                    send_edit = MIRROR_MARKER + (edit_html if edit_has_html else edit_text)
                    await client.edit_message_text(
                        tg_chat_id, tg_msg_id, send_edit,
                        parse_mode=tg_enums.ParseMode.HTML if edit_has_html else tg_enums.ParseMode.DISABLED,
                    )

        elif event.event_type == "reaction":
            if event.source_msg_id is not None:
                tg_msg_id = self.store.get_tg_msg_id(
                    max_chat_id=max_chat_id,
                    max_msg_id=str(event.source_msg_id),
                    tg_chat_id=tg_chat_id,
                )
                if tg_msg_id:
                    emoji = event.reaction_emoji
                    self.mirrors.mark_tg_reaction(tg_msg_id, emoji)
                    # Pyrogram send_reaction: emoji string (empty = remove)
                    await client.send_reaction(tg_chat_id, tg_msg_id,
                                               emoji or "")

        elif event.event_type == "delete":
            if event.delete_source_msg_id is not None:
                tg_msg_ids = self.store.get_tg_msg_ids(
                    max_chat_id=max_chat_id,
                    max_msg_id=str(event.delete_source_msg_id),
                    tg_chat_id=tg_chat_id,
                )
                if tg_msg_ids:
                    await client.delete_messages(tg_chat_id, tg_msg_ids)
