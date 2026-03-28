"""Ring-buffer logging handler for serving recent logs via admin bot."""

import logging
from collections import deque


class LogRingBuffer(logging.Handler):
    """Stores the last *capacity* formatted log lines in memory."""

    def __init__(self, capacity: int = 200):
        super().__init__()
        self._buffer: deque[tuple[str, str]] = deque(maxlen=capacity)
        # Each entry: (level_name, formatted_message)

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            self._buffer.append((record.levelname, msg))
        except Exception:
            self.handleError(record)

    def get_recent(self, count: int = 20, level: str | None = None) -> list[str]:
        """Return last *count* log lines, optionally filtered by level."""
        entries = list(self._buffer)
        if level:
            lvl = level.upper()
            entries = [(l, m) for l, m in entries if l == lvl]
        return [m for _, m in entries[-count:]]
