#!/usr/bin/env python3
"""Bounds-checked decoders for BattleTanx: Global Assault (PS1, SLUS-01044) assets.

Every layout here was established from the retail executable's loaders; the
function addresses are cited beside each decoder and in
docs/PS1_ASSET_FORMATS.md.  All reads are explicit little-endian fixed-width
reads at checked, aligned offsets.  File offsets and tokens stay integers;
nothing is ever turned into a host pointer.

The decoders preserve what they do not understand: each record keeps its raw
byte range and SHA-256 so unknown fields, padding and unused palettes remain
recoverable from the manifest.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field


class FormatError(ValueError):
    """Raised when a file does not match a proven layout."""


# PS1 VRAM is 1024x512 16-bit units; anything larger cannot be uploaded.
VRAM_HALFWORDS_WIDE = 1024
VRAM_LINES = 512

MODE_4BPP = 0
MODE_8BPP = 1
MODE_16BPP = 2
MODE_NAMES = {MODE_4BPP: "4bpp-clut", MODE_8BPP: "8bpp-clut", MODE_16BPP: "16bpp-direct"}
PALETTE_ENTRIES = {MODE_4BPP: 16, MODE_8BPP: 256}

TEX_MAGIC = b"3TEX"
# func_8003CB80 reads a per-record palette count only when the file's first
# word is "3PAL"; every other magic means exactly one palette per record.
PAL_MAGIC = b"3PAL"
IMG_MAGIC = b"3IMG"
PLT_MAGIC = b"3PLT"
TTT_MAGIC = b"TT01"
TTT_ENTRY_TAG = b"LBTX"
MMM_MAGIC = b"MM01"
MODEL_MAGIC = b"CM01"
MODEL_BODY_MAGIC = b"3MDL"
MODEL_HEADER_SIZE = 0x24
MODEL_BODY_HEADER_SIZE = 0x18
MODEL_VERTEX_SIZE = 8
MODEL_STREAM_END = 0x68
FLB_SUPPORTED_DIMENSIONS = (0x40, 0x80, 0x100)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Reader:
    """Little-endian reads that refuse to leave the buffer or misalign."""

    def __init__(self, data: bytes, name: str = "buffer"):
        self.data = bytes(data)
        self.name = name

    def __len__(self) -> int:
        return len(self.data)

    def _check(self, offset: int, size: int, align: int) -> None:
        if offset < 0 or size < 0 or offset + size > len(self.data):
            raise FormatError(
                f"{self.name}: read of {size} bytes at {offset:#x} exceeds {len(self.data):#x}"
            )
        if offset % align:
            raise FormatError(f"{self.name}: {size}-byte read at {offset:#x} is misaligned")

    def u16(self, offset: int) -> int:
        self._check(offset, 2, 2)
        return struct.unpack_from("<H", self.data, offset)[0]

    def s16(self, offset: int) -> int:
        self._check(offset, 2, 2)
        return struct.unpack_from("<h", self.data, offset)[0]

    def u32(self, offset: int) -> int:
        self._check(offset, 4, 4)
        return struct.unpack_from("<I", self.data, offset)[0]

    def s32(self, offset: int) -> int:
        self._check(offset, 4, 4)
        return struct.unpack_from("<i", self.data, offset)[0]

    def bytes_at(self, offset: int, size: int) -> bytes:
        self._check(offset, size, 1)
        return self.data[offset : offset + size]


def raw_range(data: bytes, offset: int, size: int) -> dict:
    return {"offset": offset, "size": size, "sha256": sha256(data[offset : offset + size])}


# ---------------------------------------------------------------- colours ---


def _five_to_eight(value: int) -> int:
    return (value << 3) | (value >> 2)


def ps1_color_to_rgba(color: int) -> bytes:
    """Convert one PS1 15-bit colour to RGBA8888 for previews.

    Bits 0-4 are red, 5-9 green, 10-14 blue and bit 15 is the
    semi-transparency (STP) flag.  The GPU skips texels whose whole value is
    0x0000, so only that value becomes alpha 0; STP is preserved in the raw
    palette data, not folded into preview alpha.
    """

    if not 0 <= color <= 0xFFFF:
        raise FormatError(f"PS1 colour {color:#x} is not 16-bit")
    if color == 0:
        return bytes((0, 0, 0, 0))
    return bytes(
        (
            _five_to_eight(color & 0x1F),
            _five_to_eight((color >> 5) & 0x1F),
            _five_to_eight((color >> 10) & 0x1F),
            0xFF,
        )
    )


def read_palette(data: bytes, offset: int, entries: int) -> list[int]:
    reader = Reader(data, "palette")
    return [reader.u16(offset + index * 2) for index in range(entries)]


# --------------------------------------------------------------- textures ---


def loader_pixel_size(width: int, height: int, mode: int) -> int:
    """Pixel bytes as computed by func_8003CB80 / func_8003D5B8.

    4bpp uses a signed halving of the width (``(w / 2) * h``), so an odd
    4bpp width loses its last column of storage.
    """

    if mode == MODE_4BPP:
        return (width // 2) * height
    if mode == MODE_8BPP:
        return width * height
    if mode == MODE_16BPP:
        return width * 2 * height
    raise FormatError(f"unsupported pixel mode {mode}")


def _validate_dimensions(width: int, height: int, mode: int, where: str) -> None:
    if mode not in MODE_NAMES:
        raise FormatError(f"{where}: unsupported pixel mode {mode}")
    if width <= 0 or height <= 0:
        raise FormatError(f"{where}: dimensions {width}x{height} must be positive")
    halfwords = {MODE_4BPP: (width + 3) // 4, MODE_8BPP: (width + 1) // 2, MODE_16BPP: width}[mode]
    if halfwords > VRAM_HALFWORDS_WIDE or height > VRAM_LINES:
        raise FormatError(f"{where}: {width}x{height} {MODE_NAMES[mode]} does not fit PS1 VRAM")


@dataclass
class Texture:
    index: int
    width: int
    height: int
    mode: int
    header_offset: int
    pixel_offset: int
    pixel_size: int
    # The loaders advance by pixel_size rounded down to a word, so palettes
    # start pixel_advance bytes after the pixels even when that overlaps.
    pixel_advance: int
    palettes: list[list[int]] = field(default_factory=list)
    palette_offset: int | None = None
    authored_palette_offset: int | None = None
    authored_palettes: list[list[int]] = field(default_factory=list)

    @property
    def misaligned_palette(self) -> bool:
        return self.pixel_size != self.pixel_advance


def _read_palettes(reader: Reader, offset: int, mode: int, count: int, where: str) -> list[list[int]]:
    entries = PALETTE_ENTRIES[mode]
    size = count * entries * 2
    if offset + size > len(reader):
        raise FormatError(f"{where}: {count} palette(s) at {offset:#x} exceed the file")
    data = reader.bytes_at(offset, size)
    return [read_palette(data, index * entries * 2, entries) for index in range(count)]


def parse_tex(data: bytes, name: str = "TEX") -> dict:
    """Parse a TEX file exactly as func_8003CCE8/func_8003CEDC walk it.

    Layout: magic, u32 count, then per record u32 width, u32 height, u32 mode,
    pixel bytes (advanced by the word-truncated size), an optional u32
    palette count (only for the "3PAL" magic), and the palettes.
    """

    reader = Reader(data, name)
    if len(data) < 8:
        raise FormatError(f"{name}: shorter than the 8-byte header")
    magic = reader.bytes_at(0, 4)
    if magic not in (TEX_MAGIC, PAL_MAGIC):
        raise FormatError(f"{name}: magic {magic!r} is not 3TEX/3PAL")
    count = reader.u32(4)
    if count == 0 or count > (len(data) - 8) // 12:
        raise FormatError(f"{name}: record count {count} cannot fit in {len(data)} bytes")

    textures = []
    cursor = 8
    for index in range(count):
        where = f"{name}[{index}]"
        width, height, mode = reader.u32(cursor), reader.u32(cursor + 4), reader.u32(cursor + 8)
        _validate_dimensions(width, height, mode, where)
        header_offset = cursor
        cursor += 12
        size = loader_pixel_size(width, height, mode)
        if cursor + size > len(data):
            raise FormatError(f"{where}: {size} pixel bytes at {cursor:#x} exceed the file")
        texture = Texture(index, width, height, mode, header_offset, cursor, size, size & ~3)
        authored_cursor = cursor + size
        cursor += texture.pixel_advance
        if mode in PALETTE_ENTRIES:
            palette_count = 1
            if magic == PAL_MAGIC:
                palette_count = reader.u32(cursor)
                cursor += 4
                if palette_count == 0:
                    raise FormatError(f"{where}: palette count is zero")
            texture.palette_offset = cursor
            texture.palettes = _read_palettes(reader, cursor, mode, palette_count, where)
            cursor += palette_count * PALETTE_ENTRIES[mode] * 2
            if texture.misaligned_palette and magic == TEX_MAGIC:
                texture.authored_palette_offset = authored_cursor
                end = authored_cursor + PALETTE_ENTRIES[mode] * 2
                if end <= len(data):
                    texture.authored_palettes = _read_palettes(reader, authored_cursor, mode, 1, where)
        textures.append(texture)

    trailing = len(data) - cursor
    return {
        "magic": magic.decode("ascii"),
        "count": count,
        "textures": textures,
        "consumed": cursor,
        "trailing": raw_range(data, cursor, trailing) if trailing else None,
    }


def parse_img(data: bytes, name: str = "IMG") -> dict:
    """Parse IMG pixel records (func_8003D5B8 second half): w, h, mode, pixels."""

    reader = Reader(data, name)
    if len(data) < 8:
        raise FormatError(f"{name}: shorter than the 8-byte header")
    magic = reader.bytes_at(0, 4)
    if magic != IMG_MAGIC:
        raise FormatError(f"{name}: magic {magic!r} is not 3IMG")
    count = reader.u32(4)
    if count == 0 or count > (len(data) - 8) // 12:
        raise FormatError(f"{name}: record count {count} cannot fit in {len(data)} bytes")
    records = []
    cursor = 8
    for index in range(count):
        where = f"{name}[{index}]"
        width, height, mode = reader.u32(cursor), reader.u32(cursor + 4), reader.u32(cursor + 8)
        _validate_dimensions(width, height, mode, where)
        header_offset = cursor
        cursor += 12
        size = loader_pixel_size(width, height, mode)
        if cursor + size > len(data):
            raise FormatError(f"{where}: {size} pixel bytes at {cursor:#x} exceed the file")
        records.append(Texture(index, width, height, mode, header_offset, cursor, size, size & ~3))
        cursor += size & ~3
    trailing = len(data) - cursor
    return {
        "magic": magic.decode("ascii"),
        "count": count,
        "textures": records,
        "consumed": cursor,
        "trailing": raw_range(data, cursor, trailing) if trailing else None,
    }


def parse_plt(data: bytes, name: str = "PLT") -> dict:
    """Parse PLT palette records (func_8003D0CC): mode, [u32 count, palettes]."""

    reader = Reader(data, name)
    if len(data) < 8:
        raise FormatError(f"{name}: shorter than the 8-byte header")
    magic = reader.bytes_at(0, 4)
    if magic != PLT_MAGIC:
        raise FormatError(f"{name}: magic {magic!r} is not 3PLT")
    count = reader.u32(4)
    if count == 0 or count > (len(data) - 8) // 4:
        raise FormatError(f"{name}: record count {count} cannot fit in {len(data)} bytes")
    records = []
    cursor = 8
    for index in range(count):
        where = f"{name}[{index}]"
        mode = reader.u32(cursor)
        if mode not in MODE_NAMES:
            raise FormatError(f"{where}: unsupported palette mode {mode}")
        record = {"index": index, "mode": mode, "offset": cursor, "palette_offset": None, "palettes": []}
        cursor += 4
        if mode in PALETTE_ENTRIES:
            palette_count = reader.u32(cursor)
            cursor += 4
            if palette_count == 0:
                raise FormatError(f"{where}: palette count is zero")
            record["palette_offset"] = cursor
            record["palettes"] = _read_palettes(reader, cursor, mode, palette_count, where)
            cursor += palette_count * PALETTE_ENTRIES[mode] * 2
        records.append(record)
    trailing = len(data) - cursor
    return {
        "magic": magic.decode("ascii"),
        "count": count,
        "records": records,
        "consumed": cursor,
        "trailing": raw_range(data, cursor, trailing) if trailing else None,
    }


def pair_img_plt(img: dict, plt: dict, name: str = "IMG/PLT") -> list[Texture]:
    """Attach PLT palettes to IMG records; func_8003D5B8 pairs them by index."""

    if img["count"] != plt["count"]:
        raise FormatError(f"{name}: IMG has {img['count']} records, PLT has {plt['count']}")
    paired = []
    for texture, record in zip(img["textures"], plt["records"]):
        if texture.mode != record["mode"]:
            raise FormatError(
                f"{name}[{texture.index}]: IMG mode {texture.mode} != PLT mode {record['mode']}"
            )
        texture.palettes = record["palettes"]
        texture.palette_offset = record["palette_offset"]
        paired.append(texture)
    return paired


def decode_indices(data: bytes, texture: Texture) -> list[int]:
    """Return row-major texel values (CLUT indices or 16-bit colours).

    4bpp stores the left texel in the low nibble, as the PS1 GPU samples it.
    Rows are packed with no padding; an odd 4bpp width is decoded from the
    loader-visible storage only, so its last column reads as index 0.
    """

    reader = Reader(data, "pixels")
    width, height = texture.width, texture.height
    values = []
    if texture.mode == MODE_4BPP:
        row_bytes = width // 2
        for y in range(height):
            row = reader.bytes_at(texture.pixel_offset + y * row_bytes, row_bytes)
            for x in range(width):
                byte_index = x // 2
                if byte_index >= row_bytes:
                    values.append(0)
                    continue
                byte = row[byte_index]
                values.append(byte & 0x0F if x % 2 == 0 else byte >> 4)
    elif texture.mode == MODE_8BPP:
        values.extend(reader.bytes_at(texture.pixel_offset, width * height))
    else:
        if texture.pixel_offset % 2:
            raise FormatError("16bpp pixels are not halfword-aligned")
        values.extend(reader.u16(texture.pixel_offset + index * 2) for index in range(width * height))
    return values


def texture_rgba(data: bytes, texture: Texture, palette: list[int] | None = None) -> bytes:
    values = decode_indices(data, texture)
    if texture.mode == MODE_16BPP:
        return b"".join(ps1_color_to_rgba(value) for value in values)
    if palette is None:
        if not texture.palettes:
            raise FormatError(f"texture {texture.index} has no palette")
        palette = texture.palettes[0]
    lookup = [ps1_color_to_rgba(color) for color in palette]
    return b"".join(lookup[value] for value in values)


def max_index(data: bytes, texture: Texture) -> int | None:
    if texture.mode == MODE_16BPP:
        return None
    return max(decode_indices(data, texture), default=0)


# ------------------------------------------------------- texture lists -----


def parse_ttt(data: bytes, name: str = "TTT") -> dict:
    """Parse per-model texture lists (func_8003D5B8 tail, 0x8003D9F8).

    Layout: "TT01", u32 count, then per model "LBTX", s32 n, n u32 indices
    into the paired IMG/PLT record array.  The loader skips the list when n
    is not positive.
    """

    reader = Reader(data, name)
    if len(data) < 8:
        raise FormatError(f"{name}: shorter than the 8-byte header")
    magic = reader.bytes_at(0, 4)
    if magic != TTT_MAGIC:
        raise FormatError(f"{name}: magic {magic!r} is not TT01")
    count = reader.s32(4)
    if count < 0 or count > (len(data) - 8) // 8:
        raise FormatError(f"{name}: list count {count} cannot fit in {len(data)} bytes")
    cursor = 8
    lists = []
    for index in range(count):
        where = f"{name}[{index}]"
        tag = reader.bytes_at(cursor, 4) if cursor + 4 <= len(data) else b""
        if tag != TTT_ENTRY_TAG:
            raise FormatError(f"{where}: tag {tag!r} at {cursor:#x} is not LBTX")
        entries = reader.s32(cursor + 4)
        if entries < 0:
            raise FormatError(f"{where}: negative texture count {entries}")
        start = cursor
        cursor += 8
        indices = [reader.u32(cursor + slot * 4) for slot in range(entries)]
        cursor += entries * 4
        lists.append({"index": index, "offset": start, "textures": indices})
    trailing = len(data) - cursor
    return {
        "magic": magic.decode("ascii"),
        "count": count,
        "lists": lists,
        "consumed": cursor,
        "trailing": raw_range(data, cursor, trailing) if trailing else None,
    }


# ------------------------------------------------------------------ models ---


def tokenize_model_stream(data: bytes, offset: int, end: int, where: str) -> dict:
    """Split a model's command stream into 32-bit tokens.

    Each token's low byte is the opcode (low two bits are flags).  The
    stream's semantics are not decoded; only its framing is checked.
    """

    reader = Reader(data, where)
    if (end - offset) % 4:
        raise FormatError(f"{where}: command stream is not a whole number of words")
    words = [reader.u32(cursor) for cursor in range(offset, end, 4)]
    if not words or words[-1] != MODEL_STREAM_END:
        raise FormatError(f"{where}: command stream does not end with token {MODEL_STREAM_END:#x}")
    histogram: dict[int, int] = {}
    for word in words:
        histogram[word & 0xFF] = histogram.get(word & 0xFF, 0) + 1
    return {"words": len(words), "opcodes": dict(sorted(histogram.items()))}


def parse_model(data: bytes, offset: int, size: int, where: str) -> dict:
    """Validate one CM01 model record and its 3MDL body framing."""

    reader = Reader(data, where)
    if size < MODEL_HEADER_SIZE + MODEL_BODY_HEADER_SIZE:
        raise FormatError(f"{where}: {size} bytes is smaller than the model headers")
    if reader.bytes_at(offset, 4) != MODEL_MAGIC:
        raise FormatError(f"{where}: missing CM01 header")
    body = offset + MODEL_HEADER_SIZE
    if reader.bytes_at(body, 4) != MODEL_BODY_MAGIC:
        raise FormatError(f"{where}: missing 3MDL body")
    parts = reader.u32(body + 4)
    vertices = reader.u32(body + 8)
    field_0c = reader.u32(body + 0xC)
    field_10 = reader.u32(body + 0x10)
    body_size = reader.u32(body + 0x14)
    if body_size != size - MODEL_HEADER_SIZE:
        raise FormatError(f"{where}: body size {body_size} != record remainder {size - MODEL_HEADER_SIZE}")
    vertex_offset = body + MODEL_BODY_HEADER_SIZE
    stream_offset = vertex_offset + vertices * MODEL_VERTEX_SIZE
    if stream_offset > offset + size:
        raise FormatError(f"{where}: {vertices} vertices overrun the model")
    stream = tokenize_model_stream(data, stream_offset, offset + size, where)
    return {
        "offset": offset,
        "size": size,
        "sha256": sha256(data[offset : offset + size]),
        # 0x80069DE8 culls with this signed radius (shifted to 20.12).
        "radius": reader.s16(offset + 0x22),
        "header_raw": raw_range(data, offset, MODEL_HEADER_SIZE),
        "parts": parts,
        "vertices": vertices,
        "field_0c": field_0c,
        "field_10": field_10,
        "vertex_block": raw_range(data, vertex_offset, vertices * MODEL_VERTEX_SIZE),
        "stream": {"offset": stream_offset, **stream},
    }


def parse_mmm(data: bytes, name: str = "MMM") -> dict:
    """Parse a model bank as func_8003BF88 relocates it.

    Layout: "MM01", u32 count, count x {s32 offset, u32, u32}; offset -1 is
    an empty slot, otherwise it is relative to the end of the table.
    """

    reader = Reader(data, name)
    if len(data) < 8:
        raise FormatError(f"{name}: shorter than the 8-byte header")
    magic = reader.bytes_at(0, 4)
    if magic != MMM_MAGIC:
        raise FormatError(f"{name}: magic {magic!r} is not MM01")
    count = reader.u32(4)
    if count > (len(data) - 8) // 12:
        raise FormatError(f"{name}: model count {count} cannot fit in {len(data)} bytes")
    base = 8 + count * 12
    slots = []
    for index in range(count):
        entry = 8 + index * 12
        slots.append((index, reader.s32(entry), reader.u32(entry + 4), reader.u32(entry + 8)))
    for index, relative, aux1, aux2 in slots:
        if relative != -1 and (relative < 0 or base + relative >= len(data)):
            raise FormatError(f"{name}[{index}]: model offset {relative:#x} is outside the file")
        if relative != -1 and relative % 4:
            raise FormatError(f"{name}[{index}]: model offset {relative:#x} is misaligned")
    starts = sorted({relative for _, relative, _, _ in slots if relative != -1})
    if starts and starts[0] != 0:
        raise FormatError(f"{name}: first model starts at {starts[0]:#x}, leaving an unowned gap")
    ends = starts[1:] + [len(data) - base]
    extents = dict(zip(starts, ends))
    models = []
    for index, relative, aux1, aux2 in slots:
        if relative == -1:
            models.append({"index": index, "empty": True, "aux": [aux1, aux2]})
            continue
        model = parse_model(data, base + relative, extents[relative] - relative, f"{name}[{index}]")
        models.append({"index": index, "empty": False, "aux": [aux1, aux2], **model})
    return {"magic": magic.decode("ascii"), "count": count, "table_end": base, "models": models}


# ------------------------------------------------------------- floor map ---


def flb_cell_color(cell: int) -> tuple[int, int, int]:
    """Vertex colour 0x8003B100 builds from an FLB cell's low seven bits."""

    word = ((cell & 0x7C) << 19) | ((cell & 0x3E) << 11) | ((cell & 0x1F) << 3)
    return word & 0xFF, (word >> 8) & 0xFF, (word >> 16) & 0xFF


def parse_flb(data: bytes, name: str = "FLB") -> dict:
    """Parse a floor grid (func_8003AF64): u16 dimension, u16, then u32 cells.

    The loader addresses only dimension^2 cells, indexed by
    (world >> 7) + dimension / 2 on each axis; any further words are kept
    as an unreachable tail.
    """

    reader = Reader(data, name)
    if len(data) < 4:
        raise FormatError(f"{name}: shorter than the 4-byte header")
    dimension = reader.u16(0)
    second = reader.u16(2)
    if dimension not in FLB_SUPPORTED_DIMENSIONS:
        raise FormatError(f"{name}: grid dimension {dimension} is not one the loader handles")
    expected = 4 + dimension * dimension * 4
    if len(data) < expected:
        raise FormatError(f"{name}: {len(data)} bytes, need {expected} for a {dimension}^2 grid")
    if (len(data) - 4) % 4:
        raise FormatError(f"{name}: cell area is not a whole number of words")
    cells = [reader.u32(4 + index * 4) for index in range(dimension * dimension)]
    low = {}
    for cell in cells:
        low[cell & 0x7F] = low.get(cell & 0x7F, 0) + 1
    tail = len(data) - expected
    return {
        "dimension": dimension,
        "header_word_2": second,
        "cells": cells,
        "unreachable_tail": raw_range(data, expected, tail) if tail else None,
        "shade_histogram": dict(sorted(low.items())),
        "distinct_high_bits": len({cell >> 7 for cell in cells}),
    }


# --------------------------------------------------------------- level BIN --


def parse_level_bin(data: bytes, name: str = "BIN") -> dict:
    """Parse the level BIN header (func_8002429C).

    Words 0-4 are byte offsets from the start of the file; the range between
    offsets 2 and 3 holds 12-byte records, and word 6 is a further count.
    The loader reads a u16 count at section 1 and s16 values at section
    1 + 0x14, 0x18, 0x1A and 0x1E (each scaled by 64/72).  Those reads are
    not confined to section 1: when section 1 is short they land in
    section 2, so the values are reported with the section they came from.
    """

    reader = Reader(data, name)
    if len(data) < 0x20:
        raise FormatError(f"{name}: shorter than the 0x20-byte header")
    offsets = [reader.u32(index * 4) for index in range(5)]
    words = [reader.u32(index * 4) for index in range(8)]
    for index, value in enumerate(offsets):
        if not 0x20 <= value <= len(data):
            raise FormatError(f"{name}: section {index} offset {value:#x} is outside the file")
    if offsets != sorted(offsets):
        raise FormatError(f"{name}: section offsets are not ascending")
    span = offsets[3] - offsets[2]
    if span % 12:
        raise FormatError(f"{name}: section 2 spans {span} bytes, not whole 12-byte records")
    section1 = offsets[1]
    reads = []
    for delta in (0x14, 0x18, 0x1A, 0x1E):
        position = section1 + delta
        value = reader.s16(position)
        owner = max(index for index in range(5) if offsets[index] <= position)
        reads.append({"delta": delta, "value": value, "section": owner})
    sections = []
    bounds = offsets + [len(data)]
    for index in range(5):
        sections.append(raw_range(data, bounds[index], bounds[index + 1] - bounds[index]))
    # 0x8002457C walks section 2 records [first, first + count) taken from
    # the u16 pair at the start of section 1.  Each record is s16 x, y, z
    # (shifted left 14 into the world's 20.12 space, i.e. x4 world units),
    # a u16 passed through to func_80025094, and a u32 byte offset added to
    # the section 3 base.
    count, first = reader.u16(section1), reader.u16(section1 + 2)
    records = span // 12
    if first + count > records:
        raise FormatError(f"{name}: section 1 selects records {first}..{first + count} of {records}")
    section3_size = offsets[4] - offsets[3]
    placements = []
    for index in range(records):
        record = offsets[2] + index * 12
        target = reader.u32(record + 8)
        if target >= section3_size:
            raise FormatError(f"{name}: record {index} points {target:#x} past section 3")
        placements.append(
            {
                "position": [reader.s16(record), reader.s16(record + 2), reader.s16(record + 4)],
                "word_6": reader.u16(record + 6),
                "section3_offset": target,
            }
        )
    return {
        "offsets": offsets,
        "header_words": words,
        "section1_count": count,
        "section1_first": first,
        "section1_reads": reads,
        "placements": placements,
        "section2_records": records,
        "word6_count": words[6],
        "sections": sections,
    }


# -------------------------------------------------------------- executable --

PSX_EXE_MAGIC = b"PS-X EXE"
PSX_EXE_HEADER_SIZE = 0x800
# SLUS-01044: the level loaders index this pointer table by level number
# (0x8003BDEC, 0x8003BF04, 0x8003D6B0, 0x8003DE80).
LEVEL_NAME_TABLE = 0x800A48E0


def parse_psx_exe(data: bytes) -> dict:
    reader = Reader(data, "PS-X EXE")
    if len(data) < PSX_EXE_HEADER_SIZE or reader.bytes_at(0, 8) != PSX_EXE_MAGIC:
        raise FormatError("not a PS-X EXE")
    text_address = reader.u32(0x18)
    text_size = reader.u32(0x1C)
    if PSX_EXE_HEADER_SIZE + text_size > len(data):
        raise FormatError("PS-X EXE text extends past the file")
    return {"entry": reader.u32(0x10), "text_address": text_address, "text_size": text_size}


def exe_offset(header: dict, address: int, size: int = 1) -> int:
    start = header["text_address"]
    if not (start <= address and address + size <= start + header["text_size"]):
        raise FormatError(f"address {address:#x} is outside the executable image")
    return PSX_EXE_HEADER_SIZE + address - start


def read_level_names(data: bytes, table: int = LEVEL_NAME_TABLE, limit: int = 256) -> list[str]:
    """Return the level names the retail loaders build asset paths from."""

    header = parse_psx_exe(data)
    reader = Reader(data, "PS-X EXE")
    names = []
    start, end = header["text_address"], header["text_address"] + header["text_size"]
    for index in range(limit):
        pointer = reader.u32(exe_offset(header, table + index * 4, 4))
        if not start <= pointer < end:
            break
        offset = exe_offset(header, pointer)
        terminator = data.find(b"\0", offset, min(len(data), offset + 64))
        if terminator < 0 or terminator == offset:
            break
        raw = data[offset:terminator]
        if not all(0x20 <= byte < 0x7F for byte in raw):
            break
        names.append(raw.decode("ascii"))
    return names


# ------------------------------------------------------------------- PNG ----


def encode_png_rgba(width: int, height: int, pixels: bytes) -> bytes:
    """Deterministic RGBA8888 PNG (no timestamps, fixed zlib level)."""

    import zlib

    if len(pixels) != width * height * 4:
        raise FormatError("RGBA pixel count does not match the image dimensions")

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    stride = width * 4
    scanlines = b"".join(b"\0" + pixels[row * stride : (row + 1) * stride] for row in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines, 9))
        + chunk(b"IEND", b"")
    )
