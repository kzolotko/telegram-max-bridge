"""
Diagnostic script for Telegram ↔ MAX Bridge.
Run this to check connectivity and server responses before auth.

Usage:
    python diagnose.py
"""

import asyncio
import json
import sys

# ── helpers ──────────────────────────────────────────────────────────────────

SEP = "─" * 60

def ok(msg):   print(f"  ✅  {msg}")
def fail(msg): print(f"  ❌  {msg}")
def info(msg): print(f"  ℹ️   {msg}")
def raw(label, data):
    print(f"\n  {label}:")
    print(json.dumps(data, indent=4, ensure_ascii=False))


# ── 1. External IP ────────────────────────────────────────────────────────────

async def check_external_ip():
    print(f"\n{SEP}")
    print("1. External IP address")
    print(SEP)
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.ipify.org?format=json", timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
                ip = data.get("ip", "?")
                ok(f"Your external IP: {ip}")

            async with session.get(f"https://ipapi.co/{ip}/json/", timeout=aiohttp.ClientTimeout(total=5)) as r:
                geo = await r.json()
                country = geo.get("country_name", "?")
                city = geo.get("city", "?")
                org = geo.get("org", "?")
                info(f"Country: {country}, City: {city}")
                info(f"ISP/Org: {org}")
                if geo.get("country_code") != "RU":
                    fail(f"NOT a Russian IP — MAX phone auth will be blocked!")
                else:
                    ok("Russian IP — MAX phone auth should be available")
    except Exception as e:
        fail(f"Could not determine external IP: {e}")


# ── 2. WebSocket connection ───────────────────────────────────────────────────

async def check_websocket():
    print(f"\n{SEP}")
    print("2. MAX WebSocket connection")
    print(SEP)
    try:
        import websockets
        async with websockets.connect(
            "wss://ws-api.oneme.ru/websocket",
            origin="https://web.max.ru",
            additional_headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/137.0.0.0 Safari/537.36"
                )
            },
            open_timeout=10,
        ) as ws:
            ok("WebSocket connected (with Origin header)")
            await ws.close()
    except Exception as e:
        fail(f"WebSocket connection failed: {e}")
        return False
    return True


# ── 3. Hello packet ───────────────────────────────────────────────────────────

async def check_hello():
    print(f"\n{SEP}")
    print("3. MAX hello packet (opcode 6)")
    print(SEP)
    try:
        from src.max.patched_client import PatchedMaxClient
        client = PatchedMaxClient()
        await client.connect()
        ok("Connected")

        resp = await client._send_hello_packet()
        payload = resp.get("payload", {})
        raw("Raw hello response", resp)

        phone_auth = payload.get("phone-auth-enabled")
        location   = payload.get("location", "?")

        if phone_auth is True:
            ok(f"phone-auth-enabled = True  (location: {location})")
        elif phone_auth is False:
            fail(f"phone-auth-enabled = False (location: {location}) — auth will be blocked")
        else:
            info(f"phone-auth-enabled not present in response (location: {location})")

        await client._connection.close()
        return phone_auth, location
    except Exception as e:
        fail(f"Hello packet failed: {e}")
        return None, None


# ── 4. Send code (dry run) ────────────────────────────────────────────────────

async def check_send_code(phone_auth_enabled, location):
    print(f"\n{SEP}")
    print("4. MAX send_code (opcode 17)")
    print(SEP)

    if phone_auth_enabled is False:
        info(f"Skipping — phone auth is already known to be disabled ({location})")
        return

    test_phone = input("  Enter a phone number to test send_code (or press Enter to skip): ").strip()
    if not test_phone:
        info("Skipped")
        return

    try:
        from src.max.patched_client import PatchedMaxClient
        from vkmax.client import RPC_VERSION
        import itertools

        client = PatchedMaxClient()
        await client.connect()

        # send hello first (required before opcode 17)
        await client._send_hello_packet()

        seq = next(client._seq)
        future = asyncio.get_event_loop().create_future()
        client._pending[seq] = future

        request = {
            "ver": RPC_VERSION,
            "cmd": 0,
            "seq": seq,
            "opcode": 17,
            "payload": {
                "phone": test_phone,
                "type": "START_AUTH",
                "language": "ru"
            }
        }
        await client._connection.send(json.dumps(request))
        response = await asyncio.wait_for(future, timeout=10)
        raw("Raw send_code response", response)

        if "error" in response.get("payload", {}):
            fail(f"Error: {response['payload']['error']} — {response['payload'].get('message', '')}")
        elif "token" in response.get("payload", {}):
            ok("Got sms_token — auth flow works!")
        else:
            info("Unexpected response structure — see raw output above")

        await client._connection.close()
    except Exception as e:
        fail(f"send_code failed: {e}")


# ── 5. Existing sessions ──────────────────────────────────────────────────────

async def check_sessions():
    print(f"\n{SEP}")
    print("5. Existing session files")
    print(SEP)
    import os
    from pathlib import Path

    sessions_dir = Path("sessions")
    if not sessions_dir.exists():
        info("sessions/ directory does not exist yet")
        return

    tg_sessions  = list(sessions_dir.glob("*.session"))
    max_sessions = list(sessions_dir.glob("*.max_session"))

    if tg_sessions:
        ok(f"Telegram sessions: {[f.name for f in tg_sessions]}")
    else:
        info("No Telegram sessions found")

    if max_sessions:
        ok(f"MAX sessions: {[f.name for f in max_sessions]}")
    else:
        info("No MAX sessions found")


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("Telegram ↔ MAX Bridge — Diagnostics")
    print("=" * 60)

    await check_external_ip()
    ws_ok = await check_websocket()
    if ws_ok:
        phone_auth, location = await check_hello()
        await check_send_code(phone_auth, location)
    await check_sessions()

    print(f"\n{SEP}")
    print("Diagnostics complete.")
    print(SEP)


if __name__ == "__main__":
    asyncio.run(main())
