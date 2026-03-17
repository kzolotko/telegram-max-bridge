class EchoGuard:
    """Prevents echo loops by tracking managed account IDs."""

    def __init__(self):
        self._tg_user_ids: set[int] = set()
        self._max_user_ids: set[int] = set()

    def add_tg_user_id(self, user_id: int):
        self._tg_user_ids.add(user_id)

    def add_max_user_id(self, user_id: int):
        self._max_user_ids.add(user_id)

    def is_managed_tg_user(self, user_id: int) -> bool:
        return user_id in self._tg_user_ids

    def is_managed_max_user(self, user_id: int) -> bool:
        return user_id in self._max_user_ids
