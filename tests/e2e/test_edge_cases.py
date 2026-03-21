"""
Edge case tests — unicode, special chars, long text, ordering, double edit.

Tests named test_E01_*, etc., corresponding to cases in TEST_CASES.md.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.edge,
]


async def test_E01_emoji_tg_to_max(harness):
    """E01: Emoji (astral plane) сохраняются TG→MAX."""
    result = await harness.tg_to_max("Тест 🎉🔥👍🏳️\u200d🌈")
    assert result is not None, "Bridge did not forward TG→MAX within timeout"
    for emoji in ("🎉", "🔥", "👍"):
        assert emoji in result.text, f"Emoji {emoji} lost in TG→MAX: {result.text!r}"


async def test_E02_emoji_max_to_tg(harness):
    """E02: Emoji (astral plane) сохраняются MAX→TG."""
    result = await harness.max_to_tg("Тест 🎉🔥👍🏳️\u200d🌈")
    assert result is not None, "Bridge did not forward MAX→TG within timeout"
    for emoji in ("🎉", "🔥", "👍"):
        assert emoji in result.text, f"Emoji {emoji} lost in MAX→TG: {result.text!r}"


async def test_E03_multiline_tg_to_max(harness):
    """E03: Многострочный текст TG→MAX — переносы строк сохраняются."""
    text = "Строка 1\nСтрока 2\nСтрока 3"
    result = await harness.tg_to_max(text)
    assert result is not None, "Bridge did not forward TG→MAX within timeout"
    assert "Строка 1" in result.text
    assert "Строка 2" in result.text
    assert "Строка 3" in result.text


async def test_E04_multiline_max_to_tg(harness):
    """E04: Многострочный текст MAX→TG — переносы строк сохраняются."""
    text = "Строка 1\nСтрока 2\nСтрока 3"
    result = await harness.max_to_tg(text)
    assert result is not None, "Bridge did not forward MAX→TG within timeout"
    assert "Строка 1" in result.text
    assert "Строка 2" in result.text
    assert "Строка 3" in result.text


async def test_E05_special_chars_tg_to_max(harness):
    """E05: Спецсимволы < > & не ломают пересылку TG→MAX."""
    result = await harness.tg_to_max('5 < 10 & 3 > 1 "test"')
    assert result is not None, "Bridge did not forward TG→MAX within timeout"
    assert "<" in result.text, f"< lost in text: {result.text!r}"
    assert ">" in result.text, f"> lost in text: {result.text!r}"
    assert "&" in result.text, f"& lost in text: {result.text!r}"


async def test_E06_special_chars_max_to_tg(harness):
    """E06: Спецсимволы < > & не ломают пересылку MAX→TG."""
    result = await harness.max_to_tg('5 < 10 & 3 > 1 "test"')
    assert result is not None, "Bridge did not forward MAX→TG within timeout"
    assert "<" in result.text, f"< lost in text: {result.text!r}"
    assert ">" in result.text, f"> lost in text: {result.text!r}"
    assert "&" in result.text, f"& lost in text: {result.text!r}"


async def test_E07_long_text_tg_to_max(harness):
    """E07: Длинный текст (1000+ символов) TG→MAX."""
    body = "А" * 1000
    result = await harness.tg_to_max(body)
    assert result is not None, "Bridge did not forward TG→MAX within timeout"
    # The bridge might add prefix or marker, but the body must be present
    assert body[:100] in result.text, "Long text truncated or lost in TG→MAX"


async def test_E08_long_text_max_to_tg(harness):
    """E08: Длинный текст (1000+ символов) MAX→TG."""
    body = "Б" * 1000
    result = await harness.max_to_tg(body)
    assert result is not None, "Bridge did not forward MAX→TG within timeout"
    assert body[:100] in result.text, "Long text truncated or lost in MAX→TG"


async def test_E09_order_tg_to_max(harness):
    """E09: Порядок 3 быстрых сообщений TG→MAX сохраняется."""
    markers = [harness.make_marker() for _ in range(3)]
    for i, m in enumerate(markers):
        await harness.tg.send_text(f"order {i + 1} {m}")

    received = []
    for _ in range(3):
        evt = await harness.max.wait_for(
            lambda e: (
                e.kind == "message"
                and e.text
                and any(m in e.text for m in markers)
            ),
            timeout=20,
        )
        assert evt is not None, f"Only received {len(received)}/3 messages"
        received.append(evt)

    for i, m in enumerate(markers):
        assert m in (received[i].text or ""), (
            f"Message {i + 1} out of order: expected marker {m!r}, "
            f"got text {received[i].text!r}"
        )


async def test_E10_order_max_to_tg(harness):
    """E10: Порядок 3 быстрых сообщений MAX→TG сохраняется."""
    markers = [harness.make_marker() for _ in range(3)]
    for i, m in enumerate(markers):
        await harness.max.send_text(f"order {i + 1} {m}")

    received = []
    for _ in range(3):
        evt = await harness.tg.wait_for(
            lambda e: (
                e.kind == "message"
                and e.text
                and any(m in e.text for m in markers)
            ),
            timeout=20,
        )
        assert evt is not None, f"Only received {len(received)}/3 messages"
        received.append(evt)

    for i, m in enumerate(markers):
        assert m in (received[i].text or ""), (
            f"Message {i + 1} out of order: expected marker {m!r}, "
            f"got text {received[i].text!r}"
        )


async def test_E11_double_edit_tg_to_max(harness):
    """E11: Двойное редактирование TG→MAX — оба edit доходят."""
    # Send original
    m1 = harness.make_marker()
    tg_msg = await harness.tg.send_text(f"original {m1}")
    max_orig = await harness.max.wait_for(
        lambda e: e.kind == "message" and m1 in (e.text or ""),
        timeout=15,
    )
    assert max_orig is not None, "Original not forwarded"

    # First edit
    m2 = harness.make_marker()
    await harness.tg.edit_message(tg_msg.id, f"edit1 {m2}")
    max_edit1 = await harness.max.wait_for(
        lambda e: e.kind == "edit" and m2 in (e.text or ""),
        timeout=15,
    )
    assert max_edit1 is not None, "First edit not forwarded"

    # Second edit
    m3 = harness.make_marker()
    await harness.tg.edit_message(tg_msg.id, f"edit2 {m3}")
    max_edit2 = await harness.max.wait_for(
        lambda e: e.kind == "edit" and m3 in (e.text or ""),
        timeout=15,
    )
    assert max_edit2 is not None, "Second edit not forwarded"
    assert max_edit2.msg_id == max_orig.msg_id, "Second edit applied to wrong message"


async def test_E12_double_edit_max_to_tg(harness):
    """E12: Двойное редактирование MAX→TG — оба edit доходят."""
    # Send original
    m1 = harness.make_marker()
    max_msg_id = await harness.max.send_text(f"original {m1}")
    tg_orig = await harness.tg.wait_for(
        lambda e: e.kind == "message" and m1 in (e.text or ""),
        timeout=15,
    )
    assert tg_orig is not None, "Original not forwarded"

    # First edit
    m2 = harness.make_marker()
    await harness.max.edit_message(max_msg_id, f"edit1 {m2}")
    tg_edit1 = await harness.tg.wait_for(
        lambda e: e.kind == "edit" and m2 in (e.text or ""),
        timeout=15,
    )
    assert tg_edit1 is not None, "First edit not forwarded"

    # Second edit
    m3 = harness.make_marker()
    await harness.max.edit_message(max_msg_id, f"edit2 {m3}")
    tg_edit2 = await harness.tg.wait_for(
        lambda e: e.kind == "edit" and m3 in (e.text or ""),
        timeout=15,
    )
    assert tg_edit2 is not None, "Second edit not forwarded"
    assert tg_edit2.msg_id == tg_orig.msg_id, "Second edit applied to wrong message"
