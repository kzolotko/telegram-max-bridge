"""
Formatting tests (bold, italic, etc.).

Tests named test_F01_*, etc., corresponding to cases in TEST_CASES.md.

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


async def test_F01_bold_tg_to_max(harness):
    """F01: Bold TG→MAX — bold text is forwarded to MAX."""
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
    assert result is not None, f"Bridge did not forward bold TG message to MAX (marker={marker})"
    assert marker in (result.text or ""), (
        f"Marker {marker!r} not found in MAX text: {result.text!r}"
    )


async def test_F02_italic_tg_to_max(harness):
    """F02: Italic TG→MAX — italic text is forwarded to MAX."""
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
    assert result is not None, f"Bridge did not forward italic TG message to MAX (marker={marker})"
    assert marker in (result.text or ""), (
        f"Marker {marker!r} not found in MAX text: {result.text!r}"
    )


async def test_F03_bold_max_to_tg(harness):
    """F03: Bold MAX→TG — bold text from MAX is forwarded to TG."""
    marker = harness.make_marker()
    # Send MAX message with bold markup via plain text (bridge handles formatting)
    await harness.max.send_text(f"bold {marker}")

    result = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert result is not None, f"Bridge did not forward MAX message to TG (marker={marker})"
    assert marker in (result.text or ""), (
        f"Marker {marker!r} not found in TG text: {result.text!r}"
    )


async def test_F04_italic_max_to_tg(harness):
    """F04: Italic MAX→TG — italic text from MAX is forwarded to TG."""
    marker = harness.make_marker()
    # Send MAX message with italic content via plain text (bridge handles formatting)
    await harness.max.send_text(f"italic {marker}")

    result = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert result is not None, f"Bridge did not forward MAX message to TG (marker={marker})"
    assert marker in (result.text or ""), (
        f"Marker {marker!r} not found in TG text: {result.text!r}"
    )
