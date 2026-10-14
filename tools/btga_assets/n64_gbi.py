"""N64 display-list decoding for the F3DEX2 (Global Assault) and F3DEX (BattleTanx) dialects.

Command semantics are the documented RSP/RDP GBI encodings; the dialect a
pool uses is established by the game's own relocation code (Global
Assault relocates G_VTX 0x01 and G_SETTIMG 0xFD at 0x800BA6C0; BattleTanx
pool chunks begin with F3DEX G_VTX 0x04 / G_SETOTHERMODE_H 0xBA streams
relocated by 0x80086DC0 and 0x800852E0).

Addresses inside a pool chunk are chunk-relative before relocation, so
vertex and texture addresses are resolved against the chunk's own bytes.
Unknown commands are preserved raw; nothing is silently dropped.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .n64_binary import BEReader, FormatError

VERTEX_SIZE = 16
VERTEX = struct.Struct(">hhhHhhBBBB")
RDP_OPCODES = {
    0xE4: "G_TEXRECT",
    0xE6: "G_RDPLOADSYNC",
    0xE7: "G_RDPPIPESYNC",
    0xE8: "G_RDPTILESYNC",
    0xE9: "G_RDPFULLSYNC",
    0xF0: "G_LOADTLUT",
    0xF2: "G_SETTILESIZE",
    0xF3: "G_LOADBLOCK",
    0xF4: "G_LOADTILE",
    0xF5: "G_SETTILE",
    0xF6: "G_FILLRECT",
    0xF7: "G_SETFILLCOLOR",
    0xF8: "G_SETFOGCOLOR",
    0xF9: "G_SETBLENDCOLOR",
    0xFA: "G_SETPRIMCOLOR",
    0xFB: "G_SETENVCOLOR",
    0xFC: "G_SETCOMBINE",
    0xFD: "G_SETTIMG",
    0xFE: "G_SETZIMG",
    0xFF: "G_SETCIMG",
}


@dataclass(frozen=True)
class Dialect:
    name: str
    opcodes: dict
    vtx: int
    tri1: int
    tri2: int | None
    enddl: int
    texture: int
    othermode_h: int
    othermode_l: int
    geometry_set: int
    geometry_clear: int | None
    cull_front: int
    cull_back: int
    lighting: int
    shading_smooth: int
    texture_gen: int
    fog: int


F3DEX2 = Dialect(
    name="F3DEX2",
    opcodes={
        0x00: "G_NOOP", 0x01: "G_VTX", 0x02: "G_MODIFYVTX", 0x03: "G_CULLDL", 0x04: "G_BRANCH_Z",
        0x05: "G_TRI1", 0x06: "G_TRI2", 0x07: "G_QUAD", 0xD7: "G_TEXTURE", 0xD8: "G_POPMTX",
        0xD9: "G_GEOMETRYMODE", 0xDA: "G_MTX", 0xDB: "G_MOVEWORD", 0xDC: "G_MOVEMEM",
        0xDE: "G_DL", 0xDF: "G_ENDDL", 0xE1: "G_RDPHALF_1", 0xE2: "G_SETOTHERMODE_L",
        0xE3: "G_SETOTHERMODE_H", **RDP_OPCODES,
    },
    vtx=0x01, tri1=0x05, tri2=0x06, enddl=0xDF, texture=0xD7, othermode_h=0xE3, othermode_l=0xE2,
    geometry_set=0xD9, geometry_clear=None,
    cull_front=0x200, cull_back=0x400, lighting=0x20000, shading_smooth=0x200000, texture_gen=0x40000, fog=0x10000,
)

F3DEX = Dialect(
    name="F3DEX",
    opcodes={
        0x00: "G_SPNOOP", 0x01: "G_MTX", 0x03: "G_MOVEMEM", 0x04: "G_VTX", 0x06: "G_DL",
        0xB1: "G_TRI2", 0xB2: "G_MODIFYVTX", 0xB3: "G_RDPHALF_2", 0xB4: "G_RDPHALF_1",
        0xB5: "G_QUAD", 0xB6: "G_CLEARGEOMETRYMODE", 0xB7: "G_SETGEOMETRYMODE", 0xB8: "G_ENDDL",
        0xB9: "G_SETOTHERMODE_L", 0xBA: "G_SETOTHERMODE_H", 0xBB: "G_TEXTURE", 0xBC: "G_MOVEWORD",
        0xBD: "G_POPMTX", 0xBE: "G_CULLDL", 0xBF: "G_TRI1", 0xC0: "G_NOOP", **RDP_OPCODES,
    },
    vtx=0x04, tri1=0xBF, tri2=0xB1, enddl=0xB8, texture=0xBB, othermode_h=0xBA, othermode_l=0xB9,
    geometry_set=0xB7, geometry_clear=0xB6,
    cull_front=0x1000, cull_back=0x2000, lighting=0x20000, shading_smooth=0x200, texture_gen=0x40000, fog=0x10000,
)

DIALECTS = {"F3DEX2": F3DEX2, "F3DEX": F3DEX}


@dataclass
class Command:
    offset: int
    w0: int
    w1: int

    @property
    def opcode(self) -> int:
        return self.w0 >> 24


def find_stream(payload: bytes, dialect: Dialect, allowed: set[int] | None = None) -> tuple[int, list[Command]]:
    """Longest 8-byte-aligned run of known commands ending in a zero G_ENDDL.

    Most chunks start their list at byte 0; a few carry leading payload,
    which is why every aligned start is considered.
    """

    valid = set(dialect.opcodes) if allowed is None else set(allowed) | {dialect.enddl}
    best: list[Command] | None = None
    for start in range(0, len(payload) - 7, 8):
        commands = []
        for offset in range(start, len(payload) - 7, 8):
            w0, w1 = struct.unpack_from(">II", payload, offset)
            if w0 >> 24 not in valid:
                break
            commands.append(Command(offset, w0, w1))
            if w0 >> 24 == dialect.enddl:
                if w1 == 0 and w0 & 0xFFFFFF == 0 and len(commands) > 1:
                    if best is None or (len(commands), -commands[0].offset) > (len(best), -best[0].offset):
                        best = commands
                break
    if best is None:
        raise FormatError(f"no {dialect.name} display list ending in G_ENDDL")
    return best[0].offset, best


@dataclass
class Tile:
    fmt: int = 0
    siz: int = 0
    line: int = 0
    tmem: int = 0
    palette: int = 0
    cmt: int = 0
    maskt: int = 0
    shiftt: int = 0
    cms: int = 0
    masks: int = 0
    shifts: int = 0
    uls: int = 0
    ult: int = 0
    lrs: int = 0
    lrt: int = 0
    sized: bool = False


@dataclass
class RenderState:
    """RSP/RDP state accumulated over material and state lists."""

    geometry_mode: int = 0
    othermode_h: int = 0
    othermode_l: int = 0
    combine: tuple[int, int] = (0, 0)
    prim_colour: int | None = None
    env_colour: int | None = None
    fog_colour: int | None = None
    blend_colour: int | None = None
    texture_on: bool = False
    texture_scale: tuple[int, int] = (0xFFFF, 0xFFFF)
    texture_tile: int = 0
    timg: tuple[int, int, int, int] | None = None  # fmt, siz, width, chunk address
    tiles: list[Tile] = field(default_factory=lambda: [Tile() for _ in range(8)])
    image_load: dict | None = None
    tlut_load: dict | None = None
    # TMEM palette entries: entry index (0..255 from TMEM 0x100) -> chunk address.
    tlut_entries: dict = field(default_factory=dict)
    loads: list[dict] = field(default_factory=list)
    unknown: list[dict] = field(default_factory=list)


def apply(state: RenderState, command: Command, dialect: Dialect, source: str) -> None:
    w0, w1, opcode = command.w0, command.w1, command.opcode
    if opcode == dialect.geometry_set:
        if dialect is F3DEX2:
            state.geometry_mode = (state.geometry_mode & (w0 & 0xFFFFFF | 0xFF000000)) | w1
        else:
            state.geometry_mode |= w1
    elif dialect.geometry_clear is not None and opcode == dialect.geometry_clear:
        state.geometry_mode &= ~w1
    elif opcode == dialect.texture:
        state.texture_scale = (w1 >> 16, w1 & 0xFFFF)
        state.texture_tile = (w0 >> 8) & 7
        state.texture_on = bool(w0 & 0xFF) if dialect is F3DEX else bool((w0 >> 1) & 0x7F)
    elif opcode in (dialect.othermode_h, dialect.othermode_l):
        if dialect is F3DEX2:
            length = (w0 & 0xFF) + 1
            shift = 32 - ((w0 >> 8) & 0xFF) - length
        else:
            shift = (w0 >> 8) & 0xFF
            length = w0 & 0xFF
        mask = ((1 << length) - 1) << shift
        if opcode == dialect.othermode_h:
            state.othermode_h = (state.othermode_h & ~mask) | (w1 & mask)
        else:
            state.othermode_l = (state.othermode_l & ~mask) | (w1 & mask)
    elif opcode == 0xFC:
        state.combine = (w0 & 0xFFFFFF, w1)
    elif opcode == 0xFA:
        state.prim_colour = w1
    elif opcode == 0xFB:
        state.env_colour = w1
    elif opcode == 0xF8:
        state.fog_colour = w1
    elif opcode == 0xF9:
        state.blend_colour = w1
    elif opcode == 0xFD:
        state.timg = ((w0 >> 21) & 7, (w0 >> 19) & 3, (w0 & 0xFFF) + 1, w1)
    elif opcode == 0xF5:
        tile = state.tiles[(w1 >> 24) & 7]
        tile.fmt, tile.siz = (w0 >> 21) & 7, (w0 >> 19) & 3
        tile.line, tile.tmem = (w0 >> 9) & 0x1FF, w0 & 0x1FF
        tile.palette = (w1 >> 20) & 0xF
        tile.cmt, tile.maskt, tile.shiftt = (w1 >> 18) & 3, (w1 >> 14) & 0xF, (w1 >> 10) & 0xF
        tile.cms, tile.masks, tile.shifts = (w1 >> 8) & 3, (w1 >> 4) & 0xF, w1 & 0xF
    elif opcode == 0xF2:
        tile = state.tiles[(w1 >> 24) & 7]
        tile.uls, tile.ult = (w0 >> 12) & 0xFFF, w0 & 0xFFF
        tile.lrs, tile.lrt = (w1 >> 12) & 0xFFF, w1 & 0xFFF
        tile.sized = True
    elif opcode in (0xF3, 0xF4, 0xF0):
        if state.timg is None:
            raise FormatError(f"{source}: texture load at {command.offset:#x} has no G_SETTIMG")
        load = {
            "kind": {0xF3: "block", 0xF4: "tile", 0xF0: "tlut"}[opcode],
            "command": command.offset,
            "tile": (w1 >> 24) & 7,
            "timg": list(state.timg),
            "w0": w0,
            "w1": w1,
        }
        if opcode == 0xF0:
            load["entries"] = ((w1 >> 14) & 0x3FF) + 1
            load["tmem"] = state.tiles[load["tile"]].tmem
            if load["tmem"] < 0x100:
                raise FormatError(f"{source}: G_LOADTLUT at {command.offset:#x} targets TMEM {load['tmem']:#x} below the palette area")
            first = load["tmem"] - 0x100
            for index in range(load["entries"]):
                if first + index < 256:
                    state.tlut_entries[first + index] = state.timg[3] + 2 * index
            state.tlut_load = load
        else:
            state.image_load = load
        state.loads.append(load)
    elif opcode not in dialect.opcodes:
        state.unknown.append({"offset": command.offset, "w0": f"{w0:08x}", "w1": f"{w1:08x}"})


def run_state(payload: bytes, commands: list[Command], dialect: Dialect, state: RenderState, source: str) -> RenderState:
    for command in commands:
        if command.opcode in (dialect.vtx, dialect.tri1) or (dialect.tri2 is not None and command.opcode == dialect.tri2):
            raise FormatError(f"{source}: geometry command in a material/state list at {command.offset:#x}")
        apply(state, command, dialect, source)
    return state


@dataclass
class Mesh:
    vertices: list[dict]
    triangles: list[tuple[int, int, int]]
    vertex_offsets: list[int]
    commands: int
    overlaps_commands: bool


def run_geometry(payload: bytes, commands: list[Command], dialect: Dialect, source: str) -> Mesh:
    """Simulate the 32-entry vertex cache across G_VTX and triangle commands."""

    reader = BEReader(payload, source)
    cache: dict[int, int] = {}
    triangles = []
    offsets: dict[int, int] = {}
    command_start = commands[0].offset
    command_end = commands[-1].offset + 8
    overlap = False

    def load(address: int, count: int, first: int) -> None:
        nonlocal overlap
        if count <= 0 or first < 0 or first + count > 32:
            raise FormatError(f"{source}: G_VTX loads slots {first}..{first + count}")
        if address < 0 or address + count * VERTEX_SIZE > len(payload):
            raise FormatError(f"{source}: G_VTX reads past the chunk")
        if address % 8:
            raise FormatError(f"{source}: G_VTX address {address:#x} is misaligned")
        if max(address, command_start) < min(address + count * VERTEX_SIZE, command_end):
            overlap = True
        for index in range(count):
            vertex_offset = address + index * VERTEX_SIZE
            cache[first + index] = vertex_offset
            offsets.setdefault(vertex_offset, len(offsets))

    def triangle(slots: list[int], at: int) -> None:
        if any(slot not in cache for slot in slots):
            raise FormatError(f"{source}: triangle at {at:#x} uses an unloaded vertex slot")
        triangles.append(tuple(offsets[cache[slot]] for slot in slots))

    for command in commands:
        w0, w1, opcode = command.w0, command.w1, command.opcode
        if opcode == dialect.vtx:
            if dialect is F3DEX2:
                count = (w0 >> 12) & 0xFF
                first = ((w0 >> 1) & 0x7F) - count
            else:
                count = (w0 >> 10) & 0x3F
                first = ((w0 >> 16) & 0xFF) // 2
                if (w0 & 0x3FF) != count * VERTEX_SIZE - 1:
                    raise FormatError(f"{source}: F3DEX G_VTX length field disagrees with its count")
            load(w1, count, first)
        elif opcode == dialect.tri1:
            word = w0 if dialect is F3DEX2 else w1
            encoded = [(word >> shift) & 0xFF for shift in (16, 8, 0)]
            if any(value & 1 for value in encoded):
                raise FormatError(f"{source}: odd triangle index at {command.offset:#x}")
            triangle([value // 2 for value in encoded], command.offset)
        elif dialect.tri2 is not None and opcode == dialect.tri2:
            for word in (w0, w1):
                encoded = [(word >> shift) & 0xFF for shift in (16, 8, 0)]
                triangle([value // 2 for value in encoded], command.offset)
        elif opcode == dialect.enddl:
            break
    vertices = []
    for vertex_offset in sorted(offsets, key=offsets.get):
        x, y, z, flag, s, t, a, b, c, d = VERTEX.unpack_from(reader.data, vertex_offset)
        vertices.append({"offset": vertex_offset, "position": [x, y, z], "flag": flag, "st": [s, t], "rgba": [a, b, c, d]})
    for triangle_ in triangles:
        if len(set(triangle_)) != 3:
            raise FormatError(f"{source}: degenerate triangle")
    return Mesh(vertices, triangles, list(offsets), len(commands), overlap)


# ----------------------------------------------------------- textures ------

FORMAT_NAMES = {0: "RGBA", 1: "YUV", 2: "CI", 3: "IA", 4: "I"}
BITS = (4, 8, 16, 32)
TLUT_NONE, TLUT_RGBA16, TLUT_IA16 = 0, 2, 3


def _five(value: int) -> int:
    return (value << 3) | (value >> 2)


def rgba5551(pixel: int) -> tuple[int, int, int, int]:
    return _five((pixel >> 11) & 31), _five((pixel >> 6) & 31), _five((pixel >> 1) & 31), 255 if pixel & 1 else 0


def ia16(pixel: int) -> tuple[int, int, int, int]:
    intensity = pixel >> 8
    return intensity, intensity, intensity, pixel & 0xFF


def decode_image(data: bytes, fmt: int, siz: int, width: int, height: int, stride: int,
                 palette: list[int] | None = None, tlut: int = TLUT_RGBA16) -> bytes:
    """Decode linear DRAM texels to RGBA8888 (rows `stride` bytes apart)."""

    if width <= 0 or height <= 0 or width * height > 1 << 20:
        raise FormatError(f"texture dimensions {width}x{height} are not plausible")
    bits = BITS[siz]
    if stride * 8 < width * bits:
        raise FormatError("texture row stride is shorter than its width")
    if stride * (height - 1) + (width * bits + 7) // 8 > len(data):
        raise FormatError(f"{width}x{height} {FORMAT_NAMES.get(fmt, fmt)}{bits} texels exceed {len(data)} bytes")
    if fmt == 2 and palette is None:
        raise FormatError("colour-indexed texture without a palette")
    convert = ia16 if tlut == TLUT_IA16 else rgba5551
    out = bytearray()
    for y in range(height):
        row = data[y * stride : y * stride + (width * bits + 7) // 8]
        for x in range(width):
            if bits == 4:
                value = (row[x // 2] >> (4 if x % 2 == 0 else 0)) & 0xF
            elif bits == 8:
                value = row[x]
            elif bits == 16:
                value = (row[2 * x] << 8) | row[2 * x + 1]
            else:
                value = int.from_bytes(row[4 * x : 4 * x + 4], "big")
            if fmt == 2:
                if value >= len(palette) or palette[value] is None:
                    raise FormatError("palette index outside the loaded palette")
                out.extend(convert(palette[value]))
            elif fmt == 0 and bits == 16:
                out.extend(rgba5551(value))
            elif fmt == 0 and bits == 32:
                out.extend(value.to_bytes(4, "big"))
            elif fmt == 3 and bits == 4:
                intensity = (value >> 1) & 7
                level = (intensity << 5) | (intensity << 2) | (intensity >> 1)
                out.extend((level, level, level, 255 if value & 1 else 0))
            elif fmt == 3 and bits == 8:
                level = (value >> 4) * 17
                out.extend((level, level, level, (value & 0xF) * 17))
            elif fmt == 3 and bits == 16:
                out.extend(ia16(value))
            elif fmt == 4 and bits in (4, 8):
                level = value * 17 if bits == 4 else value
                out.extend((level, level, level, level))
            else:
                raise FormatError(f"unsupported texture format {FORMAT_NAMES.get(fmt, fmt)}{bits}")
    return bytes(out)


def texture_from_state(state: RenderState, payload: bytes, source: str) -> dict | None:
    """Describe and decode the render tile's texture from a material chunk.

    Returns None when the state loads no texture.  The image comes from
    the last G_LOADBLOCK/G_LOADTILE source; CI palettes from the last
    G_LOADTLUT, offset by the render tile's palette number for CI4.
    """

    if state.image_load is None:
        return None
    tile = state.tiles[state.texture_tile]
    if not tile.sized:
        return {"status": "no tile size", "source": source}
    width = ((tile.lrs - tile.uls) >> 2) + 1
    height = ((tile.lrt - tile.ult) >> 2) + 1
    bits = BITS[tile.siz]
    # The RDP stores 32-bit texels as split 16-bit halves in TMEM, so a
    # 32-bit tile's line field counts 16-bit rows: DRAM stride is twice it.
    stride = tile.line * 8 * (2 if tile.siz == 3 else 1) if tile.line else (width * bits + 7) // 8
    address = state.image_load["timg"][3]
    record = {
        "status": "decoded",
        "format": f"{FORMAT_NAMES.get(tile.fmt, tile.fmt)}{bits}",
        "fmt": tile.fmt,
        "siz": tile.siz,
        "width": width,
        "height": height,
        "row_stride": stride,
        "image_address": address,
        "wrap": {"s": tile.cms, "t": tile.cmt, "mask_s": tile.masks, "mask_t": tile.maskt, "shift_s": tile.shifts, "shift_t": tile.shiftt},
        "tile_origin": [tile.uls / 4.0, tile.ult / 4.0],
        "palette": None,
    }
    palette = None
    tlut = (state.othermode_h >> 14) & 3
    if tile.fmt == 2:
        if not state.tlut_entries:
            record["status"] = "no palette load"
            return record
        first, count = (tile.palette * 16, 16) if bits == 4 else (0, 256)
        indices = range(first, first + count)
        if bits == 4 and any(index not in state.tlut_entries for index in indices):
            record["status"] = "CI4 palette outside the loaded TLUT"
            return record
        palette = []
        for index in indices:
            entry_address = state.tlut_entries.get(index)
            if entry_address is None:
                palette.append(None)
                continue
            if entry_address < 0 or entry_address + 2 > len(payload):
                record["status"] = "palette outside chunk"
                return record
            palette.append(int.from_bytes(payload[entry_address : entry_address + 2], "big"))
        loaded = [index - first for index in indices if index in state.tlut_entries]
        record["palette"] = {"first_entry": first, "entries_loaded": len(loaded), "tlut": {0: "none", 2: "RGBA16", 3: "IA16"}.get(tlut, tlut),
                             "addresses": [state.tlut_entries[index] for index in indices if index in state.tlut_entries][:1],
                             "banks_loaded": sorted({index // 16 for index in state.tlut_entries})}
    try:
        rgba = decode_image(payload[address:], tile.fmt, tile.siz, width, height, stride, palette, TLUT_IA16 if tlut == TLUT_IA16 else TLUT_RGBA16)
    except FormatError as error:
        record["status"] = f"undecodable: {error}"
        return record
    record["rgba"] = rgba
    return record
