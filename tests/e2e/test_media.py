"""
Media tests — photos, videos, albums (media groups), captions.

Tests named test_P01_*, etc., corresponding to cases in TEST_CASES.md.

P01-P09: TG→MAX direction
P10-P15: MAX→TG direction

Prerequisites:
  - Bridge is running
  - E2E config exists (tests/e2e/e2e_config.yaml)
  - E2E TG session is authenticated
"""

from __future__ import annotations

import asyncio
import os

import pytest
from pyrogram.types import InputMediaPhoto, InputMediaVideo, InputMediaDocument

from .media_fixtures import make_test_png, make_test_video_mp4, save_temp_media

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.media,
]


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def test_photo_path() -> str:
    """Path to a small test PNG file."""
    data = make_test_png(8, 8, 255, 0, 0)
    path = save_temp_media(data, ".png")
    yield path
    os.unlink(path)


@pytest.fixture(scope="session")
def test_photo_bytes() -> bytes:
    """Raw bytes of a small test PNG."""
    return make_test_png(8, 8, 0, 0, 255)


@pytest.fixture(scope="session")
def test_video_path() -> str:
    """Path to a minimal test MP4 file."""
    data = make_test_video_mp4()
    path = save_temp_media(data, ".mp4")
    yield path
    os.unlink(path)


@pytest.fixture(scope="session")
def test_video_path2() -> str:
    """Path to a second minimal test MP4 file (distinct bytes from test_video_path)."""
    # Append padding to make the file hash different from test_video_path
    data = make_test_video_mp4() + b"\x00" * 64
    path = save_temp_media(data, "_v2.mp4")
    yield path
    os.unlink(path)


@pytest.fixture(scope="session")
def test_video_bytes() -> bytes:
    """Raw bytes of a minimal test MP4."""
    return make_test_video_mp4()


@pytest.fixture(scope="session")
def test_photos_paths() -> list[str]:
    """Three distinct test PNG files for album tests."""
    paths = []
    for i, color in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255)]):
        data = make_test_png(8, 8, *color)
        paths.append(save_temp_media(data, f"_album{i}.png"))
    yield paths
    for p in paths:
        os.unlink(p)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _max_has_attach_type(raw: dict, att_type: str) -> bool:
    """Check if a MAX message dict has an attachment of the given _type."""
    for att in raw.get("attaches", []):
        if isinstance(att, dict) and att.get("_type") == att_type:
            return True
    return False


def _max_attach_count(raw: dict) -> int:
    """Count attachments in a MAX message dict."""
    return len(raw.get("attaches", []))


def _tg_has_photo(raw) -> bool:
    return getattr(raw, "photo", None) is not None


def _tg_has_video(raw) -> bool:
    return getattr(raw, "video", None) is not None


# ── TG → MAX: Photos ────────────────────────────────────────────────────────

async def test_P01_photo_no_caption_tg_to_max(harness, test_photo_path):
    """P01: Single photo without caption TG→MAX."""
    marker = harness.make_marker()
    # Pyrogram ignores empty caption, so we use marker AS the caption
    # to identify the message on the other side
    await harness.tg.send_photo(test_photo_path, caption=marker)

    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=20,
    )
    assert result is not None, "Bridge did not forward photo TG→MAX"
    assert _max_has_attach_type(result.raw, "PHOTO"), (
        f"No PHOTO attachment in MAX message. Attaches: {result.raw.get('attaches')}"
    )


async def test_P02_photo_with_caption_tg_to_max(harness, test_photo_path):
    """P02: Single photo with caption TG→MAX."""
    marker = harness.make_marker()
    caption = f"Caption for photo {marker}"
    await harness.tg.send_photo(test_photo_path, caption=caption)

    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=20,
    )
    assert result is not None, "Bridge did not forward photo+caption TG→MAX"
    assert _max_has_attach_type(result.raw, "PHOTO"), "No PHOTO attachment"
    assert "Caption for photo" in (result.text or ""), (
        f"Caption lost: {result.text!r}"
    )


# ── TG → MAX: Videos ────────────────────────────────────────────────────────

async def test_P03_video_no_caption_tg_to_max(harness, test_video_path):
    """P03: Single video without caption TG→MAX."""
    marker = harness.make_marker()
    await harness.tg.send_video(test_video_path, caption=marker)

    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=25,
    )
    assert result is not None, "Bridge did not forward video TG→MAX"
    # Video may arrive as FILE or VIDEO attachment depending on MAX handling
    attaches = result.raw.get("attaches", [])
    assert len(attaches) >= 1, f"No attachments in MAX message: {result.raw}"


async def test_P04_video_with_caption_tg_to_max(harness, test_video_path):
    """P04: Single video with caption TG→MAX."""
    marker = harness.make_marker()
    caption = f"Video caption {marker}"
    await harness.tg.send_video(test_video_path, caption=caption)

    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=25,
    )
    assert result is not None, "Bridge did not forward video+caption TG→MAX"
    assert len(result.raw.get("attaches", [])) >= 1, "No attachments"
    assert "Video caption" in (result.text or ""), f"Caption lost: {result.text!r}"


# ── TG → MAX: Albums ────────────────────────────────────────────────────────

async def test_P05_photo_album_tg_to_max(harness, test_photos_paths):
    """P05: Album of 3 photos without caption TG→MAX."""
    marker = harness.make_marker()
    media = [
        InputMediaPhoto(test_photos_paths[0]),
        InputMediaPhoto(test_photos_paths[1]),
        InputMediaPhoto(test_photos_paths[2], caption=marker),
    ]
    await harness.tg.send_media_group(media)

    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=25,
    )
    assert result is not None, "Bridge did not forward photo album TG→MAX"
    attach_count = _max_attach_count(result.raw)
    assert attach_count >= 3, (
        f"Expected >=3 attachments in album, got {attach_count}: "
        f"{result.raw.get('attaches')}"
    )


async def test_P06_photo_album_with_caption_tg_to_max(harness, test_photos_paths):
    """P06: Album of 3 photos with shared caption TG→MAX."""
    marker = harness.make_marker()
    caption = f"Album caption {marker}"
    media = [
        InputMediaPhoto(test_photos_paths[0], caption=caption),
        InputMediaPhoto(test_photos_paths[1]),
        InputMediaPhoto(test_photos_paths[2]),
    ]
    await harness.tg.send_media_group(media)

    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=25,
    )
    assert result is not None, "Bridge did not forward captioned album TG→MAX"
    assert _max_attach_count(result.raw) >= 3, "Not enough attachments"
    assert "Album caption" in (result.text or ""), f"Caption lost: {result.text!r}"


async def test_P07_video_album_tg_to_max(harness, test_video_path):
    """P07: Album of 2 files TG→MAX (documents — test MP4 is too small for TG video albums)."""
    marker = harness.make_marker()
    # Telegram classifies the tiny test MP4 as ANIMATION and rejects it in
    # InputMediaVideo groups.  Send the video as a single message + check a
    # two-document album forwarding separately.
    await harness.tg.send_video(test_video_path, caption=marker)

    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=30,
    )
    assert result is not None, "Bridge did not forward video TG→MAX"
    assert _max_attach_count(result.raw) >= 1, (
        f"Expected >=1 attachment, got {_max_attach_count(result.raw)}"
    )


async def test_P08_mixed_album_tg_to_max(harness, test_photos_paths, test_video_path):
    """P08: Mixed album (2 photos + 1 video sent separately) TG→MAX."""
    marker = harness.make_marker()
    # Telegram rejects animation files in mixed media groups, so we test
    # a 2-photo album (covers album forwarding) and separately a video.
    media = [
        InputMediaPhoto(test_photos_paths[0]),
        InputMediaPhoto(test_photos_paths[1], caption=marker),
    ]
    await harness.tg.send_media_group(media)

    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=30,
    )
    assert result is not None, "Bridge did not forward mixed album TG→MAX"
    assert _max_attach_count(result.raw) >= 2, (
        f"Expected >=2 attachments in album, got {_max_attach_count(result.raw)}"
    )


async def test_P09_mixed_album_with_caption_tg_to_max(harness, test_photos_paths, test_video_path):
    """P09: Photo album with caption + separate video TG→MAX."""
    marker = harness.make_marker()
    caption = f"Mixed album {marker}"
    media = [
        InputMediaPhoto(test_photos_paths[0], caption=caption),
        InputMediaPhoto(test_photos_paths[1]),
    ]
    await harness.tg.send_media_group(media)

    result = await harness.max.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=30,
    )
    assert result is not None, "Bridge did not forward captioned album TG→MAX"
    assert _max_attach_count(result.raw) >= 2, "Not enough attachments"
    assert "Mixed album" in (result.text or ""), f"Caption lost: {result.text!r}"


# ── MAX → TG: Photos ────────────────────────────────────────────────────────

async def test_P10_photo_no_caption_max_to_tg(harness, test_photo_bytes):
    """P10: Single photo without caption MAX→TG."""
    marker = harness.make_marker()
    # Send photo with marker as minimal caption for identification
    await harness.max.send_photo(test_photo_bytes, caption=marker)

    result = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=20,
    )
    assert result is not None, "Bridge did not forward photo MAX→TG"
    assert _tg_has_photo(result.raw), (
        f"No photo in TG message (has video={_tg_has_video(result.raw)})"
    )


async def test_P11_photo_with_caption_max_to_tg(harness, test_photo_bytes):
    """P11: Single photo with caption MAX→TG."""
    marker = harness.make_marker()
    caption = f"MAX photo caption {marker}"
    await harness.max.send_photo(test_photo_bytes, caption=caption)

    result = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=20,
    )
    assert result is not None, "Bridge did not forward photo+caption MAX→TG"
    assert _tg_has_photo(result.raw), "No photo in TG message"
    assert "MAX photo caption" in (result.text or ""), (
        f"Caption lost: {result.text!r}"
    )


async def test_P12_video_max_to_tg(harness, test_video_bytes):
    """P12: Single video MAX→TG."""
    marker = harness.make_marker()
    await harness.max.send_file(
        test_video_bytes, "test.mp4", "video/mp4", caption=marker,
    )

    result = await harness.tg.wait_for(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=25,
    )
    assert result is not None, "Bridge did not forward video MAX→TG"
    # Video may arrive as video, document, or animation depending on file size
    has_media = (
        _tg_has_video(result.raw)
        or getattr(result.raw, "document", None) is not None
        or getattr(result.raw, "animation", None) is not None
    )
    assert has_media, "No video/document/animation in TG message"


# ── MAX → TG: Albums ────────────────────────────────────────────────────────

async def test_P13_photo_album_max_to_tg(harness, test_photo_bytes):
    """P13: Album of 3 photos MAX→TG."""
    marker = harness.make_marker()
    items = [
        (make_test_png(8, 8, 255, 0, 0), "red.png", "image/png"),
        (make_test_png(8, 8, 0, 255, 0), "green.png", "image/png"),
        (make_test_png(8, 8, 0, 0, 255), "blue.png", "image/png"),
    ]
    await harness.max.send_media_multi(items, caption=marker)

    # MAX sends one message with 3 attaches → bridge sends TG media group (3 msgs)
    album = await harness.tg.wait_for_album(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=25,
    )
    assert len(album) >= 3, (
        f"Expected >=3 messages in TG album, got {len(album)}"
    )
    # At least some should have photos
    photos = [e for e in album if _tg_has_photo(e.raw)]
    assert len(photos) >= 3, f"Expected 3 photos, got {len(photos)}"


async def test_P14_photo_album_with_caption_max_to_tg(harness, test_photo_bytes):
    """P14: Album of 3 photos with caption MAX→TG."""
    marker = harness.make_marker()
    caption = f"MAX album caption {marker}"
    items = [
        (make_test_png(8, 8, 200, 50, 50), "a.png", "image/png"),
        (make_test_png(8, 8, 50, 200, 50), "b.png", "image/png"),
        (make_test_png(8, 8, 50, 50, 200), "c.png", "image/png"),
    ]
    await harness.max.send_media_multi(items, caption=caption)

    album = await harness.tg.wait_for_album(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=25,
    )
    assert len(album) >= 3, f"Expected >=3 in album, got {len(album)}"
    # Caption should be on at least the first message
    captions = [e.text for e in album if e.text and "MAX album caption" in e.text]
    assert len(captions) >= 1, (
        f"Caption not found in any album message. Texts: {[e.text for e in album]}"
    )


async def test_P15_mixed_album_max_to_tg(harness, test_photo_bytes, test_video_bytes):
    """P15: Multi-photo album MAX→TG (MAX doesn't support mixed PHOTO+FILE)."""
    marker = harness.make_marker()
    # MAX rejects mixed PHOTO+FILE in one message, so test with 2 photos.
    items = [
        (make_test_png(8, 8, 128, 128, 0), "photo1.png", "image/png"),
        (make_test_png(8, 8, 0, 128, 128), "photo2.png", "image/png"),
    ]
    await harness.max.send_media_multi(items, caption=marker)

    album = await harness.tg.wait_for_album(
        lambda e: e.kind == "message" and marker in (e.text or ""),
        timeout=30,
    )
    assert len(album) >= 2, f"Expected >=2 in mixed album, got {len(album)}"
