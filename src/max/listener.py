import asyncio
import logging
from typing import Callable, Awaitable, Any

from .bridge_client import BridgeMaxClient
from ..bridge.formatting import max_elements_to_internal
from ..bridge.mirror_tracker import MirrorTracker
from ..config import ConfigLookup
from ..types import AppConfig, BridgeEvent, MediaInfo, UserMapping
from .session import MaxSession
from .media import download_media

log = logging.getLogger("bridge.max.listener")

RECONNECT_BASE_DELAY = 2
RECONNECT_MAX_DELAY = 60


class MaxListener:
    """Listens for messages in MAX using a user account via native TCP/SSL."""

    def __init__(
        self,
        config: AppConfig,
        lookup: ConfigLookup,
        mirror_tracker: MirrorTracker,
        on_event: Callable[[BridgeEvent], Awaitable[None]],
        user: UserMapping,
    ):
        self.config = config
        self.lookup = lookup
        self.mirrors = mirror_tracker
        self.on_event = on_event
        self.user = user
        self.client: BridgeMaxClient | None = None
        self._my_user_id: int = user.max_user_id
        self._login_token: str | None = None
        self._device_id: str | None = None
        self._stopped = False
        self._monitor_task: asyncio.Task | None = None
        self._name_cache: dict[int, str] = {}  # max_user_id -> display name

        # Pre-populate name cache with known bridge users.
        # For these users the bridge routes via their own TG account,
        # so the display name is rarely shown — but having it cached
        # avoids any network call and eliminates the delay entirely.
        for entry in config.bridges:
            u = entry.user
            self._name_cache[u.max_user_id] = u.name

    async def start(self) -> int:
        session = MaxSession(self.user.max_session, self.config.sessions_dir)
        if not session.exists():
            raise RuntimeError(
                f"MAX session not found ({self.user.max_session}) for user {self.user.name}. "
                f"Run 'python -m src.auth' first."
            )

        self._login_token = session.load()
        self._device_id = session.load_device_id()
        if not self._device_id:
            raise RuntimeError(
                f"No device_id in MAX session for {self.user.name}. "
                f"Re-authenticate with 'python -m src.auth'."
            )
        await self._connect()

        self._monitor_task = asyncio.create_task(self._reconnect_loop())

        log.info("Started for %s (User ID: %d)", self.user.name, self._my_user_id)
        return self._my_user_id

    async def _connect(self):
        # Close previous client to avoid resource leak
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

        client = BridgeMaxClient(token=self._login_token, device_id=self._device_id)
        client.set_raw_callback(self._handle_packet)
        await client.connect_and_login()
        self.client = client
        # Pre-populate name cache with members of bridged MAX chats
        await self._preload_chat_members()

    async def _reconnect_loop(self):
        delay = RECONNECT_BASE_DELAY
        while not self._stopped:
            try:
                if self.client and self.client.recv_task:
                    await self.client.recv_task
            except Exception:
                pass

            if self._stopped:
                break

            log.warning("MAX listener %s: connection lost. Reconnecting in %ds...",
                        self.user.name, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)

            try:
                await self._connect()
                delay = RECONNECT_BASE_DELAY
                log.info("MAX listener %s: reconnected (User ID: %d)",
                         self.user.name, self._my_user_id)
            except Exception as e:
                log.error("MAX listener %s: reconnect failed: %s", self.user.name, e)

    async def stop(self):
        self._stopped = True
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    async def _preload_chat_members(self):
        """Pre-load names of all members in bridged MAX chats into _name_cache.

        Called after connect — uses load_members() which goes through
        _send_and_wait (safe here because recv_loop handlers aren't active yet
        for message processing, and _send_and_wait uses seq-based futures).
        """
        max_chat_ids = set()
        for entry in self.config.bridges:
            max_chat_ids.add(entry.max_chat_id)

        for chat_id in max_chat_ids:
            try:
                members, _ = await self.client.inner.load_members(chat_id)
                for member in members:
                    c = member.contact
                    if c and c.id and c.id not in self._name_cache:
                        name = self._extract_name_from_names(c.names) if c.names else None
                        if name:
                            self._name_cache[c.id] = name
                log.info("Pre-loaded %d member names for MAX chat %s",
                         len(members) if members else 0, chat_id)
            except Exception as e:
                log.warning("Failed to preload members for chat %s: %s", chat_id, e)

    def _resolve_sender_name(self, sender_id: int) -> str:
        """Resolve display name for a MAX user ID — no network calls.

        Known bridge users are pre-populated from config.
        Other users are looked up in PyMax's internal cache (populated
        during _sync at login and updated as messages arrive).
        Falls back to 'User:<id>' only if no cached data exists.

        NOTE: This must be synchronous because it runs inside the PyMax
        raw_receive handler which is awaited by _recv_loop. Calling
        _send_and_wait (e.g. get_users) here would deadlock.
        """
        if sender_id in self._name_cache:
            return self._name_cache[sender_id]

        # Try PyMax's internal user cache (populated at login + on messages)
        name = self._try_pymax_cache(sender_id)
        if name:
            self._name_cache[sender_id] = name
            log.debug("Resolved MAX user %s → %s (from PyMax cache)", sender_id, name)
            return name

        # Schedule a background fetch for future messages (runs outside handler)
        asyncio.ensure_future(self._bg_resolve_name(sender_id))

        fallback = f"User:{sender_id}"
        self._name_cache[sender_id] = fallback
        return fallback

    def _try_pymax_cache(self, sender_id: int) -> str | None:
        """Try to extract name from PyMax's internal user/contact cache."""
        if not self.client:
            return None
        try:
            # PyMax caches users it has seen in self.inner._users
            u = self.client.inner.get_cached_user(sender_id)
            if u is not None:
                return self._extract_name(u)

            # Also check contacts loaded during _sync
            for contact in self.client.inner.contacts:
                if contact.id == sender_id:
                    return self._extract_name(contact)
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_name_from_names(names) -> str | None:
        """Pick the best display name from a list of Name objects.

        Prefers first_name + last_name combination (like Telegram does),
        falls back to .name if both are empty.
        """
        if not names:
            return None
        for n in names:
            first = getattr(n, 'first_name', None) or ''
            last = getattr(n, 'last_name', None) or ''
            combined = f"{first} {last}".strip()
            if combined:
                return combined
            fallback = getattr(n, 'name', None)
            if fallback:
                return fallback
        return None

    @staticmethod
    def _extract_name(u) -> str | None:
        """Extract display name from a PyMax User/Contact object."""
        if hasattr(u, 'names') and u.names:
            name = MaxListener._extract_name_from_names(u.names)
            if name:
                return name
        if hasattr(u, 'display_name') and u.display_name:
            return u.display_name
        if hasattr(u, 'first_name') and u.first_name:
            return u.first_name
        return None

    async def _bg_resolve_name(self, sender_id: int):
        """Background task: fetch real name via network, update cache.

        Runs as a separate task so it doesn't deadlock the recv loop.
        The current message uses fallback; future messages get the real name.
        """
        try:
            users = await asyncio.wait_for(
                self.client.get_users([sender_id]),
                timeout=5.0,
            )
            if users:
                name = self._extract_name(users[0])
                if name:
                    self._name_cache[sender_id] = name
                    log.debug("Resolved MAX user %s → %s (background)", sender_id, name)
        except Exception:
            pass

    async def _handle_packet(self, data: dict[str, Any]):
        try:
            opcode = data.get("opcode", 0)

            # PyMax uses Opcode enum values (ints) for opcodes
            if opcode == 128:  # NOTIF_MESSAGE (new, edit, delete — via status field)
                await self._handle_message(data)
            elif opcode == 66:  # MSG_DELETE (server echo of our own delete — has chatId+messageIds)
                await self._handle_deleted_message(data)
            elif opcode == 142:  # NOTIF_MSG_DELETE (other users' deletes)
                await self._handle_deleted_message(data)
            elif opcode == 155:  # NOTIF_MSG_REACTIONS_CHANGED
                await self._handle_reaction(data)
        except Exception as e:
            log.error("Error handling packet (opcode=%s): %s", data.get("opcode"), e, exc_info=True)

    async def _handle_message(self, packet: dict):
        payload = packet.get("payload", {})
        chat_id = payload.get("chatId")
        message = payload.get("message", {})
        msg_id = str(message.get("id", ""))
        # Sender ID lives in message.sender (opcode 128), NOT payload.fromUserId
        sender_id = message.get("sender") or payload.get("fromUserId")
        status = message.get("status")

        # Edits and deletes from other users arrive as NOTIF_MESSAGE (opcode 128)
        # with status="EDITED" or status="REMOVED" — route them accordingly.
        if status == "EDITED":
            await self._handle_notif_edit(chat_id, message, msg_id, sender_id)
            return
        elif status == "REMOVED":
            await self._handle_notif_delete(chat_id, [message.get("id")])
            return

        log.debug("MAX msg: chat=%s sender=%s msg_id=%r text=%r attaches=%s",
                  chat_id, sender_id, msg_id,
                  (message.get("text") or "")[:60],
                  [a.get("_type") for a in message.get("attaches", [])])

        if not chat_id or not sender_id:
            return

        # MirrorTracker: skip messages sent by the bridge via ANY user's
        # MAX account (MAX message IDs are global — same for all users).
        if msg_id and self.mirrors.is_max_mirror(msg_id):
            log.debug("MAX msg %s → is mirror, skipping", msg_id)
            return

        bridge_entry = self.lookup.get_primary_by_max(chat_id)
        if not bridge_entry:
            return

        sender_name = self._resolve_sender_name(sender_id)
        text = message.get("text")
        elements = message.get("elements")
        fmt = max_elements_to_internal(elements) or None
        attaches = message.get("attaches", [])

        reply_to = None
        link = message.get("link")
        if link and link.get("type") == "REPLY":
            # Reply target ID can be in link.messageId or link.message.id
            reply_to = link.get("messageId")
            if not reply_to:
                link_msg = link.get("message")
                if isinstance(link_msg, dict):
                    reply_to = link_msg.get("id")
            if reply_to:
                reply_to = str(reply_to)
                log.debug("MAX reply_to: %s", reply_to)

        # ── Handle sticker (always single, return immediately) ──────────────
        for att in attaches:
            if att.get("_type") == "STICKER":
                await self.on_event(BridgeEvent(
                    direction="max-to-tg",
                    bridge_entry=bridge_entry,
                    sender_display_name=sender_name,
                    sender_user_id=sender_id,
                    event_type="sticker",
                    text="[Sticker]",
                    reply_to_source_msg_id=reply_to,
                    source_msg_id=msg_id,
                ))
                return

        # ── Download ALL media attachments ───────────────────────────────────
        _ATT_META = {
            "PHOTO": ("photo", "photo.jpg",  "image/jpeg"),
            "VIDEO": ("video", "video.mp4",  "video/mp4"),
            "FILE":  ("file",  "file",        "application/octet-stream"),
            "AUDIO": ("audio", "audio.mp3",  "audio/mpeg"),
        }

        downloaded: list[tuple[str, MediaInfo]] = []  # (evt_type, media)
        failed_labels: list[str] = []

        for att in attaches:
            att_type = att.get("_type", "")
            if att_type not in _ATT_META:
                continue

            media_url = att.get("url") or att.get("baseUrl")
            log.debug("MAX attachment: type=%s url=%s", att_type, media_url)
            default_evt, default_fname, default_mime = _ATT_META[att_type]
            fname = att.get("fileName") or default_fname
            mime  = att.get("mimeType") or default_mime

            if media_url:
                try:
                    data = await download_media(media_url)
                    downloaded.append((default_evt, MediaInfo(data=data, filename=fname, mime_type=mime)))
                except Exception as e:
                    log.error("Failed to download %s from %s: %s", att_type, media_url, e, exc_info=True)
                    failed_labels.append(att_type.capitalize())
            else:
                failed_labels.append(att_type.capitalize())

        # Emit download-failure events for any failed attachments
        if failed_labels:
            fail_text = f"{text or ''}\n" + "\n".join(f"[{l} — media download failed]" for l in failed_labels)
            await self.on_event(BridgeEvent(
                direction="max-to-tg",
                bridge_entry=bridge_entry,
                sender_display_name=sender_name,
                sender_user_id=sender_id,
                event_type="text",
                text=fail_text.strip(),
                reply_to_source_msg_id=reply_to,
                source_msg_id=msg_id,
            ))

        if not downloaded:
            # Nothing else to send (text-only path follows below)
            pass
        elif len(downloaded) == 1:
            # Single attachment — keep original per-type event for backward compat
            evt_type, media = downloaded[0]
            await self.on_event(BridgeEvent(
                direction="max-to-tg",
                bridge_entry=bridge_entry,
                sender_display_name=sender_name,
                sender_user_id=sender_id,
                event_type=evt_type,
                text=text,
                media=media,
                reply_to_source_msg_id=reply_to,
                source_msg_id=msg_id,
                formatting=fmt,
            ))
            return
        else:
            # Multiple attachments — send as media_group
            media_list = [mi for _, mi in downloaded]
            await self.on_event(BridgeEvent(
                direction="max-to-tg",
                bridge_entry=bridge_entry,
                sender_display_name=sender_name,
                sender_user_id=sender_id,
                event_type="media_group",
                text=text,
                media_list=media_list,
                reply_to_source_msg_id=reply_to,
                source_msg_id=msg_id,
                formatting=fmt,
            ))
            return

        if text:
            await self.on_event(BridgeEvent(
                direction="max-to-tg",
                bridge_entry=bridge_entry,
                sender_display_name=sender_name,
                sender_user_id=sender_id,
                event_type="text",
                text=text,
                reply_to_source_msg_id=reply_to,
                source_msg_id=msg_id,
                formatting=fmt,
            ))

    async def _handle_notif_edit(self, chat_id, message: dict, msg_id: str, sender_id):
        """Handle edit notification from NOTIF_MESSAGE (opcode 128, status=EDITED)."""
        if not chat_id or not msg_id:
            return
        if self.mirrors.is_max_mirror(msg_id):
            return
        bridge_entry = self.lookup.get_primary_by_max(chat_id)
        if not bridge_entry:
            return
        sender_name = self._resolve_sender_name(sender_id) if sender_id else "Unknown"
        text = message.get("text")
        elements = message.get("elements")
        fmt = max_elements_to_internal(elements) or None
        log.debug("MAX edit: chat=%s msg=%s sender=%s", chat_id, msg_id, sender_id)
        await self.on_event(BridgeEvent(
            direction="max-to-tg",
            bridge_entry=bridge_entry,
            sender_display_name=sender_name,
            sender_user_id=sender_id,
            event_type="edit",
            text=text,
            edit_source_msg_id=msg_id,
            source_msg_id=msg_id,
            formatting=fmt,
        ))

    async def _handle_notif_delete(self, chat_id, message_ids: list):
        """Handle delete notification (opcode 142 or NOTIF_MESSAGE status=REMOVED)."""
        if not chat_id or not message_ids:
            return
        bridge_entry = self.lookup.get_primary_by_max(chat_id)
        if not bridge_entry:
            return
        for mid in message_ids:
            if mid is None:
                continue
            log.debug("MAX delete: chat=%s msg=%s", chat_id, mid)
            await self.on_event(BridgeEvent(
                direction="max-to-tg",
                bridge_entry=bridge_entry,
                sender_display_name="Unknown",
                event_type="delete",
                delete_source_msg_id=str(mid),
                source_msg_id=str(mid),
            ))

    async def _handle_deleted_message(self, packet: dict):
        """Handle MSG_DELETE (opcode 66) and NOTIF_MSG_DELETE (opcode 142)."""
        payload = packet.get("payload", {})
        chat_id = payload.get("chatId")
        message_ids = payload.get("messageIds", [])
        await self._handle_notif_delete(chat_id, message_ids)

    async def _handle_reaction(self, packet: dict):
        """Handle NOTIF_MSG_REACTIONS_CHANGED (opcode 155).

        Payload contains:
          chatId, messageId, totalCount, yourReaction (str|null), counters[].
        ``yourReaction`` is the reaction OUR account currently has on the
        message — syncing it to TG is correct because this listener runs under
        a single user account.
        """
        payload = packet.get("payload", {})
        chat_id = payload.get("chatId")
        message_id = str(payload.get("messageId", ""))
        if not chat_id or not message_id:
            return

        bridge_entry = self.lookup.get_primary_by_max(chat_id)
        if not bridge_entry:
            return

        # Echo suppression — we set this reaction ourselves via the bridge
        our_emoji: str | None = payload.get("yourReaction") or None
        if self.mirrors.is_max_reaction_mirror(message_id, our_emoji):
            return

        log.debug("MAX reaction: chat=%s msg=%s emoji=%r", chat_id, message_id, our_emoji)
        await self.on_event(BridgeEvent(
            direction="max-to-tg",
            bridge_entry=bridge_entry,
            sender_display_name=self.user.name,
            sender_user_id=self._my_user_id,
            event_type="reaction",
            source_msg_id=message_id,
            reaction_emoji=our_emoji,  # None = remove
        ))
