"""
Interactive setup wizard for Telegram ↔ MAX Bridge.

Three modes:
  python -m src.setup               — full wizard (credentials + users + bridges)
  python -m src.setup credentials   — set up Telegram API credentials only (one-time)
  python -m src.setup bridges       — configure users and chat bridges (requires credentials)

Usage: python -m src.setup [credentials|bridges]
"""

import asyncio
import logging
import re
import sys
from pathlib import Path

log = logging.getLogger("bridge.setup")

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

def _load_existing_config() -> tuple[list[dict], list[dict]]:
    """Load existing config.yaml and return (users, bridges) in setup dict format.

    Returns empty lists if file doesn't exist or can't be parsed.
    """
    config_path = Path(CONFIG_FILE)
    if not config_path.exists():
        return [], []

    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except Exception:
        return [], []

    bridges = []
    seen_users: dict[str, dict] = {}  # name → user dict

    for b in raw.get("bridges", []):
        u = b.get("user", {})
        user_name = u.get("name", "")
        user_dict = {
            "name": user_name,
            "telegram_user_id": u.get("telegram_user_id"),
            "max_user_id": u.get("max_user_id"),
        }
        if user_name and user_name not in seen_users:
            seen_users[user_name] = user_dict

        bridges.append({
            "name": b.get("name", ""),
            "telegram_chat_id": b.get("telegram_chat_id"),
            "max_chat_id": b.get("max_chat_id"),
            "user": user_dict,
        })

    return list(seen_users.values()), bridges


async def setup_bridges():
    """Configure user accounts and chat bridges. Requires credentials.yaml."""

    # Load credentials (must exist)
    try:
        creds = load_credentials()
    except FileNotFoundError as e:
        print(f"\n  ❌ {e}")
        print("  Run './bridge.sh setup credentials' first.")
        sys.exit(1)

    api_id = creds["api_id"]
    api_hash = creds["api_hash"]

    existing_users, existing_bridges = _load_existing_config()

    if existing_bridges:
        print(f"\n  Found existing config with {len(existing_bridges)} bridge(s) "
              f"and {len(existing_users)} user(s):")
        for b in existing_bridges:
            print(f"    • {b['name']}  ({b['user']['name']})")
        print()
        print("  Options:")
        print("    1. Add new bridge(s) to existing config")
        print("    2. Add new user to existing bridge")
        print("    3. Start from scratch (overwrite)")
        print("    4. Cancel")
        choice = prompt("Choose [1/2/3/4]", default="1")

        if choice == "4":
            print("  Cancelled.")
            return
        elif choice == "3":
            existing_users = []
            existing_bridges = []
        elif choice == "2":
            await _add_user_to_bridge(
                api_id, api_hash, existing_users, existing_bridges,
            )
            print_section("Writing config.yaml")
            write_config(existing_bridges, CONFIG_FILE)
            _print_done()
            return
        # choice == "1" falls through to normal flow with existing data preserved

    # Authenticate users — offer to reuse existing ones
    users = list(existing_users)  # start with existing
    if users:
        print_section("User Accounts")
        print("  Existing users:")
        for u in users:
            print(f"    • {u['name']} (TG: {u['telegram_user_id']}, MAX: {u['max_user_id']})")
        print()
        if confirm("Add a new user?"):
            while True:
                user = await auth_one_user(api_id, api_hash, SESSIONS_DIR)
                users.append(user)
                print()
                if not confirm("Add another user?"):
                    break
    else:
        print_section("User Accounts")
        print("  Configure the user account(s) that will power the bridge.")
        print("  Each user needs a Telegram and MAX account.")
        print()
        while True:
            user = await auth_one_user(api_id, api_hash, SESSIONS_DIR)
            users.append(user)
            print()
            if not confirm("Add another user?"):
                break

    # Configure new bridges
    new_bridges = await collect_bridges(users, api_id, api_hash, SESSIONS_DIR)
    all_bridges = existing_bridges + new_bridges

    # Write config.yaml
    print_section("Writing config.yaml")
    write_config(all_bridges, CONFIG_FILE)
    _print_done()


async def _add_user_to_bridge(
    api_id: int,
    api_hash: str,
    existing_users: list[dict],
    existing_bridges: list[dict],
):
    """Add a new user to an existing bridge (multi-user for same chat pair)."""
    print_section("Add User to Existing Bridge")

    # Pick which bridge
    unique_pairs: list[dict] = []
    seen = set()
    for b in existing_bridges:
        key = (b["telegram_chat_id"], b["max_chat_id"])
        if key not in seen:
            seen.add(key)
            unique_pairs.append(b)

    print("  Select the bridge to add a user to:")
    for i, b in enumerate(unique_pairs):
        current_users = [
            eb["user"]["name"]
            for eb in existing_bridges
            if eb["telegram_chat_id"] == b["telegram_chat_id"]
            and eb["max_chat_id"] == b["max_chat_id"]
        ]
        print(f"    {i + 1}. {b['name']}  (users: {', '.join(current_users)})")
    print()

    while True:
        try:
            idx = int(prompt("Select bridge number")) - 1
            if 0 <= idx < len(unique_pairs):
                break
        except ValueError:
            pass
        print(f"  Enter a number between 1 and {len(unique_pairs)}")

    target = unique_pairs[idx]

    # Authenticate new user
    print()
    user = await auth_one_user(api_id, api_hash, SESSIONS_DIR)

    # Check if this user already exists for this bridge
    for eb in existing_bridges:
        if (eb["telegram_chat_id"] == target["telegram_chat_id"]
                and eb["max_chat_id"] == target["max_chat_id"]
                and eb["user"]["name"] == user["name"]):
            print(f"  ⚠️  User '{user['name']}' is already configured for this bridge.")
            return

    # Verify membership in both chats
    print()
    print(f"  Verifying chat membership for {user['name']}...")
    tg_ok = await _verify_tg_membership(
        user["name"], api_id, api_hash, SESSIONS_DIR, target["telegram_chat_id"],
    )
    max_ok = await _verify_max_membership(
        user["name"], SESSIONS_DIR, target["max_chat_id"],
    )

    if not tg_ok or not max_ok:
        if not confirm("Continue anyway? (bridge may not work correctly for this user)"):
            print("  Skipped.")
            return

    # Add new bridge entry (same chat pair, new user)
    existing_bridges.append({
        "name": target["name"],
        "telegram_chat_id": target["telegram_chat_id"],
        "max_chat_id": target["max_chat_id"],
        "user": user,
    })
    existing_users.append(user)
    print(f"  ✅ User '{user['name']}' added to bridge '{target['name']}'")


async def _verify_tg_membership(
    user_name: str,
    api_id: int,
    api_hash: str,
    sessions_dir: str,
    chat_id: int,
) -> bool:
    """Check if user is a member of the Telegram chat. Returns True if OK."""
    try:
        tg_client = Client(
            name=f"tg_{user_name}",
            api_id=api_id,
            api_hash=api_hash,
            workdir=sessions_dir,
        )
        await tg_client.start()
        # Warm up Pyrogram's peer cache — without this, get_chat() fails
        # with "Peer id invalid" for chats not yet in the local cache.
        async for _ in tg_client.get_dialogs():
            pass
        chat = await tg_client.get_chat(chat_id)
        await tg_client.stop()
        print(f"  ✅ Telegram: {user_name} is a member of '{chat.title}'")
        return True
    except Exception as e:
        print(f"  ❌ Telegram: {user_name} is NOT a member of chat {chat_id} ({e})")
        return False


async def _verify_max_membership(
    user_name: str,
    sessions_dir: str,
    chat_id: int,
) -> bool:
    """Check if user is a member of the MAX chat. Returns True if OK."""
    try:
        max_session = MaxSession(f"max_{user_name}", sessions_dir)
        login_token = max_session.load()
        device_id = max_session.load_device_id()
        if not login_token or not device_id:
            print(f"  ⚠️  MAX: no session for {user_name} — cannot verify membership")
            return True  # can't check, assume OK

        max_client = BridgeMaxClient(token=login_token, device_id=device_id)
        await max_client.connect_and_login()

        # Try to get chat info — will fail if user is not a member
        chats = await max_client.inner.get_chats([chat_id])
        await max_client.disconnect()

        if chats:
            print(f"  ✅ MAX: {user_name} is a member of '{chats[0].title}'")
            return True
        else:
            print(f"  ❌ MAX: {user_name} is NOT a member of chat {chat_id}")
            return False
    except Exception as e:
        print(f"  ❌ MAX: could not verify membership for {user_name} in chat {chat_id} ({e})")
        return False


def _print_done():
    print()
    print("=" * 60)
    print("Setup complete!")
    print()
    print("Start the bridge:")
    print("  ./bridge.sh start")
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
    await tg_client.stop()

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
    hello = await client.handshake()
    hello_p = hello.get("payload", {})
    phone_auth = hello_p.get("phone-auth-enabled")
    location = hello_p.get("location", "?")
    print(f"  Connected: location={location}, phone-auth={phone_auth}")
    if phone_auth is False:
        print("  ⚠️  Server reports phone-auth DISABLED for this connection")

    phone = prompt("MAX phone number (e.g. +79991234567)")

    try:
        sms_token = await client.send_code(phone)
    except RuntimeError as e:
        await client.close()
        if "limit.violate" in str(e):
            print()
            print("  ⛔ MAX: слишком много попыток авторизации на этот номер.")
            print("  Сервер временно заблокировал отправку SMS.")
            print("  Подождите 1-2 часа и попробуйте снова: ./bridge.sh setup bridges")
            raise SystemExit(1) from None
        raise

    print("  SMS code sent!")
    code = prompt("Enter SMS code")
    account_data = await client.sign_in(sms_token, int(code))

    # Handle 2FA
    password_challenge = account_data.get("passwordChallenge")
    token_attrs = account_data.get("tokenAttrs", {})
    login_attrs = token_attrs.get("LOGIN", {})

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
    client_device_id = client.device_id  # save before potential close

    # If sign_in didn't return user_id, use BridgeMaxClient (PyMax full _sync)
    if not max_user_id:
        log.info("sign_in profile keys: %s", list(profile.keys()))
        log.info("Trying BridgeMaxClient login to get user_id...")
        try:
            # Close NativeMaxAuth before connecting BridgeMaxClient
            max_session.save(login_token, user_id=None, device_id=client_device_id)
            await client.close()
            client = None  # mark closed

            bridge_client = BridgeMaxClient(
                token=login_token, device_id=client_device_id,
            )
            await bridge_client.connect_and_login()
            if bridge_client.inner.me is not None:
                max_user_id = bridge_client.inner.me.id
                log.info("Got user_id from BridgeMaxClient: %s", max_user_id)
            else:
                log.warning("BridgeMaxClient.me is None after login")
            await bridge_client.disconnect()
        except Exception as e:
            log.warning("BridgeMaxClient login for user_id failed: %s", e)

    # Save final session (with user_id if we got it)
    max_session.save(login_token, user_id=max_user_id, device_id=client_device_id)
    if client is not None:
        await client.close()

    if max_user_id:
        print(f"  ✅ MAX: authenticated (ID: {max_user_id})")
    else:
        print("  ⚠️  MAX: authenticated but user ID not found automatically")
        print("  You can find it at https://web.max.ru → DevTools → viewerId")

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
    # Fall back to previously stored user_id in session (if it exists)
    try:
        return session.load_user_id()
    except FileNotFoundError:
        return None


# ── Bridge configuration ─────────────────────────────────────────────────────

async def _load_tg_chats_for_user(
    user_name: str,
    api_id: int,
    api_hash: str,
    sessions_dir: str,
) -> list:
    """Load TG group chats for a single user."""
    tg_session = f"tg_{user_name}"
    session_path = Path(sessions_dir) / f"{tg_session}.session"
    if not session_path.exists():
        return []

    try:
        client = Client(
            name=tg_session,
            api_id=api_id,
            api_hash=api_hash,
            workdir=sessions_dir,
        )
        await client.start()
        tg_chats = []
        async for dialog in client.get_dialogs():
            if dialog.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
                tg_chats.append(dialog.chat)
        await client.stop()
        tg_chats.sort(key=lambda c: (c.title or "").lower())
        return tg_chats
    except Exception as e:
        log.warning("Failed to load TG chats for %s: %s", user_name, e)
        return []


async def _load_max_chats_for_user(
    user_name: str,
    sessions_dir: str,
) -> list[dict]:
    """Load MAX group chats for a single user."""
    max_session = MaxSession(f"max_{user_name}", sessions_dir)
    if not max_session.exists():
        return []

    try:
        login_token = max_session.load()
        device_id = max_session.load_device_id()
        if not login_token or not device_id:
            return []

        max_client = BridgeMaxClient(token=login_token, device_id=device_id)
        await max_client.connect_and_login()

        chats: list[dict] = []
        for chat in max_client.inner.chats:
            chats.append({
                "id": chat.id,
                "title": chat.title or f"Chat {chat.id}",
                "type": "CHAT",
                "members": chat.participants_count,
            })
        for ch in max_client.inner.channels:
            chats.append({
                "id": ch.id,
                "title": ch.title or f"Channel {ch.id}",
                "type": "CHANNEL",
                "members": ch.participants_count,
            })

        await max_client.disconnect()
        chats.sort(key=lambda c: c["title"].lower())
        return chats
    except Exception as e:
        log.warning("Failed to load MAX chats for %s: %s", user_name, e)
        return []


def _select_from_list(items: list, label: str) -> int:
    """Show numbered list and return selected index."""
    while True:
        try:
            idx = int(prompt(f"Select {label} number")) - 1
            if 0 <= idx < len(items):
                return idx
            print(f"  Enter a number between 1 and {len(items)}")
        except ValueError:
            print("  Enter a number")


async def collect_bridges(
    users: list[dict],
    api_id: int,
    api_hash: str,
    sessions_dir: str,
) -> list[dict]:
    print_section("Configure Chat Bridges")

    bridges = []
    while True:
        print()

        # 1. Pick user first
        if len(users) == 1:
            user = users[0]
            print(f"  User: {user['name']}")
        else:
            print("  Which user should handle this bridge?")
            for i, u in enumerate(users):
                print(f"    {i + 1}. {u['name']}")
            idx = _select_from_list(users, "user")
            user = users[idx]

        # 2. Load this user's chats
        print(f"\n  Loading chats for {user['name']}...")
        tg_chats = await _load_tg_chats_for_user(
            user["name"], api_id, api_hash, sessions_dir,
        )
        max_chats = await _load_max_chats_for_user(user["name"], sessions_dir)

        if not tg_chats:
            print("  ⚠️  No Telegram groups found for this user")
        else:
            print(f"  Found {len(tg_chats)} Telegram group(s)")

        if not max_chats:
            print("  ⚠️  No MAX chats found — you'll need to enter ID manually")
        else:
            print(f"  Found {len(max_chats)} MAX chat(s)/channel(s)")

        # 3. Bridge name
        print()
        bridge_name = prompt("Bridge name (e.g. team-general)")

        # 4. Pick TG chat
        print()
        print("  Available Telegram groups:")
        for i, chat in enumerate(tg_chats):
            print(f"    {i + 1:2}. {chat.title}  (ID: {chat.id})")
        print()
        idx = _select_from_list(tg_chats, "Telegram chat")
        telegram_chat_id = tg_chats[idx].id
        print(f"  ✅ Telegram: {tg_chats[idx].title} (ID: {telegram_chat_id})")

        # 5. Pick MAX chat
        print()
        manual_max = False
        if max_chats:
            print("  Available MAX chats:")
            for i, chat in enumerate(max_chats):
                type_tag = "📢" if chat["type"] == "CHANNEL" else "💬"
                members = f", {chat['members']} members" if chat["members"] else ""
                print(f"    {i + 1:2}. {type_tag} {chat['title']}  (ID: {chat['id']}{members})")
            print(f"    {len(max_chats) + 1:2}. Enter ID manually")
            print()
            while True:
                try:
                    idx = int(prompt("Select MAX chat number")) - 1
                    if idx == len(max_chats):
                        max_chat_id = _prompt_max_chat_id_manual()
                        manual_max = True
                        break
                    if 0 <= idx < len(max_chats):
                        max_chat_id = max_chats[idx]["id"]
                        print(f"  ✅ MAX: {max_chats[idx]['title']} (ID: {max_chat_id})")
                        break
                    print(f"  Enter a number between 1 and {len(max_chats) + 1}")
                except ValueError:
                    print("  Enter a number")
        else:
            max_chat_id = _prompt_max_chat_id_manual()
            manual_max = True

        # 6. Verify membership only for manually entered MAX ID
        if manual_max:
            print(f"\n  Verifying MAX membership for {user['name']}...")
            max_ok = await _verify_max_membership(
                user["name"], sessions_dir, max_chat_id,
            )
            if not max_ok:
                if not confirm("Continue anyway?"):
                    print("  Skipped this bridge.")
                    continue

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


def _prompt_max_chat_id_manual() -> int:
    """Fallback: ask user to paste MAX chat URL or ID manually."""
    print("  Open https://web.max.ru → go to the desired chat")
    print("  The URL will look like: https://web.max.ru/#/chats/@chat/-72099589405396")
    while True:
        try:
            url_or_id = prompt("Paste the MAX chat URL (or just the numeric ID)")
            max_chat_id = parse_max_chat_id(url_or_id)
            print(f"  ✅ MAX chat ID: {max_chat_id}")
            return max_chat_id
        except ValueError as e:
            print(f"  ❌ {e}")


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
    import logging
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

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
