from dataclasses import dataclass, field
from typing import Optional


@dataclass
class UserMapping:
    name: str
    telegram_user_id: int
    max_user_id: int

    @property
    def telegram_session(self) -> str:
        """Pyrogram session filename (in sessions/): tg_{name}.session"""
        return f"tg_{self.name}"

    @property
    def max_session(self) -> str:
        """MAX session filename (in sessions/): max_{name}.max_session"""
        return f"max_{self.name}"


@dataclass
class BridgeEntry:
    name: str
    telegram_chat_id: int
    max_chat_id: int
    user: UserMapping


@dataclass
class AppConfig:
    api_id: int  # Telegram API ID from my.telegram.org
    api_hash: str  # Telegram API hash
    bridges: list['BridgeEntry'] = field(default_factory=list)
    sessions_dir: str = "sessions"


@dataclass
class MediaInfo:
    data: bytes
    filename: str
    mime_type: str


@dataclass
class BridgeEvent:
    direction: str  # 'tg-to-max' or 'max-to-tg'
    bridge_entry: BridgeEntry
    sender_display_name: str
    event_type: str  # 'text', 'photo', 'video', 'file', 'audio', 'sticker', 'edit', 'delete'
    sender_user_id: Optional[int] = None  # TG user ID or MAX user ID of the original sender
    text: Optional[str] = None
    media: Optional[MediaInfo] = None
    reply_to_source_msg_id: Optional[int | str] = None
    edit_source_msg_id: Optional[int | str] = None
    delete_source_msg_id: Optional[int | str] = None
    source_msg_id: Optional[int | str] = None
    formatting: Optional[list[dict]] = None  # platform-agnostic formatting entities
    media_list: Optional[list['MediaInfo']] = None  # multiple media (album / multi-attach)
    reaction_emoji: Optional[str] = None  # emoji string; None = remove reaction
