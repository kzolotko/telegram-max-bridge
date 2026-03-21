"""
Tracks message IDs of mirrors sent by the bridge to prevent echo loops.

Unlike sender-ID-based echo guard, this approach correctly distinguishes:
  - original messages (from_user → should forward)
  - mirror messages (sent by bridge → should NOT forward again)

Usage:
  After sending a mirror to TG:   tracker.mark_tg(msg.id)
  After sending a mirror to MAX:  tracker.mark_max(max_msg_id)
  In TG listener:                 if tracker.is_tg_mirror(message.id): return
  In MAX listener:                if tracker.is_max_mirror(msg_id): return

Mirror IDs are automatically evicted when the set exceeds MAX_SIZE to
prevent unbounded memory growth during long bridge uptime.
"""

import logging
from collections import OrderedDict

log = logging.getLogger("bridge.mirror_tracker")

# Keep at most this many mirror IDs per direction.
# At ~1 msg/sec this covers ~2.7 hours, more than enough for echo detection.
MAX_SIZE = 10_000


class MirrorTracker:
    """In-memory LRU set of recently sent mirror message IDs."""

    def __init__(self):
        # OrderedDict gives us O(1) insertion-order eviction.
        # Values are unused (always True) — we only care about key membership.
        self._tg: OrderedDict[int, bool] = OrderedDict()
        self._max: OrderedDict[str, bool] = OrderedDict()

    # ── mark sent ─────────────────────────────────────────────────────────────

    def mark_tg(self, msg_id: int):
        """Called after the bridge sends a mirror message to Telegram."""
        self._tg[msg_id] = True
        if len(self._tg) > MAX_SIZE:
            # Evict oldest 10%
            for _ in range(MAX_SIZE // 10):
                self._tg.popitem(last=False)
        log.debug("mark_tg: %s  (tracked=%d)", msg_id, len(self._tg))

    def mark_max(self, msg_id: str):
        """Called after the bridge sends a mirror message to MAX."""
        self._max[msg_id] = True
        if len(self._max) > MAX_SIZE:
            for _ in range(MAX_SIZE // 10):
                self._max.popitem(last=False)
        log.debug("mark_max: %r  (tracked=%d)", msg_id, len(self._max))

    # ── check ─────────────────────────────────────────────────────────────────

    def is_tg_mirror(self, msg_id: int) -> bool:
        """Returns True if this TG message ID was sent by the bridge."""
        result = msg_id in self._tg
        log.debug("is_tg_mirror(%s) -> %s  (tracked=%d)", msg_id, result, len(self._tg))
        return result

    def is_max_mirror(self, msg_id: str) -> bool:
        """Returns True if this MAX message ID was sent by the bridge."""
        result = msg_id in self._max
        log.debug("is_max_mirror(%r) -> %s  (tracked=%d)", msg_id, result, len(self._max))
        return result
