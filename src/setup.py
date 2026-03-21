"""
Interactive setup wizard for Telegram ↔ MAX Bridge.

Three modes:
  python -m src.setup               — full wizard (credentials + users + bridges)
  python -m src.setup credentials   — set up Telegram API credentials only (one-time)
  python -m src.setup bridges       — configure users and chat bridges (requires credentials)

Usage: python -m src.setup [credentials|bridges]
"""

import asyncio
import re
import sys
from pathlib import Path

import aiohttp
import yaml
from pyrogram import Client
from pyrogram.enums import ChatType

from .max.native_client import NativeMaxAuth
from .max.bridge_client import BridgeMaxClient
from .max.session import MaxSession
from .config import load_credentials


SEP = "─" * 60

CREDENTIALS_FILE = "credentials.yaml"
CONFIG_FILE = "config.yaml"
SESSIONS_DIR = "sessions"


# ── Helpers ───────────────────────────────────────────────────────────────────

def prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"  {label}{suffix}: ").strip()
        if not value and default is not None:
            return default
        if value:
            return value
        print("  (required)")


def confirm(question: str) -> bool:
    answer = input(f"  {question} [y/N]: ").strip().lower()
    return answer in ("y", "yes", "д", "да")


def print_section(title: str):
    print(f"\n{SEP}")
    print(title)
    print(SEP)


def parse_max_chat_id(text: str) -> int:
    """Extract MAX chat ID from a URL or raw number."""
    text = text.strip()
    # https://web.max.ru/#/chats/@chat/-72099589405396
    m = re.search(r'/@chat/(-?\d+)', text)
    if m:
        return int(m.group(1))
    # Plain large negative number
    m = re.search(r'(-?\d{10,})', text)
    if m:
        return int(m.group(1))
    raise ValueError(
        f"Cannot parse MAX chat ID from: {text!r}\n"
        f"  Expected a URL like https://web.max.ru/#/chats/@chat/-72099589405396\n"
        f"  or a plain negative number like -72099589405396"
    )


# ── Geo-check ────────────────────────────────────────────────────────────────

async def check_geo() -> bool:
    """Returns True if Russian IP, False otherwise."""
    print_section("Checking your IP location")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.ipify.org?format=json",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                ip = (await r.json()).get("ip", "?")

            async with session.get(
                f"https://ipapi.co/{ip}/json/",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                geo = await r.json()

        country_code = geo.get("country_code", "?")
        country = geo.get("country_name", "?")
        city = geo.get("city", "?")
        print(f"  IP: {ip}  |  Country: {country} ({country_code})  |  City: {city}")

        if country_code != "RU":
            print()
            print("  ⚠️  WARNING: Not a Russian IP address.")
            print("  MAX requires a Russian IP for phone-based authentication.")
            print("  Options:")
            print("    1. Disable VPN and retry")
            print("    2. Enable a VPN with a Russian exit node")
            print("    3. Run this on a machine with a Russian IP")
            print()
            if not confirm("Continue anyway? (auth may fail)"):
                sys.exit(1)
            return False
        else:
            print("  ✅ Russian IP — MAX phone auth should work.")
            return True
    except Exception as e:
        print(f"  ⚠️  Could not determine IP location: {e}")
        if not confirm("Continue without geo check?"):
            sys.exit(1)
        return True


# ── Mode: credentials ────────────────────────────────────────────────────────

async def setup_credentials():
    """Set up Telegram API credentials (api_id + api_hash). One-time."""
    print_section("Telegram API Credentials")
    print("  Get these from https://my.telegram.org → API development tools")
    print()

    creds_path = Path(CREDENTIALS_FILE)
    if creds_path.exists():
        print(f"  ⚠️  {CREDENTIALS_FILE} already exists.")
        if not confirm(f"Overwrite {CREDENTIALS_FILE}?"):
            print("  Keeping existing credentials.")
            return

    api_id = int(prompt("api_id (number)"))
    api_hash = prompt("api_hash (32-char hex string)")

    creds = {
        "api_id": api_id,
        "api_hash": api_hash,
    }
    creds_path.write_text(
        "# Telegram API credentials — obtained once from https://my.telegram.org\n"
        "# This file is created by: python -m src.setup credentials\n"
        + yaml.dump(creds, default_flow_style=False)
    )
    print(f"\n  ✅ Credentials saved to {CREDENTIALS_FILE}")


# ── Mode: bridges (users + chat configuration) ───────────────────────────────

async def setup_bridges():
    """Configure user accounts and chat bridges. Requires credentials.yaml."""

    # Load credentials (must exist)
    try:
        creds = load_credentials()
    except FileNotFoundError as e:
        print(f"\n  ❌ {e}")
        print("  Run 'python -m src.setup credentials' first.")
        sys.exit(1)

    api_id = creds["api_id"]
    api_hash = creds["api_hash"]

    config_path = Path(CONFIG_FILE)
    if config_path.exists():
        print(f"\n  ⚠️  {CONFIG_FILE} already exists.")
        if not confirm(f"Overwrite {CONFIG_FILE}?"):
            print("  Keeping existing config.")
            return

    # Geo-check (needed for MAX phone auth)
    await check_geo()

    # Authenticate users
    print_section("User Accounts")
    print("  Configure the user account(s) that will power the bridge.")
    print("  Each user needs a Telegram and MAX account.")
    print()

    users = []
    while True:
        user = await auth_one_user(api_id, api_hash, SESSIONS_DIR)
        users.append(user)
        print()
        if not confirm("Add another user?"):
            break

    # Configure bridges
    bridges = await collect_bridges(users, api_id, api_hash, SESSIONS_DIR)

    # Write config.yaml (bridges only — no credentials)
    print_section("Writing config.yaml")
    write_config(bridges, CONFIG_FILE)

    print()
    print("=" * 60)
    print("Setup complete!")
    print()
    print("Start the bridge:")
    print("  python -m src")
    print("=" * 60)


# ── User authentication ──────────────────────────────────────────────────────

async def auth_one_user(
    api_id: int,
    api_hash: str,
    sessions_dir: str,
) -> dict:
    """Authenticates one user for both TG and MAX. Returns user dict."""
    print()
    name = prompt("User name (latin letters/digits, used for session file names, e.g. alice)")
    if not re.match(r'^[a-z0-9_]+$', name):
        print("  ⚠️  Use only lowercase letters, digits and underscores.")
        return await auth_one_user(api_id, api_hash, sessions_dir)

    sessions_path = Path(sessions_dir)
    sessions_path.mkdir(parents=True, exist_ok=True)

    # ── Telegram ──────────────────────────────────────────────────────────────
    print()
    print(f"  [Telegram] Authenticating {name}...")
    tg_session_name = f"tg_{name}"
    tg_session_path = sessions_path / f"{tg_session_name}.session"

    tg_client = Client(
        name=tg_session_name,
        api_id=api_id,
        api_hash=api_hash,
        workdir=sessions_dir,
    )
    if tg_session_path.exists():
        print(f"  Telegram session already exists — verifying...")
    else:
        print("  Follow the prompts to authenticate your Telegram account:")

    await tg_client.start()
    me = await tg_client.get_me()
    telegram_user_id = me.id
    print(f"  ✅ Telegram: @{me.username or me.first_name} (ID: {telegram_user_id})")

    # ── MAX ───────────────────────────────────────────────────────────────────
    print()
    print(f"  [MAX] Authenticating {name}...")
    max_session = MaxSession(f"max_{name}", sessions_dir)

    if max_session.exists():
        print(f"  MAX session already exists — verifying...")
        try:
            login_token = max_session.load()
            device_id = max_session.load_device_id()
            if not device_id:
                raise RuntimeError("No device_id in session")
            max_client = BridgeMaxClient(token=login_token, device_id=device_id)
            await max_client.connect_and_login()
            max_user_id = max_session.load_user_id()
            await max_client.disconnect()
            print(f"  ✅ MAX: session valid (ID: {max_user_id})")
        except Exception as e:
            print(f"  ⚠️  MAX session verification failed: {e}")
            print("  Options:")
            print("    1. Keep existing session (skip re-auth, use it as-is)")
            print("    2. Re-authenticate via phone+SMS")
            choice = prompt("Choose [1/2]", default="1")
            if choice == "2":
                max_user_id = await _do_max_auth(name, sessions_dir)
            else:
                max_user_id = max_session.load_user_id()
                print(f"  Using existing session. Stored user ID: {max_user_id}")
    else:
        max_user_id = await _do_max_auth(name, sessions_dir)

    await tg_client.stop()

    return {
        "name": name,
        "telegram_user_id": telegram_user_id,
        "max_user_id": max_user_id,
        "_tg_client": None,  # already stopped
    }


async def _do_max_auth(name: str, sessions_dir: str) -> int:
    max_session = MaxSession(f"max_{name}", sessions_dir)

    print("  Using native TCP/SSL protocol (device_type=DESKTOP)")
    client = NativeMaxAuth()
    await client.connect()
    await client.handshake()

    phone = prompt("MAX phone number (e.g. +79991234567)")
    sms_token = await client.send_code(phone)

    print("  SMS code sent!")
    code = prompt("Enter SMS code")
    account_data = await client.sign_in(sms_token, int(code))

    # Handle 2FA
    password_challenge = account_data.get("passwordChallenge")
    login_attrs = account_data.get("tokenAttrs", {}).get("LOGIN", {})

    if password_challenge and not login_attrs:
        await client.close()
        raise RuntimeError(
            "MAX account has 2FA enabled. "
            "2FA password auth is not yet supported."
        )

    login_token = login_attrs.get("token")
    if not login_token:
        await client.close()
        raise RuntimeError(
            f"No login token in response. Keys: {list(account_data.keys())}"
        )

    profile = account_data.get("profile", {})
    max_user_id = _extract_max_user_id(profile, max_session)

    max_session.save(login_token, user_id=max_user_id, device_id=client.device_id)
    await client.close()

    if max_user_id:
        print(f"  ✅ MAX: authenticated (ID: {max_user_id})")
    else:
        print("  ✅ MAX: authenticated (user ID not found in response — check session file)")

    return max_user_id


def _extract_max_user_id(profile: dict, session: MaxSession) -> int | None:
    """Try to extract user_id from MAX profile response."""
    user_id = (
        profile.get("userId")
        or profile.get("id")
        or profile.get("sn")
    )
    if user_id:
        return int(user_id)
    # Fall back to previously stored user_id in session
    return session.load_user_id()


# ── Bridge configuration ─────────────────────────────────────────────────────

async def collect_bridges(
    users: list[dict],
    api_id: int,
    api_hash: str,
    sessions_dir: str,
) -> list[dict]:
    print_section("Configure Chat Bridges")

    # Load TG dialogs once (using first user's client)
    primary = users[0]
    tg_client = Client(
        name=f"tg_{primary['name']}",
        api_id=api_id,
        api_hash=api_hash,
        workdir=sessions_dir,
    )
    await tg_client.start()

    print(f"\n  Loading Telegram chats for {primary['name']}...")
    tg_chats = []
    async for dialog in tg_client.get_dialogs():
        if dialog.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            tg_chats.append(dialog.chat)

    await tg_client.stop()

    bridges = []
    while True:
        print()
        bridge_name = prompt("Bridge name (e.g. team-general)")

        # Pick TG chat
        print()
        print("  Available Telegram groups:")
        for i, chat in enumerate(tg_chats):
            print(f"    {i + 1:2}. {chat.title}  (ID: {chat.id})")
        print()
        while True:
            try:
                idx = int(prompt("Select Telegram chat number")) - 1
                if 0 <= idx < len(tg_chats):
                    break
                print(f"  Enter a number between 1 and {len(tg_chats)}")
            except ValueError:
                print("  Enter a number")
        telegram_chat_id = tg_chats[idx].id
        print(f"  ✅ Telegram chat: {tg_chats[idx].title} (ID: {telegram_chat_id})")

        # Pick MAX chat
        print()
        print("  Open https://web.max.ru → go to the desired chat")
        print("  The URL will look like: https://web.max.ru/#/chats/@chat/-72099589405396")
        while True:
            try:
                url_or_id = prompt("Paste the MAX chat URL (or just the numeric ID)")
                max_chat_id = parse_max_chat_id(url_or_id)
                print(f"  ✅ MAX chat ID: {max_chat_id}")
                break
            except ValueError as e:
                print(f"  ❌ {e}")

        # Pick user for this bridge
        if len(users) == 1:
            user = users[0]
        else:
            print()
            print("  Which user should handle this bridge?")
            for i, u in enumerate(users):
                print(f"    {i + 1}. {u['name']}")
            while True:
                try:
                    idx = int(prompt("Select user number")) - 1
                    if 0 <= idx < len(users):
                        break
                except ValueError:
                    pass
                print(f"  Enter a number between 1 and {len(users)}")
            user = users[idx]

        bridges.append({
            "name": bridge_name,
            "telegram_chat_id": telegram_chat_id,
            "max_chat_id": max_chat_id,
            "user": user,
        })

        print(f"\n  Bridge '{bridge_name}' configured.")
        if not confirm("Add another bridge?"):
            break

    return bridges


def write_config(bridges: list[dict], output_path: str = CONFIG_FILE):
    """Write config.yaml with bridges only (no credentials)."""
    config = {"bridges": []}

    for b in bridges:
        user = b["user"]
        config["bridges"].append({
            "name": b["name"],
            "telegram_chat_id": b["telegram_chat_id"],
            "max_chat_id": b["max_chat_id"],
            "user": {
                "name": user["name"],
                "telegram_user_id": user["telegram_user_id"],
                "max_user_id": user["max_user_id"],
            },
        })

    path = Path(output_path)
    path.write_text(yaml.dump(config, allow_unicode=True, default_flow_style=False))
    print(f"\n  ✅ Config written to: {path.resolve()}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    args = sys.argv[1:] if len(sys.argv) > 1 else []
    mode = args[0] if args else None

    print("=" * 60)
    print("Telegram ↔ MAX Bridge — Setup")
    print("=" * 60)

    if mode == "credentials":
        await setup_credentials()
    elif mode == "bridges":
        await setup_bridges()
    elif mode is None:
        # Full wizard: credentials first, then bridges
        if not Path(CREDENTIALS_FILE).exists():
            await setup_credentials()
        else:
            print(f"\n  ✅ {CREDENTIALS_FILE} already exists.")
            if confirm("Re-configure credentials?"):
                await setup_credentials()

        await setup_bridges()
    else:
        print(f"\n  Unknown mode: {mode}")
        print("  Usage: python -m src.setup [credentials|bridges]")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
