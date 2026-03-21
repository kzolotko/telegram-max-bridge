"""
Native TCP/SSL client for MAX (oneme.ru) phone authentication.

MAX disabled phone-auth for WebSocket/WEB clients. Native apps (desktop,
Android, iOS) use a binary protocol over TCP/SSL on api.oneme.ru:443.
This client implements just enough of that protocol to perform SMS auth
and obtain a login_token that can then be used with PyMax SocketMaxClient.

Binary packet format (10-byte header + payload):
  [ver:1B] [cmd:2B] [seq:1B] [opcode:2B] [packed_len:4B] [payload:NB]

  packed_len high byte = compression flag (0 = raw, else LZ4)
  packed_len low 3 bytes = payload length
  payload = msgpack-encoded dict
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from random import choice, randint
from typing import Any
from uuid import uuid4

import lz4.block
import msgpack

log = logging.getLogger("bridge.max.native")

HOST = "api.oneme.ru"
PORT = 443

# Opcodes
OP_SESSION_INIT = 6
OP_AUTH_REQUEST = 17
OP_AUTH = 18
OP_LOGIN = 19

OS_VERSIONS = [
    "Windows 10", "Windows 11",
    "macOS Monterey", "macOS Ventura", "macOS Sonoma",
    "Ubuntu 22.04", "Fedora 38",
]

TIMEZONES = [
    "Europe/Moscow", "Europe/Kaliningrad", "Europe/Samara",
    "Asia/Yekaterinburg", "Asia/Novosibirsk", "Asia/Krasnoyarsk",
    "Asia/Irkutsk", "Asia/Vladivostok",
]


def _default_user_agent() -> dict[str, Any]:
    return {
        "deviceType": "DESKTOP",
        "locale": "ru",
        "deviceLocale": "ru",
        "osVersion": choice(OS_VERSIONS),
        "deviceName": "vkmax Python",
        "headerUserAgent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/137.0.0.0 Safari/537.36"
        ),
        "appVersion": "25.12.14",
        "screen": "1080x1920 1.0x",
        "timezone": choice(TIMEZONES),
        "clientSessionId": randint(1, 15),
        "buildNumber": 0x97CB,
    }


class NativeMaxAuth:
    """Minimal native TCP/SSL client for MAX phone authentication."""

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._seq = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._recv_task: asyncio.Task[Any] | None = None
        self._device_id = str(uuid4())
        self._connected = False

    @property
    def device_id(self) -> str:
        return self._device_id

    # ── public API ──────────────────────────────────────────────

    async def connect(self) -> None:
        """Establish TCP/SSL connection to api.oneme.ru:443."""
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT")
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2

        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(
            None, lambda: socket.create_connection((HOST, PORT))
        )
        self._sock = ctx.wrap_socket(raw, server_hostname=HOST)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self._connected = True
        self._recv_task = asyncio.create_task(self._recv_loop())
        log.info("Connected to %s:%s (native TCP/SSL)", HOST, PORT)

    async def handshake(self) -> dict[str, Any]:
        """Send hello/session-init packet (opcode 6)."""
        payload = {
            "deviceId": self._device_id,
            "userAgent": _default_user_agent(),
        }
        resp = await self._send_and_wait(OP_SESSION_INIT, payload)
        p = resp.get("payload", {})
        phone_auth = p.get("phone-auth-enabled")
        location = p.get("location", "?")
        log.info(
            "Handshake OK: location=%s phone-auth-enabled=%s",
            location, phone_auth,
        )
        return resp

    async def send_code(self, phone: str) -> str:
        """Request SMS code (opcode 17). Returns sms_token."""
        payload = {
            "phone": phone,
            "type": "START_AUTH",
            "language": "ru",
        }
        resp = await self._send_and_wait(OP_AUTH_REQUEST, payload)
        p = resp.get("payload", {})

        if p.get("error"):
            raise RuntimeError(
                f"MAX auth error: {p.get('error')} — {p.get('message', '')}"
            )

        token = p.get("token")
        if not token:
            raise RuntimeError(
                f"No token in auth response. Payload keys: {list(p.keys())}"
            )
        log.info("SMS code requested, token received")
        return token

    async def sign_in(self, sms_token: str, code: int) -> dict[str, Any]:
        """Submit SMS code (opcode 18). Returns full payload with tokenAttrs."""
        payload = {
            "token": sms_token,
            "verifyCode": str(code),
            "authTokenType": "CHECK_CODE",
        }
        resp = await self._send_and_wait(OP_AUTH, payload)
        p = resp.get("payload", {})

        if p.get("error"):
            raise RuntimeError(
                f"MAX sign-in error: {p.get('error')} — {p.get('message', '')}"
            )

        return p

    async def login_by_token(self, token: str, device_id: str | None = None
                            ) -> dict[str, Any]:
        """Connect, handshake, and login with an existing token (opcode 19)."""
        if device_id:
            self._device_id = device_id
        if not self._connected:
            await self.connect()
        await self.handshake()

        payload = {
            "interactive": True,
            "token": token,
            "chatsSync": 0,
            "contactsSync": 0,
            "presenceSync": 0,
            "draftsSync": 0,
            "chatsCount": 40,
            "userAgent": _default_user_agent(),
        }
        resp = await self._send_and_wait(OP_LOGIN, payload)
        p = resp.get("payload", {})

        if p.get("error"):
            raise RuntimeError(
                f"MAX login error: {p.get('error')} — {p.get('message', '')}"
            )

        log.info("Logged in by token via native protocol")
        return resp

    async def close(self) -> None:
        """Close connection."""
        self._connected = False
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        log.info("Connection closed")

    # ── binary protocol ─────────────────────────────────────────

    @staticmethod
    def _pack(ver: int, cmd: int, seq: int, opcode: int,
              payload: dict[str, Any]) -> bytes:
        payload_bytes = msgpack.packb(payload)
        length = len(payload_bytes) & 0xFFFFFF  # no compression flag
        return (
            ver.to_bytes(1, "big")
            + cmd.to_bytes(2, "big")
            + (seq % 256).to_bytes(1, "big")
            + opcode.to_bytes(2, "big")
            + length.to_bytes(4, "big")
            + payload_bytes
        )

    @staticmethod
    def _unpack(data: bytes) -> dict[str, Any] | None:
        if len(data) < 10:
            return None
        ver = int.from_bytes(data[0:1], "big")
        cmd = int.from_bytes(data[1:3], "big")
        seq = int.from_bytes(data[3:4], "big")
        opcode = int.from_bytes(data[4:6], "big")
        packed_len = int.from_bytes(data[6:10], "big", signed=False)
        comp_flag = packed_len >> 24
        payload_length = packed_len & 0xFFFFFF
        payload_bytes = data[10:10 + payload_length]

        payload = None
        if payload_bytes:
            if comp_flag != 0:
                try:
                    payload_bytes = lz4.block.decompress(
                        payload_bytes, uncompressed_size=99999
                    )
                except lz4.block.LZ4BlockError:
                    return None
            payload = msgpack.unpackb(payload_bytes, raw=False,
                                      strict_map_key=False)

        return {
            "ver": ver, "cmd": cmd, "seq": seq,
            "opcode": opcode, "payload": payload,
        }

    # ── transport ────────────────────────────────────────────────

    def _recv_exactly(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                return bytes(buf)
            buf.extend(chunk)
        return bytes(buf)

    async def _recv_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while self._connected and self._sock:
            try:
                header = await loop.run_in_executor(
                    None, lambda: self._recv_exactly(10)
                )
                if not header or len(header) < 10:
                    log.warning("Connection closed by server")
                    self._connected = False
                    break

                packed_len = int.from_bytes(header[6:10], "big", signed=False)
                payload_length = packed_len & 0xFFFFFF

                if payload_length > 0:
                    payload_data = await loop.run_in_executor(
                        None, lambda: self._recv_exactly(payload_length)
                    )
                else:
                    payload_data = b""

                raw = header + payload_data
                data = self._unpack(raw)
                if not data:
                    continue

                seq_key = data.get("seq")
                if isinstance(seq_key, int):
                    fut = self._pending.get(seq_key % 256)
                    if fut and not fut.done():
                        fut.set_result(data)

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Error in native recv loop")
                break

    async def _send_and_wait(self, opcode: int,
                              payload: dict[str, Any],
                              timeout: float = 15.0) -> dict[str, Any]:
        if not self._connected or not self._sock:
            raise RuntimeError("Not connected")

        self._seq += 1
        seq = self._seq
        packet = self._pack(11, 0, seq, opcode, payload)

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[seq % 256] = fut

        try:
            await loop.run_in_executor(None, lambda: self._sock.sendall(packet))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(seq % 256, None)
