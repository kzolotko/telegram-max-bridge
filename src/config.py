import logging
import shutil
from pathlib import Path

import yaml

from .types import AdminBotConfig, AppConfig, BridgeEntry, DmBridgeConfig, UserMapping

_log = logging.getLogger("bridge.config")


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


def _is_new_format(raw: dict) -> bool:
    """Detect whether config uses new format (top-level users section)."""
    return isinstance(raw.get("users"), list) and len(raw.get("users", [])) > 0


def _parse_optional_sections(raw: dict) -> tuple['DmBridgeConfig | None', 'AdminBotConfig | None']:
    """Parse dm_bridge and admin_bot sections (shared between old and new format)."""
    dm_bridge_cfg = None
    dm_raw = raw.get("dm_bridge")
    if dm_raw:
        bot_token = dm_raw.get("bot_token")
        if not bot_token:
            raise ValueError("dm_bridge.bot_token is required")
        dm_bridge_cfg = DmBridgeConfig(bot_token=str(bot_token))

    admin_bot_cfg = None
    admin_raw = raw.get("admin_bot")
    if admin_raw:
        bot_token = admin_raw.get("bot_token")
        if not bot_token:
            raise ValueError("admin_bot.bot_token is required")
        raw_ids = admin_raw.get("admin_ids", [])
        if not raw_ids:
            raise ValueError("admin_bot.admin_ids must contain at least one user ID")
        admin_ids = [int(uid) for uid in raw_ids]
        admin_bot_cfg = AdminBotConfig(bot_token=str(bot_token), admin_ids=admin_ids)

    return dm_bridge_cfg, admin_bot_cfg


def _to_int(value, field_name: str, context: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"{context}.{field_name} must be an integer, got {value!r}"
        ) from None


def _load_new_format(raw: dict, creds: dict) -> AppConfig:
    """Parse new config format with top-level users and bridges sections."""
    # ── Parse users ─────────────────────────────────────────────────────────
    user_registry: dict[str, UserMapping] = {}
    for i, u in enumerate(raw.get("users", [])):
        for f in ("name", "telegram_user_id", "max_user_id"):
            if not u.get(f):
                raise ValueError(f"users[{i}].{f} is required")
        name = str(u["name"])
        if name in user_registry:
            raise ValueError(f"Duplicate user name: {name!r}")
        user_registry[name] = UserMapping(
            name=name,
            telegram_user_id=_to_int(u["telegram_user_id"], "telegram_user_id", f"users[{i}]"),
            max_user_id=_to_int(u["max_user_id"], "max_user_id", f"users[{i}]"),
        )

    # ── Parse bridges ───────────────────────────────────────────────────────
    bridges: list[BridgeEntry] = []
    seen_bridge_names: set[str] = set()
    for i, b in enumerate(raw.get("bridges", [])):
        for f in ("name", "telegram_chat_id", "max_chat_id", "users"):
            if not b.get(f):
                raise ValueError(f"bridges[{i}].{f} is required")

        name = str(b["name"])
        if name in seen_bridge_names:
            raise ValueError(f"Duplicate bridge name: {name!r}")
        seen_bridge_names.add(name)

        tg_chat_id = _to_int(b["telegram_chat_id"], "telegram_chat_id", f"bridges[{i}]")
        max_chat_id = _to_int(b["max_chat_id"], "max_chat_id", f"bridges[{i}]")

        user_names = b["users"]
        if not isinstance(user_names, list) or not user_names:
            raise ValueError(f"bridges[{i}].users must be a non-empty list")

        for uname in user_names:
            uname = str(uname)
            if uname not in user_registry:
                raise ValueError(
                    f"bridges[{i}].users references unknown user {uname!r}. "
                    f"Available users: {', '.join(user_registry.keys())}"
                )
            bridges.append(BridgeEntry(
                name=name,
                telegram_chat_id=tg_chat_id,
                max_chat_id=max_chat_id,
                user=user_registry[uname],
            ))

    if not bridges:
        raise ValueError("At least one bridge entry is required")

    dm_bridge_cfg, admin_bot_cfg = _parse_optional_sections(raw)

    return AppConfig(
        api_id=creds["api_id"],
        api_hash=creds["api_hash"],
        users=list(user_registry.values()),
        bridges=bridges,
        dm_bridge=dm_bridge_cfg,
        admin_bot=admin_bot_cfg,
    )


def _load_old_format(raw: dict, creds: dict) -> AppConfig:
    """Parse old config format (inline user per bridge entry). Backward compat."""
    _log.warning(
        "Old config format detected (inline user per bridge). "
        "Run 'python -m src.setup migrate' to convert to the new format."
    )

    bridges: list[BridgeEntry] = []
    bridge_name_pairs: dict[str, tuple[int, int]] = {}
    seen_users: dict[str, UserMapping] = {}

    for i, b in enumerate(raw.get("bridges", [])):
        for f in ("name", "telegram_chat_id", "max_chat_id"):
            if not b.get(f):
                raise ValueError(f"bridges[{i}].{f} is required")
        u = b.get("user", {})
        for f in ("name", "telegram_user_id", "max_user_id"):
            if not u.get(f):
                raise ValueError(f"bridges[{i}].user.{f} is required")

        tg_chat_id = _to_int(b["telegram_chat_id"], "telegram_chat_id", f"bridges[{i}]")
        max_chat_id = _to_int(b["max_chat_id"], "max_chat_id", f"bridges[{i}]")
        tg_user_id = _to_int(u["telegram_user_id"], "user.telegram_user_id", f"bridges[{i}]")
        max_user_id = _to_int(u["max_user_id"], "user.max_user_id", f"bridges[{i}]")

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
        seen_users.setdefault(user.name, user)
        bridges.append(BridgeEntry(
            name=name,
            telegram_chat_id=tg_chat_id,
            max_chat_id=max_chat_id,
            user=user,
        ))

    if not bridges:
        raise ValueError("At least one bridge entry is required")

    dm_bridge_cfg, admin_bot_cfg = _parse_optional_sections(raw)

    return AppConfig(
        api_id=creds["api_id"],
        api_hash=creds["api_hash"],
        users=list(seen_users.values()),
        bridges=bridges,
        dm_bridge=dm_bridge_cfg,
        admin_bot=admin_bot_cfg,
    )


def load_config(
    config_path: str | None = None,
    credentials_path: str | None = None,
) -> AppConfig:
    """Load bridge config + credentials from separate files.

    Supports two config formats:
      - New format: top-level ``users`` + ``bridges`` with user name references
      - Old format: inline ``user`` dict per bridge entry (backward compat)
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

    if _is_new_format(raw):
        return _load_new_format(raw, creds)
    else:
        return _load_old_format(raw, creds)


# ── Migration ────────────────────────────────────────────────────────────────

def migrate_config(config_path: str = "config.yaml") -> bool:
    """Convert old-format config.yaml to new format.

    Returns True if migration was performed, False if already new format.
    Backs up old file to config.yaml.bak before overwriting.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    if _is_new_format(raw):
        return False  # already new format

    # Extract unique users
    users: dict[str, dict] = {}
    for b in raw.get("bridges", []):
        u = b.get("user", {})
        name = u.get("name", "")
        if name and name not in users:
            users[name] = {
                "name": name,
                "telegram_user_id": int(u["telegram_user_id"]),
                "max_user_id": int(u["max_user_id"]),
            }

    # Group bridges by (name, tg_chat_id, max_chat_id), collect user names
    bridge_groups: dict[tuple, dict] = {}
    for b in raw.get("bridges", []):
        key = (b["name"], int(b["telegram_chat_id"]), int(b["max_chat_id"]))
        if key not in bridge_groups:
            bridge_groups[key] = {
                "name": b["name"],
                "telegram_chat_id": int(b["telegram_chat_id"]),
                "max_chat_id": int(b["max_chat_id"]),
                "users": [],
            }
        user_name = b.get("user", {}).get("name", "")
        if user_name and user_name not in bridge_groups[key]["users"]:
            bridge_groups[key]["users"].append(user_name)

    # Build new config dict
    new_config: dict = {
        "users": list(users.values()),
        "bridges": list(bridge_groups.values()),
    }
    # Preserve optional sections
    for section in ("dm_bridge", "admin_bot"):
        if section in raw:
            new_config[section] = raw[section]

    # Backup and write
    backup_path = path.with_suffix(".yaml.bak")
    shutil.copy2(path, backup_path)
    path.write_text(yaml.dump(new_config, allow_unicode=True, default_flow_style=False))

    return True


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
        """Deduplicated list of users."""
        if self.config.users:
            return list(self.config.users)
        # Fallback for old-format configs loaded without top-level users
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
