"""
Formatting tests — bold, italic, underline, strikethrough, mixed, code.

Tests named test_F01_*, etc., corresponding to cases in TEST_CASES.md.

F01-F06: TG→MAX direction — checks MAX message has correct elements.
F07-F10: MAX→TG direction — checks TG message has correct entities.

Prerequisites:
  - Bridge is running
  - E2E config exists (tests/e2e/e2e_config.yaml)
  - E2E TG session is authenticated
"""

from __future__ import annotations

import pytest
from pyrogram.enums import ParseMode

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.formatting,
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _max_has_element_type(raw: dict, max_type: str) -> bool:
    """Check if a MAX message dict has an element of the given type."""
    elements = raw.get("elements", [])
    for el in elements:
        el_type = el.get("type", "") if isinstance(el, dict) else getattr(el, "type", "")
        if hasattr(el_type, "value"):
            el_type = el_type.value
        if el_type == max_type:
            return True
    return False


def _tg_has_entity_type(raw, tg_type_name: str) -> bool:
    """Check if a Pyrogram Message has an entity of the given type name."""
    entities = getattr(raw, "entities", None) or []
    for ent in entities:
        name = ent.type.name if hasattr(ent.type, "name") else str(ent.type)
        if name == tg_type_name:
            return True
    return False


# ── TG → MAX ─────────────────────────────────────────────────────────────────

async def test_F01_bold_tg_to_max(harness):
    """F01: Bold TG→MAX — bold text is forwarded with STRONG element."""
    marker = harness.make_marker()
    await harness.tg._client.send_message(
        harness.tg.chat_id,
        f"**bold {marker}**",
        parse_mode=ParseMode.MARKDOWN,
    )
    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert result is not None, "Bridge did not forward bold TG message to MAX"
    assert _max_has_element_type(result.raw, "STRONG"), (
        f"No STRONG element in MAX message. Elements: {result.raw.get('elements')}"
    )


async def test_F02_italic_tg_to_max(harness):
    """F02: Italic TG→MAX — italic text is forwarded with EMPHASIZED element."""
    marker = harness.make_marker()
    await harness.tg._client.send_message(
        harness.tg.chat_id,
        f"__italic {marker}__",
        parse_mode=ParseMode.MARKDOWN,
    )
    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert result is not None, "Bridge did not forward italic TG message to MAX"
    assert _max_has_element_type(result.raw, "EMPHASIZED"), (
        f"No EMPHASIZED element in MAX message. Elements: {result.raw.get('elements')}"
    )


async def test_F05_underline_tg_to_max(harness):
    """F05: Underline TG→MAX — underline is forwarded with UNDERLINE element."""
    marker = harness.make_marker()
    # Pyrogram HTML mode supports <u> for underline
    await harness.tg._client.send_message(
        harness.tg.chat_id,
        f"<u>underline {marker}</u>",
        parse_mode=ParseMode.HTML,
    )
    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert result is not None, "Bridge did not forward underline TG message to MAX"
    assert _max_has_element_type(result.raw, "UNDERLINE"), (
        f"No UNDERLINE element in MAX message. Elements: {result.raw.get('elements')}"
    )


async def test_F06_strikethrough_tg_to_max(harness):
    """F06: Strikethrough TG→MAX — strikethrough is forwarded with STRIKETHROUGH element."""
    marker = harness.make_marker()
    await harness.tg._client.send_message(
        harness.tg.chat_id,
        f"<s>strike {marker}</s>",
        parse_mode=ParseMode.HTML,
    )
    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert result is not None, "Bridge did not forward strikethrough TG message to MAX"
    assert _max_has_element_type(result.raw, "STRIKETHROUGH"), (
        f"No STRIKETHROUGH element in MAX message. Elements: {result.raw.get('elements')}"
    )


async def test_F09_mixed_tg_to_max(harness):
    """F09: Смешанное форматирование (bold + italic) TG→MAX."""
    marker = harness.make_marker()
    await harness.tg._client.send_message(
        harness.tg.chat_id,
        f"<b>bold</b> and <i>italic {marker}</i>",
        parse_mode=ParseMode.HTML,
    )
    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert result is not None, "Bridge did not forward mixed-format TG message to MAX"
    assert _max_has_element_type(result.raw, "STRONG"), "Missing STRONG in mixed format"
    assert _max_has_element_type(result.raw, "EMPHASIZED"), "Missing EMPHASIZED in mixed format"


async def test_F10_code_tg_to_max(harness):
    """F10: Code block TG→MAX — code is forwarded as plain text (MAX has no code support)."""
    marker = harness.make_marker()
    await harness.tg._client.send_message(
        harness.tg.chat_id,
        f"`code {marker}`",
        parse_mode=ParseMode.MARKDOWN,
    )
    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert result is not None, "Bridge did not forward code TG message to MAX"
    # Code content should be present as plain text (MAX doesn't support code elements)
    assert f"code {marker}" in result.text, (
        f"Code text lost in forwarding: {result.text!r}"
    )


# ── MAX → TG ─────────────────────────────────────────────────────────────────

async def test_F03_bold_max_to_tg(harness):
    """F03: Bold MAX→TG — bold text from MAX arrives in TG with BOLD entity."""
    marker = harness.make_marker()
    text = f"bold {marker}"
    elements = [{"type": "STRONG", "from": 0, "length": len("bold")}]
    await harness.max.send_text_with_elements(text, elements)

    result = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert result is not None, "Bridge did not forward bold MAX message to TG"
    assert _tg_has_entity_type(result.raw, "BOLD"), (
        f"No BOLD entity in TG message. Entities: {getattr(result.raw, 'entities', None)}"
    )


async def test_F04_italic_max_to_tg(harness):
    """F04: Italic MAX→TG — italic text from MAX arrives in TG with ITALIC entity."""
    marker = harness.make_marker()
    text = f"italic {marker}"
    elements = [{"type": "EMPHASIZED", "from": 0, "length": len("italic")}]
    await harness.max.send_text_with_elements(text, elements)

    result = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert result is not None, "Bridge did not forward italic MAX message to TG"
    assert _tg_has_entity_type(result.raw, "ITALIC"), (
        f"No ITALIC entity in TG message. Entities: {getattr(result.raw, 'entities', None)}"
    )


async def test_F07_underline_max_to_tg(harness):
    """F07: Underline MAX→TG — underline from MAX arrives in TG with UNDERLINE entity."""
    marker = harness.make_marker()
    text = f"underline {marker}"
    elements = [{"type": "UNDERLINE", "from": 0, "length": len("underline")}]
    await harness.max.send_text_with_elements(text, elements)

    result = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert result is not None, "Bridge did not forward underline MAX message to TG"
    assert _tg_has_entity_type(result.raw, "UNDERLINE"), (
        f"No UNDERLINE entity in TG message. Entities: {getattr(result.raw, 'entities', None)}"
    )


async def test_F08_strikethrough_max_to_tg(harness):
    """F08: Strikethrough MAX→TG — strikethrough from MAX arrives in TG with STRIKETHROUGH entity."""
    marker = harness.make_marker()
    text = f"strike {marker}"
    elements = [{"type": "STRIKETHROUGH", "from": 0, "length": len("strike")}]
    await harness.max.send_text_with_elements(text, elements)

    result = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert result is not None, "Bridge did not forward strikethrough MAX message to TG"
    assert _tg_has_entity_type(result.raw, "STRIKETHROUGH"), (
        f"No STRIKETHROUGH entity in TG message. Entities: {getattr(result.raw, 'entities', None)}"
    )
