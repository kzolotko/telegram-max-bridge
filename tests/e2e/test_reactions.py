"""
Reaction tests — add and remove reactions in both directions.

Tests named test_R01_*, etc., corresponding to cases in TEST_CASES.md.

R01: TG → MAX  (add reaction)
R02: MAX → TG  (add reaction)
R03: TG → MAX  (remove reaction)
R04: MAX → TG  (remove reaction)

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
    pytest.mark.reaction,
]

_REACTION_EMOJI = "👍"

# E2E tests use a single user account for both TG and MAX.  Messenger servers
# (both Telegram and MAX) do NOT send reaction-change notifications back to the
# same user that set the reaction — they only notify OTHER chat members.
# Because the test client and the bridge share the same user, the test client
# can never observe the reaction forwarded by the bridge.
#
# The bridge code itself is correct (verified via logs), but verification
# requires a second independent user account which is not available in E2E.
_SKIP_REASON = (
    "Reaction notifications are not delivered to the same user who reacted. "
    "E2E tests share one user account between test client and bridge, "
    "so the test client cannot observe bridge-forwarded reactions."
)


# ── TG → MAX ─────────────────────────────────────────────────────────────────

@pytest.mark.skip(reason=_SKIP_REASON)
async def test_R01_reaction_tg_to_max(harness):
    """R01: TG→MAX добавление реакции 👍."""
    # Step 1: send from TG, wait for it in MAX to get MAX msg_id
    marker = harness.make_marker()
    tg_msg = await harness.tg.send_text(f"reaction target {marker}")
    max_orig = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert max_orig is not None, f"Bridge did not forward TG message to MAX (marker={marker})"

    harness.tg.drain()
    harness.max.drain()

    # Step 2: add TG reaction to the original TG message
    await harness.tg.send_reaction(tg_msg.id, _REACTION_EMOJI)

    # Step 3: bridge should forward the reaction to MAX (opcode 155)
    reaction_evt = await harness.max.wait_for(
        lambda e: e.kind == "reaction" and e.msg_id == max_orig.msg_id,
        timeout=15,
    )
    assert reaction_evt is not None, (
        f"Bridge did not forward TG reaction to MAX (max_msg_id={max_orig.msg_id})"
    )
    assert reaction_evt.emoji == _REACTION_EMOJI, (
        f"Expected emoji {_REACTION_EMOJI!r}, got {reaction_evt.emoji!r}"
    )


@pytest.mark.skip(reason=_SKIP_REASON)
async def test_R03_remove_reaction_tg_to_max(harness):
    """R03: TG→MAX снятие реакции."""
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

    # Step 2: add reaction first
    await harness.tg.send_reaction(tg_msg.id, _REACTION_EMOJI)
    await harness.max.wait_for(
        lambda e: e.kind == "reaction" and e.msg_id == max_orig.msg_id,
        timeout=12,
    )

    harness.max.drain()

    # Step 3: remove the reaction (send empty reaction list)
    await harness.tg.send_reaction(tg_msg.id, None)

    # Step 4: bridge should forward the removal to MAX
    removal_evt = await harness.max.wait_for(
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

@pytest.mark.skip(reason=_SKIP_REASON)
async def test_R02_reaction_max_to_tg(harness):
    """R02: MAX→TG добавление реакции 👍."""
    # Step 1: send from MAX, wait for it in TG to get TG msg_id
    marker = harness.make_marker()
    max_msg_id = await harness.max.send_text(f"reaction target {marker}")
    tg_orig = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=15,
    )
    assert tg_orig is not None, f"Bridge did not forward MAX message to TG (marker={marker})"
    assert max_msg_id is not None, "MAX send_text did not return msg_id"

    harness.tg.drain()
    harness.max.drain()

    # Step 2: MAX test client adds a reaction to the MAX message
    await harness.max.add_reaction(max_msg_id, _REACTION_EMOJI)

    # Step 3: bridge forwards reaction to TG (UpdateMessageReactions)
    reaction_evt = await harness.tg.wait_for(
        lambda e: e.kind == "reaction" and e.msg_id == tg_orig.msg_id,
        timeout=15,
    )
    assert reaction_evt is not None, (
        f"Bridge did not forward MAX reaction to TG (tg_msg_id={tg_orig.msg_id})"
    )
    assert reaction_evt.emoji == _REACTION_EMOJI, (
        f"Expected emoji {_REACTION_EMOJI!r}, got {reaction_evt.emoji!r}"
    )


@pytest.mark.skip(reason=_SKIP_REASON)
async def test_R04_remove_reaction_max_to_tg(harness):
    """R04: MAX→TG снятие реакции."""
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
    harness.max.drain()

    # Step 2: add reaction
    await harness.max.add_reaction(max_msg_id, _REACTION_EMOJI)
    await harness.tg.wait_for(
        lambda e: e.kind == "reaction" and e.msg_id == tg_orig.msg_id,
        timeout=12,
    )

    harness.tg.drain()

    # Step 3: remove the reaction
    await harness.max.remove_reaction(max_msg_id)

    # Step 4: bridge forwards removal to TG (emoji=None in UpdateMessageReactions)
    removal_evt = await harness.tg.wait_for(
        lambda e: e.kind == "reaction" and e.msg_id == tg_orig.msg_id,
        timeout=15,
    )
    assert removal_evt is not None, (
        f"Bridge did not forward MAX reaction removal to TG (tg_msg_id={tg_orig.msg_id})"
    )
    assert removal_evt.emoji is None, (
        f"Expected no emoji after removal, got {removal_evt.emoji!r}"
    )
