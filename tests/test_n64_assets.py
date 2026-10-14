import contextlib
import io
import json
import math
import os
import struct
import tempfile
import unittest
from pathlib import Path

from tools import btga_world
from tools.btga_assets import cross_platform as xp
from tools.btga_assets import export as ex
from tools.btga_assets import gltf, mesh as meshmod
from tools.btga_assets import n64_bt1, n64_btga
from tools.btga_assets import n64_gbi as gbi
from tools.btga_assets import n64_sources as src
from tools.btga_assets.n64_binary import BEReader, FormatError
from tools.btga_assets.studio import StudioError, StudioLibrary

ROOT = Path(__file__).resolve().parents[1]
SHA = "cd" * 32


def cmd(w0, w1=0):
    return struct.pack(">II", w0, w1)


def vtx(x, y, z, s=0, t=0, rgba=(255, 255, 255, 255), flag=0):
    return struct.pack(">hhhHhhBBBB", x, y, z, flag, s, t, *rgba)


def f3dex2_vtx(count, first, address):
    return cmd((0x01 << 24) | (count << 12) | ((first + count) << 1), address)


def f3dex2_tri(a, b, c):
    return cmd((0x05 << 24) | (a * 2 << 16) | (b * 2 << 8) | c * 2)


def f3dex_vtx(count, first, address):
    return cmd((0x04 << 24) | (first * 2 << 16) | (count << 10) | (count * 16 - 1), address)


def f3dex_tri(a, b, c):
    return cmd(0xBF << 24, (a * 2 << 16) | (b * 2 << 8) | c * 2)


class ReaderTests(unittest.TestCase):
    def test_big_endian_widths_and_bounds(self):
        reader = BEReader(bytes.fromhex("12345678fffe3f800000"))
        self.assertEqual(reader.u32(0), 0x12345678)
        self.assertEqual(reader.u16(0), 0x1234)
        self.assertEqual(reader.s16(4), -2)
        self.assertEqual(reader.s8(4), -1)
        with self.assertRaisesRegex(FormatError, "misaligned"):
            reader.u32(2)
        with self.assertRaisesRegex(FormatError, "exceeds"):
            reader.u16(10)
        with self.assertRaisesRegex(FormatError, "exceeds"):
            reader.u8(10)
        with self.assertRaisesRegex(FormatError, "exceeds"):
            reader.bytes_at(8, 3)
        with self.assertRaisesRegex(FormatError, "not whole"):
            reader.records(0, 6, 4, "x")
        self.assertEqual(BEReader(struct.pack(">f", 1.5)).f32(0), 1.5)


class GbiGeometryTests(unittest.TestCase):
    def test_f3dex2_cache_and_triangles(self):
        dl = f3dex2_vtx(3, 0, 0x20) + f3dex2_tri(0, 1, 2) + cmd(0xDF000000) + bytes(8)
        payload = dl + vtx(0, 0, 0) + vtx(10, 0, 0) + vtx(0, 0, 10)
        start, commands = gbi.find_stream(payload, gbi.F3DEX2, {0x01, 0x05})
        self.assertEqual(start, 0)
        mesh = gbi.run_geometry(payload, commands, gbi.F3DEX2, "t")
        self.assertEqual(mesh.triangles, [(0, 1, 2)])
        self.assertEqual(mesh.vertices[1]["position"], [10, 0, 0])

    def test_f3dex_second_slot_load(self):
        dl = f3dex_vtx(2, 0, 0x28) + f3dex_vtx(1, 2, 0x48) + f3dex_tri(2, 1, 0) + cmd(0xB8000000) + bytes(8)
        payload = dl + vtx(1, 2, 3) + vtx(4, 5, 6) + vtx(7, 8, 9)
        _, commands = gbi.find_stream(payload, gbi.F3DEX, {0x04, 0xBF})
        mesh = gbi.run_geometry(payload, commands, gbi.F3DEX, "t")
        positions = [v["position"] for v in mesh.vertices]
        self.assertEqual([positions[i] for i in mesh.triangles[0]], [[7, 8, 9], [4, 5, 6], [1, 2, 3]])

    def test_leading_payload_stream(self):
        payload = bytes(16) + f3dex2_vtx(3, 0, 0x30) + f3dex2_tri(0, 1, 2) + cmd(0xDF000000) + vtx(0, 0, 0) + vtx(1, 0, 0) + vtx(0, 1, 0)
        start, commands = gbi.find_stream(payload, gbi.F3DEX2, {0x01, 0x05})
        self.assertEqual(start, 16)
        with self.assertRaises(FormatError):
            gbi.find_stream(bytes(32), gbi.F3DEX2, {0x01})

    def test_malformed_geometry(self):
        cases = {
            "unloaded": f3dex2_tri(0, 1, 2) + cmd(0xDF000000),
            "slots": f3dex2_vtx(3, 31, 0x18) + cmd(0xDF000000),
            "past the chunk": f3dex2_vtx(3, 0, 0x4000) + cmd(0xDF000000),
            "misaligned": f3dex2_vtx(1, 0, 0x14) + cmd(0xDF000000) + bytes(16),
            "odd triangle": cmd(0x01 << 24 | 3 << 12 | 6, 0x20) + cmd(0x05 << 24 | 1 << 16 | 2 << 8 | 4) + cmd(0xDF000000) + bytes(8) + bytes(48),
            "degenerate": f3dex2_vtx(3, 0, 0x20) + f3dex2_tri(0, 0, 1) + cmd(0xDF000000) + bytes(8) + bytes(48),
        }
        for reason, data in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaises(FormatError):
                    _, commands = gbi.find_stream(data, gbi.F3DEX2, {0x01, 0x05})
                    gbi.run_geometry(data, commands, gbi.F3DEX2, reason)
        bad_len = cmd((0x04 << 24) | (2 << 10) | 5, 0x10) + cmd(0xB8000000) + bytes(32)
        with self.assertRaisesRegex(FormatError, "length field"):
            _, commands = gbi.find_stream(bad_len, gbi.F3DEX, {0x04})
            gbi.run_geometry(bad_len, commands, gbi.F3DEX, "x")


def state_for(commands: bytes, dialect=gbi.F3DEX2):
    state = gbi.RenderState()
    _, parsed = gbi.find_stream(commands, dialect)
    gbi.run_state(commands, parsed, dialect, state, "t")
    return state


class GbiStateTests(unittest.TestCase):
    def test_geometry_mode_and_othermode(self):
        state = state_for(cmd(0xD9FFFFFF, 0x400) + cmd(0xD9FFFBFF, 0x200) + cmd(0xE3001001, 0x8000) + cmd(0xDF000000))
        self.assertEqual(state.geometry_mode, 0x200)
        self.assertEqual((state.othermode_h >> 14) & 3, 2)
        old = state_for(cmd(0xB7000000, 0x2201) + cmd(0xB6000000, 0x200) + cmd(0xBA000E02, 0xC000) + cmd(0xB8000000), gbi.F3DEX)
        self.assertEqual(old.geometry_mode, 0x2001)
        self.assertEqual((old.othermode_h >> 14) & 3, 3)

    def test_unknown_commands_preserved(self):
        state = gbi.RenderState()
        gbi.apply(state, gbi.Command(8, 0x77000000, 1), gbi.F3DEX2, "t")
        self.assertEqual(state.unknown, [{"offset": 8, "w0": "77000000", "w1": "00000001"}])
        with self.assertRaisesRegex(FormatError, "no G_SETTIMG"):
            gbi.apply(gbi.RenderState(), gbi.Command(0, 0xF3000000, 0), gbi.F3DEX2, "t")
        with self.assertRaisesRegex(FormatError, "geometry command"):
            gbi.run_state(b"", [gbi.Command(0, 0x01000000, 0)], gbi.F3DEX2, gbi.RenderState(), "t")

    def texture_state(self, fmt, siz, width, height, image, palette=b"", tlut=0x8000, line=None, palette_number=0, tlut_tmem=0x100):
        bits = (4, 8, 16, 32)[siz]
        line = line if line is not None else max(1, (width * bits // 8 + 7) // 8)
        commands = b""
        base = 0x100
        if palette:
            commands += cmd(0xFD100000, base + len(image)) + cmd(0xF5000000 | tlut_tmem, 0x07000000) + cmd(0xF0000000, 0x07000000 | ((len(palette) // 2 - 1) << 14))
        commands += cmd(0xFD000000 | (fmt << 21) | (siz << 19), base) + cmd(0xF3000000, 0x07000000)
        commands += cmd(0xF5000000 | (fmt << 21) | (siz << 19) | (line << 9), palette_number << 20)
        commands += cmd(0xF2000000, ((width - 1) * 4 << 12) | (height - 1) * 4) + cmd(0xE3001001, tlut) + cmd(0xDF000000)
        payload = commands + bytes(base - len(commands)) + image + palette
        state = gbi.RenderState()
        _, parsed = gbi.find_stream(payload, gbi.F3DEX2)
        gbi.run_state(payload, parsed, gbi.F3DEX2, state, "t")
        return gbi.texture_from_state(state, payload, "t")

    def test_texture_formats(self):
        rgba16 = self.texture_state(0, 2, 2, 1, struct.pack(">HH", 0xF801, 0x07C0))
        self.assertEqual(rgba16["rgba"], bytes((255, 0, 0, 255, 0, 255, 0, 0)))
        rgba32 = self.texture_state(0, 3, 2, 2, bytes(range(32)), line=1)
        self.assertEqual(rgba32["row_stride"], 16)
        self.assertEqual(rgba32["rgba"][8:12], bytes((16, 17, 18, 19)))
        palette = struct.pack(">16H", 0x0001, 0xF801, *([0] * 14))
        ci4 = self.texture_state(2, 0, 2, 1, bytes((0x10,)) + bytes(7), palette)
        self.assertEqual(ci4["rgba"], bytes((255, 0, 0, 255, 0, 0, 0, 255)))
        ia = self.texture_state(2, 0, 2, 1, bytes((0x10,)) + bytes(7), struct.pack(">16H", 0x80FF, 0x4020, *([0] * 14)), tlut=0xC000)
        self.assertEqual(ia["rgba"][:4], bytes((0x40, 0x40, 0x40, 0x20)))
        ci8 = self.texture_state(2, 1, 1, 1, bytes((1,)) + bytes(7), struct.pack(">256H", 0, 0x07C1, *([0] * 254)))
        self.assertEqual(ci8["rgba"], bytes((0, 255, 0, 255)))
        self.assertEqual(self.texture_state(3, 0, 2, 1, bytes((0xF1,)) + bytes(7))["rgba"], bytes((255, 255, 255, 255, 0, 0, 0, 255)))
        self.assertEqual(self.texture_state(3, 0, 2, 1, bytes((0x6E,)) + bytes(7))["rgba"], bytes((109, 109, 109, 0, 255, 255, 255, 0)))
        self.assertEqual(self.texture_state(3, 1, 1, 1, bytes((0xA5,)) + bytes(7))["rgba"], bytes((170, 170, 170, 85)))
        self.assertEqual(self.texture_state(3, 2, 1, 1, bytes((0x80, 0x40)) + bytes(6))["rgba"], bytes((128, 128, 128, 64)))
        self.assertEqual(self.texture_state(4, 0, 2, 1, bytes((0xF0,)) + bytes(7))["rgba"], bytes((255,) * 4 + (0,) * 4))
        self.assertEqual(self.texture_state(4, 1, 1, 1, bytes((7,)) + bytes(7))["rgba"], bytes((7,) * 4))

    def test_ci4_palette_number_and_malformed(self):
        palette = struct.pack(">32H", *([0] * 16), 0xF801, *([0] * 15))
        selected = self.texture_state(2, 0, 2, 1, bytes((0x00,)) + bytes(7), palette, palette_number=1)
        self.assertEqual(selected["rgba"][:4], bytes((255, 0, 0, 255)))
        outside = self.texture_state(2, 0, 2, 1, bytes(8), struct.pack(">16H", *([0] * 16)), palette_number=3)
        self.assertEqual(outside["status"], "CI4 palette outside the loaded TLUT")
        short = self.texture_state(0, 2, 64, 64, bytes(8))
        self.assertTrue(short["status"].startswith("undecodable"))
        with self.assertRaisesRegex(FormatError, "plausible"):
            gbi.decode_image(b"", 0, 2, 0, 1, 2)
        with self.assertRaisesRegex(FormatError, "stride"):
            gbi.decode_image(bytes(16), 0, 2, 4, 1, 2)


SLOT_SOURCE = """
s32 func_800E8380(u32 arg0, s32 a1, u32 *a2, u32 *a3) {
    switch (arg0) {
    case 0:
        a2[0] = (u32)D_B0100000;
        a3[0] = (u32)D_B0100FFF;
        if (a1) {
            a2[1] = (u32)(D_B0102000 + 0x10);
            a3[1] = (u32)D_B0102FFF;
            goto ret2;
        }
        if (D_80117EB4 == 10) {
            a2[1] = (u32)(D_B0104010 - 0x10);
            a3[1] = (u32)D_B0104FFF;
            goto ret2;
        }
        a2[1] = (u32)D_B0103000;
        a3[1] = (u32)D_B0103FFF;
        goto ret2;
        ret2:
        return 2;
    case 1:
        a2[0] = (u32)D_B0105000;
        a3[0] = (u32)D_B0105FFF;
        if (!a1) {
            a2[1] = (u32)D_B0106000;
            a3[1] = (u32)D_B0106FFF;
            return 2;
        }
        /* fallthrough */
    case 2:
        a2[0] = (u32)D_B0107000;
        a3[0] = (u32)D_B0107FFF;
        return 1;
    default:
        return 0;
    }
}
"""


class SourceTableTests(unittest.TestCase):
    def test_slot_range_evaluation(self):
        self.assertEqual(src.ga_level_streams(SLOT_SOURCE, 0, False, False), [(0x100000, 0x101000), (0x103000, 0x104000)])
        self.assertEqual(src.ga_level_streams(SLOT_SOURCE, 0, True, False), [(0x100000, 0x101000), (0x102010, 0x103000)])
        self.assertEqual(src.ga_level_streams(SLOT_SOURCE, 0, False, True), [(0x100000, 0x101000), (0x104000, 0x105000)])
        # Matching fallthrough: with the flag set, case 1 continues into case 2.
        self.assertEqual(src.ga_level_streams(SLOT_SOURCE, 1, True, False), [(0x107000, 0x108000)])
        self.assertEqual(src.ga_level_streams(SLOT_SOURCE, 1, False, False), [(0x105000, 0x106000), (0x106000, 0x107000)])
        self.assertEqual(src.ga_level_streams(SLOT_SOURCE, 9, False, False), [])
        with self.assertRaisesRegex(FormatError, "unrecognised statement"):
            src.parse_slot_ranges(SLOT_SOURCE.replace("goto ret2;", "frobnicate();", 1))
        with self.assertRaisesRegex(FormatError, "address expression"):
            src._address("(u32)D_80000000")

    def test_bt1_stream_table_simulation(self):
        rom = bytearray(src.bt1_offset(0x80125A68) + 28 * 16 + 16)
        code = []
        for address, value in ((0x80125A68, 0xB0100000), (0x80125A6C, 0xB0100FF9), (0x80125A70, 0xB0200000), (0x80125A74, 0xB0200007)):
            hi = (value >> 16) + (1 if value & 0x8000 else 0)
            code += [0x3C020000 | (hi & 0xFFFF), 0x24420000 | (value & 0xFFFF)]
            ahi = (address >> 16) + (1 if address & 0x8000 else 0)
            code += [0x3C010000 | (ahi & 0xFFFF), 0xAC220000 | (address & 0xFFFF)]
        code.append(0x03E00008)
        struct.pack_into(f">{len(code)}I", rom, src.bt1_offset(0x800865E0), *code)
        table = src.bt1_stream_table(bytes(rom))
        self.assertEqual(table[0]["world"], (0x100000, 0x101000))
        self.assertEqual(table[0]["nav"], (0x200000, 0x200008))
        self.assertIsNone(table[1]["world"])
        struct.pack_into(">I", rom, src.bt1_offset(0x800865E0), 0x00000001 | (0x23 << 26))
        with self.assertRaisesRegex(FormatError, "unexpected instruction"):
            src.bt1_stream_table(bytes(rom))

    def test_identity_and_streams(self):
        with self.assertRaisesRegex(FormatError, "unrecognised ROM"):
            src.identify(b"not a rom")
        with self.assertRaisesRegex(FormatError, "outside the ROM"):
            src.decode_stream(b"abc", 2, 9)


def bt1_world(placements=None, models=1, bad=None):
    header = bytearray(n64_bt1.HEADER_SIZE)
    struct.pack_into(">4f", header, 0, -100, -100, 100, 100)
    struct.pack_into(">I", header, 0x27C, 1)
    struct.pack_into(">i", header, 0x280, 0)
    placements = placements if placements is not None else [
        n64_bt1.PLACEMENT.pack(0, 0, 0, 0, 0, 0, 21, 141.0, -2812.0, 0, -1, -1, 0),
        n64_bt1.PLACEMENT.pack(0, 0, 0, 0, 0, 0, 0, 10.0, 20.0, 0x4000, 0, 0, 0),
    ]
    groups = n64_bt1.GROUP.pack(1, 0, 0, 0, 0, 0) + n64_bt1.GROUP.pack(len(placements) - 1, 1, -1, -1, 1, 1)
    model = b"".join(n64_bt1.MODEL.pack(1, 0, 0, -5, -6, 5, 6) for _ in range(models))
    sets = n64_bt1.MODEL_SET.pack(1, 0, 0)
    parts = n64_bt1.PART.pack(1, 0, 0)
    refs = n64_bt1.REFERENCE.pack(0x10, 0, 0x40, 0x20, 0x80)
    sections = [bytes(header), groups, b"".join(placements), model, sets, parts, refs, b""]
    if bad:
        sections = bad(sections)
    offsets = [36]
    for section in sections:
        offsets.append(offsets[-1] + len(section))
    return struct.pack(">9I", *offsets) + b"".join(sections)


class Bt1ParseTests(unittest.TestCase):
    def test_world_parse(self):
        world = n64_bt1.parse_world(bt1_world())
        self.assertTrue(world["groups"][0]["marker_group"])
        self.assertTrue(world["groups"][1]["active"])
        self.assertEqual(world["placements"][0]["type"], 21)
        self.assertEqual((world["placements"][0]["x"], world["placements"][0]["z"]), (141.0, -2812.0))
        self.assertEqual(world["placements"][1]["yaw"], 0x4000)
        self.assertEqual(world["header"]["bounds_a"], [-100.0, -100.0, 100.0, 100.0])

    def test_world_rejects_malformed(self):
        cases = {
            "cover": lambda: bt1_world()[:-4],
            "header is": lambda: bt1_world(bad=lambda s: [s[0][:-4]] + s[1:]),
            "not whole": lambda: bt1_world(bad=lambda s: s[:2] + [s[2] + b"\0\0\0\0"] + s[3:]),
            "model set": lambda: bt1_world(placements=[n64_bt1.PLACEMENT.pack(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 5, -1, 0)] * 2),
            "placement model": lambda: bt1_world(placements=[n64_bt1.PLACEMENT.pack(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -1, 9, 0)] * 2),
            "target section": lambda: bt1_world(bad=lambda s: s[:5] + [n64_bt1.PART.pack(3, 0, 0)] + s[6:]),
            "group count": lambda: bt1_world(bad=lambda s: [s[0][:0x27C] + struct.pack(">I", 7) + s[0][0x280:]] + s[1:]),
            "nine-offset": lambda: b"\0" * 8,
        }
        for reason, make in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(FormatError, reason):
                    n64_bt1.parse_world(make())

    def test_nav_parse(self):
        points = struct.pack(">4f", 72.0, 2808.0, -1.0, 2.0)
        links = struct.pack(">6H", 0, 1, 2, 3, 4, 5)
        header = struct.pack(">10I", 0x4C, 0x4C, (2 << 16) | 1, 0x28, 0x38, 0x38, 0x44, 0x48, 0, 0)
        nav = n64_bt1.parse_nav(header + points + links + bytes(8))
        self.assertEqual(nav["points"], [[72.0, 2808.0], [-1.0, 2.0]])
        self.assertEqual(nav["links"], [[0, 1, 2, 3, 4, 5]])
        with self.assertRaisesRegex(FormatError, "overrun"):
            n64_bt1.parse_nav(struct.pack(">10I", 0x50, 0x50, (9 << 16) | 1, 0x28, 0x38, 0x38, 0x44, 0x48, 0, 0) + bytes(40))


def ga_rom_with_pools():
    rom = bytearray(src.GA_POOLS["geometry"][1])
    geometry = f3dex2_vtx(3, 0, 0x20) + f3dex2_tri(0, 1, 2) + cmd(0xDF000000) + bytes(8)
    geometry += vtx(0, 0, 0, rgba=(255, 0, 0, 255)) + vtx(64, 0, 0, s=32 * 4) + vtx(0, 64, 0, t=32 * 4)
    state = cmd(0xD9FFFFFF, 0x400) + cmd(0xD7000002, 0xFFFFFFFF) + cmd(0xF5100000 | (1 << 9), 0) + cmd(0xF2000000, (3 * 4 << 12) | 3 * 4) + cmd(0xDF000000)
    texture = cmd(0xFD100000, 0x20) + cmd(0xF3000000, 0x07000000) + cmd(0xDF000000) + bytes(8) + struct.pack(">16H", *([0xF801] * 16))
    rom[src.GA_POOLS["geometry"][0] : src.GA_POOLS["geometry"][0] + len(geometry)] = geometry
    rom[src.GA_POOLS["state"][0] : src.GA_POOLS["state"][0] + len(state)] = state
    rom[src.GA_POOLS["texture"][0] : src.GA_POOLS["texture"][0] + len(texture)] = texture
    return bytes(rom), len(geometry), len(state), len(texture)


def ga_world(definitions, placements):
    blob = b"".join(definitions)
    offsets = []
    cursor = 0
    for definition in definitions:
        offsets.append(cursor)
        cursor += len(definition)
    world = {
        "groups": [{"placement_count": len(placements), "first_placement": 0, "min_x": -500, "min_y": 0, "min_z": -500, "max_x": 500, "max_y": 100, "max_z": 500}],
        "placements": [{"x": x, "y": y, "z": z, "yaw": yaw, "definition_offset": offsets[index]} for (x, y, z, yaw, index) in placements],
        "definitions": [{"offset": offsets[i], "size": len(d), "kind": d[0], "raw_hex": d.hex()} for i, d in enumerate(definitions)],
        "models": [{"part_count": 1, "first_part": 0, "min_x": -8, "min_y": 0, "min_z": -9, "max_x": 8, "max_y": 20, "max_z": 9}],
        "parts": [{"reference_count": 1, "first_reference": 0}],
        "references": [],
    }
    return world


class GlobalAssaultTests(unittest.TestCase):
    def setUp(self):
        self.rom, g, s, t = ga_rom_with_pools()
        self.sizes = (g, s, t)

    def level(self, definitions, placements, texture=True):
        world = ga_world(definitions, placements)
        world["references"] = [{"geometry_offset": 0, "geometry_size": self.sizes[0], "state_offset": 0, "state_size": self.sizes[1],
                                "texture_offset": 0 if texture else -1, "texture_size": self.sizes[2] if texture else -1}]
        data = btga_world.build_world(world)
        parsed = btga_world.parse_world(data)
        return n64_btga.Level(3, "TEST LEVEL", "default", [n64_btga.Bundle(0, (0, len(data)), data, parsed, 0)])

    def test_scene_rules(self):
        definitions = [
            bytes((0, 0)) + struct.pack(">H", 0) + bytes(4),
            bytes((1, 0)) + struct.pack(">HB", 0, 0) + bytes(3),
            bytes((30, 1)) + struct.pack(">6h", -10, 0, -20, 10, 50, 20),
            bytes((30, 0)) + struct.pack(">6h", -300, 0, -400, 300, 0, 400),
            bytes((37,)) + bytes(range(1, 22)),
            bytes((38, 9, 8, 7)),
            bytes((16, 3, 3, 1)),
            bytes((9, 0, 0, 0)),
            bytes((39, 0)) + struct.pack(">HI", 5, 0),
        ]
        placements = [(100, 5, 200, 0x4000, 0), (0, 0, 0, 0, 1), (1, 2, 3, 0, 2), (10, 0, 20, 0, 3), (0, 0, 0, 0, 4), (0, 0, 0, 0, 5), (7, 0, 7, 0, 6),
                      (0, 0, 0, 0, 7), (9, 9, 9, 0, 8)]
        level = self.level(definitions, placements)
        textures = {}
        scene, models = n64_btga.scene_record(self.rom, SHA, level, textures)
        self.assertEqual(len(models), 1)
        self.assertEqual(len(scene["instances"]), 3)
        self.assertEqual(scene["instances"][0]["translation"], [100, 5, 200])
        self.assertEqual(scene["instances"][0]["yaw"], 0x4000)
        self.assertEqual(scene["instances"][2]["extras"]["conditions"], [{"condition": 0, "flag": 5}])
        boxes = {c["flags"]: c for c in scene["collision"]}
        self.assertEqual((boxes[1]["min"], boxes[1]["max"]), ([-8, 0, -9], [8, 20, 9]))
        self.assertEqual(boxes[1]["vertical"]["status"], "proven")
        self.assertEqual(boxes[1]["orientation"], "axis_aligned_snapped")
        self.assertEqual((boxes[0x01000000]["min"], boxes[0x01000000]["max"]), ([-10, 0, -20], [10, 50, 20]))
        self.assertEqual((boxes[0x10000]["min"], boxes[0x10000]["max"]), ([-3, 0, -3], [3, 100, 3]))
        self.assertEqual([(lane["group"], lane["x"], lane["z"]) for lane in scene["bounds"]["lanes"]], [(0, [-290, 310], [-380, 420])])
        env = [e for e in scene["environment"] if e["kind"] == 37][0]
        self.assertEqual(env["clear_and_fog_colour"], [1, 2, 3])
        self.assertEqual(env["sky_colour_b"], [16, 17, 18])
        self.assertEqual([e["area_colour"] for e in scene["environment"] if e["kind"] == 38], [[9, 8, 7]])
        categories = sorted(m["category"] for m in scene["markers"])
        self.assertEqual(categories, ["ignored_by_dispatcher", "pickup"])
        pickup = next(m for m in scene["markers"] if m["category"] == "pickup")
        self.assertEqual({k: v["value"] for k, v in pickup["fields"].items()}, {"half_width_x": 3, "half_width_z": 3, "pickup_type": 1})
        self.assertEqual(pickup["restrictions"]["modes_excluded"], [0, 1, 2, 3, 6, 8, 11, 12, 13])
        model = next(iter(models.values()))
        corners = model["mesh"]["triangles"][0]["corners"]
        self.assertEqual(corners[0]["colour"], [1.0, 0.0, 0.0, 1.0])
        self.assertEqual([round(c["uv"][0], 6) for c in corners], [0.0, 1.0, 0.0])
        self.assertFalse(model["mesh"]["materials"][0]["double_sided"])
        self.assertEqual(next(iter(textures.values()))["width"], 4)

    def test_content_ids_are_stable_and_deduplicated(self):
        definitions = [bytes((0, 0)) + struct.pack(">H", 0) + bytes(4)]
        level = self.level(definitions, [(0, 0, 0, 0, 0), (5, 0, 5, 0, 0)])
        first, models = n64_btga.scene_record(self.rom, SHA, level, {})
        second, _ = n64_btga.scene_record(self.rom, SHA, level, {})
        self.assertEqual(len(models), 1)
        self.assertEqual(json.dumps(first, sort_keys=True, default=str), json.dumps(second, sort_keys=True, default=str))
        self.assertNotEqual(n64_btga.content_id("model", SHA, [1]), n64_btga.content_id("model", "ef" * 32, [1]))
        self.assertNotEqual(n64_btga.content_id("model", SHA, [1]), n64_btga.content_id("texture", SHA, [1]))

    def test_pool_bounds_and_uv_math(self):
        with self.assertRaisesRegex(FormatError, "leaves its pool"):
            n64_btga.chunk(self.rom, "state", 0x9378, 0x10)
        self.assertEqual(n64_btga._st_to_uv(64, 0x8000, 0, 0, 16), 1 / 16)
        self.assertEqual(n64_btga._st_to_uv(64, 0xFFFF, 1, 0, 1), 1.0)
        self.assertEqual(n64_btga._st_to_uv(64, 0xFFFF, 15, 0, 1), 4.0)
        self.assertEqual(n64_btga._wrap(1), "mirror")
        self.assertEqual(n64_btga._wrap(2), "clamp")


class MeshAndGltfTests(unittest.TestCase):
    def builder(self):
        builder = meshmod.MeshBuilder()
        for p in ((0, 0, 0), (1, 0, 0), (0, 1, 0)):
            builder.position(p)
        material = builder.material({"id": "m", "double_sided": False, "alpha_mode": "OPAQUE", "texture": None})
        builder.triangle(material, [{"p": 0, "colour": [1, 0, 0, 1]}, {"p": 1, "colour": [0, 1, 0, 1]}, {"p": 2, "colour": [0, 0, 1, 1]}])
        return builder

    def test_mesh_validation(self):
        builder = self.builder()
        for bad in ([{"p": 0}, {"p": 0}, {"p": 1}], [{"p": 0}, {"p": 1}], [{"p": 0}, {"p": 1}, {"p": 9}], [{"p": 0, "uv": [math.nan, 0]}, {"p": 1}, {"p": 2}]):
            with self.assertRaises(meshmod.MeshError):
                builder.triangle(0, bad)
        with self.assertRaises(meshmod.MeshError):
            builder.material({"id": "x", "double_sided": True, "alpha_mode": "SOMETIMES"})
        with self.assertRaises(meshmod.MeshError):
            builder.position((0, math.inf, 0))

    def test_mesh_glb_and_scene_layers(self):
        record = {"id": "n64_btga:model:abc", "schema": "btga-neutral", "schema_version": 1, "source": {"platform": "n64_btga"}, "mesh": self.builder().build()}
        first = gltf.mesh_glb(record, {})
        self.assertEqual(first, gltf.mesh_glb(record, {}))
        document, _ = gltf.read_glb(first)
        self.assertIn("COLOR_0", document["meshes"][0]["primitives"][0]["attributes"])
        scene = {"id": "s", "schema": "btga-neutral", "schema_version": 1,
                 "instances": [{"id": "i", "model": record["id"], "translation": [1, 2, 3], "yaw": 0x8000}],
                 "collision": [{"id": "c", "translation": [0, 0, 0], "yaw": 0, "min": [-1, 0, -1], "max": [1, 1, 1]}],
                 "markers": [{"id": "k", "category": "pickup", "translation": [5, 0, 5], "yaw": 0}]}
        document, _ = gltf.read_glb(gltf.scene_mesh_glb(scene, {record["id"]: record}, {}))
        names = [document["nodes"][i]["name"] for i in document["scenes"][0]["nodes"]]
        self.assertEqual(names, ["visible", "collision", "markers"])
        self.assertAlmostEqual(document["nodes"][0]["rotation"][1], 1.0)

    def test_texture_samplers_follow_wrap(self):
        builder = meshmod.MeshBuilder()
        for p in ((0, 0, 0), (1, 0, 0), (0, 1, 0)):
            builder.position(p)
        material = builder.material({"id": "t", "double_sided": True, "alpha_mode": "MASK", "texture": "tex"})
        builder.triangle(material, [{"p": 0, "uv": [0, 0]}, {"p": 1, "uv": [2, 0]}, {"p": 2, "uv": [0, 1]}])
        texture = {"id": "tex", "width": 1, "height": 1, "rgba": bytes(4), "wrap": {"s": "mirror", "t": "repeat"}}
        document, _ = gltf.read_glb(gltf.mesh_glb({"id": "x:model:y", "schema": "s", "schema_version": 1, "source": {}, "mesh": builder.build()}, {"tex": texture}))
        self.assertEqual(document["samplers"][0]["wrapS"], 33648)
        self.assertEqual(document["samplers"][0]["wrapT"], 10497)
        self.assertEqual(document["materials"][0]["alphaMode"], "MASK")


def box_positions(sx=1.0, sy=2.0, sz=3.0):
    return [[x * sx, y * sy, z * sz] for x in (0, 1) for y in (0, 1) for z in (0, 1)] + [[0.5 * sx, 2.5 * sy, 0.2 * sz]]


class CrossPlatformTests(unittest.TestCase):
    def test_signature_invariance(self):
        base = xp.shape_signature(box_positions(), 12)
        scaled = xp.shape_signature([[4 * v for v in p] for p in box_positions()], 12)
        rotated = xp.shape_signature([[-p[2], p[1], p[0]] for p in box_positions()], 12)
        mirrored = xp.shape_signature([[-p[0], p[1], p[2]] for p in box_positions()], 12)
        for other in (scaled, rotated, mirrored):
            self.assertEqual(base["d2"], other["d2"])
            self.assertEqual(base["extent_ratios"], other["extent_ratios"])
        result = xp.compare_shapes(base, scaled)
        self.assertEqual(result["scale_estimate"], 4.0)
        self.assertIsNone(xp.compare_shapes(base, xp.shape_signature(box_positions(1, 10, 1), 12)))
        self.assertIsNone(xp.shape_signature([[0, 0, 0]] * 3, 1))

    def test_model_matching_finds_rotated_copy_only(self):
        left = [("ps1:model:a", xp.shape_signature(box_positions(), 12))]
        right = [("n64_btga:model:b", xp.shape_signature([[-p[2], p[1], p[0]] for p in box_positions(2, 4, 6)], 12)),
                 ("n64_btga:model:c", xp.shape_signature(box_positions(1, 9, 1), 12))]
        links = xp.match_models(left, right)
        self.assertEqual([link["b"] for link in links], ["n64_btga:model:b"])
        self.assertEqual(links[0]["status"], "candidate")

    def test_layout_comparison_handles_rotation_and_reflection(self):
        points = [(0, 0), (10, 0), (10, 5), (3, 8), (-4, 2), (6, -6)]
        rotated = [(-z, x) for x, z in points]
        result = xp.compare_layouts(points, [(2 * x, 2 * z) for x, z in rotated])
        self.assertLess(result["rms_distance"], 1e-9)
        self.assertEqual(result["scale_estimate"], 2.0)
        self.assertFalse(result["reflection"])
        reflected = xp.compare_layouts(points, [(-x, z) for x, z in points])
        self.assertTrue(reflected["reflection"])
        self.assertIsNone(xp.compare_layouts(points[:3], points))
        self.assertIsNone(xp.compare_layouts(xp.normalise_layout(points[:3]), xp.normalise_layout(points)))


def write_synthetic_export(root: Path) -> None:
    exporter = ex.Exporter(root)
    builder = meshmod.MeshBuilder()
    for p in ((0, 0, 0), (1, 0, 0), (0, 1, 0)):
        builder.position(p)
    material = builder.material({"id": "m", "double_sided": False, "alpha_mode": "OPAQUE", "texture": None})
    builder.triangle(material, [{"p": 0}, {"p": 1}, {"p": 2}])
    for platform, yaw in (("ps1", 0x4000), ("n64_btga", 0x4000)):
        model = {"id": f"{platform}:model:demo", "schema": "btga-neutral", "schema_version": 1, "kind": "model",
                 "source": {"platform": platform}, "mesh": builder.build(), "bounds": meshmod.bounds(builder.build())}
        scene = {"id": f"{platform}:scene:demo", "schema": "btga-neutral", "schema_version": 1, "kind": "scene", "source": {"platform": platform},
                 "instances": [{"id": f"{platform}:placement:1", "model": model["id"], "translation": [10, 0, 20], "yaw": yaw}],
                 "collision": [], "markers": [{"id": f"{platform}:marker:1", "category": "spawn", "translation": [0, 0, 0], "yaw": 0}],
                 "environment": [], "bounds": {}, "minimap": {}, "models": [model["id"]]}
        exporter.sources.append({"platform": platform, "title": platform})
        exporter.add_scene(scene, {model["id"]: model}, {}, {"platform": platform, "level": "demo", "name": "Demo", "campaign": "Demo", "variant": "default"})
    exporter.finish()


def load_any_map(library: StudioLibrary, level_id: str) -> list[tuple]:
    """What Studio does: identical code whatever the source platform."""

    game_map = library.load_map(level_id)
    return [(instance.position, instance.yaw_turns, len(library.model(instance.model).mesh["triangles"])) for instance in game_map.instances]


class StudioApiTests(unittest.TestCase):
    def test_platform_neutral_map_loading(self):
        with tempfile.TemporaryDirectory() as temp:
            write_synthetic_export(Path(temp))
            library = StudioLibrary.open(Path(temp))
            levels = library.levels()
            self.assertEqual({level.platform for level in levels}, {"ps1", "n64_btga"})
            results = [load_any_map(library, level.id) for level in levels]
            self.assertEqual(results[0], results[1])
            self.assertEqual(results[0], [((10.0, 0.0, 20.0), 0.25, 1)])
            self.assertEqual(library.campaigns(), {"Demo": sorted(level.id for level in levels)})
            self.assertEqual(library.load_map(levels[0].id).markers[0].category, "spawn")
            self.assertEqual(library.placeable_objects()[0]["category"], "model")
            with self.assertRaises(StudioError):
                library.load_map("nope")
            catalog = json.loads((Path(temp) / "catalog.json").read_text())
            catalog["levels"][0]["file"] = "../escape.json"
            with self.assertRaises(Exception):
                StudioLibrary(Path(temp), catalog).load_map(catalog["levels"][0]["scene"])

    def test_export_safety(self):
        with self.assertRaises(FormatError):
            ex.file_stem("../../x")
        with self.assertRaises(TypeError):
            ex.dump({"data": b"x"})
        with self.assertRaisesRegex(FormatError, "covers"):
            ex._inventory_summary("n64_bt1", src.RomImage("n64_bt1", bytes(16)), [{"start": 0, "end": 8, "status": "decoded"}])


GA_ROM = os.environ.get("BTGA_N64_ROM")
BT1_ROM = os.environ.get("BTGA_BT1_ROM")


@unittest.skipUnless(GA_ROM and Path(GA_ROM).is_file(), "set BTGA_N64_ROM to the Global Assault N64 USA Rev 0 ROM")
class RealGlobalAssaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = src.load_rom(Path(GA_ROM))
        cls.slots = (ROOT / "src/code/frontend/800E8380_slot_asset_ranges.c").read_text()

    def test_every_level_stream_and_scene_decodes(self):
        catalog = src.ga_level_catalog(self.slots)
        streams = {tuple(r) for entry in catalog for ranges in entry["variants"].values() for r in ranges}
        self.assertEqual(len(streams), 74)
        textures, totals = {}, {"rejected": 0, "unknown": 0, "instances": 0}
        for entry in catalog:
            for variant, ranges in entry["variants"].items():
                if not ranges:
                    continue
                level = n64_btga.load_level(self.rom.data, self.slots, entry["level"], variant)
                scene, models = n64_btga.scene_record(self.rom.data, self.rom.sha256, level, textures)
                totals["instances"] += len(scene["instances"])
                totals["rejected"] += sum(len(m["mesh"]["rejected_triangles"]) for m in models.values())
                totals["unknown"] += sum(len(r["unknown_commands"]) for m in models.values() for r in m["references"])
                # Only handler-interpreted kinds carry out-of-range "model index" fields.
                self.assertEqual([u for u in scene["unresolved"] if u["kind"] in n64_btga.MODEL_KINDS], [])
                totals["unresolved"] = totals.get("unresolved", 0) + len(scene["unresolved"])
        self.assertEqual((totals["rejected"], totals["unknown"]), (0, 0))
        self.assertGreater(totals["instances"], 10000)
        statuses = [t.get("status", "decoded") for t in textures.values()]
        # The repository's texture inventory documents these two pool exceptions:
        # a tile sampled larger than its stored payload, and the overlapping
        # texture/state range without tile commands.
        self.assertEqual(sorted(s for s in statuses if s != "decoded"),
                         ["no tile size", "undecodable: 32x32 RGBA32 texels exceed 2048 bytes"])


@unittest.skipUnless(BT1_ROM and Path(BT1_ROM).is_file(), "set BTGA_BT1_ROM to the BattleTanx N64 USA ROM")
class RealBattleTanxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = src.load_rom(Path(BT1_ROM))

    def test_every_world_accounts_for_every_placement(self):
        worlds = 0
        for entry in src.bt1_stream_table(self.rom.data):
            if entry["world"] is None:
                continue
            level = n64_bt1.load_level(self.rom.data, entry["slot"])
            world = level.world
            covered = sum(g["count"] for g in world["groups"] if g["active"] or g["marker_group"])
            self.assertEqual(covered, len(world["placements"]))
            worlds += 1
        self.assertEqual(worlds, 22)

    def test_world_nine_chicago_bonus(self):
        scenarios = src.bt1_scenarios(self.rom.data)
        bonus = [s for s in scenarios if s["mode"] == 7]
        self.assertIn((8, 9, 5), [(s["index"], s["world"], s["bonus_kill_target"]) for s in bonus])
        scene, models = n64_bt1.scene_record(self.rom.data, self.rom.sha256, n64_bt1.load_level(self.rom.data, 9), {})
        generators = sorted(round(m["translation"][0]) for m in scene["markers"] if m["category"] == "tank_generator")
        self.assertEqual(generators, [-1385, 141, 1645])
        self.assertEqual(len(scene["navigation"]["points"]), 115)
        self.assertEqual(len(scene["navigation"]["links"]), 220)
        self.assertTrue(all(0 <= t < 115 for link in scene["navigation"]["links"] for t in link["targets"]))
        self.assertEqual(sum(len(m["mesh"]["rejected_triangles"]) for m in models.values()), 0)

    def test_inventory_covers_rom(self):
        inventory = ex.bt1_inventory(self.rom)
        self.assertEqual(sum(r["end"] - r["start"] for r in inventory["regions"]), 8 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
