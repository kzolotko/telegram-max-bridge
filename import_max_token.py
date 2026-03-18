"""
Manually import a MAX login token into a session file.

Use this when phone-based auth is unavailable (phone-auth-enabled=false)
and you have extracted the token via extract_max_token.js in the browser.

Usage:
    python import_max_token.py
"""

import asyncio
import json
import sys
from pathlib import Path

# Load config to know where sessions dir is
sys.path.insert(0, str(Path(__file__).parent))
from src.config import load_config
from src.max.session import MaxSession
from src.max.patched_client import PatchedMaxClient


async def verify_and_save(session_name: str, token: str, sessions_dir: str):
    print(f"\nVerifying token with MAX server...")
    client = PatchedMaxClient()
    await client.connect()
    try:
        resp = await client.login_by_token(token)
        profile = resp.get("payload", {}).get("profile", {})
        user_id = profile.get("userId")
        phone   = profile.get("phone")
        name    = profile.get("name") or profile.get("firstName", "")
        print(f"  ✅ Token valid!")
        print(f"     userId : {user_id}")
        print(f"     phone  : {phone}")
        print(f"     name   : {name}")
    except Exception as e:
        print(f"  ❌ Token verification failed: {e}")
        return False
    finally:
        try:
            await client._connection.close()
        except Exception:
            pass

    session = MaxSession(session_name, sessions_dir)
    session.save(token, user_id=user_id)
    path = Path(sessions_dir) / f"{session_name}.max_session"
    print(f"\n  Session saved → {path}")
    return True


async def main():
    print("=" * 60)
    print("MAX Token Importer")
    print("=" * 60)
    print()
    print("This tool imports a MAX login token extracted from the browser.")
    print("See extract_max_token.js for instructions on how to get the token.")
    print()

    config = load_config()
    sessions_dir = config.sessions_dir
    Path(sessions_dir).mkdir(parents=True, exist_ok=True)

    # List available session names from config
    session_names = [config.listener_max_session] + [u.max_session for u in config.users]
    print("Session names from config:")
    for i, name in enumerate(session_names):
        path = Path(sessions_dir) / f"{name}.max_session"
        status = "✅ exists" if path.exists() else "❌ missing"
        print(f"  [{i}] {name}  ({status})")

    print()
    choice = input("Enter session name (or select number): ").strip()
    if choice.isdigit():
        idx = int(choice)
        if 0 <= idx < len(session_names):
            session_name = session_names[idx]
        else:
            print("Invalid selection.")
            return
    else:
        session_name = choice

    print(f"\nImporting token for session: '{session_name}'")
    print()
    print("Paste the login_token value (from DevTools console) and press Enter:")
    token = input("  login_token: ").strip()

    if not token:
        print("No token entered.")
        return

    # Strip JSON wrapper if user pasted the whole JSON
    if token.startswith("{"):
        try:
            parsed = json.loads(token)
            token = parsed.get("login_token") or parsed.get("token") or token
        except Exception:
            pass

    await verify_and_save(session_name, token, sessions_dir)


if __name__ == "__main__":
    asyncio.run(main())
