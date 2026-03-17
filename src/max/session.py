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

    def save(self, login_token: str):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "login_token": login_token,
        }))
