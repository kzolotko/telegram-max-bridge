import json
from pathlib import Path


class MaxSession:
    """Manages MAX session persistence (login_token)."""

    def __init__(self, session_name: str, sessions_dir: str = "sessions"):
        self.path = Path(sessions_dir) / f"{session_name}.max_session"

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> str:
        """Returns login_token."""
        data = json.loads(self.path.read_text())
        return data["login_token"]

    def load_user_id(self) -> int | None:
        """Returns stored user_id, or None if not available."""
        data = json.loads(self.path.read_text())
        return data.get("user_id")

    def save(self, login_token: str, user_id: int | None = None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict = {"login_token": login_token}
        if user_id is not None:
            payload["user_id"] = user_id
        self.path.write_text(json.dumps(payload))
