"""
Interactive setup wizard for Telegram ↔ MAX Bridge.

Modes:
  python -m src.setup               — full wizard (credentials + users + bridges)
  python -m src.setup credentials   — set up Telegram API credentials only (one-time)
  python -m src.setup users         — manage user accounts (add/remove/re-auth)
  python -m src.setup bridges       — manage chat bridges (add/remove, assign users)
  python -m src.setup migrate       — convert old config format to new format

Usage: python -m src.setup [credentials|users|bridges|migrate]
"""

import asyncio
import logging
import os
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
from .config import load_credentials, migrate_config


SEP = "─" * 60

CREDENTIALS_FILE = "config/credentials.yaml"
CONFIG_FILE = "config/config.yaml"
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


def prompt_int(label: str, default: int | None = None) -> int:
    default_str = str(default) if default is not None else None
    while True:
        raw = prompt(label, default=default_str)
        try:
            return int(raw)
        except ValueError:
            print("  Enter a numeric value")


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


# ── Config I/O ────────────────────────────────────────────────────────────────

def _load_existing_config() -> tuple[list[dict], list[dict], dict]:
    """Load existing config.yaml in either format.

    Returns (users, bridges, extra_sections) where:
      - users: list of {name, telegram_user_id, max_user_id}
      - bridges: list of {name, telegram_chat_id, max_chat_id, users: [name, ...]}
      - extra_sections: dict with dm_bridge, admin_bot, etc.
    """
    config_path = Path(CONFIG_FILE)
    if not config_path.exists():
        return [], [], {}

    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except Exception:
        return [], [], {}

    extra = {}
    for key in ("dm_bridge", "admin_bot"):
        if key in raw:
            extra[key] = raw[key]

    # New format: top-level users section
    if isinstance(raw.get("users"), list) and raw["users"]:
        users = []
        for u in raw["users"]:
            users.append({
                "name": u.get("name", ""),
                "telegram_user_id": u.get("telegram_user_id"),
                "max_user_id": u.get("max_user_id"),
            })

        bridges = []
        for b in raw.get("bridges", []):
            bridges.append({
                "name": b.get("name", ""),
                "telegram_chat_id": b.get("telegram_chat_id"),
                "max_chat_id": b.get("max_chat_id"),
                "users": list(b.get("users", [])),
            })
        return users, bridges, extra

    # Old format: inline user per bridge entry
    seen_users: dict[str, dict] = {}
    bridge_groups: dict[tuple, dict] = {}

    for b in raw.get("bridges", []):
        u = b.get("user", {})
        user_name = u.get("name", "")
        if user_name and user_name not in seen_users:
            seen_users[user_name] = {
                "name": user_name,
                "telegram_user_id": u.get("telegram_user_id"),
                "max_user_id": u.get("max_user_id"),
            }

        tg_id = b.get("telegram_chat_id")
        max_id = b.get("max_chat_id")
        bname = b.get("name", "")
        key = (bname, tg_id, max_id)

        if key not in bridge_groups:
            bridge_groups[key] = {
                "name": bname,
                "telegram_chat_id": tg_id,
                "max_chat_id": max_id,
                "users": [],
            }
        if user_name and user_name not in bridge_groups[key]["users"]:
            bridge_groups[key]["users"].append(user_name)

    return list(seen_users.values()), list(bridge_groups.values()), extra


def write_config(
    users: list[dict],
    bridges: list[dict],
    output_path: str = CONFIG_FILE,
    extra_sections: dict | None = None,
):
    """Write config.yaml in new format (separate users + bridges sections).

    Args:
        users: list of {name, telegram_user_id, max_user_id}
        bridges: list of {name, telegram_chat_id, max_chat_id, users: [name, ...]}
        extra_sections: optional dict with dm_bridge, admin_bot, etc.
    """
    config: dict = {"users": [], "bridges": []}

    for u in users:
        if u.get("max_user_id") is None:
            raise ValueError(
                f"User '{u.get('name', '?')}' has no max_user_id. "
                "Run setup again and provide a valid MAX user ID."
            )
        config["users"].append({
            "name": u["name"],
            "telegram_user_id": int(u["telegram_user_id"]),
            "max_user_id": int(u["max_user_id"]),
        })

    for b in bridges:
        config["bridges"].append({
            "name": b["name"],
            "telegram_chat_id": int(b["telegram_chat_id"]),
            "max_chat_id": int(b["max_chat_id"]),
            "users": list(b["users"]),
        })

    if extra_sections:
        config.update(extra_sections)

    path = Path(output_path)
    path.write_text(yaml.dump(config, allow_unicode=True, default_flow_style=False))
    print(f"\n  ✅ Config written to: {path.resolve()}")


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
    try:
        os.chmod(creds_path, 0o600)
    except OSError:
        pass
    print(f"\n  ✅ Credentials saved to {CREDENTIALS_FILE}")


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

    try:
        await tg_client.start()
        me = await tg_client.get_me()
        telegram_user_id = me.id
        print(f"  ✅ Telegram: @{me.username or me.first_name} (ID: {telegram_user_id})")
    finally:
        try:
            await tg_client.stop()
        except Exception:
            pass

    # ── MAX ───────────────────────────────────────────────────────────────────
    print()
    print(f"  [MAX] Authenticating {name}...")
    max_session = MaxSession(f"max_{name}", sessions_dir)
    max_user_id: int | None = None

    if max_session.exists():
        print(f"  MAX session already exists — verifying...")
        max_client = None
        try:
            login_token = max_session.load()
            device_id = max_session.load_device_id()
            if not device_id:
                raise RuntimeError("No device_id in session")
            max_client = BridgeMaxClient(token=login_token, device_id=device_id)
            await max_client.connect_and_login()
            # Get user_id from PyMax (reliable) — session file may lack it
            max_user_id = None
            if max_client.inner.me is not None:
                max_user_id = max_client.inner.me.id
            if not max_user_id:
                max_user_id = max_session.load_user_id()
            # Update session with user_id if we found it
            if max_user_id:
                max_session.save(login_token, user_id=max_user_id, device_id=device_id)
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
        finally:
            if max_client is not None:
                try:
                    await max_client.disconnect()
                except Exception:
                    pass
    else:
        max_user_id = await _do_max_auth(name, sessions_dir)

    if not max_user_id:
        print("  ⚠️  MAX user ID is required for bridge routing.")
        max_user_id = prompt_int("Enter MAX user ID (numeric)")
        # Ensure session has user_id for future runs.
        if max_session.exists():
            login_token = max_session.load()
            device_id = max_session.load_device_id()
            if device_id:
                max_session.save(login_token, user_id=max_user_id, device_id=device_id)

    return {
        "name": name,
        "telegram_user_id": telegram_user_id,
        "max_user_id": int(max_user_id),
    }


async def _reauth_user(
    user: dict,
    api_id: int,
    api_hash: str,
    sessions_dir: str,
    platform: str,
):
    """Re-authenticate an existing user for TG, MAX, or both."""
    name = user["name"]
    sessions_path = Path(sessions_dir)
    sessions_path.mkdir(parents=True, exist_ok=True)

    if platform in ("tg", "both"):
        print(f"\n  [Telegram] Re-authenticating {name}...")
        tg_session_name = f"tg_{name}"
        tg_client = Client(
            name=tg_session_name,
            api_id=api_id,
            api_hash=api_hash,
            workdir=sessions_dir,
        )
        try:
            await tg_client.start()
            me = await tg_client.get_me()
            user["telegram_user_id"] = me.id
            print(f"  ✅ Telegram: @{me.username or me.first_name} (ID: {me.id})")
        finally:
            try:
                await tg_client.stop()
            except Exception:
                pass

    if platform in ("max", "both"):
        print(f"\n  [MAX] Re-authenticating {name}...")
        max_user_id = await _do_max_auth(name, sessions_dir)
        if max_user_id:
            user["max_user_id"] = int(max_user_id)


async def _do_max_auth(name: str, sessions_dir: str) -> int:
    max_session = MaxSession(f"max_{name}", sessions_dir)

    print("  Using native TCP/SSL protocol (device_type=DESKTOP)")
    client = NativeMaxAuth()
    max_user_id: int | None = None
    login_token: str | None = None
    client_device_id = client.device_id

    try:
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
            raise RuntimeError(
                "MAX account has 2FA enabled. "
                "2FA password auth is not yet supported."
            )

        login_token = login_attrs.get("token")
        if not login_token:
            raise RuntimeError(
                f"No login token in response. Keys: {list(account_data.keys())}"
            )

        profile = account_data.get("profile", {})
        max_user_id = _extract_max_user_id(account_data, max_session)
        client_device_id = client.device_id

        # MAX doesn't include userId in the sign_in (opcode 18) response —
        # it only appears in the login (opcode 19) response.  Do a quick
        # second login on a fresh connection to retrieve it.
        if not max_user_id:
            log.debug(
                "sign_in response has no userId (expected) — "
                "top-level keys: %s | profile keys: %s | tokenAttrs.LOGIN keys: %s",
                list(account_data.keys()),
                list(profile.keys()),
                list(((account_data.get("tokenAttrs") or {}).get("LOGIN") or {}).keys()),
            )
            try:
                # Persist token+device_id early (without user_id) as safety fallback.
                max_session.save(login_token, user_id=None, device_id=client_device_id)
                # Fresh instance → new device_id so MAX won't reject rapid reconnect.
                login_native = NativeMaxAuth()
                try:
                    login_resp = await login_native.login_by_token(login_token)
                    login_payload = login_resp.get("payload", {}) or {}
                    log.debug("login_by_token payload keys: %s", list(login_payload.keys()))
                    max_user_id = _extract_max_user_id(login_payload, max_session)
                    if max_user_id:
                        log.debug("Got user_id via login_by_token: %s", max_user_id)
                finally:
                    try:
                        await login_native.close()
                    except Exception:
                        pass
            except Exception as e:
                log.warning("Native login_by_token for user_id failed: %s", e)

        if not max_user_id:
            print("  ⚠️  MAX user ID not found automatically.")
            print("  It is required to route messages through the correct account.")
            max_user_id = prompt_int("Enter MAX user ID (numeric)")

        # Save final session with mandatory user_id.
        max_session.save(login_token, user_id=int(max_user_id), device_id=client_device_id)
        print(f"  ✅ MAX: authenticated (ID: {max_user_id})")
        return int(max_user_id)
    finally:
        try:
            await client.close()
        except Exception:
            pass


def _extract_max_user_id(data: dict, max_session: MaxSession) -> int | None:
    """Search for user_id across multiple known locations of a MAX API response.

    MAX returns user_id in different places depending on the opcode and
    server version: top-level, inside ``profile``, ``account``, ``me``,
    or ``tokenAttrs.LOGIN``.
    """
    candidates: list[dict] = [data]
    for sub_key in ("account", "profile", "user", "me"):
        sub = data.get(sub_key)
        if isinstance(sub, dict):
            candidates.append(sub)
    login_attrs = ((data.get("tokenAttrs") or {}).get("LOGIN")) or {}
    if isinstance(login_attrs, dict):
        candidates.append(login_attrs)

    for obj in candidates:
        for key in ("userId", "sn", "id"):
            val = obj.get(key)
            if val is not None:
                try:
                    uid = int(val)
                    if uid > 0:
                        return uid
                except (ValueError, TypeError):
                    pass

    # Fall back to previously stored user_id in session (if it exists)
    try:
        stored = max_session.load_user_id()
        if stored:
            return stored
    except FileNotFoundError:
        pass
    return None


# ── Chat loading helpers ──────────────────────────────────────────────────────

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

    client = None
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
        tg_chats.sort(key=lambda c: (c.title or "").lower())
        return tg_chats
    except Exception as e:
        log.warning("Failed to load TG chats for %s: %s", user_name, e)
        return []
    finally:
        if client is not None:
            try:
                await client.stop()
            except Exception:
                pass


async def _load_max_chats_for_user(
    user_name: str,
    sessions_dir: str,
) -> list[dict]:
    """Load MAX group chats for a single user."""
    max_session = MaxSession(f"max_{user_name}", sessions_dir)
    if not max_session.exists():
        return []

    max_client = None
    try:
        login_token = max_session.load()
        device_id = max_session.load_device_id()
        if not login_token or not device_id:
            return []

        # Brief pause — MAX server rejects rapid reconnects from same device
        await asyncio.sleep(2)

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

        chats.sort(key=lambda c: c["title"].lower())
        return chats
    except Exception as e:
        log.warning("Failed to load MAX chats for %s: %s", user_name, e)
        return []
    finally:
        if max_client is not None:
            try:
                await max_client.disconnect()
            except Exception:
                pass


async def _verify_tg_membership(
    user_name: str,
    api_id: int,
    api_hash: str,
    sessions_dir: str,
    chat_id: int,
) -> bool:
    """Check if user is a member of the Telegram chat. Returns True if OK."""
    tg_client = None
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
        print(f"  ✅ Telegram: {user_name} is a member of '{chat.title}'")
        return True
    except Exception as e:
        print(f"  ❌ Telegram: {user_name} is NOT a member of chat {chat_id} ({e})")
        return False
    finally:
        if tg_client is not None:
            try:
                await tg_client.stop()
            except Exception:
                pass


async def _verify_max_membership(
    user_name: str,
    sessions_dir: str,
    chat_id: int,
) -> bool:
    """Check if user is a member of the MAX chat. Returns True if OK."""
    max_client = None
    try:
        max_session = MaxSession(f"max_{user_name}", sessions_dir)
        login_token = max_session.load()
        device_id = max_session.load_device_id()
        if not login_token or not device_id:
            print(f"  ⚠️  MAX: no session for {user_name} — cannot verify membership")
            return True  # can't check, assume OK

        # Brief pause — MAX server rejects rapid reconnects from same device
        await asyncio.sleep(2)

        max_client = BridgeMaxClient(token=login_token, device_id=device_id)
        await max_client.connect_and_login()

        # Try to get chat info — will fail if user is not a member
        chats = await max_client.inner.get_chats([chat_id])

        if chats:
            print(f"  ✅ MAX: {user_name} is a member of '{chats[0].title}'")
            return True
        else:
            print(f"  ❌ MAX: {user_name} is NOT a member of chat {chat_id}")
            return False
    except Exception as e:
        print(f"  ❌ MAX: could not verify membership for {user_name} in chat {chat_id} ({e})")
        return False
    finally:
        if max_client is not None:
            try:
                await max_client.disconnect()
            except Exception:
                pass


# ── Chat selection helpers ────────────────────────────────────────────────────

def _search_and_select_tg_chat(chats: list) -> tuple[int, str]:
    """Search TG chats by name and return (chat_id, title)."""
    while True:
        query = prompt("Enter Telegram chat name (or part of it)").strip()
        if not query:
            continue
        matches = [c for c in chats if query.lower() in (c.title or "").lower()]
        if not matches:
            print(f"  No chats matching '{query}'. Try again.")
            continue
        if len(matches) == 1:
            print(f"  Found: {matches[0].title} (ID: {matches[0].id})")
            return matches[0].id, matches[0].title
        print(f"  Found {len(matches)} match(es):")
        for i, c in enumerate(matches, 1):
            print(f"    {i}. {c.title}  (ID: {c.id})")
        while True:
            try:
                idx = int(prompt("Select number")) - 1
                if 0 <= idx < len(matches):
                    return matches[idx].id, matches[idx].title
                print(f"  Enter a number between 1 and {len(matches)}")
            except ValueError:
                print("  Enter a number")


def _search_and_select_max_chat(chats: list[dict]) -> tuple[int, str] | None:
    """Search MAX chats by name and return (chat_id, title), or None for manual entry."""
    while True:
        query = prompt("Enter MAX chat name (or part of it), or 'manual' to enter ID").strip()
        if not query:
            continue
        if query.lower() == "manual":
            return None
        matches = [c for c in chats if query.lower() in c["title"].lower()]
        if not matches:
            print(f"  No chats matching '{query}'. Try again or type 'manual'.")
            continue
        if len(matches) == 1:
            c = matches[0]
            tag = "📢" if c["type"] == "CHANNEL" else "💬"
            members = f", {c['members']} members" if c["members"] else ""
            print(f"  Found: {tag} {c['title']} (ID: {c['id']}{members})")
            return c["id"], c["title"]
        print(f"  Found {len(matches)} match(es):")
        for i, c in enumerate(matches, 1):
            tag = "📢" if c["type"] == "CHANNEL" else "💬"
            members = f", {c['members']} members" if c["members"] else ""
            print(f"    {i}. {tag} {c['title']}  (ID: {c['id']}{members})")
        print(f"    {len(matches) + 1}. Enter ID manually")
        while True:
            try:
                idx = int(prompt("Select number")) - 1
                if idx == len(matches):
                    return None
                if 0 <= idx < len(matches):
                    return matches[idx]["id"], matches[idx]["title"]
                print(f"  Enter a number between 1 and {len(matches) + 1}")
            except ValueError:
                print("  Enter a number")


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


def _print_done():
    print()
    print("=" * 60)
    print("Setup complete!")
    print()
    print("Start the bridge:")
    print("  ./bridge.sh start")
    print("=" * 60)


# ── Mode: users (user management) ────────────────────────────────────────────

async def setup_users():
    """Manage user accounts: add, remove, re-auth."""
    try:
        creds = load_credentials()
    except FileNotFoundError as e:
        print(f"\n  ❌ {e}")
        print("  Run './bridge.sh setup credentials' first.")
        sys.exit(1)

    api_id = creds["api_id"]
    api_hash = creds["api_hash"]

    existing_users, existing_bridges, extra_sections = _load_existing_config()

    while True:
        print_section("User Management")
        if existing_users:
            print("  Current users:")
            for u in existing_users:
                print(f"    • {u['name']} (TG: {u['telegram_user_id']}, MAX: {u['max_user_id']})")
        else:
            print("  No users configured yet.")
        print()
        print("  Options:")
        print("    1. Add new user")
        print("    2. Remove user")
        print("    3. Re-auth user (TG/MAX/both)")
        print("    4. Done")
        choice = prompt("Choose [1/2/3/4]", default="4")

        if choice == "1":
            # Add user with uniqueness check
            user = await _add_user_flow(api_id, api_hash, existing_users)
            if user:
                existing_users.append(user)
                write_config(existing_users, existing_bridges, CONFIG_FILE, extra_sections)
                print(f"  ✅ User '{user['name']}' added.")

        elif choice == "2":
            if not existing_users:
                print("  No users to remove.")
                continue
            print()
            print("  Select user to remove:")
            for i, u in enumerate(existing_users, 1):
                # Show bridges this user is assigned to
                bridges_with_user = [b["name"] for b in existing_bridges if u["name"] in b.get("users", [])]
                bridge_info = f"  (in bridges: {', '.join(bridges_with_user)})" if bridges_with_user else ""
                print(f"    {i}. {u['name']}{bridge_info}")
            print(f"    {len(existing_users) + 1}. Cancel")
            try:
                idx = int(prompt("Select number")) - 1
            except ValueError:
                continue
            if idx < 0 or idx >= len(existing_users):
                continue

            target = existing_users[idx]
            # Check if user is in any bridges
            affected = [b["name"] for b in existing_bridges if target["name"] in b.get("users", [])]
            if affected:
                print(f"  ⚠️  User '{target['name']}' is used in bridges: {', '.join(affected)}")
                print("  Removing will also remove them from these bridges.")
                if not confirm("Continue?"):
                    continue
                for b in existing_bridges:
                    if target["name"] in b.get("users", []):
                        b["users"].remove(target["name"])
                # Remove bridges that became empty
                empty = [b for b in existing_bridges if not b.get("users")]
                if empty:
                    for eb in empty:
                        print(f"  ⚠️  Bridge '{eb['name']}' has no users left — removing it.")
                        existing_bridges.remove(eb)

            existing_users.remove(target)
            write_config(existing_users, existing_bridges, CONFIG_FILE, extra_sections)
            print(f"  ✅ User '{target['name']}' removed.")

        elif choice == "3":
            if not existing_users:
                print("  No users to re-auth.")
                continue
            print()
            print("  Select user to re-authenticate:")
            for i, u in enumerate(existing_users, 1):
                print(f"    {i}. {u['name']}")
            print(f"    {len(existing_users) + 1}. Cancel")
            try:
                idx = int(prompt("Select number")) - 1
            except ValueError:
                continue
            if idx < 0 or idx >= len(existing_users):
                continue

            target = existing_users[idx]
            print()
            print("  Re-auth platform:")
            print("    1. Telegram only")
            print("    2. MAX only")
            print("    3. Both")
            p_choice = prompt("Choose [1/2/3]", default="3")
            platform = {"1": "tg", "2": "max", "3": "both"}.get(p_choice, "both")
            await _reauth_user(target, api_id, api_hash, SESSIONS_DIR, platform)
            # Update user IDs in config if they changed
            write_config(existing_users, existing_bridges, CONFIG_FILE, extra_sections)
            print(f"  ✅ User '{target['name']}' re-authenticated.")

        elif choice == "4":
            break


async def _add_user_flow(api_id: int, api_hash: str, existing_users: list[dict]) -> dict | None:
    """Add a new user with uniqueness check. Returns user dict or None."""
    print()
    name = prompt("User name (latin letters/digits, e.g. alice)")
    if not re.match(r'^[a-z0-9_]+$', name):
        print("  ⚠️  Use only lowercase letters, digits and underscores.")
        return None

    # Uniqueness check
    if any(u["name"] == name for u in existing_users):
        print(f"  ❌ User '{name}' already exists. Choose a different name.")
        return None

    sessions_path = Path(SESSIONS_DIR)
    sessions_path.mkdir(parents=True, exist_ok=True)

    # Telegram auth
    print()
    print(f"  [Telegram] Authenticating {name}...")
    tg_session_name = f"tg_{name}"
    tg_session_path = sessions_path / f"{tg_session_name}.session"
    tg_client = Client(
        name=tg_session_name,
        api_id=api_id,
        api_hash=api_hash,
        workdir=SESSIONS_DIR,
    )
    if tg_session_path.exists():
        print(f"  Telegram session already exists — verifying...")
    else:
        print("  Follow the prompts to authenticate your Telegram account:")

    try:
        await tg_client.start()
        me = await tg_client.get_me()
        telegram_user_id = me.id
        print(f"  ✅ Telegram: @{me.username or me.first_name} (ID: {telegram_user_id})")
    finally:
        try:
            await tg_client.stop()
        except Exception:
            pass

    # MAX auth
    print()
    print(f"  [MAX] Authenticating {name}...")
    max_session = MaxSession(f"max_{name}", SESSIONS_DIR)
    max_user_id: int | None = None

    if max_session.exists():
        print(f"  MAX session already exists — verifying...")
        max_client = None
        try:
            login_token = max_session.load()
            device_id = max_session.load_device_id()
            if not device_id:
                raise RuntimeError("No device_id in session")
            max_client = BridgeMaxClient(token=login_token, device_id=device_id)
            await max_client.connect_and_login()
            max_user_id = None
            if max_client.inner.me is not None:
                max_user_id = max_client.inner.me.id
            if not max_user_id:
                max_user_id = max_session.load_user_id()
            if max_user_id:
                max_session.save(login_token, user_id=max_user_id, device_id=device_id)
            print(f"  ✅ MAX: session valid (ID: {max_user_id})")
        except Exception as e:
            print(f"  ⚠️  MAX session verification failed: {e}")
            max_user_id = await _do_max_auth(name, SESSIONS_DIR)
        finally:
            if max_client is not None:
                try:
                    await max_client.disconnect()
                except Exception:
                    pass
    else:
        max_user_id = await _do_max_auth(name, SESSIONS_DIR)

    if not max_user_id:
        print("  ⚠️  MAX user ID is required for bridge routing.")
        max_user_id = prompt_int("Enter MAX user ID (numeric)")

    return {
        "name": name,
        "telegram_user_id": telegram_user_id,
        "max_user_id": int(max_user_id),
    }


# ── Mode: bridges (bridge management) ────────────────────────────────────────

async def setup_bridges():
    """Manage chat bridges: add, remove, assign users."""
    try:
        creds = load_credentials()
    except FileNotFoundError as e:
        print(f"\n  ❌ {e}")
        print("  Run './bridge.sh setup credentials' first.")
        sys.exit(1)

    api_id = creds["api_id"]
    api_hash = creds["api_hash"]

    existing_users, existing_bridges, extra_sections = _load_existing_config()

    if not existing_users:
        print("\n  ❌ No users configured. Run './bridge.sh setup users' first to add user accounts.")
        sys.exit(1)

    while True:
        print_section("Bridge Management")
        if existing_bridges:
            print("  Current bridges:")
            for b in existing_bridges:
                users_str = ", ".join(b.get("users", []))
                print(f"    • {b['name']}  (users: {users_str})")
                print(f"      TG: {b['telegram_chat_id']}  MAX: {b['max_chat_id']}")
        else:
            print("  No bridges configured yet.")
        print()
        print("  Options:")
        print("    1. Add new bridge")
        print("    2. Remove bridge")
        print("    3. Add user to existing bridge")
        print("    4. Remove user from bridge")
        print("    5. Done")
        choice = prompt("Choose [1/2/3/4/5]", default="5")

        if choice == "1":
            bridge = await _add_bridge_flow(
                existing_users, existing_bridges, api_id, api_hash,
            )
            if bridge:
                existing_bridges.append(bridge)
                write_config(existing_users, existing_bridges, CONFIG_FILE, extra_sections)

        elif choice == "2":
            if not existing_bridges:
                print("  No bridges to remove.")
                continue
            print()
            print("  Select bridge to remove:")
            for i, b in enumerate(existing_bridges, 1):
                print(f"    {i}. {b['name']}  (users: {', '.join(b.get('users', []))})")
            print(f"    {len(existing_bridges) + 1}. Cancel")
            try:
                idx = int(prompt("Select number")) - 1
            except ValueError:
                continue
            if idx < 0 or idx >= len(existing_bridges):
                continue
            target = existing_bridges[idx]
            if confirm(f"Remove bridge '{target['name']}'?"):
                existing_bridges.remove(target)
                write_config(existing_users, existing_bridges, CONFIG_FILE, extra_sections)
                print(f"  ✅ Bridge '{target['name']}' removed.")

        elif choice == "3":
            await _add_user_to_bridge_flow(
                existing_users, existing_bridges, extra_sections, api_id, api_hash,
            )

        elif choice == "4":
            await _remove_user_from_bridge_flow(
                existing_users, existing_bridges, extra_sections,
            )

        elif choice == "5":
            break

    if existing_bridges:
        _print_done()


async def _add_bridge_flow(
    users: list[dict],
    existing_bridges: list[dict],
    api_id: int,
    api_hash: str,
) -> dict | None:
    """Create a new bridge. Returns bridge dict or None."""
    print()
    bridge_name = prompt("Bridge name (e.g. team-general)")

    # Uniqueness check
    if any(b["name"] == bridge_name for b in existing_bridges):
        print(f"  ❌ Bridge '{bridge_name}' already exists. Choose a different name.")
        return None

    # Select primary user
    print()
    print("  Select primary user (will be used to load chat lists):")
    for i, u in enumerate(users, 1):
        print(f"    {i}. {u['name']}")
    while True:
        try:
            idx = int(prompt("Select user number")) - 1
            if 0 <= idx < len(users):
                primary_user = users[idx]
                break
            print(f"  Enter a number between 1 and {len(users)}")
        except ValueError:
            print("  Enter a number")

    # Load chats only for primary user
    print(f"\n  Loading chats for {primary_user['name']}...")
    tg_chats = await _load_tg_chats_for_user(
        primary_user["name"], api_id, api_hash, SESSIONS_DIR,
    )
    max_chats = await _load_max_chats_for_user(primary_user["name"], SESSIONS_DIR)

    if not tg_chats:
        print("  ⚠️  No Telegram groups found for this user.")
        print("  Join at least one Telegram group with this account and retry.")
        return None
    else:
        print(f"  Found {len(tg_chats)} Telegram group(s)")

    if not max_chats:
        print("  ⚠️  No MAX chats found — you'll need to enter ID manually")
    else:
        print(f"  Found {len(max_chats)} MAX chat(s)/channel(s)")

    # Select TG chat
    print()
    telegram_chat_id, tg_title = _search_and_select_tg_chat(tg_chats)
    print(f"  ✅ Telegram: {tg_title} (ID: {telegram_chat_id})")

    # Select MAX chat
    print()
    if max_chats:
        result = _search_and_select_max_chat(max_chats)
        if result is None:
            max_chat_id = _prompt_max_chat_id_manual()
        else:
            max_chat_id, max_title = result
            print(f"  ✅ MAX: {max_title} (ID: {max_chat_id})")
    else:
        max_chat_id = _prompt_max_chat_id_manual()

    bridge_users = [primary_user["name"]]

    # Optionally add more users
    other_users = [u for u in users if u["name"] != primary_user["name"]]
    if other_users and confirm("Add additional users to this bridge?"):
        while other_users:
            print()
            print("  Available users:")
            available = [u for u in other_users if u["name"] not in bridge_users]
            if not available:
                print("  No more users available.")
                break
            for i, u in enumerate(available, 1):
                print(f"    {i}. {u['name']}")
            print(f"    {len(available) + 1}. Done adding users")
            try:
                idx = int(prompt("Select user number")) - 1
            except ValueError:
                continue
            if idx < 0 or idx >= len(available):
                break
            bridge_users.append(available[idx]["name"])
            print(f"  ✅ Added {available[idx]['name']} to bridge.")
            if not confirm("Add another user?"):
                break

    print(f"\n  Bridge '{bridge_name}' configured with users: {', '.join(bridge_users)}")

    return {
        "name": bridge_name,
        "telegram_chat_id": telegram_chat_id,
        "max_chat_id": max_chat_id,
        "users": bridge_users,
    }


async def _add_user_to_bridge_flow(
    users: list[dict],
    bridges: list[dict],
    extra_sections: dict,
    api_id: int,
    api_hash: str,
):
    """Add an existing user to an existing bridge."""
    if not bridges:
        print("  No bridges configured.")
        return
    print()
    print("  Select bridge:")
    for i, b in enumerate(bridges, 1):
        users_str = ", ".join(b.get("users", []))
        print(f"    {i}. {b['name']}  (users: {users_str})")
    print(f"    {len(bridges) + 1}. Cancel")
    try:
        idx = int(prompt("Select number")) - 1
    except ValueError:
        return
    if idx < 0 or idx >= len(bridges):
        return

    target_bridge = bridges[idx]
    bridge_user_names = set(target_bridge.get("users", []))

    # Show available users (not already in this bridge)
    available = [u for u in users if u["name"] not in bridge_user_names]
    if not available:
        print("  All users are already in this bridge.")
        return

    print()
    print("  Available users to add:")
    for i, u in enumerate(available, 1):
        print(f"    {i}. {u['name']}")
    print(f"    {len(available) + 1}. Cancel")
    try:
        idx = int(prompt("Select number")) - 1
    except ValueError:
        return
    if idx < 0 or idx >= len(available):
        return

    selected = available[idx]

    # Optional membership verification
    if confirm(f"Verify that {selected['name']} is a member of both chats?"):
        print(f"\n  Verifying membership for {selected['name']}...")
        tg_ok = await _verify_tg_membership(
            selected["name"], api_id, api_hash, SESSIONS_DIR,
            target_bridge["telegram_chat_id"],
        )
        max_ok = await _verify_max_membership(
            selected["name"], SESSIONS_DIR,
            target_bridge["max_chat_id"],
        )
        if not tg_ok or not max_ok:
            if not confirm("Continue anyway? (bridge may not work correctly for this user)"):
                return

    target_bridge["users"].append(selected["name"])
    write_config(users, bridges, CONFIG_FILE, extra_sections)
    print(f"  ✅ User '{selected['name']}' added to bridge '{target_bridge['name']}'.")


async def _remove_user_from_bridge_flow(
    users: list[dict],
    bridges: list[dict],
    extra_sections: dict,
):
    """Remove a user from an existing bridge."""
    if not bridges:
        print("  No bridges configured.")
        return
    print()
    print("  Select bridge:")
    for i, b in enumerate(bridges, 1):
        users_str = ", ".join(b.get("users", []))
        print(f"    {i}. {b['name']}  (users: {users_str})")
    print(f"    {len(bridges) + 1}. Cancel")
    try:
        idx = int(prompt("Select number")) - 1
    except ValueError:
        return
    if idx < 0 or idx >= len(bridges):
        return

    target_bridge = bridges[idx]
    bridge_users = target_bridge.get("users", [])

    if not bridge_users:
        print("  Bridge has no users.")
        return

    print()
    print("  Select user to remove:")
    for i, uname in enumerate(bridge_users, 1):
        role = " (primary)" if i == 1 else ""
        print(f"    {i}. {uname}{role}")
    print(f"    {len(bridge_users) + 1}. Cancel")
    try:
        idx = int(prompt("Select number")) - 1
    except ValueError:
        return
    if idx < 0 or idx >= len(bridge_users):
        return

    target_name = bridge_users[idx]

    # Warnings
    if idx == 0 and len(bridge_users) > 1:
        print(f"  ⚠️  '{target_name}' is the primary user. "
              f"'{bridge_users[1]}' will become the new primary.")
        if not confirm("Continue?"):
            return
    elif len(bridge_users) == 1:
        print(f"  ⚠️  '{target_name}' is the only user. Bridge will become empty.")
        if confirm("Remove the entire bridge instead?"):
            bridges.remove(target_bridge)
            write_config(users, bridges, CONFIG_FILE, extra_sections)
            print(f"  ✅ Bridge '{target_bridge['name']}' removed.")
            return
        if not confirm("Keep empty bridge?"):
            return

    bridge_users.remove(target_name)
    write_config(users, bridges, CONFIG_FILE, extra_sections)
    print(f"  ✅ User '{target_name}' removed from bridge '{target_bridge['name']}'.")


# ── Full wizard ───────────────────────────────────────────────────────────────

async def setup_full():
    """Full wizard: credentials → users → bridges."""
    if not Path(CREDENTIALS_FILE).exists():
        await setup_credentials()
    else:
        print(f"\n  ✅ {CREDENTIALS_FILE} already exists.")
        if confirm("Re-configure credentials?"):
            await setup_credentials()

    await setup_users()
    await setup_bridges()


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
    elif mode == "users":
        await setup_users()
    elif mode == "bridges":
        await setup_bridges()
    elif mode == "migrate":
        print_section("Config Migration")
        try:
            migrated = migrate_config()
            if migrated:
                print("  ✅ Config migrated to new format.")
                print("  Old config backed up to config.yaml.bak")
            else:
                print("  Config is already in new format. Nothing to do.")
        except Exception as e:
            print(f"  ❌ Migration failed: {e}")
    elif mode is None:
        await setup_full()
    else:
        print(f"\n  Unknown mode: {mode}")
        print("  Usage: python -m src.setup [credentials|users|bridges|migrate]")
        sys.exit(1)

    # Cancel any lingering background tasks (e.g. BridgeMaxClient ping/recv loops
    # started during chat-list loading or membership verification).
    # Without this, asyncio.run() waits for them to time out before exiting.
    _current = asyncio.current_task()
    _pending = {t for t in asyncio.all_tasks() if t is not _current}
    if _pending:
        for t in _pending:
            t.cancel()
        await asyncio.gather(*_pending, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
