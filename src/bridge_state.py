"""Shared mutable state for bridge pause/resume control."""


class BridgeState:
    """Tracks global and per-bridge pause flags.

    All methods are synchronous — no locks needed since we run
    on a single-threaded asyncio event loop.
    """

    def __init__(self):
        self._paused_global: bool = False
        self._paused_bridges: set[str] = set()

    @property
    def is_globally_paused(self) -> bool:
        return self._paused_global

    def get_paused_bridges(self) -> set[str]:
        return set(self._paused_bridges)

    def should_forward(self, bridge_name: str) -> bool:
        return not self._paused_global and bridge_name not in self._paused_bridges

    def pause_global(self):
        self._paused_global = True

    def resume_global(self):
        self._paused_global = False
        self._paused_bridges.clear()

    def pause_bridge(self, name: str):
        self._paused_bridges.add(name)

    def resume_bridge(self, name: str):
        self._paused_bridges.discard(name)
