import json
import os
import struct
import tempfile
import unittest
from pathlib import Path

from tools import inventory_ps1_assets as inventory
from tools import ps1_assets as ps1


ROOT = Path(__file__).resolve().parents[1]


def tex_record(width, height, mode, pixels, palettes=(), palette_count=None):
    out = struct.pack("<3I", width, height, mode) + pixels
    if palette_count is not None:
        out += struct.pack("<I", palette_count)
    for palette in palettes:
        out += struct.pack(f"<{len(palette)}H", *palette)
    return out


def tex_file(*records, magic=b"3TEX"):
    return magic + struct.pack("<I", len(records)) + b"".join(records)


def palette16(*colors):
    return list(colors) + [0] * (16 - len(colors))


def model_record(vertices, stream_words, radius=0x40, parts=1):
    header = b"CM01" + bytes(0x1E) + struct.pack("<h", radius)
    vertex_block = b"".join(struct.pack("<4h", *vertex) for vertex in vertices)
    stream = struct.pack(f"<{len(stream_words)}I", *stream_words)
    body_size = 0x18 + len(vertex_block) + len(stream)
    body = b"3MDL" + struct.pack("<5I", parts, len(vertices), 0, 0x47F, body_size)
    return header + body + vertex_block + stream


def mmm_file(slots, blobs):
    table = b"".join(struct.pack("<iII", offset, 0, 0) for offset in slots)
    return b"MM01" + struct.pack("<I", len(slots)) + table + b"".join(blobs)


class ReaderTests(unittest.TestCase):
    def test_little_endian_widths(self):
        reader = ps1.Reader(bytes((0x34, 0x12, 0xFF, 0xFF, 0x78, 0x56, 0x34, 0x12)))
        self.assertEqual(reader.u16(0), 0x1234)
        self.assertEqual(reader.s16(2), -1)
        self.assertEqual(reader.u32(4), 0x12345678)

    def test_rejects_out_of_bounds_and_misaligned_reads(self):
        reader = ps1.Reader(bytes(8))
        with self.assertRaisesRegex(ps1.FormatError, "exceeds"):
            reader.u32(8)
        with self.assertRaisesRegex(ps1.FormatError, "exceeds"):
            reader.u16(-2)
        with self.assertRaisesRegex(ps1.FormatError, "misaligned"):
            reader.u32(2)
        with self.assertRaisesRegex(ps1.FormatError, "misaligned"):
            reader.u16(1)


class ColorTests(unittest.TestCase):
    def test_bgr555_channels_and_transparency(self):
        self.assertEqual(ps1.ps1_color_to_rgba(0x001F), bytes((255, 0, 0, 255)))
        self.assertEqual(ps1.ps1_color_to_rgba(0x03E0), bytes((0, 255, 0, 255)))
        self.assertEqual(ps1.ps1_color_to_rgba(0x7C00), bytes((0, 0, 255, 255)))
        self.assertEqual(ps1.ps1_color_to_rgba(0x0000), bytes((0, 0, 0, 0)))
        # STP-flagged black is drawn, so it stays opaque.
        self.assertEqual(ps1.ps1_color_to_rgba(0x8000), bytes((0, 0, 0, 255)))
        self.assertEqual(ps1.ps1_color_to_rgba(0x0421), bytes((8, 8, 8, 255)))


class TexTests(unittest.TestCase):
    def test_4bpp_low_nibble_is_left_texel(self):
        data = tex_file(
            tex_record(8, 1, 0, bytes((0x21, 0x03, 0x00, 0x00)), [palette16(0, 0x001F, 0x03E0, 0x7C00)])
        )
        parsed = ps1.parse_tex(data)
        texture = parsed["textures"][0]
        self.assertEqual(parsed["consumed"], len(data))
        self.assertIsNone(parsed["trailing"])
        self.assertEqual(ps1.decode_indices(data, texture), [1, 2, 3, 0, 0, 0, 0, 0])
        self.assertEqual(
            ps1.texture_rgba(data, texture)[:16],
            bytes((255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255, 0, 0, 0, 0)),
        )

    def test_multiple_records_and_8bpp(self):
        palette = [0] * 256
        palette[7] = 0x001F
        data = tex_file(
            tex_record(2, 2, 1, bytes((7, 7, 0, 7)), [palette]),
            tex_record(1, 2, 2, struct.pack("<2H", 0x7C00, 0x8000)),
        )
        parsed = ps1.parse_tex(data)
        self.assertEqual([t.mode for t in parsed["textures"]], [1, 2])
        self.assertEqual(parsed["consumed"], len(data))
        self.assertEqual(ps1.decode_indices(data, parsed["textures"][1]), [0x7C00, 0x8000])
        self.assertEqual(ps1.max_index(data, parsed["textures"][0]), 7)

    def test_loader_word_truncation_shifts_palette(self):
        palette = [0x1111 * (index % 16) for index in range(256)]
        pixels = bytes((5, 6, 7, 8, 9, 0xAA))  # 6 bytes: loader advances by 4
        data = tex_file(tex_record(3, 2, 1, pixels, [palette]))
        parsed = ps1.parse_tex(data)
        texture = parsed["textures"][0]
        self.assertTrue(texture.misaligned_palette)
        self.assertEqual(texture.palette_offset, 20 + 4)
        self.assertEqual(texture.authored_palette_offset, 20 + 6)
        # The loader's first entry is the last two pixel bytes.
        self.assertEqual(texture.palettes[0][0], 0xAA09)
        self.assertEqual(texture.palettes[0][1:], palette[:255])
        self.assertEqual(texture.authored_palettes[0], palette)
        self.assertEqual(parsed["trailing"]["size"], 2)

    def test_pal_magic_reads_palette_count(self):
        data = tex_file(
            tex_record(8, 1, 0, bytes(4), [palette16(1), palette16(2)], palette_count=2), magic=b"3PAL"
        )
        parsed = ps1.parse_tex(data)
        self.assertEqual(parsed["consumed"], len(data))
        self.assertEqual(len(parsed["textures"][0].palettes), 2)
        self.assertEqual(parsed["textures"][0].palettes[1][0], 2)
        with self.assertRaisesRegex(ps1.FormatError, "zero"):
            ps1.parse_tex(tex_file(tex_record(8, 1, 0, bytes(4), palette_count=0), magic=b"3PAL"))

    def test_rejects_malformed_tex(self):
        good = tex_file(tex_record(8, 1, 0, bytes(4), [palette16()]))
        cases = {
            "magic": b"XTEX" + good[4:],
            "cannot fit": good[:4] + struct.pack("<I", 1000) + good[8:],
            "count": good[:4] + struct.pack("<I", 0) + good[8:],
            "pixel bytes": tex_file(struct.pack("<3I", 64, 64, 0) + bytes(10)),
            "palette": good[:-2],
            "mode": tex_file(tex_record(4, 1, 3, bytes(2))),
            "positive": tex_file(tex_record(0, 1, 0, b"", [palette16()])),
            "VRAM": tex_file(tex_record(2048, 1, 2, bytes(4096))),
            "8-byte header": b"3TEX",
        }
        for reason, data in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ps1.FormatError, reason):
                    ps1.parse_tex(data)

    def test_odd_4bpp_width_uses_loader_storage(self):
        data = tex_file(tex_record(3, 2, 0, bytes((0x21, 0x43)), [palette16()]))
        texture = ps1.parse_tex(data)["textures"][0]
        self.assertEqual(texture.pixel_size, 2)
        self.assertEqual(ps1.decode_indices(data, texture), [1, 2, 0, 3, 4, 0])


class ImgPltTests(unittest.TestCase):
    def img(self, *records):
        return b"3IMG" + struct.pack("<I", len(records)) + b"".join(
            struct.pack("<3I", w, h, mode) + pixels for w, h, mode, pixels in records
        )

    def plt(self, *records):
        out = b"3PLT" + struct.pack("<I", len(records))
        for mode, palettes in records:
            out += struct.pack("<I", mode)
            if mode != 2:
                out += struct.pack("<I", len(palettes))
                for palette in palettes:
                    out += struct.pack(f"<{len(palette)}H", *palette)
        return out

    def test_pairs_by_index_and_keeps_extra_palettes(self):
        data = self.img((8, 1, 0, bytes((0x10, 0, 0, 0))), (2, 1, 2, struct.pack("<2H", 0x7FFF, 0)))
        img = ps1.parse_img(data)
        plt = ps1.parse_plt(self.plt((0, [palette16(0, 0x001F), palette16(0, 0x03E0)]), (2, [])))
        textures = ps1.pair_img_plt(img, plt)
        self.assertEqual(img["consumed"], len(data))
        self.assertEqual(len(textures[0].palettes), 2)
        self.assertEqual(ps1.texture_rgba(data, textures[0])[:8], bytes((0, 0, 0, 0, 255, 0, 0, 255)))
        self.assertEqual(
            ps1.texture_rgba(data, textures[0], textures[0].palettes[1])[4:8], bytes((0, 255, 0, 255))
        )
        self.assertEqual(textures[1].palettes, [])
        self.assertEqual(ps1.texture_rgba(data, textures[1]), bytes((255, 255, 255, 255, 0, 0, 0, 0)))

    def test_img_record_rows_advance_by_truncated_size(self):
        # A 3-byte record advances by 0, so the next header overlaps it.
        data = b"3IMG" + struct.pack("<I", 2) + struct.pack("<3I", 3, 1, 1) + struct.pack("<3I", 4, 1, 1) + bytes(4)
        parsed = ps1.parse_img(data)
        self.assertEqual(parsed["textures"][0].pixel_size, 3)
        self.assertEqual(parsed["textures"][1].header_offset, 8 + 12)
        self.assertEqual(parsed["consumed"], len(data))

    def test_rejects_mismatched_pairs(self):
        img = ps1.parse_img(self.img((8, 1, 0, bytes(4))))
        with self.assertRaisesRegex(ps1.FormatError, "records"):
            ps1.pair_img_plt(img, ps1.parse_plt(self.plt((0, [palette16()]), (0, [palette16()]))))
        with self.assertRaisesRegex(ps1.FormatError, "mode"):
            ps1.pair_img_plt(img, ps1.parse_plt(self.plt((1, [[0] * 256]))))

    def test_rejects_malformed_plt(self):
        with self.assertRaisesRegex(ps1.FormatError, "palette mode"):
            ps1.parse_plt(self.plt((5, [])))
        with self.assertRaisesRegex(ps1.FormatError, "exceed"):
            ps1.parse_plt(self.plt((0, [palette16()]))[:-1])
        with self.assertRaisesRegex(ps1.FormatError, "zero"):
            ps1.parse_plt(b"3PLT" + struct.pack("<3I", 1, 0, 0))
        with self.assertRaisesRegex(ps1.FormatError, "3PLT"):
            ps1.parse_plt(b"3IMG" + bytes(8))


class TttTests(unittest.TestCase):
    def test_lists(self):
        data = b"TT01" + struct.pack("<i", 2) + b"LBTX" + struct.pack("<i2I", 2, 4, 9) + b"LBTX" + struct.pack("<i", 0)
        parsed = ps1.parse_ttt(data)
        self.assertEqual([item["textures"] for item in parsed["lists"]], [[4, 9], []])
        self.assertIsNone(parsed["trailing"])

    def test_rejects_bad_lists(self):
        with self.assertRaisesRegex(ps1.FormatError, "LBTX"):
            ps1.parse_ttt(b"TT01" + struct.pack("<i", 1) + b"XXXX" + struct.pack("<i", 0))
        with self.assertRaisesRegex(ps1.FormatError, "negative"):
            ps1.parse_ttt(b"TT01" + struct.pack("<i", 1) + b"LBTX" + struct.pack("<i", -1))
        with self.assertRaisesRegex(ps1.FormatError, "exceeds"):
            ps1.parse_ttt(b"TT01" + struct.pack("<i", 1) + b"LBTX" + struct.pack("<i", 3) + bytes(4))
        with self.assertRaisesRegex(ps1.FormatError, "cannot fit"):
            ps1.parse_ttt(b"TT01" + struct.pack("<i", 50))


class ModelTests(unittest.TestCase):
    def test_bank_relocates_offsets_after_table(self):
        first = model_record([(1, 2, 3, 0)], [0x58, 0x68], radius=-5)
        second = model_record([], [0x5C, 0x9D, 0x68], parts=3)
        data = mmm_file([0, -1, len(first)], [first, second])
        parsed = ps1.parse_mmm(data)
        self.assertEqual(parsed["table_end"], 8 + 36)
        models = parsed["models"]
        self.assertEqual(models[0]["radius"], -5)
        self.assertEqual(models[0]["vertices"], 1)
        self.assertEqual(models[0]["stream"]["opcodes"], {0x58: 1, 0x68: 1})
        self.assertTrue(models[1]["empty"])
        self.assertEqual(models[2]["parts"], 3)
        self.assertEqual(models[2]["stream"]["words"], 3)

    def test_rejects_malformed_models(self):
        good = model_record([(0, 0, 0, 0)], [0x58, 0x68])
        no_end = model_record([], [0x58, 0x69])
        bad_size = good[:0x24 + 0x14] + struct.pack("<I", 4) + good[0x24 + 0x18 :]
        cases = {
            "outside": mmm_file([4096], [good]),
            "misaligned": mmm_file([2], [bytes(2) + good]),
            "unowned gap": mmm_file([4], [bytes(4) + good]),
            "end with token": mmm_file([0], [no_end]),
            "body size": mmm_file([0], [bad_size]),
            "CM01": mmm_file([0], [b"XX01" + good[4:]]),
            "3MDL": mmm_file([0], [good[:0x24] + b"XMDL" + good[0x28:]]),
            "overrun": mmm_file([0], [good[:0x2C] + struct.pack("<I", 999) + good[0x30:]]),
            "MM01": b"MX01" + bytes(4),
        }
        for reason, data in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ps1.FormatError, reason):
                    ps1.parse_mmm(data)


class FloorAndBinTests(unittest.TestCase):
    def test_flb_grid_and_tail(self):
        cells = [0x1F, 0x80] + [0] * (64 * 64 - 2)
        data = struct.pack("<2H", 64, 256) + struct.pack(f"<{len(cells)}I", *cells) + bytes(8)
        parsed = ps1.parse_flb(data)
        self.assertEqual(parsed["dimension"], 64)
        self.assertEqual(parsed["header_word_2"], 256)
        self.assertEqual(parsed["cells"][:2], [0x1F, 0x80])
        self.assertEqual(parsed["unreachable_tail"]["size"], 8)
        self.assertEqual(parsed["distinct_high_bits"], 2)

    def test_flb_rejects_bad_grids(self):
        with self.assertRaisesRegex(ps1.FormatError, "dimension"):
            ps1.parse_flb(struct.pack("<2H", 32, 32) + bytes(32 * 32 * 4))
        with self.assertRaisesRegex(ps1.FormatError, "need"):
            ps1.parse_flb(struct.pack("<2H", 64, 64) + bytes(100))
        with self.assertRaisesRegex(ps1.FormatError, "whole number"):
            ps1.parse_flb(struct.pack("<2H", 64, 64) + bytes(64 * 64 * 4 + 2))

    def test_flb_cell_color_formula(self):
        self.assertEqual(ps1.flb_cell_color(0), (0, 0, 0))
        self.assertEqual(ps1.flb_cell_color(0x1F), (0xF8, 0xF0, 0xE0))
        self.assertEqual(ps1.flb_cell_color(0x7F), (0xF8, 0xF0, 0xE1))
        self.assertEqual(ps1.flb_cell_color(0xFFFFFF80), (0, 0, 0))

    def level_bin(self, offsets, size):
        data = bytearray(size)
        struct.pack_into("<5I", data, 0, *offsets)
        return data

    def test_level_bin_sections(self):
        data = self.level_bin([0x20, 0x24, 0x34, 0x34 + 24, 0x60], 0x70)
        struct.pack_into("<2H", data, 0x24, 1, 1)
        struct.pack_into("<h", data, 0x24 + 0x14, -7)
        struct.pack_into("<3hHI", data, 0x34 + 12, -2, 3, 0x7FFF, 0x4000, 0x10)
        parsed = ps1.parse_level_bin(bytes(data))
        self.assertEqual((parsed["section1_count"], parsed["section1_first"]), (1, 1))
        self.assertEqual(parsed["section2_records"], 2)
        self.assertEqual(
            parsed["placements"][1], {"position": [-2, 3, 0x7FFF], "word_6": 0x4000, "section3_offset": 0x10}
        )
        self.assertEqual(parsed["section1_reads"][0], {"delta": 0x14, "value": -7, "section": 2})
        self.assertEqual([section["size"] for section in parsed["sections"]], [4, 16, 24, 20, 16])

    def test_level_bin_rejects_bad_headers(self):
        with self.assertRaisesRegex(ps1.FormatError, "outside"):
            ps1.parse_level_bin(bytes(self.level_bin([0x20, 0x24, 0x34, 0x40, 0x900], 0x70)))
        with self.assertRaisesRegex(ps1.FormatError, "ascending"):
            ps1.parse_level_bin(bytes(self.level_bin([0x20, 0x34, 0x24, 0x40, 0x60], 0x70)))
        with self.assertRaisesRegex(ps1.FormatError, "12-byte"):
            ps1.parse_level_bin(bytes(self.level_bin([0x20, 0x24, 0x34, 0x3C, 0x60], 0x70)))
        with self.assertRaisesRegex(ps1.FormatError, "0x20-byte"):
            ps1.parse_level_bin(bytes(16))
        data = self.level_bin([0x20, 0x24, 0x34, 0x40, 0x60], 0x70)
        struct.pack_into("<2H", data, 0x24, 2, 0)
        with self.assertRaisesRegex(ps1.FormatError, "selects records"):
            ps1.parse_level_bin(bytes(data))
        data = self.level_bin([0x20, 0x24, 0x34, 0x40, 0x60], 0x70)
        struct.pack_into("<I", data, 0x34 + 8, 0x10000)
        with self.assertRaisesRegex(ps1.FormatError, "past section 3"):
            ps1.parse_level_bin(bytes(data))


class ExecutableTests(unittest.TestCase):
    def exe(self, names):
        text_address = 0x80010000
        table = 0x100
        strings = b""
        pointers = []
        for name in names:
            pointers.append(text_address + 0x200 + len(strings))
            strings += name.encode() + b"\0\0\0\0"
        text = bytearray(0x400)
        struct.pack_into(f"<{len(pointers)}I", text, table, *pointers)
        text[0x200 : 0x200 + len(strings)] = strings
        header = bytearray(0x800)
        header[:8] = b"PS-X EXE"
        struct.pack_into("<I", header, 0x18, text_address)
        struct.pack_into("<I", header, 0x1C, len(text))
        return bytes(header + text), text_address + table

    def test_level_names(self):
        data, table = self.exe(["SF1", "berlin1", "RUSSIA5M"])
        self.assertEqual(ps1.read_level_names(data, table), ["SF1", "berlin1", "RUSSIA5M"])

    def test_rejects_non_executables(self):
        with self.assertRaisesRegex(ps1.FormatError, "PS-X EXE"):
            ps1.read_level_names(bytes(0x900))
        data, table = self.exe(["SF1"])
        with self.assertRaisesRegex(ps1.FormatError, "outside"):
            ps1.read_level_names(data, 0x90000000)


class PngTests(unittest.TestCase):
    def test_png_is_deterministic(self):
        pixels = bytes((1, 2, 3, 4)) * 6
        first = ps1.encode_png_rgba(3, 2, pixels)
        self.assertEqual(first, ps1.encode_png_rgba(3, 2, pixels))
        self.assertTrue(first.startswith(b"\x89PNG\r\n\x1a\n"))
        with self.assertRaises(ps1.FormatError):
            ps1.encode_png_rgba(3, 3, pixels)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.corpus = self.root / "extracted"
        assets = self.corpus / "ASSETS"
        assets.mkdir(parents=True)
        (assets / "ICON.TEX").write_bytes(tex_file(tex_record(8, 1, 0, bytes((0x10, 0, 0, 0)), [palette16(0, 0x7FFF)])))
        (assets / "BROKEN.TEX").write_bytes(b"3TEX" + struct.pack("<I", 9))
        (assets / "LEVEL.MMM").write_bytes(mmm_file([0], [model_record([], [0x58, 0x68])]))
        (assets / "LEVEL.TTT").write_bytes(b"TT01" + struct.pack("<i", 1) + b"LBTX" + struct.pack("<iI", 1, 5))
        img = b"3IMG" + struct.pack("<I", 1) + struct.pack("<3I", 8, 1, 0) + bytes(4)
        plt = b"3PLT" + struct.pack("<3I", 1, 0, 1) + struct.pack("<16H", *palette16())
        (assets / "LEVEL.IMG").write_bytes(img)
        (assets / "LEVEL.PLT").write_bytes(plt)
        (self.corpus / "MOVIE.STR").write_bytes(b"\x01\x01" + bytes(30))

    def tearDown(self):
        self.temp.cleanup()

    def test_manifest_accounts_for_every_file_and_is_deterministic(self):
        first = inventory.dump_manifest(inventory.build_manifest(self.corpus, self.root / "out1" / "previews"))
        second = inventory.dump_manifest(inventory.build_manifest(self.corpus, self.root / "out2" / "previews"))
        self.assertEqual(first, second)
        manifest = json.loads(first)
        self.assertEqual(manifest["corpus"]["files"], 7)
        self.assertEqual(list(manifest["rejected"]), ["ASSETS/BROKEN.TEX"])
        self.assertIn("cannot fit", manifest["rejected"]["ASSETS/BROKEN.TEX"])
        statuses = {entry["path"]: entry["status"] for entry in manifest["files"]}
        self.assertEqual(statuses["MOVIE.STR"], "inventory-only")
        self.assertEqual(statuses["ASSETS/LEVEL.MMM"], "decoded")
        self.assertEqual(statuses["ASSETS/LEVEL.IMG"], "decoded")
        group = manifest["relationships"][0]
        self.assertEqual(group["errors"], ["1 TTT indices exceed 1 IMG records"])
        self.assertTrue((self.root / "out1/previews/ASSETS/ICON.TEX.000.png").is_file())
        for path in (self.root / "out1").rglob("*.png"):
            self.assertEqual(path.read_bytes(), (self.root / "out2" / path.relative_to(self.root / "out1")).read_bytes())

    def test_source_corpus_is_never_written(self):
        before = {path: path.read_bytes() for path in self.corpus.rglob("*") if path.is_file()}
        inventory.build_manifest(self.corpus, self.root / "out" / "previews")
        after = {path: path.read_bytes() for path in self.corpus.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_output_guard(self):
        with self.assertRaisesRegex(inventory.OutputError, "extracted disc"):
            inventory.check_output_dir(self.corpus / "out", self.corpus)
        with self.assertRaisesRegex(inventory.OutputError, "not git-ignored"):
            inventory.check_output_dir(ROOT / "tools" / "ps1-out", self.corpus)
        inventory.check_output_dir(ROOT / "private-work" / "ps1-out", self.corpus)
        inventory.check_output_dir(self.root / "elsewhere", self.corpus)


@unittest.skipUnless(os.environ.get("BTGA_PS1_EXTRACTED"), "set BTGA_PS1_EXTRACTED to the extracted PS1 disc")
class RealCorpusTests(unittest.TestCase):
    """Run the decoders over the complete local disc; skips when it is absent."""

    @classmethod
    def setUpClass(cls):
        cls.corpus = Path(os.environ["BTGA_PS1_EXTRACTED"])
        if not cls.corpus.is_dir():
            raise unittest.SkipTest(f"{cls.corpus} is not a directory")
        cls.manifest = inventory.build_manifest(cls.corpus)

    def test_every_file_is_accounted_for(self):
        self.assertEqual(self.manifest["corpus"]["files"], 737)
        self.assertEqual(self.manifest["rejected"], {})
        self.assertEqual(self.manifest["corpus"]["disc_listing_mismatches"], [])
        self.assertEqual(
            self.manifest["corpus"]["executable_sha256"],
            "7bc99c17872dc244886ab85033be1596c206b298da450bea8c774851b4ab8f2f",
        )

    def test_family_counts(self):
        families = {name: stats["files"] for name, stats in self.manifest["families"].items()}
        self.assertEqual(families["TEX texture bank"], 256)
        self.assertEqual(families["IMG pixel bank"], 71)
        self.assertEqual(families["PLT palette bank"], 71)
        self.assertEqual(families["MMM model bank"], 76)
        self.assertEqual(families["TTT model texture lists"], 76)
        self.assertEqual(families["FLB floor grid"], 75)
        self.assertEqual(families["level BIN"], 75)

    def test_level_table_and_orphans(self):
        self.assertEqual(len(self.manifest["level_table"]), 70)
        orphans = sorted(
            group["stem"] for group in self.manifest["relationships"] if group["in_level_table"] is False
        )
        self.assertEqual(
            orphans,
            ["ASSETS/ENUMS", "ASSETS/LOND1M", "ASSETS/LOND2M", "ASSETS/LOND3M", "ASSETS/LOND6M", "ASSETS/SF5M"],
        )

    def test_loader_palette_shift_files(self):
        shifted = sorted(
            entry["path"]
            for entry in self.manifest["files"]
            for texture in entry.get("textures", [])
            if "loader_palette_shift_bytes" in texture
        )
        self.assertEqual(shifted, ["ASSETS/GANG/PIECE13.TEX", "ASSETS/GANG/PIECE_7.TEX"])


if __name__ == "__main__":
    unittest.main()
