"""One-shot diagnostic for MAX VIDEO attachments.

MAX hands out video URLs via ``VIDEO_PLAY`` (opcode 83) that point at the OK
CDN (``maxvdNNN.okcdn.ru``).  Those progressive-MP4 URLs have never worked from
this bridge — they answer ``400`` with a one-byte numeric body regardless of
source IP, User-Agent, Referer or Range (verified 2026-08-09 from both the
server and a residential IP).  This module exists to capture the *actual*
``VIDEO_PLAY`` payload and probe every URL it contains, so the download path
can be built against real data instead of guesses.

Enabled with ``VIDEO_PROBE=1``; it scans the newest messages of each bridged
MAX chat for a VIDEO attach, then logs everything it can learn about it.
Purely read-only — it never emits bridge events.
"""

import json
import logging
import re
import time
from typing import Any

import aiohttp

log = logging.getLogger("bridge.max.videoprobe")

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)


async def probe_recent_video(client: Any, chat_ids: list[int]) -> None:
    """Find the newest VIDEO attach across ``chat_ids`` and dump diagnostics."""
    try:
        found = await _find_videos(client, chat_ids, limit=6)
    except Exception as e:
        log.warning("PROBE: history scan failed: %s", e, exc_info=True)
        return

    if not found:
        log.info("PROBE: no VIDEO attach found in recent history of %s", chat_ids)
        return

    log.info("PROBE: %d video(s) to probe", len(found))
    for idx, (chat_id, msg_id, sent_at, att) in enumerate(found):
        log.info("PROBE: ── video msg=%s sent=%s videoId=%s %sx%s dur=%s",
                 msg_id, sent_at, att.get("videoId"),
                 att.get("width"), att.get("height"), att.get("duration"))
        # Alternative opcodes only need probing once — they are per-account,
        # not per-video.
        await _probe_one(client, chat_id, msg_id, att, alternatives=(idx == 0))


async def _probe_one(
    client: Any, chat_id: int, msg_id: str, att: dict, alternatives: bool = False,
) -> None:
    video_id = att.get("videoId")
    if not video_id:
        log.warning("PROBE: attach has no videoId, nothing to resolve")
        return

    try:
        from pymax.static.enum import Opcode
        resp = await client.inner._send_and_wait(
            opcode=Opcode.VIDEO_PLAY,
            payload={
                "chatId": chat_id,
                "messageId": int(msg_id),
                "videoId": int(video_id),
            },
        )
    except Exception as e:
        log.warning("PROBE: VIDEO_PLAY failed: %s", e, exc_info=True)
        return

    payload = (resp or {}).get("payload") or {}
    log.info("PROBE: VIDEO_PLAY payload = %s", _clip(payload))

    for key, value in payload.items():
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            await _probe_url(key, value)

    if alternatives:
        await _probe_alternatives(client, chat_id, msg_id, int(video_id), att)


async def _probe_alternatives(
    client: Any, chat_id: int, msg_id: str, video_id: int, att: dict,
) -> None:
    """Try other protocol routes to the same video.

    The ``MP4_480`` URL that VIDEO_PLAY hands out answers 400/10 with a
    provably valid signature, so the working route is probably a different
    opcode or a differently-shaped request.
    """
    from pymax.static.enum import Opcode

    async def call(label: str, opcode: Any, payload: dict) -> None:
        try:
            r = await client.inner._send_and_wait(opcode=opcode, payload=payload)
            log.info("PROBE: alt[%s] payload=%s", label, _clip((r or {}).get("payload")))
        except Exception as e:
            log.info("PROBE: alt[%s] failed: %s", label, e)

    # FILE_DOWNLOAD against the video id — videos may also be addressable as files.
    await call("FILE_DOWNLOAD/videoId", Opcode.FILE_DOWNLOAD,
               {"chatId": chat_id, "messageId": int(msg_id), "fileId": video_id})

    # Request-shape variants.  Other MAX clients call VIDEO_PLAY with
    # ``{videoId, token}`` and no chat/message context, and get MP4_720 /
    # MP4_1080 back — our chatId+messageId shape only ever yields MP4_480,
    # which is the rendition the CDN refuses.  The VIDEO attach also carries
    # its own ``token``, distinct from the session token.
    session_token = getattr(client, "_token", None)
    attach_token = att.get("token")

    shapes: list[tuple[str, dict]] = [
        ("videoId+sessionToken", {"videoId": video_id, "token": session_token}),
        ("videoId+attachToken", {"videoId": video_id, "token": attach_token}),
        ("videoId only", {"videoId": video_id}),
        ("full+attachToken", {"chatId": chat_id, "messageId": int(msg_id),
                              "videoId": video_id, "token": attach_token}),
    ]
    for label, shape in shapes:
        if all(v is not None for v in shape.values()):
            await call(f"VIDEO_PLAY/{label}", Opcode.VIDEO_PLAY, shape)


# ── history scan ──────────────────────────────────────────────────────────────

async def _find_videos(
    client: Any, chat_ids: list[int], limit: int = 6,
) -> list[tuple[int, str, str, dict]]:
    """Return up to ``limit`` (chat_id, message_id, sent_at, attach) tuples,
    newest first, across all bridged chats."""
    from pymax.static.enum import Opcode
    from pymax.payloads import FetchHistoryPayload

    out: list[tuple[int, str, str, dict]] = []
    for chat_id in chat_ids:
        payload = FetchHistoryPayload(
            chat_id=chat_id,
            from_time=int(time.time() * 1000),
            forward=0,
            backward=200,
        ).model_dump(by_alias=True)

        resp = await client.inner._send_and_wait(
            opcode=Opcode.CHAT_HISTORY, payload=payload, timeout=15,
        )
        messages = ((resp or {}).get("payload") or {}).get("messages") or []
        log.info("PROBE: chat %s → %d messages in history", chat_id, len(messages))

        # Newest last in MAX history; walk backwards to hit recent videos first.
        for msg in reversed(messages):
            for att in msg.get("attaches") or []:
                if att.get("_type") == "VIDEO":
                    sent = msg.get("time")
                    stamp = (
                        time.strftime("%Y-%m-%d %H:%M", time.localtime(sent / 1000))
                        if isinstance(sent, (int, float)) else str(sent)
                    )
                    out.append((chat_id, str(msg.get("id")), stamp, att))
                    if len(out) >= limit:
                        return out
    return out


# ── URL probing ───────────────────────────────────────────────────────────────

async def _probe_url(label: str, url: str) -> None:
    """GET ``url`` and log what came back; parse OK player pages for metadata."""
    log.info("PROBE: [%s] %s", label, url)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers={"User-Agent": _UA, "Accept": "*/*"},
                allow_redirects=True,
            ) as resp:
                body = await resp.content.read(300_000)
                log.info(
                    "PROBE: [%s] → status=%s ctype=%s len=%s final_url=%s head=%r",
                    label, resp.status, resp.headers.get("Content-Type"),
                    resp.headers.get("Content-Length"), str(resp.url)[:160],
                    body[:64],
                )
    except Exception as e:
        log.warning("PROBE: [%s] request failed: %s", label, e)
        return

    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "html" in ctype:
        _dump_player_metadata(label, body)
    elif "mpegurl" in ctype or body[:7] == b"#EXTM3U":
        log.info("PROBE: [%s] HLS playlist:\n%s", label,
                 body[:2000].decode("utf-8", "replace"))


def _dump_player_metadata(label: str, body: bytes) -> None:
    """Pull the embedded player metadata out of an OK video page.

    The OK player carries its formats in a ``data-options`` attribute holding
    JSON whose ``flashvars.metadata`` is itself a JSON string containing
    ``videos`` (progressive MP4 renditions) and ``hlsManifestUrl``.
    """
    html = body.decode("utf-8", "replace")
    m = re.search(r'data-options="([^"]+)"', html)
    if not m:
        log.info("PROBE: [%s] no data-options in page (len=%d); "
                 "title=%r", label, len(html),
                 (re.search(r"<title>(.*?)</title>", html, re.S) or [None, ""])[1][:120])
        return

    try:
        import html as html_mod
        options = json.loads(html_mod.unescape(m.group(1)))
        metadata = options.get("flashvars", {}).get("metadata")
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
    except Exception as e:
        log.warning("PROBE: [%s] could not parse data-options: %s", label, e)
        return

    if not isinstance(metadata, dict):
        log.info("PROBE: [%s] flashvars had no metadata dict (keys=%s)",
                 label, list(options.keys()))
        return

    log.info("PROBE: [%s] metadata keys=%s", label, sorted(metadata.keys()))
    for v in metadata.get("videos") or []:
        log.info("PROBE: [%s]   video name=%s url=%s",
                 label, v.get("name"), (v.get("url") or "")[:200])
    for key in ("hlsManifestUrl", "ondemandHls", "dashManifestUrl", "ondemandDash"):
        if metadata.get(key):
            log.info("PROBE: [%s]   %s=%s", label, key, str(metadata[key])[:200])


def _clip(obj: Any, limit: int = 2000) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)[:limit]
    except Exception:
        return repr(obj)[:limit]
