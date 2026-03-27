"""
E2E Test Harness — orchestrates send/wait/verify across TG and MAX.

Provides high-level methods for common test patterns:
  - tg_to_max(text) — send via TG, wait for bridge to forward to MAX
  - max_to_tg(text) — send via MAX, wait for bridge to forward to TG

Each method injects a unique marker (UUID) into the text so the forwarded
message can be reliably matched even if the chat has other traffic.

Supports an optional second user pair (tg2/max2) for two-user tests:
reactions, deletes, sender routing.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from .clients import TgTestClient, TgBotChatListener, MaxTestClient, ReceivedEvent
from .config import E2EConfig

log = logging.getLogger("e2e.harness")


class E2EHarness:
    """Ties TG and MAX test clients together for E2E testing."""

    def __init__(self, config: E2EConfig) -> None:
        self.config = config
        self.timeout = config.timeout

        # Primary user (kzolotko)
        self.tg = TgTestClient(
            session_name=config.primary.tg_e2e_session,
            api_id=config.api_id,
            api_hash=config.api_hash,
            chat_id=config.tg_chat_id,
            sessions_dir=config.sessions_dir,
        )

        self.max = MaxTestClient(
            login_token=config.primary.max_login_token,
            device_id=config.primary.max_test_device_id,
            chat_id=config.max_chat_id,
        )

        # DM bridge testing — optional
        # tg_bot_chat: listener for bot's private messages (shares primary TG client)
        # max_dm: MAX client that sends DMs AS the second user (mary) to the
        #         bridge user (kzolotko).  Uses secondary credentials.
        self.tg_bot_chat: TgBotChatListener | None = None
        self.max_dm: MaxTestClient | None = None
        self._dm_bot_id: int | None = config.dm_bot_id
        self._dm_max_chat_id: int | None = config.dm_max_chat_id

        # Second user (mary) — optional
        self.tg2: TgTestClient | None = None
        self.max2: MaxTestClient | None = None

        if config.secondary:
            self.tg2 = TgTestClient(
                session_name=config.secondary.tg_e2e_session,
                api_id=config.api_id,
                api_hash=config.api_hash,
                chat_id=config.tg_chat_id,
                sessions_dir=config.sessions_dir,
            )
            self.max2 = MaxTestClient(
                login_token=config.secondary.max_login_token,
                device_id=config.secondary.max_test_device_id,
                chat_id=config.max_chat_id,
            )

    @property
    def has_second_user(self) -> bool:
        """True if a second user is configured and available."""
        return self.tg2 is not None and self.max2 is not None

    async def start(self) -> None:
        """Connect all test clients. Call before running tests."""
        # Stagger connections to avoid overwhelming the MAX server
        await self.tg.start()
        await asyncio.sleep(1)
        await self.max.start()
        await asyncio.sleep(1)

        if self.tg2 and self.max2:
            await self.tg2.start()
            await asyncio.sleep(1)
            await self.max2.start()
            await asyncio.sleep(1)

        # DM bridge clients (tg_bot_chat shares primary TG client after it starts)
        if self._dm_bot_id and self._dm_max_chat_id and self.config.secondary:
            self.tg_bot_chat = TgBotChatListener(
                client=self.tg._client,
                bot_user_id=self._dm_bot_id,
            )
            self.tg_bot_chat.start()

            # max_dm connects as the SECOND user (mary) — she sends DMs to
            # the bridge user (kzolotko), simulating an external person.
            self.max_dm = MaxTestClient(
                login_token=self.config.secondary.max_login_token,
                device_id=self.config.secondary.max_test_device_id,
                chat_id=self._dm_max_chat_id,
            )
            await self.max_dm.start()
            await asyncio.sleep(1)
        elif self._dm_bot_id and self._dm_max_chat_id and not self.config.secondary:
            log.warning(
                "DM bridge tests require second_user_name in e2e_config.yaml "
                "(the second user sends DMs to the bridge user)"
            )

        # Drain any stale messages that arrived during connection
        self.tg.drain()
        self.max.drain()
        if self.tg2:
            self.tg2.drain()
        if self.max2:
            self.max2.drain()
        if self.tg_bot_chat:
            self.tg_bot_chat.drain()
        if self.max_dm:
            self.max_dm.drain()

        log.info(
            "E2E harness ready (timeout=%.1fs, second_user=%s, dm=%s)",
            self.timeout, self.has_second_user, self.has_dm,
        )

    @property
    def has_dm(self) -> bool:
        """True if DM bridge testing is configured and available."""
        return self.tg_bot_chat is not None and self.max_dm is not None

    async def stop(self) -> None:
        """Disconnect all test clients."""
        if self.max_dm:
            await self.max_dm.stop()
        # tg_bot_chat shares the primary TG client — no stop needed
        await self.tg.stop()
        await self.max.stop()
        if self.tg2:
            await self.tg2.stop()
        if self.max2:
            await self.max2.stop()
        log.info("E2E harness stopped")

    # ── High-level test primitives ────────────────────────────────────────────

    def make_marker(self) -> str:
        """Generate a unique marker string for message correlation."""
        return f"e2e_{uuid4().hex[:8]}"

    async def tg_to_max(
        self,
        text: str,
        timeout: float | None = None,
    ) -> ReceivedEvent | None:
        """Send *text* via TG, wait for the bridged message in MAX."""
        marker = self.make_marker()
        tagged = f"{text} [{marker}]"
        t = timeout or self.timeout

        log.info("TG→MAX: sending %r (marker=%s, timeout=%.1fs)", text, marker, t)
        await self.tg.send_text(tagged)

        result = await self.max.wait_for(
            lambda evt: evt.kind == "message" and marker in (evt.text or ""),
            timeout=t,
        )

        if result:
            log.info("TG→MAX: received in MAX: %r", (result.text or "")[:80])
        else:
            log.warning("TG→MAX: TIMEOUT after %.1fs (marker=%s)", t, marker)

        return result

    async def max_to_tg(
        self,
        text: str,
        timeout: float | None = None,
    ) -> ReceivedEvent | None:
        """Send *text* via MAX, wait for the bridged message in TG."""
        marker = self.make_marker()
        tagged = f"{text} [{marker}]"
        t = timeout or self.timeout

        log.info("MAX→TG: sending %r (marker=%s, timeout=%.1fs)", text, marker, t)
        await self.max.send_text(tagged)

        result = await self.tg.wait_for(
            lambda evt: evt.kind == "message" and marker in (evt.text or ""),
            timeout=t,
        )

        if result:
            log.info("MAX→TG: received in TG: %r", (result.text or "")[:80])
        else:
            log.warning("MAX→TG: TIMEOUT after %.1fs (marker=%s)", t, marker)

        return result

    async def tg_edit_expect_max(
        self,
        tg_msg_id: int,
        new_text: str,
        timeout: float | None = None,
    ) -> ReceivedEvent | None:
        """Edit a TG message, wait for the edit to propagate to MAX."""
        marker = self.make_marker()
        tagged = f"{new_text} [{marker}]"
        t = timeout or self.timeout

        log.info("TG edit→MAX: editing msg %d (marker=%s)", tg_msg_id, marker)
        await self.tg.edit_message(tg_msg_id, tagged)

        return await self.max.wait_for(
            lambda evt: evt.kind == "edit" and marker in (evt.text or ""),
            timeout=t,
        )

    async def max_edit_expect_tg(
        self,
        max_msg_id: str,
        new_text: str,
        timeout: float | None = None,
    ) -> ReceivedEvent | None:
        """Edit a MAX message, wait for the edit to propagate to TG."""
        marker = self.make_marker()
        tagged = f"{new_text} [{marker}]"
        t = timeout or self.timeout

        log.info("MAX edit→TG: editing msg %s (marker=%s)", max_msg_id, marker)
        await self.max.edit_message(max_msg_id, tagged)

        return await self.tg.wait_for(
            lambda evt: evt.kind == "edit" and marker in (evt.text or ""),
            timeout=t,
        )

    async def tg_delete_expect_max(
        self,
        tg_msg_ids: list[int],
        max_msg_id: str,
        timeout: float | None = None,
    ) -> ReceivedEvent | None:
        """Delete TG message(s), wait for the delete to propagate to MAX."""
        t = timeout or self.timeout
        log.info("TG delete→MAX: deleting TG msgs %s, expecting MAX %s", tg_msg_ids, max_msg_id)
        await self.tg.delete_messages(tg_msg_ids)

        return await self.max.wait_for(
            lambda evt: evt.kind == "delete" and evt.msg_id == max_msg_id,
            timeout=t,
        )

    async def max_delete_expect_tg(
        self,
        max_msg_ids: list[str],
        tg_msg_id: str,
        timeout: float | None = None,
    ) -> ReceivedEvent | None:
        """Delete MAX message(s), wait for the delete to propagate to TG."""
        t = timeout or self.timeout
        log.info("MAX delete→TG: deleting MAX msgs %s, expecting TG %s", max_msg_ids, tg_msg_id)
        await self.max.delete_messages(max_msg_ids)

        return await self.tg.wait_for(
            lambda evt: evt.kind == "delete" and evt.msg_id == tg_msg_id,
            timeout=t,
        )
