from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ChatPair:
    name: str
    telegram_chat_id: int
    max_chat_id: int


@dataclass
class UserMapping:
    name: str
    telegram_user_id: int
    max_user_id: int
    telegram_session: str  # Pyrogram session name (file in sessions/)
    max_session: str  # MAX session name (file in sessions/)


@dataclass
class AppConfig:
    api_id: int  # Telegram API ID from my.telegram.org
    api_hash: str  # Telegram API hash
    listener_telegram_session: str  # Session name for TG listener account
    listener_max_session: str  # Session name for MAX listener account
    chat_pairs: list[ChatPair] = field(default_factory=list)
    users: list[UserMapping] = field(default_factory=list)
    sessions_dir: str = "sessions"


@dataclass
class MediaInfo:
    data: bytes
    filename: str
    mime_type: str


@dataclass
class BridgeEvent:
    direction: str  # 'tg-to-max' or 'max-to-tg'
    chat_pair: ChatPair
    user: Optional[UserMapping]
    sender_display_name: str
    event_type: str  # 'text', 'photo', 'video', 'file', 'audio', 'sticker', 'edit', 'delete'
    text: Optional[str] = None
    media: Optional[MediaInfo] = None
    reply_to_source_msg_id: Optional[int | str] = None
    edit_source_msg_id: Optional[int | str] = None
    delete_source_msg_id: Optional[int | str] = None
    source_msg_id: Optional[int | str] = None
