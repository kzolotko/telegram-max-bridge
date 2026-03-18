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
"""

import logging

log = logging.getLogger("bridge.mirror_tracker")


class MirrorTracker:
    """In-memory set of recently sent mirror message IDs."""

    def __init__(self):
        self._tg: set[int] = set()   # TG message IDs we sent as mirrors
        self._max: set[str] = set()  # MAX message IDs we sent as mirrors

    # ── mark sent ─────────────────────────────────────────────────────────────

    def mark_tg(self, msg_id: int):
        """Called after the bridge sends a mirror message to Telegram."""
        log.debug("mark_tg: %s  (tracked=%d)", msg_id, len(self._tg))
        self._tg.add(msg_id)

    def mark_max(self, msg_id: str):
        """Called after the bridge sends a mirror message to MAX."""
        log.debug("mark_max: %r  (tracked=%d)", msg_id, len(self._max))
        self._max.add(msg_id)

    # ── check ─────────────────────────────────────────────────────────────────

    def is_tg_mirror(self, msg_id: int) -> bool:
        """Returns True if this TG message ID was sent by the bridge."""
        result = msg_id in self._tg
        log.debug("is_tg_mirror(%s) -> %s  known=%s", msg_id, result, sorted(self._tg)[-5:] if self._tg else [])
        return result

    def is_max_mirror(self, msg_id: str) -> bool:
        """Returns True if this MAX message ID was sent by the bridge."""
        result = msg_id in self._max
        log.debug("is_max_mirror(%r) -> %s  known=%s", msg_id, result, list(self._max)[-5:] if self._max else [])
        return result
