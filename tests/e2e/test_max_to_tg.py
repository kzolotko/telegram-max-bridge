"""
MAX → TG direction tests.

Tests named test_M01_*, test_M10_*, etc., corresponding to cases in TEST_CASES.md.

Prerequisites:
  - Bridge is running
  - E2E config exists (tests/e2e/e2e_config.yaml)
  - E2E TG session is authenticated
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.max_to_tg,
]


async def test_M01_text_registered(harness):
    """M01: MAX→TG текст от зарегистрированного пользователя."""
    result = await harness.max_to_tg("Привет из MAX")
    assert result is not None, "Bridge did not forward MAX→TG within timeout"
    assert "Привет из MAX" in (result.text or ""), (
        f"Expected 'Привет из MAX' in forwarded text, got: {result.text!r}"
    )


async def test_M10_reply_to_tg_origin(harness):
    """M10: MAX→TG reply на TG-origin сообщение."""
    # Step 1: send original TG message, wait for it in MAX
    marker1 = harness.make_marker()
    tg_sent = await harness.tg.send_text(f"tg origin {marker1}")
    max_orig = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker1 in (e.text or ""),
        timeout=15,
    )
    assert max_orig is not None, f"Bridge did not forward TG message to MAX (marker={marker1})"

    # Step 2: send MAX reply to the bridged message
    marker2 = harness.make_marker()
    await harness.max.send_text(f"max reply {marker2}", reply_to=max_orig.msg_id)

    # Step 3: wait for the reply in TG
    tg_reply = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker2 in (e.text or ""),
        timeout=15,
    )
    assert tg_reply is not None, f"Bridge did not forward MAX reply to TG (marker={marker2})"

    # Step 4: verify it replies to the original TG message
    assert tg_reply.get_reply_to_id() == str(tg_sent.id), (
        f"TG reply points to {tg_reply.get_reply_to_id()!r}, "
        f"expected {tg_sent.id!r}"
    )


async def test_M11_reply_to_max_origin(harness):
    """M11: MAX→TG reply на MAX-origin сообщение."""
    # Step 1: send original MAX message
    marker1 = harness.make_marker()
    max_msg_id = await harness.max.send_text(f"max origin {marker1}")

    # Step 2: wait for it to arrive in TG
    tg_orig = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker1 in (e.text or ""),
        timeout=15,
    )
    assert tg_orig is not None, f"Bridge did not forward MAX message to TG (marker={marker1})"

    # Step 3: send MAX reply to the original MAX message
    marker2 = harness.make_marker()
    await harness.max.send_text(f"max reply {marker2}", reply_to=max_msg_id)

    # Step 4: wait for the reply in TG
    tg_reply = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker2 in (e.text or ""),
        timeout=15,
    )
    assert tg_reply is not None, f"Bridge did not forward MAX reply to TG (marker={marker2})"

    # Step 5: verify it replies to the original TG-mirrored message
    assert tg_reply.get_reply_to_id() == tg_orig.msg_id, (
        f"TG reply points to {tg_reply.get_reply_to_id()!r}, "
        f"expected {tg_orig.msg_id!r}"
    )


async def test_M13_edit(harness):
    """M13: MAX→TG редактирование текста."""
    # Step 1: send original MAX message
    marker1 = harness.make_marker()
    max_msg_id = await harness.max.send_text(f"original {marker1}")

    # Step 2: wait for it in TG
    tg_orig = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker1 in (e.text or ""),
        timeout=15,
    )
    assert tg_orig is not None, f"Bridge did not forward MAX message to TG (marker={marker1})"

    # Step 3: edit the MAX message
    marker2 = harness.make_marker()
    await harness.max.edit_message(max_msg_id, f"edited {marker2}")

    # Step 4: wait for the edit to arrive in TG
    tg_edit = await harness.tg.wait_for(
        lambda e: e.kind == "edit" and marker2 in (e.text or ""),
        timeout=15,
    )
    assert tg_edit is not None, f"Bridge did not forward MAX edit to TG (marker={marker2})"

    # Step 5: the edit should update the same TG message
    assert tg_edit.msg_id == tg_orig.msg_id, (
        f"TG edit msg_id={tg_edit.msg_id!r} != original msg_id={tg_orig.msg_id!r}"
    )


async def test_M14_delete(harness):
    """M14: MAX→TG удаление сообщения."""
    # Step 1: send MAX message
    marker = harness.make_marker()
    max_msg_id = await harness.max.send_text(f"to delete {marker}")

    # Step 2: wait for it in TG
    tg_orig = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert tg_orig is not None, f"Bridge did not forward MAX message to TG (marker={marker})"

    # Step 3: delete the MAX message
    await harness.max.delete_messages([max_msg_id])

    # Step 4: wait for the delete to propagate to TG
    tg_delete = await harness.tg.wait_for(
        lambda e: e.kind == "delete" and e.msg_id == tg_orig.msg_id,
        timeout=15,
    )
    assert tg_delete is not None, (
        f"Bridge did not propagate MAX delete to TG (expected msg_id={tg_orig.msg_id!r})"
    )


async def test_M15_echo_loop(harness):
    """M15: MAX→TG эхо-петля отсутствует."""
    marker = harness.make_marker()

    # Step 1: send from MAX
    await harness.max.send_text(f"echo test {marker}")

    # Step 2: must arrive in TG (confirms M01 is working)
    tg_msg = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert tg_msg is not None, f"Bridge did not forward MAX message to TG (marker={marker})"

    # Step 3: wait to make sure the same marker does NOT bounce back to MAX
    echo = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=6,
    )
    assert echo is None, (
        f"Echo loop detected! Marker {marker!r} arrived back in MAX: {echo.text!r}"
    )

# ── File and audio ────────────────────────────────────────────────────────────

async def test_M06_document_max_to_tg(harness):
    """M06: MAX→TG файл/документ — arrives in TG as document."""
    marker = harness.make_marker()
    content = b"E2E test document from MAX\n"
    await harness.max.send_file(
        content, "test.txt", "text/plain", caption=marker,
    )

    result = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=20,
    )
    assert result is not None, "Bridge did not forward document MAX→TG"
    has_media = (
        getattr(result.raw, "document", None) is not None
        or getattr(result.raw, "photo", None) is not None
    )
    assert has_media, "No document/photo in TG message"


async def test_M07_audio_max_to_tg(harness):
    """M07: MAX→TG аудио — arrives in TG as audio or document."""
    from .media_fixtures import make_test_wav
    marker = harness.make_marker()
    await harness.max.send_file(
        make_test_wav(), "audio.wav", "audio/wav", caption=marker,
    )

    result = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=20,
    )
    assert result is not None, "Bridge did not forward audio MAX→TG"
    has_media = (
        getattr(result.raw, "audio", None) is not None
        or getattr(result.raw, "document", None) is not None
        or getattr(result.raw, "voice", None) is not None
    )
    assert has_media, "No audio/document in TG message"
