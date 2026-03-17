import os
from pathlib import Path

import yaml

from .types import AppConfig, ChatPair, UserMapping


def load_config(config_path: str | None = None) -> AppConfig:
    path = Path(config_path or "config.yaml")
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not raw.get("api_id"):
        raise ValueError("Missing api_id (Telegram API ID from my.telegram.org)")
    if not raw.get("api_hash"):
        raise ValueError("Missing api_hash (Telegram API hash from my.telegram.org)")
    if not raw.get("listener_telegram_session"):
        raise ValueError("Missing listener_telegram_session")
    if not raw.get("listener_max_session"):
        raise ValueError("Missing listener_max_session")

    chat_pairs = []
    for i, cp in enumerate(raw.get("chat_pairs", [])):
        if not cp.get("name"):
            raise ValueError(f"chat_pairs[{i}].name is required")
        if not cp.get("telegram_chat_id"):
            raise ValueError(f"chat_pairs[{i}].telegram_chat_id is required")
        if not cp.get("max_chat_id"):
            raise ValueError(f"chat_pairs[{i}].max_chat_id is required")
        chat_pairs.append(ChatPair(
            name=cp["name"],
            telegram_chat_id=cp["telegram_chat_id"],
            max_chat_id=cp["max_chat_id"],
        ))

    if not chat_pairs:
        raise ValueError("At least one chat_pair is required")

    users = []
    for i, u in enumerate(raw.get("users", [])):
        for field in ("name", "telegram_user_id", "max_user_id", "telegram_session", "max_session"):
            if not u.get(field):
                raise ValueError(f"users[{i}].{field} is required")
        users.append(UserMapping(
            name=u["name"],
            telegram_user_id=u["telegram_user_id"],
            max_user_id=u["max_user_id"],
            telegram_session=u["telegram_session"],
            max_session=u["max_session"],
        ))

    if len(users) > 10:
        raise ValueError("Maximum 10 users supported")

    return AppConfig(
        api_id=raw["api_id"],
        api_hash=raw["api_hash"],
        listener_telegram_session=raw["listener_telegram_session"],
        listener_max_session=raw["listener_max_session"],
        chat_pairs=chat_pairs,
        users=users,
        sessions_dir=raw.get("sessions_dir", "sessions"),
    )


class ConfigLookup:
    def __init__(self, config: AppConfig):
        self.config = config
        self._tg_chat_to_pair: dict[int, ChatPair] = {}
        self._max_chat_to_pair: dict[int, ChatPair] = {}
        self._tg_user_to_mapping: dict[int, UserMapping] = {}
        self._max_user_to_mapping: dict[int, UserMapping] = {}

        for pair in config.chat_pairs:
            self._tg_chat_to_pair[pair.telegram_chat_id] = pair
            self._max_chat_to_pair[pair.max_chat_id] = pair

        for user in config.users:
            self._tg_user_to_mapping[user.telegram_user_id] = user
            self._max_user_to_mapping[user.max_user_id] = user

    def get_pair_by_tg_chat(self, chat_id: int) -> ChatPair | None:
        return self._tg_chat_to_pair.get(chat_id)

    def get_pair_by_max_chat(self, chat_id: int) -> ChatPair | None:
        return self._max_chat_to_pair.get(chat_id)

    def get_user_by_tg_id(self, user_id: int) -> UserMapping | None:
        return self._tg_user_to_mapping.get(user_id)

    def get_user_by_max_id(self, user_id: int) -> UserMapping | None:
        return self._max_user_to_mapping.get(user_id)
