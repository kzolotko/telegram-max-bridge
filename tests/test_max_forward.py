"""Unit tests for forwarded MAX messages (src/max/listener.py).

Regression cover for the 2026-09-12 production incident: a photo forwarded
inside MAX never reached Telegram.  MAX delivers a forward as an empty
envelope — ``text == ""``, ``attaches == []`` — with the original message
under ``link.message`` (``link.type == "FORWARD"``).  The listener only knew
about ``REPLY`` links, so the envelope produced no event and no log line.
"""

import pytest

from src.config import ConfigLookup
from src.max import listener as listener_mod
from src.max.listener import MaxListener
from src.types import AppConfig, BridgeEntry, UserMapping

CHAT = -72099589405396
FORWARDER = 25471135
ORIGIN = 225147687


class _FakeMirrorTracker:
    def is_max_mirror(self, msg_id):
        return False


def _make_listener(events, names=None):
    user = UserMapping(name="kzolotko", telegram_user_id=555, max_user_id=777)
    entry = BridgeEntry(
        name="children", telegram_chat_id=-1001, max_chat_id=CHAT, user=user,
    )
    config = AppConfig(api_id=1, api_hash="h", users=[user], bridges=[entry])

    async def on_event(event):
        events.append(event)

    lst = MaxListener(
        config=config, lookup=ConfigLookup(config),
        mirror_tracker=_FakeMirrorTracker(), on_event=on_event, user=user,
    )
    # No network in unit tests: seed the name cache instead of resolving.
    lst._name_cache.update(names or {FORWARDER: "Оля", ORIGIN: "Бабушка"})
    return lst


def _photo_attach(photo_id=40455703591):
    return {
        "_type": "PHOTO", "photoId": photo_id, "width": 1920, "height": 1268,
        "baseUrl": f"https://i.oneme.ru/i?r={photo_id}",
        "photoToken": "tok", "previewData": b"RIFF",
    }


def _forward_packet(inner_text="", inner_attaches=None, own_text="",
                    inner_elements=None, msg_id=117258522280269905):
    """The exact envelope shape captured from CHAT_HISTORY on 2026-09-12."""
    return {
        "opcode": 128,
        "payload": {
            "chatId": CHAT,
            "message": {
                "id": msg_id, "time": 1789223057255, "type": "USER",
                "sender": FORWARDER, "cid": 1789222244192,
                "text": own_text, "attaches": [],
                "link": {
                    "type": "FORWARD",
                    "chatId": -72041961359904,
                    "message": {
                        "id": 117253576957107250, "time": 1789147597612,
                        "type": "USER", "sender": ORIGIN,
                        "text": inner_text,
                        "attaches": inner_attaches or [],
                        **({"elements": inner_elements} if inner_elements else {}),
                    },
                },
                "reactionInfo": {},
            },
        },
    }


@pytest.fixture
def fake_download(monkeypatch):
    calls = []

    async def download_media(url):
        calls.append(url)
        return b"jpegbytes"

    monkeypatch.setattr(listener_mod, "download_media", download_media)
    return calls


async def test_forwarded_photo_is_mirrored(fake_download):
    events = []
    lst = _make_listener(events)

    await lst._handle_message(_forward_packet(inner_attaches=[_photo_attach()]))

    assert len(events) == 1
    ev = events[0]
    assert ev.direction == "max-to-tg"
    assert ev.event_type == "photo"
    assert ev.media.data == b"jpegbytes"
    assert ev.sender_user_id == FORWARDER
    assert ev.text == "↪ Переслано от Бабушка"
    assert fake_download == ["https://i.oneme.ru/i?r=40455703591"]


async def test_forwarded_text_keeps_formatting_offsets():
    events = []
    lst = _make_listener(events)
    pkt = _forward_packet(
        inner_text="важно: завтра",
        inner_elements=[{"type": "STRONG", "from": 0, "length": 5}],
    )

    await lst._handle_message(pkt)

    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "text"
    prefix = "↪ Переслано от Бабушка\n"
    assert ev.text == prefix + "важно: завтра"
    assert ev.formatting[0]["offset"] == len(prefix)
    assert ev.text[ev.formatting[0]["offset"]:][:5] == "важно"


async def test_forwarder_comment_is_appended():
    events = []
    lst = _make_listener(events)

    await lst._handle_message(_forward_packet(inner_text="ага", own_text="смотри"))

    assert events[0].text == "↪ Переслано от Бабушка\nага\nсмотри"


async def test_unresolvable_origin_omits_name():
    events = []
    lst = _make_listener(events, names={FORWARDER: "Оля", ORIGIN: f"User:{ORIGIN}"})

    await lst._handle_message(_forward_packet(inner_text="привет"))

    assert events[0].text == "↪ Переслано\nпривет"


async def test_plain_message_is_unaffected():
    events = []
    lst = _make_listener(events)
    pkt = _forward_packet(msg_id=1)
    pkt["payload"]["message"].pop("link")
    pkt["payload"]["message"]["text"] = "обычный текст"

    await lst._handle_message(pkt)

    assert events[0].text == "обычный текст"
    assert events[0].formatting is None
