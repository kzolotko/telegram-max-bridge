"""
Interactive authentication script for Telegram and MAX accounts.
Run this BEFORE starting the bridge to create session files.

Usage: python -m src.auth
"""

import asyncio
from pathlib import Path

from pyrogram import Client

from .config import load_config, ConfigLookup
from .max.session import MaxSession
from .max.native_client import NativeMaxAuth
from .max.bridge_client import BridgeMaxClient


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
        device_id = session.load_device_id()
        if not device_id:
            print("  No device_id in session — re-authenticating...")
        else:
            try:
                client = BridgeMaxClient(token=login_token, device_id=device_id)
                await client.connect_and_login()
                print(f"  Verified: session is valid.")
                await client.disconnect()
                return
            except Exception as e:
                print(f"  Session expired ({e}). Re-authenticating...")

    print(f"  Authenticating MAX session '{session_name}'...")
    print("  Using native TCP/SSL protocol (device_type=DESKTOP)")

    client = NativeMaxAuth()
    await client.connect()
    hello = await client.handshake()

    phone = input("  Enter phone number (e.g. +79991234567): ").strip()

    sms_token = await client.send_code(phone)
    print("  SMS code sent!")

    code = input("  Enter SMS code: ").strip()
    account_data = await client.sign_in(sms_token, int(code))

    # Handle 2FA password challenge
    password_challenge = account_data.get("passwordChallenge")
    login_attrs = account_data.get("tokenAttrs", {}).get("LOGIN", {})

    if password_challenge and not login_attrs:
        raise RuntimeError(
            "MAX account has 2FA enabled. "
            "2FA password auth is not yet supported in native client."
        )

    login_token = login_attrs.get("token")
    if not login_token:
        raise RuntimeError(
            f"No login token in response. Keys: {list(account_data.keys())}"
        )

    profile = account_data.get("profile", {})
    user_id = (
        profile.get("userId")
        or profile.get("id")
        or profile.get("sn")
    )
    if user_id:
        user_id = int(user_id)
        print(f"  MAX user ID: {user_id}")
    session.save(login_token, user_id=user_id, device_id=client.device_id)

    print(f"  MAX session saved as '{session_name}'.")
    await client.close()


async def main():
    print("=" * 60)
    print("Telegram ↔ MAX Bridge — Account Authentication")
    print("=" * 60)

    config = load_config()
    sessions_dir = config.sessions_dir
    Path(sessions_dir).mkdir(parents=True, exist_ok=True)

    users = ConfigLookup(config).get_unique_users()

    for user in users:
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
