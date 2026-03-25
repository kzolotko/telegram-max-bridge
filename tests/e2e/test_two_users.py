"""
Two-user tests — sender routing, cross-user interactions, delete observation.

These tests require a second user configured in e2e_config.yaml
(second_user_name) with an authenticated TG E2E session.

U01: Sender routing TG→MAX — mary sends, bridge uses mary's MAX account (no prefix)
U02: Sender routing MAX→TG — mary sends via MAX, bridge uses mary's TG account (no prefix)
U03: Cross-user reply TG→MAX — mary replies to kzolotko's message
U04: Delete TG→MAX observed by second user
U05: Delete MAX→TG observed by second user

Prerequisites:
  - Bridge is running
  - Both users are in e2e_config.yaml
  - Both TG E2E sessions authenticated
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.twouser,
]

_NEED_SECOND_USER = "Requires second user (second_user_name in e2e_config.yaml + TG E2E session)"


async def test_U01_sender_routing_tg_to_max(harness):
    """U01: Mary sends in TG → bridge uses mary's MAX account (no [Name]: prefix)."""
    if not harness.has_second_user:
        pytest.skip(_NEED_SECOND_USER)

    marker = harness.make_marker()
    text = f"From mary in TG {marker}"

    # Mary sends via her TG test client
    await harness.tg2.send_text(text)

    # Observe in MAX (via primary's MAX client — sees all chat messages)
    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert result is not None, "Bridge did not forward mary's TG message to MAX"
    # If sender routing works, there should be NO [Name]: prefix —
    # the message was sent from mary's own MAX account.
    assert result.text is not None
    assert "[" not in result.text.split(marker)[0], (
        f"Expected no sender prefix (sender routing), got: {result.text!r}"
    )


async def test_U02_sender_routing_max_to_tg(harness):
    """U02: Mary sends in MAX → bridge uses mary's TG account (no [Name]: prefix)."""
    if not harness.has_second_user:
        pytest.skip(_NEED_SECOND_USER)

    marker = harness.make_marker()
    text = f"From mary in MAX {marker}"

    # Mary sends via her MAX test client
    await harness.max2.send_text(text)

    # Observe in TG (via primary's TG client — sees all chat messages)
    result = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert result is not None, "Bridge did not forward mary's MAX message to TG"
    # No [Name]: prefix = sender routing worked
    assert result.text is not None
    assert "[" not in result.text.split(marker)[0], (
        f"Expected no sender prefix (sender routing), got: {result.text!r}"
    )


async def test_U03_cross_user_reply_tg_to_max(harness):
    """U03: Kzolotko sends, mary replies in TG → reply preserved in MAX."""
    if not harness.has_second_user:
        pytest.skip(_NEED_SECOND_USER)

    # Step 1: kzolotko sends original message
    marker1 = harness.make_marker()
    orig_msg = await harness.tg.send_text(f"Original {marker1}")

    # Wait for it in MAX
    max_orig = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker1 in (e.text or ""),
        timeout=15,
    )
    assert max_orig is not None, "Bridge did not forward original message to MAX"

    # Step 2: mary replies to the original message
    marker2 = harness.make_marker()
    await harness.tg2.send_text(f"Reply from mary {marker2}", reply_to=orig_msg.id)

    # Step 3: verify reply arrives in MAX with reply_to set
    max_reply = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker2 in (e.text or ""),
        timeout=15,
    )
    assert max_reply is not None, "Bridge did not forward mary's reply to MAX"
    reply_to_id = max_reply.get_reply_to_id()
    assert reply_to_id == max_orig.msg_id, (
        f"Reply target mismatch: expected {max_orig.msg_id}, got {reply_to_id}"
    )


async def test_U04_delete_tg_to_max_observed(harness):
    """U04: Delete TG→MAX — observed by second user's MAX client."""
    if not harness.has_second_user:
        pytest.skip(_NEED_SECOND_USER)

    # Step 1: send from TG (primary), wait for it in MAX
    marker = harness.make_marker()
    tg_msg = await harness.tg.send_text(f"to delete {marker}")

    max_orig = await harness.max2.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert max_orig is not None, "Bridge did not forward TG message to MAX"

    harness.max2.drain()

    # Step 2: delete from TG
    await harness.tg.delete_messages([tg_msg.id])

    # Step 3: observe delete in MAX via second user (different user gets notification)
    max_delete = await harness.max2.wait_for(
        lambda e: e.kind == "delete" and e.msg_id == max_orig.msg_id,
        timeout=15,
    )
    assert max_delete is not None, (
        f"Bridge did not propagate TG delete to MAX (expected msg_id={max_orig.msg_id!r})"
    )


async def test_U05_delete_max_to_tg_observed(harness):
    """U05: Delete MAX→TG — observed by second user's TG client."""
    if not harness.has_second_user:
        pytest.skip(_NEED_SECOND_USER)

    # Step 1: send from MAX (primary), wait for it in TG
    marker = harness.make_marker()
    max_msg_id = await harness.max.send_text(f"to delete {marker}")

    tg_orig = await harness.tg2.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert tg_orig is not None, "Bridge did not forward MAX message to TG"
    assert max_msg_id is not None, "MAX send_text did not return msg_id"

    harness.tg2.drain()

    # Step 2: delete from MAX
    await harness.max.delete_messages([max_msg_id])

    # Step 3: observe delete in TG via second user
    tg_delete = await harness.tg2.wait_for(
        lambda e: e.kind == "delete" and e.msg_id == tg_orig.msg_id,
        timeout=15,
    )
    assert tg_delete is not None, (
        f"Bridge did not propagate MAX delete to TG (expected msg_id={tg_orig.msg_id!r})"
    )
