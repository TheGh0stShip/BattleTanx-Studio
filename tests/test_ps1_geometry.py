import contextlib
import hashlib
import io
import json
import math
import os
import struct
import tempfile
import unittest
from pathlib import Path

from tools import compare_btga_assets, export_ps1_geometry
from tools.btga_assets import compare as cmp
from tools.btga_assets import gltf
from tools.btga_assets import neutral
from tools.btga_assets import ps1_formats as pf
from tools.btga_assets import ps1_model as pm
from tools.btga_assets import scene as sc

ROOT = Path(__file__).resolve().parents[1]
SHA = "ab" * 32


def word(opcode, operand=0, flags=0):
    return (operand << 8) | opcode | flags


def model(vertices, words, footprint=(-4, -4, 4, 4), radius=8, parts=1, pad=None):
    header = bytearray(b"CM01" + bytes(0x20))
    struct.pack_into("<h", header, 4, footprint[0])
    struct.pack_into("<h", header, 8, footprint[1])
    struct.pack_into("<h", header, 0xC, footprint[2])
    struct.pack_into("<h", header, 0x10, footprint[3])
    struct.pack_into("<h", header, 0x22, radius)
    pads = pad or [0] * (len(vertices) - 1) + [0x8000] if vertices else []
    block = b"".join(struct.pack("<3hH", *vertex, pads[index]) for index, vertex in enumerate(vertices))
    stream = struct.pack(f"<{len(words)}I", *words)
    body = b"3MDL" + struct.pack("<5I", parts, len(vertices), 0, 0x47F, 0x18 + len(block) + len(stream))
    return bytes(header) + body + block + stream


def decode(vertices, words, **kwargs):
    data = model(vertices, words, **kwargs)
    return pm.decode_model(data, 0, len(data))


SQUARE = [(0, 0, 0), (10, 0, 0), (10, 0, 10), (0, 0, 10)]
TEXTURED_TRI = [
    word(0x58, 0, 0) | (2 << 24),
    word(0x5C) | (0xC << 28),
    word(0x00, 0x203040),
    word(0x04, 0x506070),
    word(0x08, 0x808080),
    word(0x20, 0x0201),
    word(0x24, 0x0403),
    word(0x28, 0x0605),
    word(0x40, (0 << 12) | 1),
    word(0x38, 2, 1),
    word(0x68),
]


class OpcodeTests(unittest.TestCase):
    def test_every_proven_opcode_is_in_the_table(self):
        self.assertEqual(sorted(pm.OPCODES), list(range(0, 0xA4, 4)))
        self.assertEqual(pm.OPCODES[0x30][1], 0x8007D1D8)
        self.assertEqual(pm.OPCODES[0x54][1], 0x8007D3D0)
        self.assertEqual(pm.OPCODES[0x9C][1], pm.OPCODES[0x1C][1])

    def test_corner_registers(self):
        decoded = decode(SQUARE, TEXTURED_TRI)
        names = [command.name for command in decoded.commands]
        self.assertEqual(names[:3], ["texture_select", "render_mode", "corner_colour"])
        self.assertEqual(decoded.commands[0].operands["texture_slot"], 2)
        self.assertEqual(decoded.commands[1].operands, {"mode": 0xC, "quad": False, "mode_bit1": False, "gouraud": True, "textured": True, "unused_bits_8_27": 0})
        self.assertEqual(decoded.commands[2].operands, {"corner": 0, "rgb": 0x203040})
        self.assertEqual(decoded.commands[5].operands["uv"], [1, 2])
        self.assertEqual(decoded.commands[8].operands, {"corners": [1, 0], "vertices": [1, 0]})
        primitive = decoded.primitives[0]
        self.assertEqual([corner.vertex for corner in primitive.corners[:3]], [0, 1, 2])
        self.assertEqual([corner.uv for corner in primitive.corners[:3]], [(1, 2), (3, 4), (5, 6)])
        self.assertEqual(primitive.gpu_code, 0x34)
        self.assertEqual(primitive.cull, "front_screen_cw")
        self.assertEqual(primitive.missing, [])
        self.assertEqual(decoded.covered_bytes, 4 * len(TEXTURED_TRI))

    def test_all_vertex_pairs(self):
        expected = {0x40: (1, 0), 0x44: (2, 1), 0x48: (3, 2), 0x4C: (2, 0), 0x50: (3, 0), 0x54: (3, 1)}
        for opcode, (low, high) in expected.items():
            with self.subTest(opcode=hex(opcode)):
                decoded = decode(SQUARE, [word(opcode, (3 << 12) | 2), word(0x68)])
                self.assertEqual(decoded.commands[0].operands, {"corners": [low, high], "vertices": [2, 3]})

    def test_single_vertex_corners(self):
        for slot in range(4):
            decoded = decode(SQUARE, [word(0x30 + 4 * slot, 3), word(0x68)])
            self.assertEqual(decoded.commands[0].operands, {"corner": slot, "vertex": 3})

    def test_normal_bit_layout(self):
        self.assertEqual(pm.decode_normal(0x7F7F7F10), (0x7F7F7F10 >> 19, 127 * 32, 127 * 32))
        self.assertEqual(pm.decode_normal(0x80808010), (-4080, -128 * 32, -128 * 32))
        self.assertEqual(pm.decode_normal(0x00000010), (0, 0, 0))
        decoded = decode(SQUARE, [word(0x94, 0x7F0001), word(0x68)])
        self.assertEqual(decoded.commands[0].operands["normal"], [4064, 0, 32])

    def test_uv_side_value_and_unused_bits(self):
        decoded = decode(SQUARE, [0xFFC20180 | 0x0, word(0x68)])
        self.assertEqual(decoded.commands[0].name, "corner_uv_with_side_value")
        self.assertEqual(decoded.commands[0].operands["uv"], [1, 0xC2])
        self.assertEqual(decoded.commands[0].operands["side_value"], 0x3FF)
        self.assertEqual(decoded.commands[0].operands["unused_bits_24_31"], 0xFF)

    def test_misc_opcodes(self):
        decoded = decode(SQUARE, [word(0x60), word(0x74), word(0xA0, 0x0300), word(0x68)])
        self.assertEqual([command.name for command in decoded.commands], ["no_operation", "no_operation", "depth_mode", "end"])
        self.assertEqual(decoded.commands[2].operands["depth_mode"], 3)

    def test_render_modes_map_to_gpu_codes(self):
        codes = {}
        for mode in range(16):
            decoded = decode(SQUARE, [word(0x5C) | (mode << 28), word(0x00, 1), word(0x04, 1), word(0x08, 1), word(0x0C, 1), word(0x20), word(0x24), word(0x28), word(0x2C), word(0x58), word(0x40, 1), word(0x48, (2 << 12) | 3, 3), word(0x68)])
            codes[mode] = decoded.primitives[0].gpu_code
        # Emitter pointers at 0x8007CAA8: CAE8 CC58 CAE8 CC58 CB98 CD24 CB98 CD24
        # CB2C CCA4 CB2C CCA4 CBE4 CD7C CBE4 CD7C, writing these GPU codes.
        self.assertEqual(
            [codes[mode] for mode in range(16)],
            [0x20, 0x28, 0x20, 0x28, 0x30, 0x38, 0x30, 0x38, 0x24, 0x2C, 0x24, 0x2C, 0x34, 0x3C, 0x34, 0x3C],
        )

    def test_emit_flags_and_sticky_state(self):
        words = [word(0x5C) | (4 << 28), word(0x00, 1), word(0x04, 2), word(0x08, 3), word(0x40, 1), word(0x38, 2, 1), word(0x38, 3, 2), word(0x00, 9, 3), word(0x68)]
        decoded = decode(SQUARE, words)
        self.assertEqual([p.cull for p in decoded.primitives], ["front_screen_cw", "front_screen_ccw", "double_sided"])
        self.assertEqual(decoded.primitives[1].corners[2].vertex, 3)
        self.assertEqual(decoded.primitives[1].corners[0].colour, 1)
        self.assertEqual(decoded.primitives[2].corners[0].colour, 9)
        self.assertEqual(decoded.primitives[0].corners[0].normal, (0, 0, 0x1000))

    def test_block_and_end_opcodes_never_emit(self):
        decoded = decode(SQUARE, [word(0x64, 12, 3), 1, word(0x60), word(0x68, 0, 3)])
        self.assertEqual(len(decoded.primitives), 0)

    def test_missing_state_is_reported_not_invented(self):
        decoded = decode(SQUARE, [word(0x60, 0, 1), word(0x5C) | (0xD << 28), word(0x30, 0, 1), word(0x68)])
        self.assertEqual(decoded.primitives[0].missing[0], "mode")
        self.assertIn("corner1.vertex", decoded.primitives[1].missing)
        self.assertIn("corner3.uv", decoded.primitives[1].missing)
        self.assertIn("texture_slot", decoded.primitives[1].missing)

    def test_visibility_blocks(self):
        words = [word(0x70, 7 * 4 + 8), 1, 2, 3, 4, 5, 6, word(0x60), word(0x60), word(0x6C, 6 * 4 + 4), 9, 0x11, 0x22, 0x33, 0x44, word(0x60, 0, 0), word(0x64, 12), 7, word(0x60), word(0x68)]
        decoded = decode(SQUARE, words)
        block = decoded.commands[0]
        self.assertEqual(block.operands["masks"], [1, 2])
        self.assertEqual(block.operands["unknown_words"], [3, 4, 5, 6])
        self.assertEqual(decoded.commands[1].block_path, (block.offset,))
        self.assertEqual(decoded.commands[3].operands["masks"], [9])
        self.assertEqual(decoded.commands[3].operands["unknown_words"], [0x11, 0x22, 0x33, 0x44])
        self.assertEqual(decoded.commands[4].block_path, (decoded.commands[3].offset,))
        self.assertEqual(decoded.covered_bytes, 4 * len(words))

    def test_end_inside_block_resumes_after_block(self):
        words = [word(0x64, 16), 1, word(0x68), word(0x60), word(0x5C), word(0x68)]
        decoded = decode(SQUARE, words)
        self.assertTrue(decoded.commands[1].operands["conditional"])
        self.assertEqual([command.name for command in decoded.commands][-2:], ["render_mode", "end"])
        # The no-op after the conditional END is skipped on the not-taken path.
        self.assertEqual(decoded.covered_bytes, 4 * len(words) - 4)

    def test_parts_follow_pad_bit(self):
        decoded = decode(SQUARE, [word(0x68)], parts=2, pad=[0, 0x8000, 0, 0x8000])
        self.assertEqual(decoded.parts, [(0, 2), (2, 4)])
        self.assertTrue(decoded.part_markers_match)
        decoded = decode(SQUARE, [word(0x68)], parts=3, pad=[0, 0x8000, 0, 0x8000])
        self.assertFalse(decoded.part_markers_match)


class MalformedModelTests(unittest.TestCase):
    def assert_rejected(self, data, reason):
        with self.assertRaisesRegex(pf.FormatError, reason):
            pm.decode_model(data, 0, len(data))

    def test_impossible_streams(self):
        cases = {
            "outside the dispatch table": [word(0xA4), word(0x68)],
            "without opcode 0x68": [word(0x60)],
            "uses vertex 4": [word(0x30, 4), word(0x68)],
            "uses vertex 4095": [word(0x40, 0xFFF), word(0x68)],
            "header at .* is truncated": [word(0x70, 0x40), 1],
            "impossible length": [word(0x64, 6), 0, word(0x68)],
            "impossible length 0xffffff": [word(0x64, 0xFFFFFF), 0, word(0x68)],
            "impossible length 0x4": [word(0x64, 4), 0, word(0x68)],
        }
        for reason, words in cases.items():
            with self.subTest(reason=reason):
                self.assert_rejected(model(SQUARE, words), reason)

    def test_header_validation(self):
        good = model(SQUARE, [word(0x68)])
        self.assert_rejected(b"XM01" + good[4:], "CM01")
        self.assert_rejected(good[:0x24] + b"XMDL" + good[0x28:], "3MDL")
        bad_size = bytearray(good)
        struct.pack_into("<I", bad_size, 0x24 + 0x14, 4)
        self.assert_rejected(bytes(bad_size), "body size")
        huge = bytearray(good)
        struct.pack_into("<I", huge, 0x24 + 8, 0xFFFFFFFF)
        self.assert_rejected(bytes(huge), "overrun")
        with self.assertRaisesRegex(pf.FormatError, "past the file"):
            pm.decode_model(good, 4, len(good))

    def test_truncation_at_every_boundary(self):
        good = model(SQUARE, TEXTURED_TRI)
        for cut in range(0, len(good), 2):
            with self.subTest(cut=cut):
                data = bytearray(good[:cut])
                if len(data) >= 0x3C:
                    struct.pack_into("<I", data, 0x24 + 0x14, len(data) - 0x24)
                with self.assertRaises(pf.FormatError):
                    pm.decode_model(bytes(data), 0, len(data))


class NeutralTests(unittest.TestCase):
    def test_stable_ids(self):
        first = neutral.stable_id("ps1", SHA, "ASSETS/BERLIN1.MMM", "model", 3)
        self.assertEqual(first, neutral.stable_id("ps1", SHA, "ASSETS/BERLIN1.MMM", "model", 3))
        self.assertTrue(first.startswith("ps1:model:"))
        others = {
            neutral.stable_id("ps1", SHA, "ASSETS/BERLIN1.MMM", "model", 4),
            neutral.stable_id("ps1", SHA, "ASSETS/BERLIN2.MMM", "model", 3),
            neutral.stable_id("ps1", "cd" * 32, "ASSETS/BERLIN1.MMM", "model", 3),
            neutral.stable_id("ps1", SHA, "ASSETS/BERLIN1.MMM", "texture", 3),
            neutral.stable_id("n64_btga", SHA, "ASSETS/BERLIN1.MMM", "model", 3),
        }
        self.assertNotIn(first, others)
        self.assertEqual(len(others), 5)
        for bad in (("pc", SHA, "a", "model", 0), ("ps1", "XYZ", "a", "model", 0), ("ps1", SHA, "a", "Model", 0), ("ps1", SHA, "a", "model", -1), ("ps1", SHA, "a", "model", True)):
            with self.assertRaises(neutral.NeutralError):
                neutral.stable_id(*bad)

    def test_path_safety(self):
        self.assertEqual(neutral.safe_source_path("PS1 BTGA/ASSETS/BERLIN 1.MMM"), "PS1 BTGA/ASSETS/BERLIN 1.MMM")
        for bad in ("../ASSETS/X.MMM", "ASSETS/../../etc", "/etc/passwd", "C:/Windows", "ASSETS\\X", "ASSETS//X", "A/./B", "a\x00b", "a\nb", "", "a/<b>"):
            with self.subTest(path=bad):
                with self.assertRaises(neutral.NeutralError):
                    neutral.safe_source_path(bad)

    def test_triangulation_and_winding(self):
        quad = {"quad": True, "cull": "front_screen_cw"}
        self.assertEqual(neutral.triangle_corner_order(quad), [(0, 1, 3), (1, 2, 3)])
        self.assertEqual(neutral.triangle_corner_order(dict(quad, cull="front_screen_ccw")), [(0, 3, 1), (1, 3, 2)])
        self.assertEqual(neutral.triangle_corner_order(dict(quad, cull="double_sided")), [(0, 1, 3), (1, 2, 3)])
        self.assertEqual(neutral.triangle_corner_order({"quad": False, "cull": "front_screen_ccw"}), [(0, 2, 1)])

    def test_record_is_lossless_and_stable(self):
        decoded = decode(SQUARE, TEXTURED_TRI)
        record = neutral.ps1_model_record(decoded, path="ASSETS/T 1.MMM", file_sha256=SHA, bank_index=0, texture_list=[5, 6, 7])
        again = neutral.ps1_model_record(decode(SQUARE, TEXTURED_TRI), path="ASSETS/T 1.MMM", file_sha256=SHA, bank_index=0, texture_list=[5, 6, 7])
        self.assertEqual(json.dumps(record, sort_keys=True), json.dumps(again, sort_keys=True))
        self.assertEqual([int(c["word"], 16) for c in record["commands"]], TEXTURED_TRI)
        self.assertEqual(record["schema_version"], neutral.SCHEMA_VERSION)
        self.assertEqual(record["bounds"], {"min": [0, 0, 0], "max": [10, 0, 10]})
        with self.assertRaises(neutral.NeutralError):
            neutral.ps1_model_record(decoded, path="../x.MMM", file_sha256=SHA, bank_index=0, texture_list=None)


def texture(width=8, height=8, transparent=False):
    rgba = bytes((200, 100, 50, 0 if transparent else 255)) * (width * height)
    return {
        "key": "ps1:texture:fixture",
        "name": "fixture",
        "png": pf.encode_png_rgba(width, height, rgba),
        "width": width,
        "height": height,
        "has_transparent_texels": transparent,
        "metadata": {"record": 0},
    }


class GltfTests(unittest.TestCase):
    def record(self, words=TEXTURED_TRI):
        return neutral.ps1_model_record(decode(SQUARE, words), path="ASSETS/T.MMM", file_sha256=SHA, bank_index=0, texture_list=[0, 0, 0])

    def test_model_glb_is_deterministic_and_valid(self):
        record = self.record()
        first = gltf.model_glb(record, {2: texture()})
        self.assertEqual(first, gltf.model_glb(self.record(), {2: texture()}))
        document, binary = gltf.read_glb(first)
        self.assertEqual(document["asset"]["version"], "2.0")
        primitive = document["meshes"][0]["primitives"][0]
        position = document["accessors"][primitive["attributes"]["POSITION"]]
        self.assertEqual((position["count"], position["min"], position["max"]), (3, [0.0, 0.0, 0.0], [10.0, 0.0, 10.0]))
        uv = document["accessors"][primitive["attributes"]["TEXCOORD_0"]]
        view = document["bufferViews"][uv["bufferView"]]
        self.assertEqual(struct.unpack_from("<6f", binary, view["byteOffset"]), (1 / 8, 2 / 8, 3 / 8, 4 / 8, 5 / 8, 6 / 8))
        colour = document["accessors"][primitive["attributes"]["COLOR_0"]]
        view = document["bufferViews"][colour["bufferView"]]
        self.assertAlmostEqual(struct.unpack_from("<f", binary, view["byteOffset"])[0], 0x40 / 128)
        self.assertEqual(struct.unpack_from("<3f", binary, view["byteOffset"] + 24), (1.0, 1.0, 1.0))
        self.assertEqual(document["meshes"][0]["extras"]["source"]["file"], "ASSETS/T.MMM")
        self.assertEqual(len(document["images"]), 1)
        self.assertFalse(document["materials"][0]["doubleSided"])
        self.assertNotIn("alphaMode", document["materials"][0])

    def test_winding_and_double_sided_materials(self):
        words = TEXTURED_TRI[:-2] + [word(0x40, 1), word(0x38, 2, 2), word(0x38, 2, 3), word(0x68)]
        document, binary = gltf.read_glb(gltf.model_glb(self.record(words), {2: texture(transparent=True)}))
        primitives = document["meshes"][0]["primitives"]
        self.assertEqual(sorted(document["materials"][p["material"]]["doubleSided"] for p in primitives), [False, True])
        self.assertEqual(document["materials"][0]["alphaMode"], "MASK")
        single = next(p for p in primitives if not document["materials"][p["material"]]["doubleSided"])
        view = document["bufferViews"][document["accessors"][single["attributes"]["POSITION"]]["bufferView"]]
        # cull flag 2 (front when screen-counter-clockwise) reverses to c0, c2, c1
        self.assertEqual(struct.unpack_from("<9f", binary, view["byteOffset"]), (0, 0, 0, 10, 0, 10, 10, 0, 0))

    def test_unresolved_texture_is_reported(self):
        document, _ = gltf.read_glb(gltf.model_glb(self.record(), {}))
        self.assertEqual(document["meshes"][0]["extras"]["unresolved_primitives"], [{"primitive": 0, "missing": ["texture"]}])
        self.assertNotIn("TEXCOORD_0", document["meshes"][0]["primitives"][0]["attributes"])

    def test_yaw_quaternion_matches_executable_matrix(self):
        for units in (0, 0x1000, 0x4000, 0x8000, 0xC000, 0x2345):
            x, y, z, w = gltf.yaw_quaternion(units)
            theta = units * 2 * math.pi / 65536
            # rotate (1, 0, 0): the 0x800249A0 matrix maps it to (c, 0, -s)
            rotated = (1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w))
            self.assertAlmostEqual(rotated[0], math.cos(theta))
            self.assertAlmostEqual(rotated[2], -math.sin(theta))

    def test_read_glb_rejects_damage(self):
        good = gltf.model_glb(self.record(), {2: texture()})
        for bad in (good[:10], b"glTX" + good[4:], good[:-4]):
            with self.assertRaises(gltf.GltfError):
                gltf.read_glb(bad)


def write_level(root: Path, stem="ASSETS/LEVEL ONE", *, with_img=True, kinds=((0, 0), (1, 5), (3, 0), (0, 0xFFFF))):
    path = root / stem
    path.parent.mkdir(parents=True, exist_ok=True)
    first = model(SQUARE, TEXTURED_TRI)
    (root / f"{stem}.MMM").write_bytes(b"MM01" + struct.pack("<I", 1) + struct.pack("<iII", 0, 0, 0) + first)
    (root / f"{stem}.TTT").write_bytes(b"TT01" + struct.pack("<i", 1) + b"LBTX" + struct.pack("<i3I", 3, 0, 0, 0))
    if with_img:
        (root / f"{stem}.IMG").write_bytes(b"3IMG" + struct.pack("<I", 1) + struct.pack("<3I", 8, 8, 0) + bytes(32))
        (root / f"{stem}.PLT").write_bytes(b"3PLT" + struct.pack("<3I", 1, 0, 1) + struct.pack("<16H", 0, *([0x7FFF] * 15)))
    section3 = b"".join(struct.pack("<BBH4x", kind, 0, model_index) for kind, model_index in kinds)
    count = len(kinds)
    placements = b"".join(struct.pack("<3hHI", index, -index, 2 * index, 0x4000 * index & 0xFFFF, 8 * index) for index in range(count))
    offsets = [0x20, 0x24, 0x34, 0x34 + len(placements), 0x34 + len(placements) + len(section3)]
    data = struct.pack("<5I", *offsets) + bytes(12) + bytes(4) + struct.pack("<2H", count, 0) + bytes(12) + placements + section3
    (root / f"{stem}.BIN").write_bytes(data)
    (root / f"{stem}.FLB").write_bytes(struct.pack("<2H", 64, 64) + bytes(64 * 64 * 4))
    return stem


class SceneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "disc with spaces"

    def tearDown(self):
        self.temp.cleanup()

    def test_scene_resolution(self):
        stem = write_level(self.root)
        scene, models, textures = sc.assemble_scene(sc.LevelSet(self.root, stem))
        statuses = [p["status"] for p in scene["placements"]]
        self.assertEqual(statuses[0], "resolved")
        self.assertIn("outside the 1-model bank", statuses[1])
        self.assertEqual(statuses[2], "kind handled by dedicated code; model reference not decoded")
        self.assertEqual(statuses[3], "no model")
        self.assertEqual(scene["placements"][1]["translation"], [4, -4, 8])
        self.assertEqual(scene["placements"][1]["yaw_turns"], 0.25)
        self.assertEqual(list(models), [0])
        self.assertIsNotNone(textures[0][2])
        self.assertEqual(len(scene["dependency_problems"]), 1)
        self.assertEqual(scene["floor"]["dimension"], 64)
        self.assertEqual(scene["floor"]["cell_size"], 512)

    def test_missing_dependencies_are_reported(self):
        stem = write_level(self.root, with_img=False)
        scene, models, textures = sc.assemble_scene(sc.LevelSet(self.root, stem))
        self.assertTrue(any("no IMG/PLT pair" in problem for problem in scene["dependency_problems"]))
        self.assertIsNone(textures[0].get(2))
        (self.root / f"{stem}.BIN").unlink()
        with self.assertRaisesRegex(sc.SceneError, "missing .BIN"):
            sc.assemble_scene(sc.LevelSet(self.root, stem))
        with self.assertRaises(neutral.NeutralError):
            sc.LevelSet(self.root, "../outside")

    def test_bad_texture_slot_is_reported(self):
        stem = write_level(self.root)
        ttt = self.root / f"{stem}.TTT"
        ttt.write_bytes(b"TT01" + struct.pack("<i", 1) + b"LBTX" + struct.pack("<iI", 1, 0))
        scene, _, textures = sc.assemble_scene(sc.LevelSet(self.root, stem))
        self.assertTrue(any("texture slot 2 beyond" in problem for problem in scene["dependency_problems"]))
        self.assertIsNone(textures[0][2])

    def test_export_cli_is_deterministic_and_guarded(self):
        stem = write_level(self.root)
        outputs = []
        for run in ("a", "b"):
            out = Path(self.temp.name) / f"out {run}"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(export_ps1_geometry.main([str(self.root), str(out), "--models", stem, "--scene", stem]), 0)
            outputs.append({p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()})
        self.assertEqual(outputs[0], outputs[1])
        self.assertTrue(any(name.endswith(".glb") for name in outputs[0]))
        for name in outputs[0]:
            self.assertNotIn("..", name)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            export_ps1_geometry.main([str(self.root), str(self.root / "inside"), "--models", stem])


class CompareTests(unittest.TestCase):
    def tex(self, ident, rgba, width=8, height=8, platform="ps1"):
        fingerprints = {"width": width, "height": height, "rgba_sha256": hashlib.sha256(rgba).hexdigest(), "dhash": cmp.dhash(width, height, rgba)}
        return cmp.asset(platform, "texture", ident, ident, {"file": "f"}, fingerprints)

    def gradient(self, shift=0):
        return bytes(v for y in range(8) for x in range(8) for v in (((x * 37 + y * 91) * 7 + shift) % 256, (x * y * 13) % 256, 0, 255))

    def test_png_round_trip_and_dhash(self):
        rgba = self.gradient()
        self.assertEqual(cmp.png_rgba(pf.encode_png_rgba(8, 8, rgba)), (8, 8, rgba))
        self.assertEqual(cmp.dhash(8, 8, rgba), cmp.dhash(8, 8, rgba))
        self.assertNotEqual(cmp.dhash(8, 8, rgba), 0)
        with self.assertRaises(cmp.CompareError):
            cmp.png_rgba(b"not a png")
        with self.assertRaises(cmp.CompareError):
            cmp.dhash(8, 8, rgba[:-1])

    def test_texture_matching_is_candidate_only(self):
        a = self.tex("ps1:texture:a", self.gradient())
        b = self.tex("n64_btga:texture:b", self.gradient(), platform="n64_btga")
        c = self.tex("n64_btga:texture:c", self.gradient(4), platform="n64_btga")
        d = self.tex("n64_btga:texture:d", bytes(255 - v if i % 4 != 3 else 255 for i, v in enumerate(self.gradient())), platform="n64_btga")
        links = cmp.match_textures([a], [b, c, d])
        by_target = {(link["a"] if link["b"] == a["id"] else link["b"]): link for link in links}
        self.assertEqual(by_target["n64_btga:texture:b"]["evidence"][0]["type"], "rgba_sha256")
        self.assertIn("n64_btga:texture:c", by_target)
        self.assertNotIn("n64_btga:texture:d", by_target)
        self.assertTrue(all(link["status"] == "candidate" for link in links))
        self.assertGreater(by_target["n64_btga:texture:b"]["confidence"], by_target["n64_btga:texture:c"]["confidence"])
        wide = bytes(value for pixel in range(64) for value in self.gradient()[pixel * 4 : pixel * 4 + 4] * 2)
        stretched = self.tex("n64_btga:texture:e", wide, width=16, platform="n64_btga")
        self.assertLessEqual((stretched["fingerprints"]["dhash"] ^ a["fingerprints"]["dhash"]).bit_count(), 6)
        self.assertEqual(cmp.match_textures([a], [stretched]), [])

    def test_document_rules(self):
        a = cmp.asset("ps1", "name", "ps1:name:a", "RUSSIA1", {"file": "SLUS_010.44"}, {})
        b = cmp.asset("n64_btga", "name", "n64_btga:name:b", "russia 1", {"file": "rom"}, {})
        links = cmp.match_names([a], [b])
        self.assertEqual(links[0]["evidence"][0]["detail"], "russia 1")
        with self.assertRaisesRegex(cmp.CompareError, "proof"):
            cmp.confirm(links[0], " ")
        forged = dict(links[0], status="confirmed")
        with self.assertRaisesRegex(cmp.CompareError, "without proof"):
            cmp.document([a, b], [forged], {})
        with self.assertRaisesRegex(cmp.CompareError, "duplicate"):
            cmp.document([a, a], [], {})
        with self.assertRaisesRegex(cmp.CompareError, "unknown asset"):
            cmp.document([a], links, {})
        confirmed = cmp.confirm(links[0], "byte-identical after decode")
        self.assertEqual(cmp.document([a, b], [confirmed], {})["links"][0]["status"], "confirmed")
        with self.assertRaises(cmp.CompareError):
            cmp.asset("dreamcast", "name", "x", "x", {}, {})
        with self.assertRaises(neutral.NeutralError):
            cmp.asset("ps1", "name", "x", "x", {"file": "../../x"}, {})

    def test_names_and_references(self):
        self.assertEqual(cmp.normalise_name("BERLIN1M"), "berlin 1 m")
        self.assertEqual(cmp.normalise_name("assets\\gang\\dark_ang"), "assets gang dark ang")
        strings = [(0, "JOURNEY ACROSS RUSSIA."), (9, "RUSSIANS"), (20, "Skull Riderz")]
        self.assertEqual(cmp.references(strings, "russia"), [{"offset": 0, "text": "JOURNEY ACROSS RUSSIA."}])
        self.assertEqual(len(cmp.references(strings, "skull_riderz")), 1)

    def test_duplicate_models(self):
        fingerprint = {"geometry_sha256": "1" * 64, "vertex_count": 9, "triangle_count": 4, "bounds_size": [1, 1, 1]}
        models = [cmp.asset("ps1", "model", f"ps1:model:{i}", str(i), {}, dict(fingerprint)) for i in range(3)]
        links = cmp.duplicate_models(models)
        self.assertEqual([(l["a"], l["b"]) for l in links], [("ps1:model:0", "ps1:model:1"), ("ps1:model:0", "ps1:model:2")])
        self.assertEqual(cmp.match_models(models[:1], models[:1]), [])


def write_executable(path: Path, names: list[str]) -> None:
    base = 0x80010000
    table = pf.LEVEL_NAME_TABLE - base
    text = bytearray(table + 0x400)
    strings = table + 0x100
    for index, name in enumerate(names):
        struct.pack_into("<I", text, table + 4 * index, base + strings)
        text[strings : strings + len(name)] = name.encode()
        strings += len(name) + 4
    text += b"JOURNEY ACROSS RUSSIA.\0"
    header = bytearray(0x800)
    header[:8] = b"PS-X EXE"
    struct.pack_into("<2I", header, 0x18, base, len(text))
    path.write_bytes(bytes(header) + bytes(text))


class CompareCliTests(unittest.TestCase):
    def test_build_without_roms(self):
        with tempfile.TemporaryDirectory() as temp:
            corpus = Path(temp) / "extracted"
            write_level(corpus, "ASSETS/RUSSIA1")
            (corpus / "ASSETS/GANG").mkdir()
            (corpus / "ASSETS/GANG/GRIFFIN.TEX").write_bytes(b"3TEX" + struct.pack("<I", 1) + struct.pack("<3I", 8, 1, 0) + bytes(4) + bytes(32))
            write_executable(corpus / "SLUS_010.44", ["RUSSIA1", "SF1"])
            document = compare_btga_assets.build(corpus, Path(temp) / "work", None, None)
            titles = [entry["title"] for entry in document["catalog"]]
            self.assertIn("Russia campaign and multiplayer maps", titles)
            russia = document["catalog"][titles.index("Russia campaign and multiplayer maps")]
            self.assertEqual(russia["assets"], ["RUSSIA1"])
            self.assertIn("'ps1': 2", " ".join(russia["evidence"]))
            self.assertIn("JOURNEY ACROSS RUSSIA.", " ".join(russia["evidence"]))
            self.assertIn("ROM not supplied", russia["evidence"][-1])
            self.assertEqual(compare_btga_assets.level_title_regions([(0, "PARIS - BISTRO"), (9, "Paris - x"), (20, "RUSSIA")]), ["PARIS"])
            self.assertTrue(all(link["status"] == "candidate" for link in document["links"]))
            self.assertEqual({asset["kind"] for asset in document["assets"]}, {"texture", "model", "name"})
            self.assertIsNone(document["sources"]["n64_btga_rom_sha256"])


CORPUS = os.environ.get("BTGA_PS1_EXTRACTED")


@unittest.skipUnless(CORPUS, "set BTGA_PS1_EXTRACTED to the extracted PS1 disc")
class RealDiscGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = Path(CORPUS)
        if not cls.corpus.is_dir():
            raise unittest.SkipTest("extracted disc missing")

    def test_every_model_decodes_with_every_byte_accounted_for(self):
        totals = {"models": 0, "primitives": 0, "missing": 0, "bad_slots": 0}
        for path in sorted(self.corpus.rglob("*.MMM")):
            ttt = pf.parse_ttt(path.with_suffix(".TTT").read_bytes())
            img = path.with_suffix(".IMG")
            img_count = pf.parse_img(img.read_bytes())["count"] if img.is_file() else None
            for index, decoded in pm.decode_bank(path.read_bytes(), path.name):
                totals["models"] += 1
                self.assertEqual(decoded.covered_bytes, decoded.stream_end - decoded.stream_offset)
                self.assertEqual(decoded.unreached, [])
                texture_list = ttt["lists"][index]["textures"]
                for primitive in decoded.primitives:
                    totals["primitives"] += 1
                    totals["missing"] += bool(primitive.missing)
                    if primitive.textured:
                        if primitive.texture_slot >= len(texture_list):
                            totals["bad_slots"] += 1
                        elif img_count is not None:
                            self.assertLess(texture_list[primitive.texture_slot], img_count)
        self.assertEqual(totals, {"models": 4761, "primitives": 115599, "missing": 0, "bad_slots": 0})

    def test_representative_export_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            runs = []
            for run in ("one", "two"):
                out = Path(temp) / run
                args = [str(self.corpus), str(out), "--models", "ASSETS/ENUMS", "--models", "ASSETS/RUSSIA3", "--scene", "ASSETS/BERLIN1", "--scene", "ASSETS/RUSSIA3"]
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(export_ps1_geometry.main(args), 0)
                runs.append({p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()})
            self.assertEqual(runs[0], runs[1])
            report = json.loads(runs[0]["export_report.json"])
            expected = sum(
                pf.parse_mmm((self.corpus / f"ASSETS/{stem}.MMM").read_bytes())["count"] for stem in ("ENUMS", "RUSSIA3")
            )
            self.assertEqual(sum(item["models"] for item in report["models"]), expected)
            for item in report["models"]:
                self.assertEqual(item["problems"], [])

    def test_every_level_scene_resolves_or_explains(self):
        statuses = {}
        problems = []
        for path in sorted(self.corpus.glob("ASSETS/*.BIN")):
            if not path.with_suffix(".MMM").is_file():
                continue
            scene, _, _ = sc.assemble_scene(sc.LevelSet(self.corpus, f"ASSETS/{path.stem}"))
            for status, count in scene["placement_status"].items():
                key = status if not status.startswith("model index") else "model index out of range"
                statuses[key] = statuses.get(key, 0) + count
            problems += scene["dependency_problems"]
        self.assertEqual(sum(statuses.values()), 30758)
        self.assertEqual(statuses["resolved"], 20703)
        # 25 model-kind records hold 0xFFFF at +2; every kind-12 record (read at +4 by
        # 0x80025768) holds 0xFFFF, which the handler skips at 0x80025770.
        self.assertEqual(statuses["no model"], 225)
        self.assertEqual(statuses["model index out of range"], 2)
        unexplained = [p for p in problems if "no IMG/PLT pair" not in p and "outside the" not in p]
        self.assertEqual(unexplained, [])


if __name__ == "__main__":
    unittest.main()
