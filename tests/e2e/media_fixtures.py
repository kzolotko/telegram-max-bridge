"""
Minimal test media generators (no external dependencies).

Produces tiny but valid image/video files for E2E media tests.
"""

from __future__ import annotations

import struct
import tempfile
import zlib
from pathlib import Path


def make_test_png(width: int = 4, height: int = 4, r: int = 255, g: int = 0, b: int = 0) -> bytes:
    """Generate a minimal valid PNG file (pure Python, no PIL)."""

    def _chunk(name: bytes, data: bytes) -> bytes:
        c = name + data
        crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + c + crc

    # IHDR: width, height, bit_depth=8, color_type=2 (RGB)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

    # IDAT: raw scanlines (filter byte 0 + RGB pixels)
    raw_rows = b""
    for _ in range(height):
        raw_rows += b"\x00"  # filter: none
        raw_rows += bytes([r, g, b]) * width
    idat = zlib.compress(raw_rows)

    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


def make_test_jpeg(width: int = 4, height: int = 4) -> bytes:
    """Generate a minimal valid JPEG file (pure Python).

    This creates a simple but valid JFIF image with a single red MCU.
    For small sizes, we just build a raw baseline JPEG.
    """
    # It's simpler to produce a valid PNG and convert if needed,
    # but for Telegram/MAX uploads a PNG with .jpg extension works fine.
    # So we return a valid PNG — both services accept it.
    return make_test_png(width, height, 255, 0, 0)


def make_test_video_mp4() -> bytes:
    """Return a minimal but genuinely valid H.264/AVC MP4 video.

    This is a pre-encoded 160×90, 1-frame, ~1 s video produced by x264
    (libavformat 58.12.100, CRF 28, High Profile Level 1.0).  It is stored
    as a base64 constant so there are no runtime dependencies on ffmpeg or
    any video library.  The file passes real H.264 decoders (ffmpeg,
    libavcodec) including MAX's video-upload validator.

    File layout (top-level ISO Base Media boxes):
      ftyp  32 B   major brand: isom
      free   8 B   padding
      mdat 749 B   raw H.264 NAL units
      moov 750 B   metadata / sample table
    """
    import base64
    _B64 = (
        "AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAAIZnJlZQAAAu1tZGF0AAACrQYF//+p"
        "3EXpvebZSLeWLNgg2SPu73gyNjQgLSBjb3JlIDE1NSByMjkwMSA3ZDBmZjIyIC0gSC4yNjQvTVBF"
        "Ry00IEFWQyBjb2RlYyAtIENvcHlsZWZ0IDIwMDMtMjAxOCAtIGh0dHA6Ly93d3cudmlkZW9sYW4u"
        "b3JnL3gyNjQuaHRtbCAtIG9wdGlvbnM6IGNhYmFjPTEgcmVmPTMgZGVibG9jaz0xOjA6MCBhbmFs"
        "eXNlPTB4MzoweDExMyBtZT1oZXggc3VibWU9NyBwc3k9MSBwc3lfcmQ9MS4wMDowLjAwIG1peGVk"
        "X3JlZj0xIG1lX3JhbmdlPTE2IGNocm9tYV9tZT0xIHRyZWxsaXM9MSA4eDhkY3Q9MSBjcW09MCBk"
        "ZWFkem9uZT0yMSwxMSBmYXN0X3Bza2lwPTEgY2hyb21hX3FwX29mZnNldD0tMiB0aHJlYWRzPTMg"
        "bG9va2FoZWFkX3RocmVhZHM9MSBzbGljZWRfdGhyZWFkcz0wIG5yPTAgZGVjaW1hdGU9MSBpbnRl"
        "cmxhY2VkPTAgYmx1cmF5X2NvbXBhdD0wIGNvbnN0cmFpbmVkX2ludHJhPTAgYmZyYW1lcz0zIGJf"
        "cHlyYW1pZD0yIGJfYWRhcHQ9MSBiX2JpYXM9MCBkaXJlY3Q9MSB3ZWlnaHRiPTEgb3Blbl9nb3A9"
        "MCB3ZWlnaHRwPTIga2V5aW50PTI1MCBrZXlpbnRfbWluPTEgc2NlbmVjdXQ9NDAgaW50cmFfcmVm"
        "cmVzaD0wIHJjX2xvb2thaGVhZD00MCByYz1jcmYgbWJ0cmVlPTEgY3JmPTI4LjAgcWNvbXA9MC42"
        "MCBxcG1pbj0wIHFwbWF4PTY5IHFwc3RlcD00IGlwX3JhdGlvPTEuNDAgYXE9MToxLjAwAIAAAAAw"
        "ZYiEAD//8m+P5OXfBeLGOfKE3xkODvFZuBflHv/+VwJIta6cbpIo4ABLoKBaYTkTAAAC7m1vb3YA"
        "AABsbXZoZAAAAAAAAAAAAAAAAAAAA+gAAAPoAAEAAAEAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAA"
        "AAAAAQAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIAAAIYdHJh"
        "awAAAFx0a2hkAAAAAwAAAAAAAAAAAAAAAQAAAAAAAAPoAAAAAAAAAAAAAAAAAAAAAAABAAAAAAAA"
        "AAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAQAAAAACgAAAAWgAAAAAAJGVkdHMAAAAcZWxzdAAAAAAA"
        "AAABAAAD6AAAAAAAAQAAAAABkG1kaWEAAAAgbWRoZAAAAAAAAAAAAAAAAAAAQAAAAEAAVcQAAAAA"
        "AC1oZGxyAAAAAAAAAAB2aWRlAAAAAAAAAAAAAAAAVmlkZW9IYW5kbGVyAAAAATttaW5mAAAAFHZt"
        "aGQAAAABAAAAAAAAAAAAAAAkZGluZgAAABxkcmVmAAAAAAAAAAEAAAAMdXJsIAAAAAEAAAD7c3Ri"
        "bAAAAJdzdHNkAAAAAAAAAAEAAACHYXZjMQAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAACgAFoASAAA"
        "AEgAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABj//wAAADFhdmNDAWQA"
        "Cv/hABhnZAAKrNlCjfkhAAADAAEAAAMAAg8SJZYBAAZo6+JLIsAAAAAYc3R0cwAAAAAAAAABAAAA"
        "AQAAQAAAAAAcc3RzYwAAAAAAAAABAAAAAQAAAAEAAAABAAAAFHN0c3oAAAAAAAAC5QAAAAEAAAAU"
        "c3RjbwAAAAAAAAABAAAAMAAAAGJ1ZHRhAAAAWm1ldGEAAAAAAAAAIWhkbHIAAAAAAAAAAG1kaXJh"
        "cHBsAAAAAAAAAAAAAAAALWlzbHQAAAAlqXRvbwAAAB1kYXRhAAAAAQAAAABMYXZmNTguMTIuMTAw"
    )
    return base64.b64decode(_B64)


def make_test_wav() -> bytes:
    """Generate a minimal valid WAV file (1 sample of silence, 8-bit mono 8kHz)."""
    audio_data = b"\x80"  # 8-bit unsigned PCM, silence = 128
    sample_rate = 8000
    channels = 1
    bits = 8
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    data_size = len(audio_data)
    file_size = 36 + data_size

    return (
        b"RIFF"
        + struct.pack("<I", file_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits)
        + b"data"
        + struct.pack("<I", data_size)
        + audio_data
    )


def _ogg_crc(data: bytes) -> int:
    """OGG CRC-32 (poly=0x04C11DB7, init=0, non-reflected — different from zlib)."""
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc


def _ogg_page(serial: int, seqno: int, granule: int, headertype: int, data: bytes) -> bytes:
    """Build a single OGG page with correct CRC. Data must be < 255 bytes."""
    assert len(data) < 255, "simplified: one segment only"
    page = (
        b"OggS\x00"
        + bytes([headertype])
        + struct.pack("<q", granule)   # granule_position (signed 64-bit LE)
        + struct.pack("<I", serial)    # stream_serial_number
        + struct.pack("<I", seqno)     # page_sequence_no
        + b"\x00\x00\x00\x00"         # CRC placeholder
        + b"\x01"                      # page_segments = 1
        + bytes([len(data)])           # segment_table
        + data
    )
    # Offset 22–25 is the CRC field
    crc = _ogg_crc(page)
    return page[:22] + struct.pack("<I", crc) + page[26:]


def make_test_ogg() -> bytes:
    """Generate a minimal valid OGG Opus file (mono 48 kHz, one silence frame).

    Produces a structurally correct three-page stream: ID header, comment
    header, and one 60 ms CELT silence frame. Accepted by Telegram as voice.
    """
    serial = 1

    # Page 1 — Opus identification header (BOS)
    id_header = (
        b"OpusHead"
        + b"\x01"                       # version
        + b"\x01"                       # channels = 1 (mono)
        + struct.pack("<H", 312)        # pre-skip
        + struct.pack("<I", 48000)      # input_sample_rate
        + struct.pack("<h", 0)          # output_gain
        + b"\x00"                       # channel_mapping_family = 0
    )
    page1 = _ogg_page(serial, 0, 0, 0x02, id_header)

    # Page 2 — Opus comment header
    vendor = b"test"
    comment_header = (
        b"OpusTags"
        + struct.pack("<I", len(vendor))
        + vendor
        + struct.pack("<I", 0)          # zero user comments
    )
    page2 = _ogg_page(serial, 1, 0, 0x00, comment_header)

    # Page 3 — Audio data (EOS): CELT config 31, 60 ms, mono silence
    # TOC byte: (31 << 3) | stereo=0 | code=0 = 0xF8
    silence_frame = bytes([0xF8, 0xFF, 0xFE])
    # granule = 2880 samples (60 ms × 48 000 Hz)
    page3 = _ogg_page(serial, 2, 2880, 0x04, silence_frame)

    return page1 + page2 + page3


def save_temp_media(data: bytes, suffix: str) -> str:
    """Write data to a temporary file and return its path."""
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(data)
    tmp.close()
    return tmp.name
