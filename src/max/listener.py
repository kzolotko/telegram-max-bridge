import asyncio
import logging
from typing import Callable, Awaitable, Any

from .bridge_client import BridgeMaxClient
from ..bridge.formatting import max_elements_to_internal
from ..bridge.mirror_tracker import MirrorTracker
from ..config import ConfigLookup
from ..types import AppConfig, BridgeEvent, MediaInfo, UserMapping
from .session import MaxSession
from .media import download_media, try_download_media

log = logging.getLogger("bridge.max.listener")

RECONNECT_BASE_DELAY = 2
RECONNECT_MAX_DELAY = 60
# Active ping watchdog: if this many consecutive pings fail, force-disconnect
# so that _reconnect_loop picks up immediately instead of waiting minutes.
PING_INTERVAL = 30          # seconds between health pings
PING_MAX_FAILURES = 3       # force-disconnect after N consecutive failures


class MaxListener:
    """Listens for messages in MAX using a user account via native TCP/SSL.

    Incoming packets from pymax are placed into an internal asyncio.Queue
    by the recv callback.  A separate worker task processes them, which
    allows handler code to call ``_send_and_wait`` (e.g. ``get_file_by_id``,
    ``get_users``) without deadlocking the recv loop.
    """

    def __init__(
        self,
        config: AppConfig,
        lookup: ConfigLookup,
        mirror_tracker: MirrorTracker,
        on_event: Callable[[BridgeEvent], Awaitable[None]],
        user: UserMapping,
        on_dm: Callable[..., Awaitable[None]] | None = None,
    ):
        self.config = config
        self.lookup = lookup
        self.mirrors = mirror_tracker
        self.on_event = on_event
        self.on_dm = on_dm  # DM bridge callback (if enabled)
        self.user = user
        self.client: BridgeMaxClient | None = None
        self._my_user_id: int = user.max_user_id
        self._login_token: str | None = None
        self._device_id: str | None = None
        self._stopped = False
        self._monitor_task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._worker_task: asyncio.Task | None = None
        self._packet_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._name_cache: dict[int, str] = {}  # max_user_id -> display name
        # Set of all known group/channel chat IDs (for DM detection)
        self._known_group_ids: set[int] = set()

        # Note: name cache is populated at runtime from MAX (preload + on-demand).
        # Config user names (e.g. "mary") are NOT cached here — we prefer
        # real display names (first + last) from the MAX profile.

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

        self._worker_task = asyncio.create_task(self._worker())
        self._monitor_task = asyncio.create_task(self._reconnect_loop())
        self._ping_task = asyncio.create_task(self._ping_watchdog())

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

        client = BridgeMaxClient(token=self._login_token, device_id=self._device_id,
                                 sessions_dir=self.config.sessions_dir)
        client.set_raw_callback(self._enqueue_packet)
        await client.connect_and_login()
        self.client = client

        # Let the connection stabilize before making any requests.
        # PyMax background tasks (recv, ping, outgoing) need a moment to
        # fully initialize after _post_login_tasks().
        await asyncio.sleep(2)

        # Verify (and auto-correct) MAX user ID
        try:
            if client.inner.me:
                actual_id = int(client.inner.me.id)
                if actual_id and actual_id != self._my_user_id:
                    log.warning(
                        "MAX user ID mismatch for '%s': config=%d server=%d "
                        "— fixing routing table automatically",
                        self.user.name, self._my_user_id, actual_id,
                    )
                    updated = self.lookup.update_max_user_id(self._my_user_id, actual_id)
                    self._my_user_id = actual_id
                    log.info(
                        "MAX routing corrected for '%s': now uses server ID %d (%d entries updated)",
                        self.user.name, actual_id, updated,
                    )
                else:
                    log.info(
                        "MAX user ID verified for '%s': %d (matches config)",
                        self.user.name, self._my_user_id,
                    )
        except Exception as exc:
            log.warning("Could not verify MAX user ID for '%s': %s", self.user.name, exc)

        # Pre-populate name cache with members of bridged MAX chats.
        # Failures are non-fatal — names will be resolved on demand.
        await self._preload_chat_members()

    async def _ping_watchdog(self):
        """Periodically ping the MAX server; force-disconnect on repeated failures.

        pymax's built-in ping uses _send_and_wait which logs ERROR tracebacks
        on every timeout but does NOT trigger a reconnect.  The recv_task may
        stay alive for many minutes on a half-dead socket.  This watchdog
        detects the problem early and kills the connection so that
        _reconnect_loop picks up immediately.
        """
        consecutive_failures = 0
        while not self._stopped:
            await asyncio.sleep(PING_INTERVAL)
            if self._stopped or not self.client:
                break
            try:
                ok = await self.client.ping(timeout=10.0)
            except Exception:
                ok = False
            if ok:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                log.warning("MAX listener %s: ping failed (%d/%d)",
                            self.user.name, consecutive_failures, PING_MAX_FAILURES)
                if consecutive_failures >= PING_MAX_FAILURES:
                    log.warning("MAX listener %s: %d consecutive ping failures, "
                                "forcing disconnect for reconnect",
                                self.user.name, consecutive_failures)
                    try:
                        await self.client.disconnect()
                    except Exception:
                        pass
                    consecutive_failures = 0
                    # recv_task is now done → _reconnect_loop will proceed

    async def _reconnect_loop(self):
        delay = RECONNECT_BASE_DELAY
        while not self._stopped:
            try:
                if self.client and self.client.recv_task:
                    await self.client.recv_task
            except (Exception, asyncio.CancelledError):
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
                # Restart ping watchdog for the new connection
                if self._ping_task and not self._ping_task.done():
                    self._ping_task.cancel()
                self._ping_task = asyncio.create_task(self._ping_watchdog())
                log.info("MAX listener %s: reconnected (User ID: %d)",
                         self.user.name, self._my_user_id)
            except Exception as e:
                log.error("MAX listener %s: reconnect failed: %s", self.user.name, e)

    async def stop(self):
        self._stopped = True
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    # ── Packet queue ──────────────────────────────────────────────────────────

    async def _enqueue_packet(self, data: dict[str, Any]):
        """Recv callback — just put the packet into the queue (instant, no I/O)."""
        self._packet_queue.put_nowait(data)

    async def _worker(self):
        """Process packets from the queue.  Runs outside the recv loop, so
        ``_send_and_wait`` calls (get_file_by_id, get_users, etc.) are safe."""
        while True:
            try:
                data = await self._packet_queue.get()
            except asyncio.CancelledError:
                return
            try:
                await self._process_packet(data)
            except Exception as e:
                log.error("Error processing packet (opcode=%s): %s",
                          data.get("opcode"), e, exc_info=True)

    async def _process_packet(self, data: dict[str, Any]):
        opcode = data.get("opcode", 0)
        if opcode == 128:  # NOTIF_MESSAGE (new, edit, delete — via status field)
            await self._handle_message(data)
        elif opcode in (66, 142):  # MSG_DELETE / NOTIF_MSG_DELETE
            await self._handle_deleted_message(data)
        elif opcode == 155:  # NOTIF_MSG_REACTIONS_CHANGED
            await self._handle_reaction(data)

    # ── Name resolution ───────────────────────────────────────────────────────

    async def _preload_chat_members(self):
        """Pre-load names of all members in bridged MAX chats into _name_cache.

        Also builds _known_group_ids from the pymax chat/channel lists so that
        the DM bridge can distinguish DMs from unconfigured groups.

        Failures are non-fatal: names will be resolved on demand via
        ``_resolve_sender_name``.
        """
        # Collect all group/channel IDs visible to this account
        if self.client:
            for chat in self.client.inner.chats:
                self._known_group_ids.add(chat.id)
            for ch in self.client.inner.channels:
                self._known_group_ids.add(ch.id)

        max_chat_ids = set()
        for entry in self.config.bridges:
            max_chat_ids.add(entry.max_chat_id)

        for chat_id in max_chat_ids:
            loaded = False
            for attempt in range(3):
                if not self.client or not self.client.is_connected:
                    log.warning("Skipping preload for chat %s: client disconnected", chat_id)
                    break
                try:
                    members, _ = await asyncio.wait_for(
                        self.client.inner.load_members(chat_id),
                        timeout=10.0,
                    )
                    for member in members:
                        c = member.contact
                        if c and c.id and c.id not in self._name_cache:
                            name = self._extract_name(c)
                            if name:
                                self._name_cache[c.id] = name
                    log.info("Pre-loaded %d member names for MAX chat %s",
                             len(members) if members else 0, chat_id)
                    loaded = True
                    break
                except asyncio.TimeoutError:
                    log.warning("Preload members for chat %s timed out (attempt %d/3)",
                                chat_id, attempt + 1)
                except Exception as e:
                    log.warning("Failed to preload members for chat %s (attempt %d/3): %s",
                                chat_id, attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(2)
            if not loaded:
                log.warning("Could not preload members for chat %s after 3 attempts "
                            "(names will be resolved on demand)", chat_id)

    async def _resolve_sender_name(self, sender_id: int) -> str:
        """Resolve display name for a MAX user ID.

        Checks local cache and PyMax's internal cache first.  If both miss,
        fetches via ``get_users`` (safe because the worker runs outside the
        recv loop).
        """
        if sender_id in self._name_cache:
            return self._name_cache[sender_id]

        # Try PyMax's internal user cache
        name = self._try_pymax_cache(sender_id)
        if name:
            self._name_cache[sender_id] = name
            log.debug("Resolved MAX user %s → %s (from PyMax cache)", sender_id, name)
            return name

        # Fetch from server (no deadlock risk — worker is outside recv loop)
        try:
            users = await asyncio.wait_for(
                self.client.get_users([sender_id]),
                timeout=5.0,
            )
            if users:
                u0 = users[0]
                log.debug(
                    "MAX user %s fields: names=%s display_name=%s "
                    "first_name=%s username=%s login=%s",
                    sender_id,
                    getattr(u0, 'names', None),
                    getattr(u0, 'display_name', None),
                    getattr(u0, 'first_name', None),
                    getattr(u0, 'username', None),
                    getattr(u0, 'login', None),
                )
                name = self._extract_name(u0)
                if name:
                    self._name_cache[sender_id] = name
                    log.debug("Resolved MAX user %s → %s (network)", sender_id, name)
                    return name
        except Exception:
            pass

        fallback = f"User:{sender_id}"
        self._name_cache[sender_id] = fallback
        return fallback

    def _try_pymax_cache(self, sender_id: int) -> str | None:
        """Try to extract name from PyMax's internal user/contact cache."""
        if not self.client:
            return None
        try:
            u = self.client.inner.get_cached_user(sender_id)
            if u is not None:
                log.debug(
                    "PyMax cached user %s: names=%s display_name=%s "
                    "first_name=%s username=%s login=%s",
                    sender_id,
                    getattr(u, 'names', None),
                    getattr(u, 'display_name', None),
                    getattr(u, 'first_name', None),
                    getattr(u, 'username', None),
                    getattr(u, 'login', None),
                )
                return self._extract_name(u)

            for contact in self.client.inner.contacts:
                if contact.id == sender_id:
                    return self._extract_name(contact)
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_name_from_names(names) -> str | None:
        """Extract best display name from a list of Name objects.

        Priority: ONEME with first+last > ONEME first only > any other entry.
        """
        if not names:
            return None

        # Pass 1: prefer ONEME (profile) entries with both first+last name
        for n in names:
            ntype = getattr(n, 'type', None)
            if ntype and ntype != 'ONEME':
                continue
            first = getattr(n, 'first_name', None) or ''
            last = getattr(n, 'last_name', None) or ''
            if first and last:
                return f"{first} {last}"

        # Pass 2: ONEME with first name only
        for n in names:
            ntype = getattr(n, 'type', None)
            if ntype and ntype != 'ONEME':
                continue
            first = getattr(n, 'first_name', None) or ''
            if first:
                return first
            fallback = getattr(n, 'name', None)
            if fallback:
                return fallback

        # Pass 3: any entry (CUSTOM, etc.)
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
        # 1. names array — first_name + last_name (highest priority)
        if hasattr(u, 'names') and u.names:
            name = MaxListener._extract_name_from_names(u.names)
            if name:
                return name
        # 2. display_name, but only if it differs from username/login
        username = getattr(u, 'username', None) or getattr(u, 'login', None)
        if hasattr(u, 'display_name') and u.display_name:
            if not username or u.display_name != username:
                return u.display_name
        # 3. first_name
        if hasattr(u, 'first_name') and u.first_name:
            return u.first_name
        # 4. username as last resort
        if username:
            return username
        return None

    # ── Routing guard ─────────────────────────────────────────────────────────

    def _is_primary_for(self, chat_id: int) -> bool:
        """Return True if this MaxListener is the primary handler for *chat_id*."""
        entry = self.lookup.get_primary_by_max(chat_id)
        return (
            entry is not None
            and entry.user.telegram_user_id == self.user.telegram_user_id
        )

    # ── Message handlers ──────────────────────────────────────────────────────

    async def _handle_message(self, packet: dict):
        payload = packet.get("payload", {})
        chat_id = payload.get("chatId")

        if not self._is_primary_for(chat_id):
            # DM bridge path: if this chat isn't a configured bridge,
            # check if it's a DM and forward to the DM bridge.
            if self.on_dm and chat_id and chat_id not in self._known_group_ids:
                await self._handle_dm_message(packet)
            return

        message = payload.get("message", {})
        msg_id = str(message.get("id", ""))
        sender_id = message.get("sender") or payload.get("fromUserId")
        status = message.get("status")

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

        if msg_id and self.mirrors.is_max_mirror(msg_id):
            log.debug("MAX msg %s → is mirror, skipping", msg_id)
            return

        bridge_entry = self.lookup.get_primary_by_max(chat_id)
        if not bridge_entry:
            return

        sender_name = await self._resolve_sender_name(sender_id)
        text = message.get("text")
        elements = message.get("elements")
        fmt = max_elements_to_internal(elements) or None
        attaches = message.get("attaches", [])

        reply_to = None
        link = message.get("link")
        if link and link.get("type") == "REPLY":
            reply_to = link.get("messageId")
            if not reply_to:
                link_msg = link.get("message")
                if isinstance(link_msg, dict):
                    reply_to = link_msg.get("id")
            if reply_to:
                reply_to = str(reply_to)

        # ── Handle sticker ────────────────────────────────────────────────
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

        # ── Download media attachments ────────────────────────────────────
        downloaded, failed_labels = await self._download_attaches(
            attaches, chat_id, msg_id,
        )

        # Emit failure fallback
        if failed_labels:
            fail_text = f"{text or ''}\n" + "\n".join(
                f"[{l} — media download failed]" for l in failed_labels
            )
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

        # Emit media event(s)
        if not downloaded:
            pass  # text-only path below
        elif len(downloaded) == 1:
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

    # ── Attachment download ───────────────────────────────────────────────────

    _ATT_META = {
        "PHOTO": ("photo", "photo.jpg",  "image/jpeg"),
        "VIDEO": ("video", "video.mp4",  "video/mp4"),
        "FILE":  ("file",  "file",        "application/octet-stream"),
        "AUDIO": ("audio", "audio.mp3",  "audio/mpeg"),
    }

    async def _download_attaches(
        self, attaches: list[dict], chat_id: int, msg_id: str,
    ) -> tuple[list[tuple[str, MediaInfo]], list[str]]:
        """Download all media attachments, resolving file URLs if needed.

        Returns (downloaded, failed_labels).  Safe to call ``get_file_by_id``
        here because the worker runs outside the pymax recv loop.
        """
        downloaded: list[tuple[str, MediaInfo]] = []
        failed_labels: list[str] = []

        for att in attaches:
            att_type = att.get("_type", "")
            if att_type not in self._ATT_META:
                continue

            media_url = att.get("url") or att.get("baseUrl")
            default_evt, default_fname, default_mime = self._ATT_META[att_type]
            fname = att.get("fileName") or att.get("name") or default_fname
            mime = att.get("mimeType") or default_mime

            # FILE attachments uploaded via opcode 87 may lack a direct URL.
            # Resolve via FILE_DOWNLOAD (opcode 88) — safe here (worker context).
            if not media_url and att.get("fileId") and self.client:
                try:
                    file_req = await self.client.inner.get_file_by_id(
                        chat_id=chat_id,
                        message_id=int(msg_id),
                        file_id=int(att["fileId"]),
                    )
                    if file_req and file_req.url:
                        media_url = file_req.url
                        log.debug("Resolved file URL via opcode 88: fileId=%s", att["fileId"])
                except Exception as e:
                    log.warning("Failed to resolve file URL for fileId=%s: %s",
                                att.get("fileId"), e)

            # VIDEO attachments carry only videoId/token; resolve the playback
            # URLs via VIDEO_PLAY (opcode 83) — safe here (worker context).
            # The response contains multiple quality variants (MOBILE/SD/HD/...);
            # we try each in turn since some may return 400 from the CDN.
            video_candidates: list[str] = []
            if not media_url and att_type == "VIDEO" and att.get("videoId") and self.client:
                try:
                    from pymax.static.enum import Opcode
                    resp = await self.client.inner._send_and_wait(
                        opcode=Opcode.VIDEO_PLAY,  # 83
                        payload={
                            "chatId": chat_id,
                            "messageId": int(msg_id),
                            "videoId": int(att["videoId"]),
                        },
                    )
                    vp = (resp or {}).get("payload") or {}
                    log.info("VIDEO_PLAY payload for videoId=%s: %s",
                             att["videoId"], vp)
                    # Collect every string value that looks like a direct CDN
                    # URL. Skip ``cache`` (bool) and ``EXTERNAL`` (m.ok.ru
                    # player webpage — returns HTML, not video bytes).
                    for k, v in vp.items():
                        if k in ("cache", "EXTERNAL"):
                            continue
                        if isinstance(v, str) and v.startswith(("http://", "https://")):
                            video_candidates.append(v)
                except Exception as e:
                    log.warning("Failed to resolve video URL for videoId=%s: %s",
                                att.get("videoId"), e)

            if media_url:
                try:
                    data = await download_media(media_url)
                    downloaded.append((
                        default_evt,
                        MediaInfo(data=data, filename=fname, mime_type=mime),
                    ))
                except Exception as e:
                    log.error("Failed to download %s from %s: %s",
                              att_type, media_url, e, exc_info=True)
                    failed_labels.append(att_type.capitalize())
            elif video_candidates:
                try:
                    data = await try_download_media(video_candidates)
                    downloaded.append((
                        default_evt,
                        MediaInfo(data=data, filename=fname, mime_type=mime),
                    ))
                except Exception as e:
                    log.error("Failed to download %s from %d candidates: %s",
                              att_type, len(video_candidates), e, exc_info=True)
                    failed_labels.append(att_type.capitalize())
            else:
                log.warning("No download URL for %s attachment (keys=%s)",
                            att_type, list(att.keys()))
                failed_labels.append(att_type.capitalize())

        return downloaded, failed_labels

    # ── DM bridge handler ──────────────────────────────────────────────────────

    async def _handle_dm_message(self, packet: dict):
        """Handle a message from a DM chat — forward to DM bridge.

        Supports all message types: text, media (photo/video/file/audio),
        stickers, edits, and deletes — mirroring the group handler logic.
        """
        payload = packet.get("payload", {})
        chat_id = payload.get("chatId")
        message = payload.get("message", {})
        msg_id = str(message.get("id", ""))
        sender_id = message.get("sender") or payload.get("fromUserId")
        status = message.get("status")

        if not chat_id or not sender_id or not msg_id:
            return

        # Echo prevention: rely on MirrorTracker.
        # In MAX DMs, the notification arrives at the SENDER's connection,
        # so sender_id == self._my_user_id is the normal case for outgoing DMs.
        if self.mirrors.is_max_mirror(msg_id):
            return

        # Determine DM direction:
        # - sender == self → outgoing DM (we sent it to chat_id user)
        #   → recipient is the chat_id user, forward to their bot chat
        # - sender != self → incoming DM (someone sent it to us)
        #   → recipient is us (self._my_user_id), forward to our bot chat
        if sender_id == self._my_user_id:
            recipient_user_id = chat_id  # the other party
        else:
            recipient_user_id = self._my_user_id  # us

        if status == "EDITED":
            text = message.get("text")
            sender_name = await self._resolve_sender_name(sender_id)
            elements = message.get("elements")
            fmt = max_elements_to_internal(elements) or None
            await self.on_dm(
                sender_id=sender_id, sender_name=sender_name,
                chat_id=chat_id, msg_id=msg_id, text=text,
                event_type="edit", formatting=fmt,
                recipient_max_user_id=recipient_user_id,
            )
            return

        if status == "REMOVED":
            await self.on_dm(
                sender_id=sender_id, sender_name="Unknown",
                chat_id=chat_id, msg_id=msg_id, text=None,
                event_type="delete",
                recipient_max_user_id=recipient_user_id,
            )
            return

        text = message.get("text")
        sender_name = await self._resolve_sender_name(sender_id)
        elements = message.get("elements")
        fmt = max_elements_to_internal(elements) or None
        attaches = message.get("attaches", [])

        log.debug("MAX DM: chat=%s sender=%s (%s) msg=%s text=%r attaches=%s",
                 chat_id, sender_id, sender_name, msg_id,
                 (text or "")[:60],
                 [a.get("_type") for a in attaches])

        # Sticker
        for att in attaches:
            if att.get("_type") == "STICKER":
                await self.on_dm(
                    sender_id=sender_id, sender_name=sender_name,
                    chat_id=chat_id, msg_id=msg_id,
                    text="[Sticker]", event_type="sticker",
                    recipient_max_user_id=recipient_user_id,
                )
                return

        # Download media attachments
        downloaded, failed_labels = await self._download_attaches(
            attaches, chat_id, msg_id,
        )

        if failed_labels:
            fail_text = f"{text or ''}\n" + "\n".join(
                f"[{l} — media download failed]" for l in failed_labels
            )
            await self.on_dm(
                sender_id=sender_id, sender_name=sender_name,
                chat_id=chat_id, msg_id=msg_id,
                text=fail_text.strip(), event_type="text",
                recipient_max_user_id=recipient_user_id,
            )

        if downloaded:
            media_list = [(evt_type, media) for evt_type, media in downloaded]
            await self.on_dm(
                sender_id=sender_id, sender_name=sender_name,
                chat_id=chat_id, msg_id=msg_id,
                text=text, event_type="media",
                media_list=media_list, formatting=fmt,
                recipient_max_user_id=recipient_user_id,
            )
            return

        if text:
            await self.on_dm(
                sender_id=sender_id, sender_name=sender_name,
                chat_id=chat_id, msg_id=msg_id,
                text=text, event_type="text", formatting=fmt,
                recipient_max_user_id=recipient_user_id,
            )

    # ── Edit / delete / reaction handlers ─────────────────────────────────────

    async def _handle_notif_edit(self, chat_id, message: dict, msg_id: str, sender_id):
        if not chat_id or not msg_id:
            return
        if self.mirrors.is_max_mirror(msg_id):
            return
        bridge_entry = self.lookup.get_primary_by_max(chat_id)
        if not bridge_entry:
            return
        sender_name = await self._resolve_sender_name(sender_id) if sender_id else "Unknown"
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
        payload = packet.get("payload", {})
        chat_id = payload.get("chatId")
        if not self._is_primary_for(chat_id):
            return
        message_ids = payload.get("messageIds", [])
        await self._handle_notif_delete(chat_id, message_ids)

    async def _handle_reaction(self, packet: dict):
        """Handle NOTIF_MSG_REACTIONS_CHANGED (opcode 155)."""
        payload = packet.get("payload", {})
        chat_id = payload.get("chatId")
        message_id = str(payload.get("messageId", ""))
        if not chat_id or not message_id:
            return

        if not self._is_primary_for(chat_id):
            return

        bridge_entry = self.lookup.get_primary_by_max(chat_id)
        if not bridge_entry:
            return

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
            reaction_emoji=our_emoji,
        ))
