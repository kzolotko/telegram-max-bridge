"""
Interactive authentication script for Telegram and MAX accounts.
Run this BEFORE starting the bridge to create session files.

Usage: python -m src.auth
"""

import asyncio
from pathlib import Path

from pyrogram import Client

from .config import load_config
from .max.session import MaxSession

try:
    from vkmax.client import MaxClient
except ImportError:
    MaxClient = None


async def auth_telegram(session_name: str, api_id: int, api_hash: str, sessions_dir: str):
    """Authenticate a Telegram account interactively."""
    session_path = Path(sessions_dir) / f"{session_name}.session"
    if session_path.exists():
        print(f"  Telegram session '{session_name}' already exists. Skipping.")
        # Verify it works
        client = Client(name=session_name, api_id=api_id, api_hash=api_hash, workdir=sessions_dir)
        await client.start()
        me = await client.get_me()
        print(f"  Verified: @{me.username} ({me.first_name})")
        await client.stop()
        return

    print(f"  Authenticating Telegram session '{session_name}'...")
    print("  You will be asked for your phone number and a verification code.")
    client = Client(name=session_name, api_id=api_id, api_hash=api_hash, workdir=sessions_dir)
    await client.start()
    me = await client.get_me()
    print(f"  Authenticated as @{me.username} ({me.first_name})")
    await client.stop()


async def auth_max(session_name: str, sessions_dir: str):
    """Authenticate a MAX account interactively."""
    session = MaxSession(session_name, sessions_dir)
    if session.exists():
        print(f"  MAX session '{session_name}' already exists. Verifying...")
        login_token = session.load()
        client = MaxClient()
        await client.connect()
        try:
            await client.login_by_token(login_token)
            print(f"  Verified: session is valid.")
            await client.disconnect()
            return
        except Exception:
            print(f"  Session expired. Re-authenticating...")
            await client.disconnect()

    print(f"  Authenticating MAX session '{session_name}'...")
    client = MaxClient()
    await client.connect()

    phone = input("  Enter phone number (e.g. +79991234567): ").strip()
    sms_token = await client.send_code(phone)
    print("  SMS code sent!")

    code = int(input("  Enter SMS code: ").strip())
    account_data = await client.sign_in(sms_token, code)

    login_token = account_data["payload"]["tokenAttrs"]["LOGIN"]["token"]
    session.save(login_token)

    print(f"  MAX session saved as '{session_name}'.")
    await client.disconnect()


async def main():
    print("=" * 60)
    print("Telegram ↔ MAX Bridge — Account Authentication")
    print("=" * 60)

    config = load_config()
    sessions_dir = config.sessions_dir
    Path(sessions_dir).mkdir(parents=True, exist_ok=True)

    # 1. Authenticate listener accounts
    print("\n--- Telegram Listener Account ---")
    await auth_telegram(config.listener_telegram_session, config.api_id, config.api_hash, sessions_dir)

    print("\n--- MAX Listener Account ---")
    await auth_max(config.listener_max_session, sessions_dir)

    # 2. Authenticate user accounts
    for user in config.users:
        print(f"\n--- User: {user.name} ---")

        print(f"  [Telegram]")
        await auth_telegram(user.telegram_session, config.api_id, config.api_hash, sessions_dir)

        print(f"  [MAX]")
        await auth_max(user.max_session, sessions_dir)

    print("\n" + "=" * 60)
    print("All accounts authenticated! Session files saved in:", sessions_dir)
    print("You can now start the bridge: python -m src.main")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
