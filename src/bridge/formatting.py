# Zero-width space prepended to every message the bridge sends to Telegram.
# Listeners check for this marker to recognise bridge-sent messages and avoid
# echo loops.  In regular TG groups message IDs are per-user (sender and
# observer see different IDs), so MirrorTracker alone cannot catch echoes
# when multiple user accounts listen to the same chat.  The marker is
# invisible to end users in all Telegram clients.
MIRROR_MARKER = "\u200b"


def prepend_sender_name(name: str, text: str) -> str:
    return f"[{name}]: {text}"
