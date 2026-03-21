"""
One-time authentication for the E2E test Telegram session.

Creates a separate Pyrogram session (tg_e2e_{user_name}) that the E2E
test client uses.  This session coexists with the bridge's own session
(tg_{user_name}) — Telegram treats them as separate "devices".

Usage:
    python -m tests.e2e.auth_e2e

You only need to run this once.  After that, the session file is saved
in the sessions/ directory and reused by tests automatically.
"""

import asyncio
import sys
from pathlib import Path

import yaml
from pyrogram import Client


def _load_e2e_user_name() -> str:
    """Read user_name from e2e_config.yaml."""
    config_path = Path(__file__).resolve().parent / "e2e_config.yaml"
    if not config_path.exists():
        print(f"ERROR: {config_path} not found.")
        print(f"Copy e2e_config.example.yaml → e2e_config.yaml and fill in the values.")
        sys.exit(1)

    with open(config_path) as f:
        tc = yaml.safe_load(f) or {}

    user_name = tc.get("user_name")
    if not user_name:
        print("ERROR: user_name is required in e2e_config.yaml")
        sys.exit(1)
    return user_name


async def main():
    print("=" * 60)
    print("E2E Test — Telegram Session Authentication")
    print("=" * 60)

    # Load credentials
    from src.config import load_credentials

    creds = load_credentials()
    user_name = _load_e2e_user_name()
    sessions_dir = "sessions"

    session_name = f"tg_e2e_{user_name}"
    session_path = Path(sessions_dir) / f"{session_name}.session"

    if session_path.exists():
        print(f"\nSession '{session_name}' already exists at {session_path}")
        print("Verifying...")
        client = Client(
            name=session_name,
            api_id=creds["api_id"],
            api_hash=creds["api_hash"],
            workdir=sessions_dir,
        )
        await client.start()
        me = await client.get_me()
        print(f"  ✅ Verified: @{me.username or me.first_name} (ID: {me.id})")
        await client.stop()
        print("\nSession is valid. You can run E2E tests:")
        print("  pytest tests/e2e/ -v")
        return

    print(f"\nCreating E2E test session: {session_name}")
    print("This is a SEPARATE Telegram session from the bridge.")
    print("It will appear as a new device in your Telegram settings.")
    print()
    print("You will be asked for your phone number and a verification code.")
    print()

    Path(sessions_dir).mkdir(parents=True, exist_ok=True)

    client = Client(
        name=session_name,
        api_id=creds["api_id"],
        api_hash=creds["api_hash"],
        workdir=sessions_dir,
    )
    await client.start()
    me = await client.get_me()
    print(f"\n  ✅ Authenticated: @{me.username or me.first_name} (ID: {me.id})")
    await client.stop()

    print(f"\nSession saved: {session_path}")
    print("\nYou can now run E2E tests:")
    print("  pytest tests/e2e/ -v")


if __name__ == "__main__":
    asyncio.run(main())
