import os
from pathlib import Path

import yaml

from .types import AppConfig, BridgeEntry, UserMapping


def load_credentials(credentials_path: str | None = None) -> dict:
    """Load Telegram API credentials from credentials.yaml."""
    path = Path(credentials_path or "credentials.yaml")
    if not path.exists():
        raise FileNotFoundError(
            f"Credentials file not found: {path}\n"
            f"Run 'python -m src.setup credentials' to create it,\n"
            f"or copy credentials.example.yaml to credentials.yaml."
        )

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    if not raw.get("api_id"):
        raise ValueError("Missing api_id in credentials.yaml")
    if not raw.get("api_hash"):
        raise ValueError("Missing api_hash in credentials.yaml")

    return {"api_id": int(raw["api_id"]), "api_hash": str(raw["api_hash"])}


def load_config(
    config_path: str | None = None,
    credentials_path: str | None = None,
) -> AppConfig:
    """Load bridge config + credentials from separate files.

    For backwards compatibility, if config.yaml still contains api_id/api_hash
    (old single-file format), those values are used as fallback when
    credentials.yaml is missing.
    """
    path = Path(config_path or "config.yaml")
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    # Try loading credentials from the dedicated file first.
    # Fall back to config.yaml values for backwards compatibility.
    try:
        creds = load_credentials(credentials_path)
    except FileNotFoundError:
        if raw.get("api_id") and raw.get("api_hash"):
            creds = {"api_id": int(raw["api_id"]), "api_hash": str(raw["api_hash"])}
        else:
            raise FileNotFoundError(
                "credentials.yaml not found and config.yaml has no api_id/api_hash.\n"
                "Run 'python -m src.setup credentials' to set up Telegram API credentials."
            )

    def _to_int(value, field_name: str, idx: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"bridges[{idx}].{field_name} must be an integer, got {value!r}"
            ) from None

    bridges = []
    # Allow duplicate bridge names only for the same chat pair (multi-user mode).
    # Reusing the same name for different pairs is ambiguous and error-prone.
    bridge_name_pairs: dict[str, tuple[int, int]] = {}
    for i, b in enumerate(raw.get("bridges", [])):
        for field in ("name", "telegram_chat_id", "max_chat_id"):
            if not b.get(field):
                raise ValueError(f"bridges[{i}].{field} is required")
        u = b.get("user", {})
        for field in ("name", "telegram_user_id", "max_user_id"):
            if not u.get(field):
                raise ValueError(f"bridges[{i}].user.{field} is required")
        tg_chat_id = _to_int(b["telegram_chat_id"], "telegram_chat_id", i)
        max_chat_id = _to_int(b["max_chat_id"], "max_chat_id", i)
        tg_user_id = _to_int(u["telegram_user_id"], "user.telegram_user_id", i)
        max_user_id = _to_int(u["max_user_id"], "user.max_user_id", i)

        pair = (tg_chat_id, max_chat_id)
        name = str(b["name"])
        prev_pair = bridge_name_pairs.get(name)
        if prev_pair is None:
            bridge_name_pairs[name] = pair
        elif prev_pair != pair:
            raise ValueError(
                f"Bridge name {name!r} is reused for different chat pairs: "
                f"{prev_pair} and {pair}. "
                "Use unique names per chat pair."
            )

        user = UserMapping(
            name=str(u["name"]),
            telegram_user_id=tg_user_id,
            max_user_id=max_user_id,
        )
        bridges.append(BridgeEntry(
            name=name,
            telegram_chat_id=tg_chat_id,
            max_chat_id=max_chat_id,
            user=user,
        ))

    if not bridges:
        raise ValueError("At least one bridge entry is required")

    return AppConfig(
        api_id=creds["api_id"],
        api_hash=creds["api_hash"],
        bridges=bridges,
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

    def update_max_user_id(self, old_user_id: int, new_user_id: int) -> int:
        """Re-key all MAX routing entries from *old_user_id* to *new_user_id*.

        Called at runtime when the authenticated user ID returned by the MAX
        server differs from the value stored in config.yaml.  Returns the
        number of entries that were updated.
        """
        import logging as _logging
        _log = _logging.getLogger("bridge.config")

        to_update = [
            (k, v) for k, v in self._by_max.items() if k[1] == old_user_id
        ]
        for (chat_id, _), entry in to_update:
            del self._by_max[(chat_id, old_user_id)]
            self._by_max[(chat_id, new_user_id)] = entry

        if to_update:
            _log.warning(
                "MAX routing: corrected user_id %d → %d (%d chat entries)",
                old_user_id, new_user_id, len(to_update),
            )
        return len(to_update)
