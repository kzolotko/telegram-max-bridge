import os
from pathlib import Path

import yaml

from .types import AppConfig, BridgeEntry, UserMapping


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

    bridges = []
    for i, b in enumerate(raw.get("bridges", [])):
        for field in ("name", "telegram_chat_id", "max_chat_id"):
            if not b.get(field):
                raise ValueError(f"bridges[{i}].{field} is required")
        u = b.get("user", {})
        for field in ("name", "telegram_user_id", "max_user_id"):
            if not u.get(field):
                raise ValueError(f"bridges[{i}].user.{field} is required")
        user = UserMapping(
            name=u["name"],
            telegram_user_id=u["telegram_user_id"],
            max_user_id=u["max_user_id"],
        )
        bridges.append(BridgeEntry(
            name=b["name"],
            telegram_chat_id=b["telegram_chat_id"],
            max_chat_id=b["max_chat_id"],
            user=user,
        ))

    if not bridges:
        raise ValueError("At least one bridge entry is required")

    return AppConfig(
        api_id=raw["api_id"],
        api_hash=raw["api_hash"],
        bridges=bridges,
        sessions_dir=raw.get("sessions_dir", "sessions"),
    )


class ConfigLookup:
    """Fast lookup tables for bridge routing.

    When multiple users are configured for the same chat, only the first
    user in config order (the *primary*) listens to that chat.  Other
    users' accounts are only used for sending when they are the original
    message author — this preserves authorship without duplicating
    messages.
    """

    def __init__(self, config: AppConfig):
        self.config = config

        # ── per-sender lookup (all entries) ──────────────────────────────────
        # (tg_chat_id, tg_user_id) -> BridgeEntry
        self._by_tg: dict[tuple[int, int], BridgeEntry] = {}
        # (max_chat_id, max_user_id) -> BridgeEntry
        self._by_max: dict[tuple[int, int], BridgeEntry] = {}

        # ── primary entry per chat (first in config wins) ───────────────────
        self._primary_by_tg: dict[int, BridgeEntry] = {}
        self._primary_by_max: dict[int, BridgeEntry] = {}

        # ── listening assignments (only primary gets each chat) ─────────────
        # tg_user_id -> [tg_chat_id, ...]
        self._tg_chats_for_user: dict[int, list[int]] = {}
        # max_user_id -> [max_chat_id, ...]
        self._max_chats_for_user: dict[int, list[int]] = {}

        for entry in config.bridges:
            u = entry.user
            self._by_tg[(entry.telegram_chat_id, u.telegram_user_id)] = entry
            self._by_max[(entry.max_chat_id, u.max_user_id)] = entry

            # Assign listening responsibility to the first user per chat
            if entry.telegram_chat_id not in self._primary_by_tg:
                self._primary_by_tg[entry.telegram_chat_id] = entry
                self._tg_chats_for_user.setdefault(u.telegram_user_id, []).append(
                    entry.telegram_chat_id
                )
            if entry.max_chat_id not in self._primary_by_max:
                self._primary_by_max[entry.max_chat_id] = entry
                self._max_chats_for_user.setdefault(u.max_user_id, []).append(
                    entry.max_chat_id
                )

    # ── sender-specific lookup ───────────────────────────────────────────────

    def get_bridge_by_tg(self, chat_id: int, tg_user_id: int) -> 'BridgeEntry | None':
        """Find bridge entry for a specific TG sender (authorship routing)."""
        return self._by_tg.get((chat_id, tg_user_id))

    def get_bridge_by_max(self, chat_id: int, max_user_id: int) -> 'BridgeEntry | None':
        """Find bridge entry for a specific MAX sender (authorship routing)."""
        return self._by_max.get((chat_id, max_user_id))

    # ── primary entry per chat ───────────────────────────────────────────────

    def get_primary_by_tg(self, chat_id: int) -> 'BridgeEntry | None':
        """Primary bridge entry for a TG chat (first configured user)."""
        return self._primary_by_tg.get(chat_id)

    def get_primary_by_max(self, chat_id: int) -> 'BridgeEntry | None':
        """Primary bridge entry for a MAX chat (first configured user)."""
        return self._primary_by_max.get(chat_id)

    # ── per-user chat lists (only chats where user is primary) ──────────────

    def get_tg_chat_ids_for_user(self, tg_user_id: int) -> list[int]:
        return self._tg_chats_for_user.get(tg_user_id, [])

    def get_max_chat_ids_for_user(self, max_user_id: int) -> list[int]:
        return self._max_chats_for_user.get(max_user_id, [])

    def get_unique_users(self) -> list[UserMapping]:
        """Deduplicated list of users (by telegram_user_id)."""
        seen: dict[int, UserMapping] = {}
        for entry in self.config.bridges:
            seen.setdefault(entry.user.telegram_user_id, entry.user)
        return list(seen.values())
