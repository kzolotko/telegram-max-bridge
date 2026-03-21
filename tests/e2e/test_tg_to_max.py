"""
TG → MAX direction tests.

Tests named test_T01_*, test_T11_*, etc., corresponding to cases in TEST_CASES.md.

Prerequisites:
  - Bridge is running
  - E2E config exists (tests/e2e/e2e_config.yaml)
  - E2E TG session is authenticated
"""

from __future__ import annotations

import asyncio
import os

import pytest

from .media_fixtures import make_test_wav, make_test_ogg, save_temp_media

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.tg_to_max,
]


# ── Session-scoped media file fixtures ────────────────────────────────────────

@pytest.fixture(scope="session")
def _wav_path() -> str:
    path = save_temp_media(make_test_wav(), ".wav")
    yield path
    os.unlink(path)


@pytest.fixture(scope="session")
def _ogg_path() -> str:
    path = save_temp_media(make_test_ogg(), ".ogg")
    yield path
    os.unlink(path)


@pytest.fixture(scope="session")
def _doc_path() -> str:
    path = save_temp_media(b"E2E test document content\n", ".txt")
    yield path
    os.unlink(path)


async def test_T01_text_registered(harness):
    """T01: TG→MAX текст от зарегистрированного пользователя."""
    result = await harness.tg_to_max("Привет из Telegram")
    assert result is not None, "Bridge did not forward TG→MAX within timeout"
    assert "Привет из Telegram" in (result.text or ""), (
        f"Expected 'Привет из Telegram' in forwarded text, got: {result.text!r}"
    )


async def test_T11_reply_to_bridged(harness):
    """T11: TG→MAX reply на сообщение, прошедшее через бридж."""
    # Step 1: send original TG message
    marker1 = harness.make_marker()
    tg_msg = await harness.tg.send_text(f"original {marker1}")
    original_tg_msg_id = tg_msg.id

    # Step 2: wait for it to arrive in MAX
    max_orig = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker1 in (e.text or ""),
        timeout=15,
    )
    assert max_orig is not None, f"Bridge did not forward original TG message to MAX (marker={marker1})"

    # Step 3: send TG reply to the original message
    marker2 = harness.make_marker()
    await harness.tg.send_text(f"reply {marker2}", reply_to=original_tg_msg_id)

    # Step 4: wait for the reply to arrive in MAX
    max_reply = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker2 in (e.text or ""),
        timeout=15,
    )
    assert max_reply is not None, f"Bridge did not forward TG reply to MAX (marker={marker2})"

    # Step 5: verify the reply links to the correct MAX message
    assert max_reply.get_reply_to_id() == max_orig.msg_id, (
        f"MAX reply points to {max_reply.get_reply_to_id()!r}, "
        f"expected {max_orig.msg_id!r}"
    )


async def test_T13_edit(harness):
    """T13: TG→MAX редактирование текста."""
    # Step 1: send original TG message
    marker1 = harness.make_marker()
    tg_msg = await harness.tg.send_text(f"original {marker1}")

    # Step 2: wait for it in MAX
    max_orig = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker1 in (e.text or ""),
        timeout=15,
    )
    assert max_orig is not None, f"Bridge did not forward original TG message to MAX (marker={marker1})"

    # Step 3: edit the TG message
    marker2 = harness.make_marker()
    await harness.tg.edit_message(tg_msg.id, f"edited {marker2}")

    # Step 4: wait for the edit to arrive in MAX
    max_edit = await harness.max.wait_for(
        lambda e: e.kind == "edit" and marker2 in (e.text or ""),
        timeout=15,
    )
    assert max_edit is not None, f"Bridge did not forward TG edit to MAX (marker={marker2})"

    # Step 5: the edit should update the same MAX message
    assert max_edit.msg_id == max_orig.msg_id, (
        f"MAX edit msg_id={max_edit.msg_id!r} != original msg_id={max_orig.msg_id!r}"
    )


async def test_T14_delete(harness):
    """T14: TG→MAX удаление сообщения."""
    # Step 1: send TG message
    marker = harness.make_marker()
    tg_msg = await harness.tg.send_text(f"to delete {marker}")

    # Step 2: wait for it in MAX
    max_orig = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert max_orig is not None, f"Bridge did not forward TG message to MAX (marker={marker})"

    # Step 3: delete the TG message
    await harness.tg.delete_messages([tg_msg.id])

    # Step 4: wait for the delete to propagate to MAX
    max_delete = await harness.max.wait_for(
        lambda e: e.kind == "delete" and e.msg_id == max_orig.msg_id,
        timeout=15,
    )
    assert max_delete is not None, (
        f"Bridge did not propagate TG delete to MAX (expected msg_id={max_orig.msg_id!r})"
    )


async def test_T15_echo_loop(harness):
    """T15: TG→MAX эхо-петля отсутствует."""
    marker = harness.make_marker()

    # Step 1: send from TG
    await harness.tg.send_text(f"echo test {marker}")

    # Step 2: must arrive in MAX (confirms T01 is working)
    max_msg = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert max_msg is not None, f"Bridge did not forward TG message to MAX (marker={marker})"

    # Step 3: wait to make sure the same marker does NOT bounce back to TG
    echo = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=6,
    )
    assert echo is None, (
        f"Echo loop detected! Marker {marker!r} arrived back in TG: {echo.text!r}"
    )

# ── File, audio, voice, poll ──────────────────────────────────────────────────

async def test_T06_document_tg_to_max(harness, _doc_path):
    """T06: TG→MAX файл/документ — arrives in MAX with FILE attachment."""
    marker = harness.make_marker()
    await harness.tg.send_document(_doc_path, caption=marker)

    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=20,
    )
    assert result is not None, "Bridge did not forward document TG→MAX"
    attaches = result.raw.get("attaches", [])
    assert len(attaches) >= 1, f"No attachments in MAX message: {result.raw}"
    types = [a.get("_type") for a in attaches]
    assert any(t in ("FILE", "PHOTO") for t in types), (
        f"Expected FILE attachment, got: {types}"
    )


async def test_T07_audio_tg_to_max(harness, _wav_path):
    """T07: TG→MAX аудио — arrives in MAX as file attachment."""
    marker = harness.make_marker()
    await harness.tg.send_audio(_wav_path, caption=marker)

    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=20,
    )
    assert result is not None, "Bridge did not forward audio TG→MAX"
    attaches = result.raw.get("attaches", [])
    assert len(attaches) >= 1, f"No attachments in MAX message: {result.raw}"


async def test_T08_voice_tg_to_max(harness, _ogg_path):
    """T08: TG→MAX голосовое сообщение — arrives in MAX as file attachment."""
    marker = harness.make_marker()
    await harness.tg.send_voice(_ogg_path, caption=marker)

    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=20,
    )
    assert result is not None, "Bridge did not forward voice TG→MAX"
    attaches = result.raw.get("attaches", [])
    assert len(attaches) >= 1, f"No attachments in MAX message: {result.raw}"


async def test_T10_poll_tg_to_max(harness):
    """T10: TG→MAX опрос — bridge forwards poll as formatted text."""
    marker = harness.make_marker()
    question = f"Тест-опрос {marker}"
    await harness.tg.send_poll(question, ["Да", "Нет", "Не знаю"])

    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=20,
    )
    assert result is not None, "Bridge did not forward poll TG→MAX"
    assert "📊" in (result.text or ""), (
        f"Expected poll emoji 📊 in forwarded text: {result.text!r}"
    )
    assert "Да" in (result.text or ""), (
        f"Poll options lost in forwarding: {result.text!r}"
    )
