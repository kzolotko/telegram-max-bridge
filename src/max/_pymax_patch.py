"""
Runtime monkey-patch for a known bug in the pymax library.

pymax's SocketMixin._unpack_packet uses a hardcoded LZ4 decompression buffer
of 99999 bytes (~97 KB).  Large MAX server responses — particularly the LOGIN
reply that includes full chat/contact lists — can exceed this limit, causing
lz4.block.LZ4BlockError.  When that happens, pymax silently drops the packet
("Failed to unpack packet, skipping"), the _send_and_wait future is never
resolved, and the whole operation times out.

Root cause (pymax comment in the original source):
    # TODO: надо выяснить правильный размер распаковки

The MAX binary protocol prepends 4 big-endian bytes containing the
uncompressed payload size immediately before the LZ4 block data.  The correct
fix is to read that hint and use it as the decompression buffer size.

This module patches SocketMixin._unpack_packet at import time with a corrected
implementation.  It is imported by bridge_client.py so the patch is applied
automatically whenever pymax is used in this project, surviving any reinstall
of the pymax package.

Nothing in this module affects NativeMaxAuth (native_client.py), which has its
own packet parser with the same fix already applied directly in source.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import lz4.block
import msgpack

from pymax.mixins.socket import SocketMixin


def _fixed_unpack_packet(self, data: bytes) -> dict[str, Any] | None:  # noqa: ANN001
    """Patched _unpack_packet with correct LZ4 buffer sizing.

    Changes versus the original:
      - Reads uncompressed size from the 4-byte MAX protocol header that
        precedes the LZ4 block, rather than using the hardcoded 99999.
      - Falls back to an 8 MB ceiling if the hint is missing or implausible.
      - Secondary fallback: retries on the raw payload (no header skip) in
        case the packet layout differs from what we expect.
    """
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
            _max_buf = 8 * 1024 * 1024

            # MAX prepends 4 big-endian bytes = uncompressed size
            _hint = int.from_bytes(payload_bytes[0:4], "big")
            _usize = _hint if 0 < _hint <= _max_buf else _max_buf

            try:
                payload_bytes = lz4.block.decompress(
                    payload_bytes[4:], uncompressed_size=_usize
                )
            except lz4.block.LZ4BlockError:
                # Fallback A: whole slice, large buffer (no 4-byte header skip)
                try:
                    payload_bytes = lz4.block.decompress(
                        payload_bytes, uncompressed_size=_max_buf
                    )
                except lz4.block.LZ4BlockError:
                    return None

        payload = msgpack.unpackb(payload_bytes, raw=False, strict_map_key=False)

    return {
        "ver": ver,
        "cmd": cmd,
        "seq": seq,
        "opcode": opcode,
        "payload": payload,
    }


# ── Patch 2: SocketMixin.connect — cancel orphaned tasks before reconnecting ──
#
# When _send_and_wait() catches an SSL/Connection error, it calls
# self.connect() internally to create a new socket.  connect() creates fresh
# _recv_task and _outgoing_task but NEVER cancels the old ones.  The old
# outgoing_task then races with the new one reading from the same
# self._outgoing queue, and both call sendall() concurrently on the new
# socket, corrupting the protocol stream.  This patch cancels the old tasks
# and closes the old socket before connect() creates new ones.
#
# Note: we intentionally do NOT set settimeout() on the socket.  SSL sockets
# with timeouts cause spurious SSLError during send/recv, breaking pymax's
# _send_and_wait.  Instead, the ping watchdog in listener.py detects dead
# connections and calls disconnect(), which closes the socket — unblocking
# any executor threads stuck in recv()/send().

_original_connect = SocketMixin.connect


async def _patched_connect(self, user_agent=None):  # noqa: ANN001
    """Patched connect() that cancels orphaned recv/outgoing tasks first."""
    old_recv = getattr(self, '_recv_task', None)
    old_out = getattr(self, '_outgoing_task', None)
    old_socket = getattr(self, '_socket', None)

    # Close old socket first so executor threads blocked in recv()/send()
    # unblock immediately and can honour the upcoming task cancellations.
    if old_socket is not None:
        with contextlib.suppress(Exception):
            old_socket.close()

    # Cancel orphaned tasks.
    for task in (old_recv, old_out):
        if task is not None and not task.done():
            task.cancel()

    # Do NOT wait for cancellation to propagate.  Waiting up to 0.5 s creates
    # a gap in which incoming packets can arrive at the TCP layer but neither
    # the dying recv_task nor the not-yet-started new one processes them,
    # causing the test harness's MaxTestClient to miss bridged messages.
    # The socket close above is sufficient: executor threads unblock on their
    # own and their CancelledError is handled asynchronously.

    return await _original_connect(self, user_agent)


if SocketMixin.connect is not _patched_connect:
    SocketMixin.connect = _patched_connect  # type: ignore[method-assign]


# ── Apply patch 1 ─────────────────────────────────────────────────────────────

_original = SocketMixin._unpack_packet

if _original is not _fixed_unpack_packet:
    SocketMixin._unpack_packet = _fixed_unpack_packet  # type: ignore[method-assign]
