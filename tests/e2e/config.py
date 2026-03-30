"""
E2E test configuration loader.

Reads test-specific settings from tests/e2e/e2e_config.yaml and combines
them with existing bridge credentials and MAX session data.

Supports an optional second user for two-user tests (reactions, deletes,
sender routing).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class UserCreds:
    """Credentials for one E2E test user."""
    name: str
    max_login_token: str
    max_device_id: str
    max_test_device_id: str
    tg_e2e_session: str  # Pyrogram session name


@dataclass
class E2EConfig:
    """All settings needed to run E2E tests."""

    # Telegram
    api_id: int
    api_hash: str
    tg_chat_id: int

    # MAX
    max_chat_id: int

    # Primary user
    primary: UserCreds

    # Second user (optional — enables two-user tests)
    secondary: UserCreds | None

    # DM bridge (auto-detected from config.yaml dm_bridge section)
    dm_bot_id: int | None  # TG bot user ID (extracted from bot_token)
    dm_max_chat_id: int | None  # MAX user ID of primary user (DM target)

    # Paths
    sessions_dir: str

    # Test settings
    timeout: float

    # Backwards-compat aliases
    @property
    def user_name(self) -> str:
        return self.primary.name

    @property
    def max_login_token(self) -> str:
        return self.primary.max_login_token

    @property
    def max_test_device_id(self) -> str:
        return self.primary.max_test_device_id

    @property
    def tg_e2e_session(self) -> str:
        return self.primary.tg_e2e_session


# Fixed UUIDs for test MAX clients — avoids creating a new "device" entry
# on the MAX server every run.
_DEFAULT_TEST_DEVICE_ID = "e2e00000-cafe-4000-a000-000000000001"
_DEFAULT_TEST_DEVICE_ID_2 = "e2e00000-cafe-4000-a000-000000000002"

_CONFIG_PATH = Path("config/e2e_config.yaml")
_EXAMPLE_PATH = Path("config/e2e_config.example.yaml")


def _load_user_creds(
    user_name: str,
    sessions_dir: str,
    default_device_id: str,
    device_id_override: str | None = None,
) -> UserCreds:
    """Load MAX session + build TG session name for a single user."""
    from src.max.session import MaxSession

    max_session = MaxSession(f"max_{user_name}", sessions_dir)
    if not max_session.exists():
        raise FileNotFoundError(
            f"MAX session for '{user_name}' not found in {sessions_dir}/.\n"
            f"Run 'python -m src.auth' first."
        )

    max_login_token = max_session.load()
    max_device_id = max_session.load_device_id()
    if not max_device_id:
        raise RuntimeError(
            f"No device_id in MAX session for '{user_name}'. Re-authenticate."
        )

    tg_e2e_session = f"tg_e2e_{user_name}"

    return UserCreds(
        name=user_name,
        max_login_token=max_login_token,
        max_device_id=max_device_id,
        max_test_device_id=device_id_override or default_device_id,
        tg_e2e_session=tg_e2e_session,
    )


def load_e2e_config() -> E2EConfig:
    """Load E2E configuration from e2e_config.yaml + bridge credentials."""

    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"{_CONFIG_PATH} not found.\n"
            f"Copy {_EXAMPLE_PATH.name} → e2e_config.yaml and fill in the values."
        )

    with open(_CONFIG_PATH) as f:
        tc = yaml.safe_load(f) or {}

    user_name = tc["user_name"]
    tg_chat_id = int(tc["tg_chat_id"])
    max_chat_id = int(tc["max_chat_id"])
    timeout = float(tc.get("timeout", 15))
    sessions_dir = tc.get("sessions_dir", "sessions")

    # ── Telegram credentials ─────────────────────────────────────────────────
    from src.config import load_credentials
    creds = load_credentials()

    # ── Primary user ─────────────────────────────────────────────────────────
    primary = _load_user_creds(
        user_name, sessions_dir, _DEFAULT_TEST_DEVICE_ID,
        tc.get("max_test_device_id"),
    )

    # Verify TG E2E session exists
    tg_e2e_path = Path(sessions_dir) / f"{primary.tg_e2e_session}.session"
    if not tg_e2e_path.exists():
        raise FileNotFoundError(
            f"TG E2E session not found: {tg_e2e_path}\n"
            f"Run './bridge.sh test-auth' to create it."
        )

    # ── Second user (optional) ───────────────────────────────────────────────
    secondary: UserCreds | None = None
    second_user_name = tc.get("second_user_name")
    if second_user_name:
        try:
            secondary = _load_user_creds(
                second_user_name, sessions_dir, _DEFAULT_TEST_DEVICE_ID_2,
                tc.get("max_test_device_id_2"),
            )
            tg_e2e_path_2 = Path(sessions_dir) / f"{secondary.tg_e2e_session}.session"
            if not tg_e2e_path_2.exists():
                import logging
                logging.getLogger("e2e.config").warning(
                    "TG E2E session for '%s' not found (%s). "
                    "Two-user tests will be skipped. "
                    "Run './bridge.sh test-auth --user %s' to create it.",
                    second_user_name, tg_e2e_path_2, second_user_name,
                )
                secondary = None
        except Exception as exc:
            import logging
            logging.getLogger("e2e.config").warning(
                "Could not load second user '%s': %s. Two-user tests will be skipped.",
                second_user_name, exc,
            )

    # ── DM bridge (auto-detect from config.yaml) ────────────────────────────
    dm_bot_id: int | None = None
    dm_max_chat_id: int | None = None

    try:
        from src.config import load_config as _load_bridge_config
        bridge_cfg = _load_bridge_config()
        if bridge_cfg.dm_bridge:
            # Bot ID = the number before ":" in the token
            dm_bot_id = int(bridge_cfg.dm_bridge.bot_token.split(":")[0])
            # DM target = primary user's MAX user ID (mary sends DMs to kzolotko)
            # Find the primary user in the bridge config by matching e2e user_name
            for entry in bridge_cfg.bridges:
                if entry.user.name == user_name:
                    dm_max_chat_id = entry.user.max_user_id
                    break
    except Exception as exc:
        import logging
        logging.getLogger("e2e.config").warning(
            "Could not auto-detect DM bridge settings: %s. DM tests will be skipped.", exc,
        )

    return E2EConfig(
        api_id=creds["api_id"],
        api_hash=creds["api_hash"],
        tg_chat_id=tg_chat_id,
        max_chat_id=max_chat_id,
        primary=primary,
        secondary=secondary,
        dm_bot_id=dm_bot_id,
        dm_max_chat_id=dm_max_chat_id,
        sessions_dir=sessions_dir,
        timeout=timeout,
    )
