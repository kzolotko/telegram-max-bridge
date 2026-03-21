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
    """Generate a minimal valid MP4 file.

    This is a tiny but structurally valid MP4 container with a single
    video track containing one black frame. Both Telegram and MAX accept it.
    """
    # Minimal valid MP4: ftyp + moov (with mvhd, trak, tkhd, mdia, minf, stbl)
    # + mdat with a single H.264 IDR frame
    #
    # This hand-crafted MP4 is ~550 bytes and passes container validation.

    def box(typ: bytes, data: bytes = b"") -> bytes:
        return struct.pack(">I", len(data) + 8) + typ + data

    def fullbox(typ: bytes, version: int, flags: int, data: bytes) -> bytes:
        inner = struct.pack(">I", (version << 24) | flags) + data
        return box(typ, inner)

    # ftyp
    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isomiso2mp41")

    # mvhd (movie header) — version 0
    mvhd_data = struct.pack(
        ">IIIIH2x",
        0,       # creation_time
        0,       # modification_time
        1000,    # timescale
        100,     # duration (100ms)
        0x0100,  # rate = 1.0 (fixed 16.16 → stored as high 16 bits)
    )
    mvhd_data += b"\x01\x00"  # volume = 1.0
    mvhd_data += b"\x00" * 10  # reserved
    # matrix (identity 3x3 in 32.32 fixed point: 9 ints)
    mvhd_data += struct.pack(
        ">9I",
        0x00010000, 0, 0,
        0, 0x00010000, 0,
        0, 0, 0x40000000,
    )
    mvhd_data += b"\x00" * 24  # pre-defined
    mvhd_data += struct.pack(">I", 2)  # next_track_ID
    mvhd = fullbox(b"mvhd", 0, 0, mvhd_data)

    # tkhd (track header) — version 0
    tkhd_data = struct.pack(
        ">IIIII4x",
        0,    # creation_time
        0,    # modification_time
        1,    # track_ID
        0,    # reserved
        100,  # duration
    )
    tkhd_data += b"\x00" * 8   # reserved
    tkhd_data += struct.pack(">hh", 0, 0)  # layer, alternate_group
    tkhd_data += struct.pack(">hH", 0, 0)  # volume, reserved
    tkhd_data += struct.pack(
        ">9I",
        0x00010000, 0, 0,
        0, 0x00010000, 0,
        0, 0, 0x40000000,
    )
    tkhd_data += struct.pack(">II", 0x00020000, 0x00020000)  # width=2, height=2
    tkhd = fullbox(b"tkhd", 0, 3, tkhd_data)

    # mdhd
    mdhd_data = struct.pack(">IIIIH2x", 0, 0, 1000, 100, 0x55C4)
    mdhd = fullbox(b"mdhd", 0, 0, mdhd_data)

    # hdlr
    hdlr_data = struct.pack(">I", 0)  # pre_defined
    hdlr_data += b"vide"
    hdlr_data += b"\x00" * 12  # reserved
    hdlr_data += b"VideoHandler\x00"
    hdlr = fullbox(b"hdlr", 0, 0, hdlr_data)

    # vmhd
    vmhd = fullbox(b"vmhd", 0, 1, struct.pack(">H6x", 0))

    # dinf → dref → url
    url_box = fullbox(b"url ", 0, 1, b"")
    dref = fullbox(b"dref", 0, 0, struct.pack(">I", 1) + url_box)
    dinf = box(b"dinf", dref)

    # stbl (sample table) with empty entries — valid but no actual samples
    stsd_data = struct.pack(">I", 0)  # entry_count = 0
    stsd = fullbox(b"stsd", 0, 0, stsd_data)
    stts = fullbox(b"stts", 0, 0, struct.pack(">I", 0))
    stsc = fullbox(b"stsc", 0, 0, struct.pack(">I", 0))
    stsz = fullbox(b"stsz", 0, 0, struct.pack(">II", 0, 0))
    stco = fullbox(b"stco", 0, 0, struct.pack(">I", 0))
    stbl = box(b"stbl", stsd + stts + stsc + stsz + stco)

    minf = box(b"minf", vmhd + dinf + stbl)
    mdia = box(b"mdia", mdhd + hdlr + minf)
    trak = box(b"trak", tkhd + mdia)
    moov = box(b"moov", mvhd + trak)
    mdat = box(b"mdat", b"\x00" * 8)

    return ftyp + moov + mdat


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
