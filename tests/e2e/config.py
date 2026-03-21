"""
E2E test configuration loader.

Reads test-specific settings from tests/e2e/e2e_config.yaml and combines
them with existing bridge credentials and MAX session data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import yaml


@dataclass
class E2EConfig:
    """All settings needed to run E2E tests."""

    # Telegram
    api_id: int
    api_hash: str
    tg_chat_id: int

    # MAX
    max_login_token: str
    max_device_id: str  # original device_id from user's session
    max_test_device_id: str  # separate device_id for test client
    max_chat_id: int

    # User
    user_name: str

    # Paths
    sessions_dir: str

    # Test settings
    timeout: float

    @property
    def tg_e2e_session(self) -> str:
        """Pyrogram session name for E2E test client."""
        return f"tg_e2e_{self.user_name}"


# Fixed UUID for the test MAX client — avoids creating a new "device" entry
# on the MAX server every run.  Override via e2e_config.yaml if needed.
_DEFAULT_TEST_DEVICE_ID = "e2e00000-test-4000-a000-000000000001"

_CONFIG_PATH = Path(__file__).resolve().parent / "e2e_config.yaml"
_EXAMPLE_PATH = Path(__file__).resolve().parent / "e2e_config.example.yaml"


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
    # Import from project source (tests run from repo root)
    from src.config import load_credentials

    creds = load_credentials()

    # ── MAX session ──────────────────────────────────────────────────────────
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

    # Test client gets its own device_id so it doesn't collide with the bridge
    max_test_device_id = tc.get("max_test_device_id") or _DEFAULT_TEST_DEVICE_ID

    # ── Verify TG E2E session exists ─────────────────────────────────────────
    tg_e2e_session_path = Path(sessions_dir) / f"tg_e2e_{user_name}.session"
    if not tg_e2e_session_path.exists():
        raise FileNotFoundError(
            f"TG E2E session not found: {tg_e2e_session_path}\n"
            f"Run 'python -m tests.e2e.auth_e2e' to create it."
        )

    return E2EConfig(
        api_id=creds["api_id"],
        api_hash=creds["api_hash"],
        tg_chat_id=tg_chat_id,
        max_login_token=max_login_token,
        max_device_id=max_device_id,
        max_test_device_id=max_test_device_id,
        max_chat_id=max_chat_id,
        user_name=user_name,
        sessions_dir=sessions_dir,
        timeout=timeout,
    )
