"""Non-interactive MAX re-authentication, driven through the filesystem.

``sign_in`` must run on the same connection that requested the SMS code, so
the process cannot exit between the two steps.  Instead it parks after
requesting the code and waits for the code to appear in a file — which lets
an operator (or an agent) supply it out of band.

    MAX_DEVICE_TYPE=ANDROID python -m src.max.reauth <session_name> <phone>

Writes progress to ``<sessions_dir>/reauth.status`` and reads the SMS code
from ``<sessions_dir>/reauth.code``.  The existing session file is only
overwritten once a new token has actually been obtained.
"""

import asyncio
import logging
import sys
from pathlib import Path

from .device_profile import current_device_type
from .native_client import NativeMaxAuth
from .session import MaxSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bridge.max.reauth")

CODE_WAIT_TIMEOUT = 600  # seconds to wait for the SMS code to be supplied
POLL_INTERVAL = 2


async def _wait_for_code(code_path: Path) -> str:
    """Block until a code shows up in ``code_path``, then consume it."""
    for _ in range(CODE_WAIT_TIMEOUT // POLL_INTERVAL):
        if code_path.exists():
            code = code_path.read_text().strip()
            if code:
                code_path.unlink(missing_ok=True)
                return code
        await asyncio.sleep(POLL_INTERVAL)
    raise TimeoutError(f"No SMS code appeared in {code_path} within "
                       f"{CODE_WAIT_TIMEOUT}s")


async def reauth(session_name: str, phone: str, sessions_dir: str) -> None:
    status_path = Path(sessions_dir) / "reauth.status"
    code_path = Path(sessions_dir) / "reauth.code"
    code_path.unlink(missing_ok=True)

    def status(text: str) -> None:
        log.info("STATUS: %s", text)
        status_path.write_text(text + "\n")

    device_type = current_device_type()
    status(f"connecting as {device_type}")

    client = NativeMaxAuth()
    await client.connect()
    await client.handshake()

    sms_token = await client.send_code(phone)
    status("code requested — waiting for reauth.code")

    code = await _wait_for_code(code_path)
    status("submitting code")
    account = await client.sign_in(sms_token, int(code))

    login_attrs = account.get("tokenAttrs", {}).get("LOGIN", {})
    if not login_attrs.get("token"):
        if account.get("passwordChallenge"):
            status("2FA password required — aborting, no session changed")
            raise RuntimeError(
                "MAX asked for a 2FA password. Re-run the /authmax flow from "
                "the admin bot so the password stays with its owner."
            )
        raise RuntimeError(f"No login token in response: {list(account.keys())}")

    profile = account.get("profile", {})
    user_id = profile.get("userId") or profile.get("id") or profile.get("sn")
    user_id = int(user_id) if user_id else None

    # Only now is the old session replaced.
    MaxSession(session_name, sessions_dir).save(
        login_attrs["token"], user_id=user_id, device_id=client.device_id,
    )
    status(f"OK — session saved as {device_type}, user_id={user_id}")
    await client.close()


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    session_name, phone = sys.argv[1], sys.argv[2]
    sessions_dir = sys.argv[3] if len(sys.argv) > 3 else "sessions"
    asyncio.run(reauth(session_name, phone, sessions_dir))


if __name__ == "__main__":
    main()
