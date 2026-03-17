from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.handlers import MessageHandler, EditedMessageHandler, DeletedMessagesHandler

from ..bridge.echo_guard import EchoGuard
from ..config import ConfigLookup
from ..types import AppConfig, BridgeEvent, MediaInfo

from typing import Callable, Awaitable
import io


class TelegramListener:
    """Listens for messages in Telegram using a user account."""

    def __init__(
        self,
        config: AppConfig,
        lookup: ConfigLookup,
        echo_guard: EchoGuard,
        on_event: Callable[[BridgeEvent], Awaitable[None]],
    ):
        self.config = config
        self.lookup = lookup
        self.echo_guard = echo_guard
        self.on_event = on_event
        self.client = Client(
            name=config.listener_telegram_session,
            api_id=config.api_id,
            api_hash=config.api_hash,
            workdir=config.sessions_dir,
        )

    async def start(self) -> int:
        # Register handlers before starting
        monitored_chat_ids = [cp.telegram_chat_id for cp in self.config.chat_pairs]
        chat_filter = filters.chat(monitored_chat_ids)

        self.client.add_handler(MessageHandler(self._handle_message, chat_filter))
        self.client.add_handler(EditedMessageHandler(self._handle_edited_message, chat_filter))
        self.client.add_handler(DeletedMessagesHandler(self._handle_deleted_messages, chat_filter))

        await self.client.start()
        me = await self.client.get_me()
        print(f"[TG Listener] Started as @{me.username} (ID: {me.id})")
        return me.id

    async def stop(self):
        await self.client.stop()

    async def _handle_message(self, client: Client, message: Message):
        try:
            sender_id = message.from_user.id if message.from_user else None
            if not sender_id or self.echo_guard.is_managed_tg_user(sender_id):
                return

            chat_pair = self.lookup.get_pair_by_tg_chat(message.chat.id)
            if not chat_pair:
                return

            user = self.lookup.get_user_by_tg_id(sender_id)
            sender_name = self._get_sender_name(message)

            reply_to = None
            if message.reply_to_message:
                reply_to = message.reply_to_message.id

            if message.photo:
                media = await self._download_media(message)
                await self.on_event(BridgeEvent(
                    direction="tg-to-max",
                    chat_pair=chat_pair,
                    user=user,
                    sender_display_name=sender_name,
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
                    chat_pair=chat_pair,
                    user=user,
                    sender_display_name=sender_name,
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
                    chat_pair=chat_pair,
                    user=user,
                    sender_display_name=sender_name,
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
                    chat_pair=chat_pair,
                    user=user,
                    sender_display_name=sender_name,
                    event_type="audio",
                    text=message.caption,
                    media=media,
                    reply_to_source_msg_id=reply_to,
                    source_msg_id=message.id,
                ))
            elif message.sticker:
                await self.on_event(BridgeEvent(
                    direction="tg-to-max",
                    chat_pair=chat_pair,
                    user=user,
                    sender_display_name=sender_name,
                    event_type="sticker",
                    text=f"[Sticker: {message.sticker.emoji or ''}]",
                    reply_to_source_msg_id=reply_to,
                    source_msg_id=message.id,
                ))
            elif message.text:
                await self.on_event(BridgeEvent(
                    direction="tg-to-max",
                    chat_pair=chat_pair,
                    user=user,
                    sender_display_name=sender_name,
                    event_type="text",
                    text=message.text,
                    reply_to_source_msg_id=reply_to,
                    source_msg_id=message.id,
                ))
        except Exception as e:
            print(f"[TG Listener] Error handling message: {e}")

    async def _handle_edited_message(self, client: Client, message: Message):
        try:
            sender_id = message.from_user.id if message.from_user else None
            if not sender_id or self.echo_guard.is_managed_tg_user(sender_id):
                return

            chat_pair = self.lookup.get_pair_by_tg_chat(message.chat.id)
            if not chat_pair:
                return

            user = self.lookup.get_user_by_tg_id(sender_id)
            sender_name = self._get_sender_name(message)

            await self.on_event(BridgeEvent(
                direction="tg-to-max",
                chat_pair=chat_pair,
                user=user,
                sender_display_name=sender_name,
                event_type="edit",
                text=message.text or message.caption,
                edit_source_msg_id=message.id,
                source_msg_id=message.id,
            ))
        except Exception as e:
            print(f"[TG Listener] Error handling edited message: {e}")

    async def _handle_deleted_messages(self, client: Client, messages: list[Message]):
        try:
            for message in messages:
                chat_pair = self.lookup.get_pair_by_tg_chat(message.chat.id)
                if not chat_pair:
                    continue

                await self.on_event(BridgeEvent(
                    direction="tg-to-max",
                    chat_pair=chat_pair,
                    user=None,
                    sender_display_name="Unknown",
                    event_type="delete",
                    delete_source_msg_id=message.id,
                    source_msg_id=message.id,
                ))
        except Exception as e:
            print(f"[TG Listener] Error handling deleted messages: {e}")

    def _get_sender_name(self, message: Message) -> str:
        if message.from_user:
            parts = [message.from_user.first_name]
            if message.from_user.last_name:
                parts.append(message.from_user.last_name)
            return " ".join(parts)
        return "Unknown"

    async def _download_media(self, message: Message) -> MediaInfo | None:
        try:
            buf = io.BytesIO()
            await self.client.download_media(message, in_memory=True, file_name=buf)
            data = buf.getvalue()
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
            print(f"[TG Listener] Failed to download media: {e}")
            return None
