import json
import os
from pathlib import Path


class MaxSession:
    """Manages MAX session persistence (login_token)."""

    def __init__(self, session_name: str, sessions_dir: str = "sessions"):
        self.path = Path(sessions_dir) / f"{session_name}.max_session"

    def exists(self) -> bool:
        return self.path.exists()

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def load(self) -> str:
        """Returns login_token."""
        return self._read()["login_token"]

    def load_user_id(self) -> int | None:
        """Returns stored user_id, or None if not available."""
        return self._read().get("user_id")

    def load_device_id(self) -> str | None:
        """Returns stored device_id, or None if not available."""
        return self._read().get("device_id")

    def save(self, login_token: str, user_id: int | None = None,
             device_id: str | None = None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict = {"login_token": login_token}
        if user_id is not None:
            payload["user_id"] = user_id
        if device_id is not None:
            payload["device_id"] = device_id
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(self.path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
