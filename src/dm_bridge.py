"""
DM Bridge: forwards MAX direct messages to a Telegram bot and routes replies back.

Supports multiple users — each user's MAX DMs are forwarded to their
personal TG chat with the bot.

Handles all message types: text, photos, files, stickers, edits, deletes.
"""

import io
import logging

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.handlers import MessageHandler

from .dm_store import DmStore, DmContext
from .max.client_pool import MaxClientPool
from .bridge.mirror_tracker import MirrorTracker
from .types import UserMapping, MediaInfo

log = logging.getLogger("bridge.dm")


class DmBridge:
    """Bridges MAX DMs ↔ TG bot personal chat for one or more users."""

    def __init__(
        self,
        bot_client: Client,
        max_pool: MaxClientPool,
        mirror_tracker: MirrorTracker,
        dm_store: DmStore,
        users: list[UserMapping],
    ):
        self.bot = bot_client
        self.max_pool = max_pool
        self.mirrors = mirror_tracker
        self.store = dm_store
        self.users = users
        self._by_max: dict[int, UserMapping] = {u.max_user_id: u for u in users}
        self._by_tg: dict[int, UserMapping] = {u.telegram_user_id: u for u in users}
        self._allowed_tg_ids: set[int] = {u.telegram_user_id for u in users}

    async def start(self):
        user_filter = filters.user(list(self._allowed_tg_ids))
        self.bot.add_handler(
            MessageHandler(
                self._handle_bot_reply,
                filters.private & filters.reply & user_filter,
            )
        )
        await self.bot.start()
        me = await self.bot.get_me()
        user_names = ", ".join(u.name for u in self.users)
        log.info("DM bridge bot started: @%s (ID: %d) for users: %s",
                 me.username, me.id, user_names)

    async def stop(self):
        try:
            await self.bot.stop()
        except Exception:
            pass

    def _resolve_user(self, recipient_max_user_id: int | None) -> UserMapping | None:
        if recipient_max_user_id:
            user = self._by_max.get(recipient_max_user_id)
            if user:
                return user
        if len(self.users) == 1:
            return self.users[0]
        return None

    # ── MAX DM → TG bot ─────────────────────────────────────────────────────

    async def handle_incoming(
        self,
        sender_id: int,
        sender_name: str,
        chat_id: int,
        msg_id: str,
        text: str | None,
        recipient_max_user_id: int | None = None,
        event_type: str = "text",
        media_list: list[tuple[str, MediaInfo]] | None = None,
        formatting: list[dict] | None = None,
    ):
        user = self._resolve_user(recipient_max_user_id)
        if not user:
            log.warning("DM from %s: can't determine recipient (max_uid=%s)",
                        sender_name, recipient_max_user_id)
            return

        tg_owner = user.telegram_user_id

        try:
            if event_type == "delete":
                # We can't delete bot messages by MAX msg_id easily —
                # just notify the user
                await self.bot.send_message(
                    chat_id=tg_owner,
                    text=f"[{sender_name} deleted a message]",
                )
                return

            if event_type == "edit":
                display = f"[{sender_name}] ✏️: {text}" if text else f"[{sender_name}] ✏️: [no text]"
                bot_msg = await self.bot.send_message(chat_id=tg_owner, text=display)
                self.store.store(
                    bot_msg.id,
                    DmContext(max_user_id=sender_id, max_chat_id=chat_id,
                             max_msg_id=msg_id, sender_name=sender_name),
                    tg_owner_id=tg_owner,
                )
                log.debug("MAX DM edit → TG bot: from %s → %s bot_msg=%d",
                         sender_name, user.name, bot_msg.id)
                return

            if event_type == "sticker":
                display = f"[{sender_name}]: [Sticker]"
                bot_msg = await self.bot.send_message(chat_id=tg_owner, text=display)
                self.store.store(
                    bot_msg.id,
                    DmContext(max_user_id=sender_id, max_chat_id=chat_id,
                             max_msg_id=msg_id, sender_name=sender_name),
                    tg_owner_id=tg_owner,
                )
                return

            if event_type == "media" and media_list:
                caption = f"[{sender_name}]: {text}" if text else f"[{sender_name}]:"
                # Send each media item via bot
                first_bot_msg = None
                for evt_type, media in media_list:
                    file = io.BytesIO(media.data)
                    file.name = media.filename
                    if evt_type == "photo":
                        bot_msg = await self.bot.send_photo(
                            chat_id=tg_owner, photo=file,
                            caption=caption if not first_bot_msg else None,
                        )
                    else:
                        bot_msg = await self.bot.send_document(
                            chat_id=tg_owner, document=file,
                            caption=caption if not first_bot_msg else None,
                        )
                    if not first_bot_msg:
                        first_bot_msg = bot_msg
                        caption = None  # only first item gets caption

                if first_bot_msg:
                    self.store.store(
                        first_bot_msg.id,
                        DmContext(max_user_id=sender_id, max_chat_id=chat_id,
                                 max_msg_id=msg_id, sender_name=sender_name),
                        tg_owner_id=tg_owner,
                    )
                    log.debug("MAX DM media → TG bot: %d item(s) from %s → %s",
                             len(media_list), sender_name, user.name)
                return

            # Default: text message
            display = f"[{sender_name}]: {text}" if text else f"[{sender_name}]: [no text]"
            bot_msg = await self.bot.send_message(chat_id=tg_owner, text=display)
            self.store.store(
                bot_msg.id,
                DmContext(max_user_id=sender_id, max_chat_id=chat_id,
                         max_msg_id=msg_id, sender_name=sender_name),
                tg_owner_id=tg_owner,
            )
            log.debug("MAX DM → TG bot: from %s (chat=%s) → %s bot_msg=%d",
                     sender_name, chat_id, user.name, bot_msg.id)

        except Exception as e:
            log.error("Failed to forward MAX DM to bot: %s", e, exc_info=True)

    # ── TG bot reply → MAX DM ───────────────────────────────────────────────

    async def _handle_bot_reply(self, client: Client, message: Message):
        try:
            reply_to = message.reply_to_message
            if not reply_to:
                return

            tg_user_id = message.from_user.id if message.from_user else None
            ctx = self.store.get(reply_to.id, tg_owner_id=tg_user_id)
            if not ctx:
                await message.reply_text(
                    "Cannot route reply: original message expired or not found."
                )
                return

            user = self._by_tg.get(tg_user_id)
            if not user:
                log.error("Bot reply from unknown TG user %s", tg_user_id)
                return

            max_user_id = user.max_user_id
            # Reply target: use the original conversation's chat_id.
            # In MAX DMs from our listener's perspective, chatId is the
            # conversation partner's user ID — the correct target for
            # send_message.  ctx.max_user_id (message.sender) can differ
            # from chatId and is not always a valid send target.
            max_chat_id = ctx.max_chat_id

            # Photo reply
            if message.photo:
                data = await self._download_tg_media(message)
                if data:
                    max_msg_id = await self.max_pool.send_photo(
                        max_user_id=max_user_id, chat_id=max_chat_id,
                        photo_data=data, caption=message.caption or "",
                    )
                    if max_msg_id:
                        self.mirrors.mark_max(max_msg_id)
                        log.debug("TG bot photo reply → MAX DM: %s → %s",
                                 user.name, ctx.sender_name)
                return

            # Document/file reply
            if message.document or message.video or message.audio or message.voice:
                data = await self._download_tg_media(message)
                if data:
                    fname = "file"
                    mime = "application/octet-stream"
                    if message.document:
                        fname = message.document.file_name or "document"
                        mime = message.document.mime_type or mime
                    elif message.video:
                        fname = message.video.file_name or "video.mp4"
                        mime = message.video.mime_type or "video/mp4"
                    elif message.audio:
                        fname = message.audio.file_name or "audio.mp3"
                        mime = message.audio.mime_type or "audio/mpeg"
                    elif message.voice:
                        fname = "voice.ogg"
                        mime = "audio/ogg"

                    max_msg_id = await self.max_pool.send_file(
                        max_user_id=max_user_id, chat_id=max_chat_id,
                        file_data=data, filename=fname,
                        caption=message.caption or "",
                    )
                    if max_msg_id:
                        self.mirrors.mark_max(max_msg_id)
                        log.debug("TG bot file reply → MAX DM: %s → %s (%s)",
                                 user.name, ctx.sender_name, fname)
                return

            # Text reply
            reply_text = message.text or message.caption or ""
            if not reply_text:
                return

            max_msg_id = await self.max_pool.send_text(
                max_user_id=max_user_id, chat_id=max_chat_id, text=reply_text,
            )
            if max_msg_id:
                self.mirrors.mark_max(max_msg_id)
                log.debug("TG bot reply → MAX DM: %s → %s (chat=%s)",
                         user.name, ctx.sender_name, max_chat_id)
            else:
                log.error("Failed to send reply to MAX DM (chat=%s)", max_chat_id)
        except Exception as e:
            log.error("Error handling bot reply: %s", e, exc_info=True)

    async def _download_tg_media(self, message: Message) -> bytes | None:
        try:
            result = await self.bot.download_media(message, in_memory=True)
            if result:
                return result.getvalue()
        except Exception as e:
            log.error("Failed to download TG media: %s", e)
        return None
