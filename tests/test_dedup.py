"""Unit tests for redelivered-update suppression (src/dedup.py + listeners).

Regression cover for the 2026-08-21 production incident: Telegram redelivered
one text message seven times while the MTProto connection was degrading, and
the bridge mirrored all seven copies into MAX.
"""

import pytest

from src.dedup import SeenIds
from src.telegram.listener import TelegramListener
from src.types import AppConfig, BridgeEntry, UserMapping
from src.config import ConfigLookup


# ── SeenIds ──────────────────────────────────────────────────────────────────

def test_seen_first_time_is_false_then_true():
    seen = SeenIds("t")
    assert seen.seen(("msg", 1, 100)) is False
    assert seen.seen(("msg", 1, 100)) is True
    assert seen.seen(("msg", 1, 100)) is True


def test_seen_distinguishes_keys():
    seen = SeenIds("t")
    assert seen.seen(("msg", 1, 100)) is False
    assert seen.seen(("msg", 2, 100)) is False   # other chat
    assert seen.seen(("msg", 1, 101)) is False   # other message
    assert seen.seen(("del", 1, 100)) is False   # other event kind


def test_seen_evicts_oldest_when_full():
    seen = SeenIds("t", max_size=100)
    for i in range(101):
        seen.seen(("msg", 1, i))
    assert len(seen) <= 100
    # The oldest keys were evicted, the newest are still remembered.
    assert seen.seen(("msg", 1, 0)) is False
    assert seen.seen(("msg", 1, 100)) is True


# ── TelegramListener ─────────────────────────────────────────────────────────

class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.first_name = "Sender"
        self.last_name = None


class _FakeMessage:
    """Minimal stand-in for a Pyrogram text Message."""

    def __init__(self, chat_id, msg_id, text, sender_id=555, edit_date=None):
        self.chat = _FakeChat(chat_id)
        self.id = msg_id
        self.text = text
        self.from_user = _FakeUser(sender_id)
        self.edit_date = edit_date

    def __getattr__(self, name):
        # Every media/caption/reply attribute the handler probes is absent.
        return None


class _FakeMirrorTracker:
    def is_tg_mirror(self, msg_id):
        return False


def _make_listener(events):
    user = UserMapping(name="alice", telegram_user_id=555, max_user_id=777)
    entry = BridgeEntry(
        name="children", telegram_chat_id=-1001, max_chat_id=2002, user=user,
    )
    config = AppConfig(api_id=1, api_hash="h", users=[user], bridges=[entry])

    async def on_event(event):
        events.append(event)

    return TelegramListener(
        config=config,
        lookup=ConfigLookup(config),
        mirror_tracker=_FakeMirrorTracker(),
        on_event=on_event,
        client=None,
        # A user that is NOT a bridge account, so the handler's 2 s
        # echo-suppression delay does not apply.
        user=UserMapping(name="alice", telegram_user_id=999, max_user_id=888),
    )


async def test_redelivered_message_forwarded_once():
    events = []
    listener = _make_listener(events)
    msg = _FakeMessage(-1001, 4242, "да мы наверное какую-нибудь пасту-пиццу будем")

    for _ in range(7):  # exactly what production saw
        await listener._handle_message(None, msg)

    assert len(events) == 1
    assert events[0].text == "да мы наверное какую-нибудь пасту-пиццу будем"


async def test_distinct_messages_still_forwarded():
    events = []
    listener = _make_listener(events)

    await listener._handle_message(None, _FakeMessage(-1001, 1, "one"))
    await listener._handle_message(None, _FakeMessage(-1001, 2, "two"))
    await listener._handle_message(None, _FakeMessage(-1001, 2, "two"))

    assert [e.text for e in events] == ["one", "two"]


async def test_repeated_identical_text_from_user_is_not_dropped():
    """Two separate sends of the same text carry different message IDs."""
    events = []
    listener = _make_listener(events)

    await listener._handle_message(None, _FakeMessage(-1001, 10, "ок"))
    await listener._handle_message(None, _FakeMessage(-1001, 11, "ок"))

    assert len(events) == 2


async def test_edit_dedupes_per_revision():
    import datetime

    events = []
    listener = _make_listener(events)
    t1 = datetime.datetime(2026, 8, 21, 17, 58, 0)
    t2 = datetime.datetime(2026, 8, 21, 17, 59, 0)

    rev1 = _FakeMessage(-1001, 4242, "first edit", edit_date=t1)
    rev2 = _FakeMessage(-1001, 4242, "second edit", edit_date=t2)

    await listener._handle_edited_message(None, rev1)
    await listener._handle_edited_message(None, rev1)  # redelivery
    await listener._handle_edited_message(None, rev2)

    assert [e.text for e in events] == ["first edit", "second edit"]
