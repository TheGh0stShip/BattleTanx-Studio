"""Decoder for PS1 (SLUS-01044) model records and their command streams.

Everything here mirrors code in the retail executable; addresses are RAM
addresses (file offset = address - 0x80010000 + 0x800).

Model record (one MMM slot, located by ps1_formats.parse_mmm):

    +0x00 "CM01"
    +0x04 s16 x_a, +0x08 s16 z_a, +0x0C s16 x_b, +0x10 s16 z_b
          footprint read by 0x80025170 and rotated by 0x800249A0
    +0x22 s16 radius               culled at 0x80069DE8 as radius << 12
    +0x24 "3MDL" body:
          +0x04 u32 part_count     per-part matrix count (0x80069D04,
                                   0x80092510)
          +0x08 u32 vertex_count   transformed by 0x8007E570
          +0x0C u32, +0x10 u32     not read by any traced path
          +0x14 u32 body_size
          +0x18 vertex[vertex_count], 8 bytes: s16 x, s16 y, s16 z, u16 pad
                (0x8007E5E8: VXY = word 0, VZ = low half of word 1);
                pad bit 15 ends a part for 0x80092510
          command stream of u32 words up to the end of the record

The stream interpreter (0x8007C0D4, and an identical copy at 0x8007D580)
reads a word, dispatches through 2-instruction stubs at
0x8007C960 + (word & 0xFC) * 2 with operand = word >> 8, runs `nclip`, and
then uses word & 3 as the emit flag.  Opcodes above 0xA0 would dispatch
into unrelated code; the decoder rejects them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ps1_formats import FormatError, Reader, raw_range, sha256

MODEL_HEADER_SIZE = 0x24
BODY_HEADER_SIZE = 0x18
VERTEX_SIZE = 8
PART_END_FLAG = 0x8000
MAX_OPCODE = 0xA0

# Interpreter scratch layout for corner k: colour at 0x44 + 4k,
# normal at 0x54 + 4k / 0x64 + 2k, UV at 0x74 + 4k, vertex in GTE SXYk.
CORNERS = 4

# Pair opcodes 0x40-0x54: (corner taking operand bits 0-11,
# corner taking operand bits 12-23), from the handlers at 0x8007D268-0x8007D3D0.
PAIR_CORNERS = {
    0x40: (1, 0),
    0x44: (2, 1),
    0x48: (3, 2),
    0x4C: (2, 0),
    0x50: (3, 0),
    0x54: (3, 1),
}

# Visibility blocks: header words after the opcode, number of mask words.
BLOCK_HEADERS = {0x64: (1, 1), 0x6C: (5, 1), 0x70: (6, 2)}

CULL_NAMES = {1: "front_screen_cw", 2: "front_screen_ccw", 3: "double_sided"}

# Proven opcode table: (name, handler address).
OPCODES: dict[int, tuple[str, int]] = {}
for _slot in range(4):
    OPCODES[0x00 + 4 * _slot] = ("corner_colour", 0x8007D098 + 0xC * _slot)
    OPCODES[0x10 + 4 * _slot] = ("corner_normal", 0x8007D0C8 + 0x34 * _slot)
    OPCODES[0x90 + 4 * _slot] = ("corner_normal", 0x8007D0C8 + 0x34 * _slot)
    OPCODES[0x20 + 4 * _slot] = ("corner_uv", 0x8007D1A0 + 0x10 * _slot)
    OPCODES[0x80 + 4 * _slot] = ("corner_uv_with_side_value", 0x8007D198 + 0x10 * _slot)
    OPCODES[0x30 + 4 * _slot] = ("corner_vertex", 0x8007D1D8 + 0x24 * _slot)
for _opcode, _address in zip(sorted(PAIR_CORNERS), range(0x8007D268, 0x8007D418, 0x48)):
    OPCODES[_opcode] = ("corner_vertex_pair", _address)
OPCODES.update(
    {
        0x58: ("texture_select", 0x8007D4BC),
        0x5C: ("render_mode", 0x8007D488),
        0x60: ("no_operation", 0x8007D4B4),
        0x64: ("visibility_block", 0x8007D418),
        0x68: ("end", 0x8007D510),
        0x6C: ("visibility_block", 0x8007D438),
        0x70: ("visibility_block_pair", 0x8007D458),
        0x74: ("no_operation", 0x8007D578),
        0x78: ("no_operation", 0x8007D578),
        0x7C: ("no_operation", 0x8007D578),
        0xA0: ("depth_mode", 0x8007D560),
    }
)
del _slot, _opcode, _address

# Commands whose handler returns to the loop without the nclip/emit step.
NO_EMIT = {0x64, 0x68, 0x6C, 0x70}


def sign_extend(value: int, bits: int) -> int:
    value &= (1 << bits) - 1
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def decode_normal(word: int) -> tuple[int, int, int]:
    """Corner normal exactly as 0x8007D0C8 builds GTE VXY0/VZ0.

    VX is the word shifted right arithmetically by 19 (13 significant
    bits), VY is the signed byte at bits 16-23 times 32, and VZ is the
    signed byte at bits 8-15 times 32.  1.0 is 4096.
    """

    signed = sign_extend(word, 32)
    return sign_extend(signed >> 19, 16), sign_extend(word >> 16, 8) * 32, sign_extend(word >> 8, 8) * 32


@dataclass
class Command:
    offset: int
    word: int
    opcode: int
    flags: int
    name: str
    handler: int
    operands: dict = field(default_factory=dict)
    extra_words: list[int] = field(default_factory=list)
    block_path: tuple[int, ...] = ()

    @property
    def size(self) -> int:
        return 4 + 4 * len(self.extra_words)


@dataclass
class Corner:
    vertex: int | None = None
    colour: int | None = None
    normal: tuple[int, int, int] | None = None
    uv: tuple[int, int] | None = None

    def copy(self) -> "Corner":
        return Corner(self.vertex, self.colour, self.normal, self.uv)


@dataclass
class Primitive:
    index: int
    command: int
    mode: int | None
    texture_slot: int | None
    cull: str
    corners: list[Corner]
    block_path: tuple[int, ...]
    missing: list[str]

    @property
    def quad(self) -> bool:
        return bool(self.mode is not None and self.mode & 1)

    @property
    def gouraud(self) -> bool:
        return bool(self.mode is not None and self.mode & 4)

    @property
    def textured(self) -> bool:
        return bool(self.mode is not None and self.mode & 8)

    @property
    def gpu_code(self) -> int | None:
        """GPU polygon command the emitter at 0x8007CAA8[mode] writes."""

        if self.mode is None:
            return None
        return 0x20 | (0x08 if self.quad else 0) | (0x10 if self.gouraud else 0) | (0x04 if self.textured else 0)

    @property
    def used_corners(self) -> list[int]:
        return [0, 1, 2, 3] if self.quad else [0, 1, 2]

    def triangles(self) -> list[tuple[int, int, int]]:
        """Corner triples in the order the GPU rasterises them.

        Quads are sent as (c0, c1, c3, c2) (0x8007CC64), which the GPU
        splits into (v0, v1, v2) and (v1, v2, v3): the shared diagonal is
        c1-c3.  Triangles are returned in perimeter order c0 c1 c2 c3 so
        both share the orientation that `nclip` (corners 0, 1, 2) tests.
        """

        if self.quad:
            return [(0, 1, 3), (1, 2, 3)]
        return [(0, 1, 2)]


@dataclass
class Model:
    offset: int
    size: int
    sha256: str
    footprint: tuple[int, int, int, int]
    radius: int
    header_raw: dict
    part_count: int
    vertex_count: int
    body_word_0c: int
    body_word_10: int
    vertices: list[tuple[int, int, int, int]]
    parts: list[tuple[int, int]]
    part_markers_match: bool
    stream_offset: int
    stream_end: int
    commands: list[Command]
    primitives: list[Primitive]
    covered_bytes: int
    unreached: list[dict]


def _read_vertices(reader: Reader, offset: int, count: int) -> list[tuple[int, int, int, int]]:
    return [
        (
            reader.s16(offset + index * VERTEX_SIZE),
            reader.s16(offset + index * VERTEX_SIZE + 2),
            reader.s16(offset + index * VERTEX_SIZE + 4),
            reader.u16(offset + index * VERTEX_SIZE + 6),
        )
        for index in range(count)
    ]


def _parts(vertices: list[tuple[int, int, int, int]]) -> list[tuple[int, int]]:
    parts = []
    start = 0
    for index, vertex in enumerate(vertices):
        if vertex[3] & PART_END_FLAG:
            parts.append((start, index + 1))
            start = index + 1
    if start < len(vertices):
        parts.append((start, len(vertices)))
    return parts


class _Interpreter:
    def __init__(self, reader: Reader, start: int, end: int, vertex_count: int, where: str):
        self.reader = reader
        self.start = start
        self.end = end
        self.vertex_count = vertex_count
        self.where = where
        self.corners = [Corner() for _ in range(CORNERS)]
        # 0x8007C298-0x8007C2C8 reset only the normals (to 0, 0, 4096) at
        # stream start; colours, UVs, vertices, mode and texture persist
        # from whatever the interpreter drew before.
        for corner in self.corners:
            corner.normal = (0, 0, 0x1000)
        self.normal_from_reset = [True] * CORNERS
        self.mode: int | None = None
        self.texture_slot: int | None = None
        self.commands: list[Command] = []
        self.primitives: list[Primitive] = []
        self.blocks: list[tuple[int, int]] = []

    def vertex(self, index: int, command_offset: int) -> int:
        if index >= self.vertex_count:
            raise FormatError(
                f"{self.where}: command at {command_offset:#x} uses vertex {index} of {self.vertex_count}"
            )
        return index

    def run(self) -> int:
        cursor = self.start
        covered = 0
        while True:
            while self.blocks and cursor >= self.blocks[-1][1]:
                if cursor > self.blocks[-1][1]:
                    raise FormatError(f"{self.where}: command straddles block end {self.blocks[-1][1]:#x}")
                self.blocks.pop()
            if cursor + 4 > self.end:
                raise FormatError(f"{self.where}: command stream ends without opcode 0x68")
            word = self.reader.u32(cursor)
            opcode = word & 0xFC
            flags = word & 3
            if opcode > MAX_OPCODE:
                raise FormatError(f"{self.where}: opcode {opcode:#04x} at {cursor:#x} is outside the dispatch table")
            name, handler = OPCODES[opcode]
            command = Command(cursor, word, opcode, flags, name, handler, block_path=tuple(b[0] for b in self.blocks))
            operand = word >> 8
            cursor += 4
            if opcode in BLOCK_HEADERS:
                header_words, masks = BLOCK_HEADERS[opcode]
                if cursor + header_words * 4 > self.end:
                    raise FormatError(f"{self.where}: block header at {command.offset:#x} is truncated")
                command.extra_words = [self.reader.u32(cursor + 4 * index) for index in range(header_words)]
                cursor += header_words * 4
                block_end = command.offset + operand
                if operand % 4 or block_end < cursor or block_end > self.end:
                    raise FormatError(
                        f"{self.where}: block at {command.offset:#x} has impossible length {operand:#x}"
                    )
                command.operands = {
                    "length": operand,
                    "end": block_end,
                    "masks": command.extra_words[:masks],
                    "unknown_words": command.extra_words[masks:],
                }
                self.commands.append(command)
                covered += command.size
                self.blocks.append((command.offset, block_end))
                continue
            self.commands.append(command)
            covered += command.size
            if opcode == 0x68:
                if not self.blocks:
                    return cursor, covered
                # Inside a taken block the model ends here; otherwise the
                # interpreter resumes after the block.  Keep decoding that
                # second path so every byte is accounted for.
                command.operands = {"conditional": True}
                cursor = self.blocks[-1][1]
                continue
            self.apply(command, operand)
            # Block and END handlers jump straight back to the loop (or out),
            # skipping nclip and the emit step, so only these reach it.
            if flags:
                self.emit(command)

    def apply(self, command: Command, operand: int) -> None:
        opcode = command.opcode
        slot = (opcode >> 2) & 3
        if opcode <= 0x0C:
            self.corners[slot].colour = operand
            command.operands = {"corner": slot, "rgb": operand}
        elif 0x10 <= opcode <= 0x1C or 0x90 <= opcode <= 0x9C:
            normal = decode_normal(command.word)
            self.corners[slot].normal = normal
            self.normal_from_reset[slot] = False
            command.operands = {"corner": slot, "normal": list(normal)}
        elif 0x20 <= opcode <= 0x2C or 0x80 <= opcode <= 0x8C:
            uv = (operand & 0xFF, (operand >> 8) & 0xFF)
            self.corners[slot].uv = uv
            command.operands = {"corner": slot, "uv": list(uv), "unused_bits_24_31": command.word >> 24}
            if opcode >= 0x80:
                # 0x8007D198 also stores word >> 22 to scratch 0x1F800340,
                # which no traced code reads.
                command.operands["side_value"] = command.word >> 22
        elif 0x30 <= opcode <= 0x3C:
            self.corners[slot].vertex = self.vertex(operand, command.offset)
            command.operands = {"corner": slot, "vertex": operand}
        elif opcode in PAIR_CORNERS:
            low, high = PAIR_CORNERS[opcode]
            self.corners[low].vertex = self.vertex(operand & 0xFFF, command.offset)
            self.corners[high].vertex = self.vertex(operand >> 12, command.offset)
            command.operands = {"corners": [low, high], "vertices": [operand & 0xFFF, operand >> 12]}
        elif opcode == 0x58:
            self.texture_slot = command.word >> 24
            command.operands = {"texture_slot": self.texture_slot, "unused_bits_8_23": operand & 0xFFFF}
        elif opcode == 0x5C:
            self.mode = command.word >> 28
            command.operands = {
                "mode": self.mode,
                "quad": bool(self.mode & 1),
                "mode_bit1": bool(self.mode & 2),
                "gouraud": bool(self.mode & 4),
                "textured": bool(self.mode & 8),
                "unused_bits_8_27": operand & 0xFFFFF,
            }
        elif opcode == 0xA0:
            command.operands = {"depth_mode": (command.word >> 16) & 3, "raw_operand": operand}
        else:
            command.operands = {"raw_operand": operand}

    def emit(self, command: Command) -> None:
        primitive = Primitive(
            index=len(self.primitives),
            command=len(self.commands) - 1,
            mode=self.mode,
            texture_slot=self.texture_slot,
            cull=CULL_NAMES[command.flags],
            corners=[corner.copy() for corner in self.corners],
            block_path=command.block_path,
            missing=[],
        )
        if primitive.mode is None:
            primitive.missing.append("mode")
        used = primitive.used_corners if primitive.mode is not None else [0, 1, 2]
        for slot in used:
            corner = primitive.corners[slot]
            if corner.vertex is None:
                primitive.missing.append(f"corner{slot}.vertex")
            colour_needed = primitive.gouraud or slot == 0
            if colour_needed and corner.colour is None:
                primitive.missing.append(f"corner{slot}.colour")
            if primitive.textured and corner.uv is None:
                primitive.missing.append(f"corner{slot}.uv")
        if primitive.textured and primitive.texture_slot is None:
            primitive.missing.append("texture_slot")
        self.primitives.append(primitive)


def decode_model(data: bytes, offset: int, size: int, where: str = "model") -> Model:
    reader = Reader(data, where)
    if size < MODEL_HEADER_SIZE + BODY_HEADER_SIZE + 4:
        raise FormatError(f"{where}: {size} bytes is smaller than the model headers")
    if offset + size > len(data):
        raise FormatError(f"{where}: record extends past the file")
    if reader.bytes_at(offset, 4) != b"CM01":
        raise FormatError(f"{where}: missing CM01 header")
    body = offset + MODEL_HEADER_SIZE
    if reader.bytes_at(body, 4) != b"3MDL":
        raise FormatError(f"{where}: missing 3MDL body")
    part_count = reader.u32(body + 4)
    vertex_count = reader.u32(body + 8)
    body_size = reader.u32(body + 0x14)
    if body_size != size - MODEL_HEADER_SIZE:
        raise FormatError(f"{where}: body size {body_size} != record remainder {size - MODEL_HEADER_SIZE}")
    vertex_offset = body + BODY_HEADER_SIZE
    if vertex_count > (size - MODEL_HEADER_SIZE - BODY_HEADER_SIZE) // VERTEX_SIZE:
        raise FormatError(f"{where}: {vertex_count} vertices overrun the model")
    stream_offset = vertex_offset + vertex_count * VERTEX_SIZE
    stream_end = offset + size
    if (stream_end - stream_offset) % 4:
        raise FormatError(f"{where}: command stream is not a whole number of words")
    vertices = _read_vertices(reader, vertex_offset, vertex_count)
    parts = _parts(vertices)
    interpreter = _Interpreter(reader, stream_offset, stream_end, vertex_count, where)
    end, covered = interpreter.run()
    unreached = [raw_range(data, end, stream_end - end)] if end < stream_end else []
    return Model(
        offset=offset,
        size=size,
        sha256=sha256(data[offset : offset + size]),
        footprint=(reader.s16(offset + 4), reader.s16(offset + 8), reader.s16(offset + 0xC), reader.s16(offset + 0x10)),
        radius=reader.s16(offset + 0x22),
        header_raw=raw_range(data, offset, MODEL_HEADER_SIZE),
        part_count=part_count,
        vertex_count=vertex_count,
        body_word_0c=reader.u32(body + 0xC),
        body_word_10=reader.u32(body + 0x10),
        vertices=vertices,
        parts=parts,
        part_markers_match=sum(1 for v in vertices if v[3] & PART_END_FLAG) == part_count,
        stream_offset=stream_offset,
        stream_end=stream_end,
        commands=interpreter.commands,
        primitives=interpreter.primitives,
        covered_bytes=covered,
        unreached=unreached,
    )


def decode_bank(data: bytes, name: str = "MMM") -> list[tuple[int, Model | None]]:
    """Decode every model of an MMM bank, using the container parser."""

    from .ps1_formats import parse_mmm

    bank = parse_mmm(data, name)
    out = []
    for model in bank["models"]:
        if model["empty"]:
            out.append((model["index"], None))
            continue
        out.append((model["index"], decode_model(data, model["offset"], model["size"], f"{name}[{model['index']}]")))
    return out
