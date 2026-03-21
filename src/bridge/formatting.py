# Bridge formatting utilities.
#
# Converts between Telegram entities and MAX elements, using a platform-agnostic
# intermediate representation (list of FormattingEntity dicts).
#
# Supported mapping:
#   TG BOLD           <-> MAX STRONG
#   TG ITALIC         <-> MAX EMPHASIZED
#   TG UNDERLINE      <-> MAX UNDERLINE
#   TG STRIKETHROUGH  <-> MAX STRIKETHROUGH
#   TG CODE / PRE     --> plain text with backtick markers (MAX has no equivalent)
#   TG TEXT_LINK      --> "text (url)" inline (MAX has no equivalent)
#   TG SPOILER        --> plain text (MAX has no equivalent)

from __future__ import annotations

import html as html_lib
import logging
from typing import Any

log = logging.getLogger("bridge.formatting")

# Zero-width space prepended to every message the bridge sends to Telegram.
# Listeners check for this marker to recognise bridge-sent messages and avoid
# echo loops.  In regular TG groups message IDs are per-user (sender and
# observer see different IDs), so MirrorTracker alone cannot catch echoes
# when multiple user accounts listen to the same chat.  The marker is
# invisible to end users in all Telegram clients.
MIRROR_MARKER = "\u200b"

# ── Intermediate representation ──────────────────────────────────────────────
#
# Each entity is a dict with:
#   type: str     - "bold", "italic", "underline", "strikethrough",
#                   "code", "pre", "text_link", "spoiler", "blockquote"
#   offset: int   - Python string index (NOT UTF-16)
#   length: int   - Python character count
#   url: str|None - only for text_link
#   language: str|None - only for pre

# TG MessageEntityType.name → our normalised type
_TG_TYPE_MAP: dict[str, str] = {
    "BOLD": "bold",
    "ITALIC": "italic",
    "UNDERLINE": "underline",
    "STRIKETHROUGH": "strikethrough",
    "CODE": "code",
    "PRE": "pre",
    "TEXT_LINK": "text_link",
    "SPOILER": "spoiler",
    "BLOCKQUOTE": "blockquote",
}

# Our normalised type → MAX element type (only types MAX supports)
_TO_MAX_TYPE: dict[str, str] = {
    "bold": "STRONG",
    "italic": "EMPHASIZED",
    "underline": "UNDERLINE",
    "strikethrough": "STRIKETHROUGH",
}

# MAX element type → our normalised type
_FROM_MAX_TYPE: dict[str, str] = {
    "STRONG": "bold",
    "EMPHASIZED": "italic",
    "UNDERLINE": "underline",
    "STRIKETHROUGH": "strikethrough",
}

# Our normalised type → HTML tag name (for TG output)
_HTML_TAG: dict[str, str] = {
    "bold": "b",
    "italic": "i",
    "underline": "u",
    "strikethrough": "s",
    "code": "code",
    "spoiler": "tg-spoiler",
    "blockquote": "blockquote",
}


# ── UTF-16 helpers ───────────────────────────────────────────────────────────
# Telegram entities use UTF-16 code-unit offsets.  Python strings use codepoint
# indices.  For characters in the BMP (U+0000..U+FFFF) the two are identical,
# but astral-plane characters (most emoji) occupy 2 UTF-16 units yet only 1
# Python character.

def _utf16_to_python(text: str, utf16_offset: int) -> int:
    """Convert a UTF-16 code-unit offset to a Python string index."""
    consumed = 0
    for i, ch in enumerate(text):
        if consumed >= utf16_offset:
            return i
        consumed += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


def _utf16_span_to_python(text: str, utf16_offset: int, utf16_length: int) -> tuple[int, int]:
    """Convert a UTF-16 (offset, length) pair to Python (offset, length)."""
    py_start = _utf16_to_python(text, utf16_offset)
    py_end = _utf16_to_python(text, utf16_offset + utf16_length)
    return py_start, py_end - py_start


# ── TG entities → intermediate ───────────────────────────────────────────────

def tg_entities_to_internal(text: str, tg_entities: list | None) -> list[dict]:
    """Convert Pyrogram MessageEntity list to internal formatting dicts.

    *text* is the plain message text (``message.text`` or ``message.caption``).
    """
    if not tg_entities:
        return []

    result: list[dict] = []
    for ent in tg_entities:
        type_name = ent.type.name if hasattr(ent.type, "name") else str(ent.type)
        normalised = _TG_TYPE_MAP.get(type_name)
        if not normalised:
            continue  # mention, hashtag, url, etc. — skip

        py_offset, py_length = _utf16_span_to_python(text, ent.offset, ent.length)

        entry: dict[str, Any] = {
            "type": normalised,
            "offset": py_offset,
            "length": py_length,
        }
        if normalised == "text_link" and ent.url:
            entry["url"] = ent.url
        if normalised == "pre" and getattr(ent, "language", None):
            entry["language"] = ent.language

        result.append(entry)

    return result


# ── MAX elements → intermediate ──────────────────────────────────────────────

def max_elements_to_internal(elements: list | None) -> list[dict]:
    """Convert MAX message elements (list of dicts or objects) to internal format.

    MAX elements use plain Python-string offsets (``from`` field), so no
    UTF-16 conversion is needed.
    """
    if not elements:
        return []

    result: list[dict] = []
    for el in elements:
        if isinstance(el, dict):
            el_type = el.get("type", "")
            offset = el.get("from", 0)
            length = el.get("length", 0)
        else:
            # pymax Element object
            el_type = getattr(el, "type", "")
            if hasattr(el_type, "value"):
                el_type = el_type.value  # enum → string
            offset = getattr(el, "from_", 0)
            length = getattr(el, "length", 0)

        normalised = _FROM_MAX_TYPE.get(el_type)
        if not normalised:
            continue

        result.append({
            "type": normalised,
            "offset": offset,
            "length": length,
        })

    return result


# ── Intermediate → MAX elements ──────────────────────────────────────────────

def internal_to_max_elements(entities: list[dict] | None) -> list[dict]:
    """Convert internal formatting to MAX element dicts ``{type, from, length}``."""
    if not entities:
        return []

    result: list[dict] = []
    for ent in entities:
        max_type = _TO_MAX_TYPE.get(ent["type"])
        if not max_type:
            continue  # code, pre, text_link, etc. — not supported by MAX
        result.append({
            "type": max_type,
            "from": ent["offset"],
            "length": ent["length"],
        })

    return result


# ── Intermediate → TG HTML ───────────────────────────────────────────────────

def internal_to_tg_html(text: str, entities: list[dict] | None) -> str:
    """Render text + internal entities as Telegram-compatible HTML.

    Returns an HTML string ready for ``parse_mode="html"`` in Pyrogram.
    The ``MIRROR_MARKER`` is NOT included — callers prepend it separately.
    """
    if not text:
        return ""
    if not entities:
        return html_lib.escape(text)

    # Build a list of (position, is_close, tag, priority) events.
    # priority: opening tags at same position → larger offset first (outer opens last)
    #           closing tags at same position → smaller offset first (inner closes first)
    events: list[tuple[int, int, str]] = []  # (position, sort_key, tag_html)

    for ent in entities:
        start = ent["offset"]
        end = start + ent["length"]
        etype = ent["type"]

        if etype == "text_link":
            url = html_lib.escape(ent.get("url", ""))
            open_tag = f'<a href="{url}">'
            close_tag = "</a>"
        elif etype == "pre":
            lang = ent.get("language", "")
            if lang:
                open_tag = f'<pre><code class="language-{html_lib.escape(lang)}">'
                close_tag = "</code></pre>"
            else:
                open_tag = "<pre>"
                close_tag = "</pre>"
        else:
            tag = _HTML_TAG.get(etype)
            if not tag:
                continue
            open_tag = f"<{tag}>"
            close_tag = f"</{tag}>"

        # Sort key: at same position, opening tags come before closing.
        # For nested entities at same start: longer range opens first (outer wraps inner).
        events.append((start, 0, -ent["length"], open_tag))
        events.append((end, 1, ent["length"], close_tag))

    events.sort(key=lambda e: (e[0], e[1], e[2]))

    parts: list[str] = []
    prev = 0
    for pos, _, _, tag_html in events:
        if pos > prev:
            parts.append(html_lib.escape(text[prev:pos]))
        parts.append(tag_html)
        prev = pos

    if prev < len(text):
        parts.append(html_lib.escape(text[prev:]))

    return "".join(parts)


# ── Sender name prefix ───────────────────────────────────────────────────────

def prepend_sender_name(name: str, text: str) -> str:
    """Legacy helper — plain text only."""
    return f"[{name}]: {text}"


def prepend_sender_name_fmt(
    name: str,
    text: str,
    entities: list[dict] | None,
) -> tuple[str, list[dict] | None]:
    """Prepend ``[name]: `` to text and shift all entity offsets.

    Returns ``(new_text, shifted_entities)``.
    """
    prefix = f"[{name}]: "
    shift = len(prefix)
    new_text = prefix + text
    if not entities:
        return new_text, entities
    shifted = [
        {**ent, "offset": ent["offset"] + shift}
        for ent in entities
    ]
    return new_text, shifted
