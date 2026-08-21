"""Idempotency guard for redelivered platform updates.

Both platforms can deliver the same update more than once:

* Telegram — MTProto redelivers unacknowledged update containers when the
  connection degrades, and Pyrogram (2.0.106) does not track seen updates.
  Every redelivery is dispatched to the handlers again.  Observed in
  production on 2026-08-21: one text message forwarded to MAX seven times
  within four seconds, right before Pyrogram logged repeated
  ``updates.GetDifference`` retries and "Connection lost".
* MAX — the listener reconnects several times a day; a resumed session can
  replay packets the previous connection already delivered.

The guard is a bounded FIFO of keys.  ``seen()`` performs its check and
insert without awaiting, so concurrent handler tasks (Pyrogram's dispatcher
runs several) cannot both pass for the same key.
"""

import logging

log = logging.getLogger("bridge.dedup")


class SeenIds:
    """Bounded set of recently processed update keys (FIFO eviction)."""

    def __init__(self, name: str, max_size: int = 20_000):
        self._name = name
        self._max_size = max_size
        # dict-as-ordered-set: keys in insertion order, values unused
        self._keys: dict[tuple, None] = {}

    def seen(self, key: tuple) -> bool:
        """Return True if *key* was already processed; otherwise record it.

        Must stay synchronous — the check and the insert have to happen in a
        single event-loop step so duplicates racing through concurrent tasks
        cannot both see an empty set.
        """
        if key in self._keys:
            log.warning("%s: duplicate update %s — dropping", self._name, key)
            return True
        self._keys[key] = None
        if len(self._keys) > self._max_size:
            # Evict the oldest 10% (dict preserves insertion order)
            for old in list(self._keys)[: self._max_size // 10]:
                del self._keys[old]
        return False

    def __len__(self) -> int:
        return len(self._keys)
