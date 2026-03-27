"""
DM Bridge E2E tests.

Tests MAX DM → TG bot forwarding and TG bot reply → MAX DM routing.

Prerequisites:
  - dm_bridge.bot_token configured in config.yaml
  - second_user_name configured in e2e_config.yaml
  - Bridge must be running with DM bridge enabled
  - Both users must have started a chat with the bot (/start)

Test flow:
  mary sends a MAX DM to kzolotko → bridge detects DM → bot sends to
  kzolotko's TG → kzolotko replies via bot → reply goes back to mary in MAX.
"""

import asyncio
import os

import pytest

from .media_fixtures import make_test_png, make_test_wav, save_temp_media

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.dm,
]


def _require_dm(harness):
    """Skip test if DM bridge is not configured."""
    if not harness.has_dm:
        pytest.skip("Requires second_user_name and dm_bridge in config.yaml")


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def _dm_photo_bytes():
    return make_test_png(8, 8, r=0, g=0, b=255)


@pytest.fixture(scope="session")
def _dm_wav_path():
    path = save_temp_media(make_test_wav(), ".wav")
    yield path
    os.unlink(path)


# ── DM01: Text message MAX DM → TG bot ──────────────────────────────────────

async def test_DM01_text_max_dm_to_bot(harness):
    """DM01: A MAX DM text message is forwarded to the TG bot chat."""
    _require_dm(harness)

    marker = harness.make_marker()
    text = f"Hello from MAX DM {marker}"

    await harness.max_dm.send_text(text)

    result = await harness.tg_bot_chat.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=harness.timeout,
    )

    assert result is not None, f"Bot did not forward MAX DM (marker={marker})"
    assert marker in (result.text or "")
    assert "[" in result.text and "]:" in result.text, \
        f"Expected [Name]: prefix in bot message, got: {result.text!r}"


# ── DM02: Reply via bot → MAX DM ────────────────────────────────────────────

async def test_DM02_reply_bot_to_max_dm(harness):
    """DM02: Replying to a bot-forwarded DM sends the reply back to MAX DM."""
    _require_dm(harness)

    marker1 = harness.make_marker()
    await harness.max_dm.send_text(f"DM for reply test {marker1}")

    bot_msg = await harness.tg_bot_chat.wait_for(
        lambda e: e.kind == "message" and marker1 in (e.text or ""),
        timeout=harness.timeout,
    )
    assert bot_msg is not None, f"Bot did not forward DM (marker={marker1})"

    marker2 = harness.make_marker()
    reply_msg = await harness.tg_bot_chat.send_reply(
        f"Reply from TG {marker2}", reply_to=int(bot_msg.msg_id),
    )

    # The reply is sent via MAX pool. We can't directly observe it arriving
    # at mary's MAX client (MAX only notifies the sender's connection for DMs).
    # Verify the bot didn't respond with an error ("Cannot route reply").
    await asyncio.sleep(3)
    error = await harness.tg_bot_chat.wait_for(
        lambda e: e.kind == "message" and "Cannot route" in (e.text or ""),
        timeout=3,
    )
    assert error is None, f"Reply routing failed: {error.text!r}"


# ── DM03: Multiple DMs forwarded in order ───────────────────────────────────

async def test_DM03_multiple_dms_order(harness):
    """DM03: Multiple MAX DMs are forwarded to the bot in order."""
    _require_dm(harness)

    markers = [harness.make_marker() for _ in range(3)]

    for i, marker in enumerate(markers):
        await harness.max_dm.send_text(f"Message {i+1} {marker}")
        await asyncio.sleep(0.5)

    received = []
    for marker in markers:
        result = await harness.tg_bot_chat.wait_for(
            lambda e, m=marker: e.kind == "message" and m in (e.text or ""),
            timeout=harness.timeout,
        )
        assert result is not None, f"Bot did not forward DM (marker={marker})"
        received.append(result)

    assert len(received) == 3


# ── DM04: Echo prevention — bot reply doesn't loop back ─────────────────────

async def test_DM04_echo_prevention(harness):
    """DM04: A reply sent through the bot does not echo back as a new DM."""
    _require_dm(harness)

    marker1 = harness.make_marker()
    await harness.max_dm.send_text(f"Echo test {marker1}")

    bot_msg = await harness.tg_bot_chat.wait_for(
        lambda e: e.kind == "message" and marker1 in (e.text or ""),
        timeout=harness.timeout,
    )
    assert bot_msg is not None

    marker2 = harness.make_marker()
    await harness.tg_bot_chat.send_reply(
        f"Echo reply {marker2}", reply_to=int(bot_msg.msg_id),
    )

    # Wait for the bridge to process the reply and any potential echo
    await asyncio.sleep(5)

    # Verify no echo arrived in the bot chat — the MirrorTracker should
    # suppress the bridge's own outgoing DM from being re-forwarded.
    echo = await harness.tg_bot_chat.wait_for(
        lambda e: e.kind == "message" and marker2 in (e.text or ""),
        timeout=5,
    )
    assert echo is None, f"Echo loop detected: {echo.text!r}"


# ── DM05: Photo MAX DM → TG bot ─────────────────────────────────────────────

async def test_DM05_photo_max_dm_to_bot(harness, _dm_photo_bytes):
    """DM05: A MAX DM photo is forwarded to the TG bot chat."""
    _require_dm(harness)

    marker = harness.make_marker()
    await harness.max_dm.send_photo(_dm_photo_bytes, caption=marker)

    result = await harness.tg_bot_chat.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=harness.timeout,
    )
    assert result is not None, f"Bot did not forward photo DM (marker={marker})"
    # Bot should forward as photo or document with caption containing marker
    assert marker in (result.text or "")


# ── DM06: File MAX DM → TG bot ──────────────────────────────────────────────

async def test_DM06_file_max_dm_to_bot(harness):
    """DM06: A MAX DM file is forwarded to the TG bot chat."""
    _require_dm(harness)

    marker = harness.make_marker()
    file_data = f"Test file content {marker}".encode()
    await harness.max_dm.send_file(
        file_data, filename="test.txt",
        content_type="text/plain", caption=marker,
    )

    result = await harness.tg_bot_chat.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=harness.timeout,
    )
    assert result is not None, f"Bot did not forward file DM (marker={marker})"


# ── DM07: Edit MAX DM → TG bot notification ─────────────────────────────────

async def test_DM07_edit_max_dm_to_bot(harness):
    """DM07: An edited MAX DM appears as a new bot message with edit marker."""
    _require_dm(harness)

    marker1 = harness.make_marker()
    msg_id = await harness.max_dm.send_text(f"Original DM {marker1}")

    bot_msg = await harness.tg_bot_chat.wait_for(
        lambda e: e.kind == "message" and marker1 in (e.text or ""),
        timeout=harness.timeout,
    )
    assert bot_msg is not None
    assert msg_id is not None

    # Edit the message
    marker2 = harness.make_marker()
    await harness.max_dm.edit_message(msg_id, f"Edited DM {marker2}")

    edit_msg = await harness.tg_bot_chat.wait_for(
        lambda e: e.kind == "message" and marker2 in (e.text or ""),
        timeout=harness.timeout,
    )
    assert edit_msg is not None, f"Bot did not forward edit (marker={marker2})"
    # Should contain edit indicator
    assert "✏️" in (edit_msg.text or ""), \
        f"Expected edit marker in message, got: {edit_msg.text!r}"


# ── DM08: Sticker MAX DM → TG bot ───────────────────────────────────────────
# Note: MAX DM stickers depend on MAX protocol support.
# Marked as manual — stickers are hard to send programmatically from test client.


# ── DM09: Delete MAX DM → TG bot notification ───────────────────────────────

async def test_DM09_delete_max_dm_to_bot(harness):
    """DM09: A deleted MAX DM triggers a notification in the bot chat."""
    _require_dm(harness)

    marker = harness.make_marker()
    msg_id = await harness.max_dm.send_text(f"Delete me {marker}")

    bot_msg = await harness.tg_bot_chat.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=harness.timeout,
    )
    assert bot_msg is not None
    assert msg_id is not None

    await harness.max_dm.delete_messages([msg_id])

    delete_msg = await harness.tg_bot_chat.wait_for(
        lambda e: e.kind == "message" and "deleted" in (e.text or "").lower(),
        timeout=harness.timeout,
    )
    # Delete notification may not arrive if MAX doesn't notify about
    # own-user deletes in DMs (same limitation as groups)
    if delete_msg is None:
        pytest.skip("MAX did not send delete notification for DM")


# ── DM10: Photo reply via bot → MAX DM ──────────────────────────────────────

async def test_DM10_photo_reply_bot_to_max_dm(harness, _dm_photo_bytes):
    """DM10: Replying with a photo via bot sends it to MAX DM."""
    _require_dm(harness)

    # Step 1: Get a DM message to reply to
    marker1 = harness.make_marker()
    await harness.max_dm.send_text(f"Send me a photo {marker1}")

    bot_msg = await harness.tg_bot_chat.wait_for(
        lambda e: e.kind == "message" and marker1 in (e.text or ""),
        timeout=harness.timeout,
    )
    assert bot_msg is not None

    # Step 2: Reply with a photo from TG bot chat
    # TgBotChatListener uses the primary TG client which is a user account,
    # so we send a photo as reply in the bot's private chat.
    marker2 = harness.make_marker()
    photo_path = save_temp_media(_dm_photo_bytes, ".jpg")
    try:
        await harness.tg_bot_chat._client.send_photo(
            harness.tg_bot_chat.bot_user_id,
            photo_path,
            caption=f"Photo reply {marker2}",
            reply_to_message_id=int(bot_msg.msg_id),
        )
    finally:
        os.unlink(photo_path)

    # Step 3: Verify no routing error — the photo was sent to MAX
    await asyncio.sleep(3)
    error = await harness.tg_bot_chat.wait_for(
        lambda e: e.kind == "message" and "Cannot route" in (e.text or ""),
        timeout=3,
    )
    assert error is None, f"Photo reply routing failed: {error.text!r}"
