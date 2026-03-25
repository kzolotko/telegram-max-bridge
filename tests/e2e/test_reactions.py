"""
Reaction tests — add and remove reactions in both directions.

Tests named test_R01_*, etc., corresponding to cases in TEST_CASES.md.

R01: TG → MAX  (add reaction) — observed by second user's MAX client
R02: MAX → TG  (add reaction) — observed by second user's TG client
R03: TG → MAX  (remove reaction)
R04: MAX → TG  (remove reaction)

Prerequisites:
  - Bridge is running
  - E2E config exists with second_user_name
  - Both TG E2E sessions are authenticated
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.reaction,
]

_REACTION_EMOJI = "\U0001f44d"

_NEED_SECOND_USER = "Requires second user (second_user_name in e2e_config.yaml + TG E2E session)"
_MAX_NO_REACTION_NOTIF = (
    "MAX server does not deliver NOTIF_MSG_REACTIONS_CHANGED (opcode 155) "
    "to other users in the chat. Bridge sends reaction correctly (verified "
    "via logs), but no test client can observe it."
)


# ── TG → MAX ─────────────────────────────────────────────────────────────────

@pytest.mark.skip(reason=_MAX_NO_REACTION_NOTIF)
async def test_R01_reaction_tg_to_max(harness):
    """R01: TG→MAX добавление реакции 👍 (observed by second user)."""
    if not harness.has_second_user:
        pytest.skip(_NEED_SECOND_USER)

    # Step 1: send from TG (primary), wait for it in MAX (primary) to get MAX msg_id
    marker = harness.make_marker()
    tg_msg = await harness.tg.send_text(f"reaction target {marker}")
    max_orig = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert max_orig is not None, f"Bridge did not forward TG message to MAX (marker={marker})"

    harness.tg.drain()
    harness.max.drain()
    harness.max2.drain()

    # Step 2: add TG reaction from primary user
    await harness.tg.send_reaction(tg_msg.id, _REACTION_EMOJI)

    # Step 3: bridge forwards reaction to MAX — observed by SECOND user's MAX client
    # (same user doesn't receive their own reaction notification)
    reaction_evt = await harness.max2.wait_for(
        lambda e: e.kind == "reaction" and e.msg_id == max_orig.msg_id,
        timeout=15,
    )
    assert reaction_evt is not None, (
        f"Bridge did not forward TG reaction to MAX (max_msg_id={max_orig.msg_id})"
    )
    assert reaction_evt.emoji == _REACTION_EMOJI, (
        f"Expected emoji {_REACTION_EMOJI!r}, got {reaction_evt.emoji!r}"
    )


@pytest.mark.skip(reason=_MAX_NO_REACTION_NOTIF)
async def test_R03_remove_reaction_tg_to_max(harness):
    """R03: TG→MAX снятие реакции (observed by second user)."""
    if not harness.has_second_user:
        pytest.skip(_NEED_SECOND_USER)

    # Step 1: send message, forward to MAX
    marker = harness.make_marker()
    tg_msg = await harness.tg.send_text(f"remove-reaction target {marker}")
    max_orig = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert max_orig is not None, f"Bridge did not forward TG message (marker={marker})"

    harness.tg.drain()
    harness.max.drain()
    harness.max2.drain()

    # Step 2: add reaction first
    await harness.tg.send_reaction(tg_msg.id, _REACTION_EMOJI)
    await harness.max2.wait_for(
        lambda e: e.kind == "reaction" and e.msg_id == max_orig.msg_id,
        timeout=12,
    )

    harness.max2.drain()

    # Step 3: remove the reaction
    await harness.tg.send_reaction(tg_msg.id, None)

    # Step 4: bridge forwards removal — observed by second user
    removal_evt = await harness.max2.wait_for(
        lambda e: e.kind == "reaction" and e.msg_id == max_orig.msg_id,
        timeout=15,
    )
    assert removal_evt is not None, (
        f"Bridge did not forward TG reaction removal to MAX (max_msg_id={max_orig.msg_id})"
    )
    assert removal_evt.emoji is None, (
        f"Expected no emoji after removal, got {removal_evt.emoji!r}"
    )


# ── MAX → TG ─────────────────────────────────────────────────────────────────

@pytest.mark.skip(reason=_MAX_NO_REACTION_NOTIF)
async def test_R02_reaction_max_to_tg(harness):
    """R02: MAX→TG добавление реакции 👍 (observed by second user)."""
    if not harness.has_second_user:
        pytest.skip(_NEED_SECOND_USER)

    # Step 1: send from MAX (primary), wait for it in TG (primary)
    marker = harness.make_marker()
    max_msg_id = await harness.max.send_text(f"reaction target {marker}")
    tg_orig = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert tg_orig is not None, f"Bridge did not forward MAX message to TG (marker={marker})"
    assert max_msg_id is not None, "MAX send_text did not return msg_id"

    harness.tg.drain()
    harness.tg2.drain()
    harness.max.drain()

    # Step 2: MAX primary user adds a reaction
    await harness.max.add_reaction(max_msg_id, _REACTION_EMOJI)

    # Step 3: bridge forwards reaction to TG — observed by SECOND user's TG client
    reaction_evt = await harness.tg2.wait_for(
        lambda e: e.kind == "reaction" and e.msg_id == tg_orig.msg_id,
        timeout=15,
    )
    assert reaction_evt is not None, (
        f"Bridge did not forward MAX reaction to TG (tg_msg_id={tg_orig.msg_id})"
    )
    assert reaction_evt.emoji == _REACTION_EMOJI, (
        f"Expected emoji {_REACTION_EMOJI!r}, got {reaction_evt.emoji!r}"
    )


@pytest.mark.skip(reason=_MAX_NO_REACTION_NOTIF)
async def test_R04_remove_reaction_max_to_tg(harness):
    """R04: MAX→TG снятие реакции (observed by second user)."""
    if not harness.has_second_user:
        pytest.skip(_NEED_SECOND_USER)

    # Step 1: send from MAX, wait in TG
    marker = harness.make_marker()
    max_msg_id = await harness.max.send_text(f"remove-reaction target {marker}")
    tg_orig = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert tg_orig is not None, f"Bridge did not forward MAX message (marker={marker})"
    assert max_msg_id is not None, "MAX send_text did not return msg_id"

    harness.tg.drain()
    harness.tg2.drain()
    harness.max.drain()

    # Step 2: add reaction
    await harness.max.add_reaction(max_msg_id, _REACTION_EMOJI)
    await harness.tg2.wait_for(
        lambda e: e.kind == "reaction" and e.msg_id == tg_orig.msg_id,
        timeout=12,
    )

    harness.tg2.drain()

    # Step 3: remove the reaction
    await harness.max.remove_reaction(max_msg_id)

    # Step 4: bridge forwards removal — observed by second user
    removal_evt = await harness.tg2.wait_for(
        lambda e: e.kind == "reaction" and e.msg_id == tg_orig.msg_id,
        timeout=15,
    )
    assert removal_evt is not None, (
        f"Bridge did not forward MAX reaction removal to TG (tg_msg_id={tg_orig.msg_id})"
    )
    assert removal_evt.emoji is None, (
        f"Expected no emoji after removal, got {removal_evt.emoji!r}"
    )
