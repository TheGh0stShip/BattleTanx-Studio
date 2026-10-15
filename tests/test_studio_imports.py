"""Importing outside resources: pictures, OBJ/glTF models, the user library, sky panoramas, teleporters,
OpenStreetMap cities and computed skylines.  Synthetic data only (no network, no game data)."""

from __future__ import annotations

import base64
import json
import math
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_studio_authoring import starter_library  # noqa: E402
from tools.btga_assets import gltf  # noqa: E402
from tools.btga_assets.ps1_formats import encode_png_rgba  # noqa: E402
from tools.btga_studio.core import geo, imaging, imports, osm, package, sky, skyline  # noqa: E402
from tools.btga_studio.core import project as projects  # noqa: E402
from tools.btga_studio.core.library import LibrarySet  # noqa: E402

# 16x16 synthetic gradient (red = 16x, green = 16y, blue = 128), baseline 4:2:0 with a restart interval of one MCU (cjpeg -quality 90).
JPEG_420 = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkFBQkUDQsN"
    "FBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCAAQABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgED"
    "AwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKT"
    "lJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcF"
    "BAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaX"
    "mJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/90ABAAB/9oADAMBAAIRAxEAPwD5V8KfCn7n7n9K9m8J/Cn7n7n9K9i8J/Cn7n7n9K9m8KfC"
    "n7n7n9K6854w394nw249+D3+x//Z")
JPEG_REFERENCE = {(0, 0): (0, 3, 120), (8, 8): (134, 126, 123), (15, 15): (241, 237, 138), (3, 12): (202, 42, 130)}  # djpeg -dct float, (row, column)


def _png(width, height, rows: bytes, colour: int, depth: int = 8, extra: bytes = b"", interlace: int = 0) -> bytes:
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0, interlace)) + extra
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class ImageTests(unittest.TestCase):
    def test_png_layouts(self):
        rgba = bytes(range(64))
        self.assertEqual(imaging.decode(encode_png_rgba(4, 4, rgba)), (4, 4, rgba))
        palette = b"\x00\x00\x00" + b"\xff\x00\x00" + b"\x00\xff\x00"
        rows = b"".join(b"\x00" + bytes([0x12, 0x00]) for _ in range(2))  # 2-bit indices 0,1,0,2 per row
        data = _png(4, 2, rows, 3, 2, extra=struct.pack(">I", 9) + b"PLTE" + palette + struct.pack(">I", zlib.crc32(b"PLTE" + palette))
                    + struct.pack(">I", 1) + b"tRNS\x80" + struct.pack(">I", zlib.crc32(b"tRNS\x80")))
        width, height, pixels = imaging.decode(data)
        self.assertEqual((width, height), (4, 2))
        self.assertEqual(pixels[:16], bytes([0, 0, 0, 128, 255, 0, 0, 255, 0, 0, 0, 128, 0, 255, 0, 255]))
        palette_two = b"\x00\x00\x00" + b"\xff\x00\x00"
        bad = _png(1, 1, b"\x00\x05", 3, 8, extra=struct.pack(">I", 6) + b"PLTE" + palette_two + struct.pack(">I", zlib.crc32(b"PLTE" + palette_two)))
        with self.assertRaisesRegex(imaging.ImageError, "outside its palette"):
            imaging.decode(bad)
        grey16 = b"".join(b"\x01" + struct.pack(">HH", 0x1234, 0x0101) for _ in range(2))  # filter 1 (sub) on 16-bit grey
        _, _, pixels = imaging.decode(_png(2, 2, grey16, 0, 16))
        self.assertEqual(pixels[:8], bytes([0x12, 0x12, 0x12, 255, 0x13, 0x13, 0x13, 255]))

    def test_png_paeth_and_interlace_agree(self):
        rng = np.random.default_rng(1)
        image = rng.integers(0, 256, (9, 7, 4), dtype=np.uint8)
        plain = imaging.decode(encode_png_rgba(7, 9, image.tobytes()))[2]
        self.assertEqual(plain, image.tobytes())
        # Adam7 interlaced copy, every pass filtered with Paeth (4) after encoding manually.
        out = b""
        for x0, y0, dx, dy in imaging.ADAM7:
            sub = image[y0::dy, x0::dx]
            if sub.size == 0:
                continue
            previous = np.zeros(sub.shape[1] * 4, np.int32)
            for row in sub.reshape(sub.shape[0], -1).astype(np.int32):
                filtered = []
                for i, value in enumerate(row):
                    left = row[i - 4] if i >= 4 else 0
                    up = previous[i]
                    corner = previous[i - 4] if i >= 4 else 0
                    p = left + up - corner
                    pa, pb, pc = abs(p - left), abs(p - up), abs(p - corner)
                    predictor = left if pa <= pb and pa <= pc else up if pb <= pc else corner
                    filtered.append((value - predictor) & 0xFF)
                out += b"\x04" + bytes(filtered)
                previous = row
        self.assertEqual(imaging.decode(_png(7, 9, out, 6, 8, interlace=1))[2], image.tobytes())

    def test_jpeg_matches_libjpeg(self):
        width, height, pixels = imaging.decode(JPEG_420, "gradient.jpg")
        self.assertEqual((width, height), (16, 16))
        image = np.frombuffer(pixels, np.uint8).reshape(16, 16, 4)
        for (row, column), expected in JPEG_REFERENCE.items():
            # Studio replicates chroma instead of libjpeg's fancy upsampling; values stay within a few levels.
            self.assertLessEqual(max(abs(int(a) - b) for a, b in zip(image[row, column, :3], expected)), 12, (row, column))

    def test_progressive_jpeg_refused_with_advice(self):
        progressive = JPEG_420.replace(b"\xff\xc0", b"\xff\xc2", 1)
        with self.assertRaisesRegex(imaging.ImageError, "baseline"):
            imaging.decode(progressive)

    def test_bmp_and_tga(self):
        rgb = bytes([10, 20, 30, 40, 50, 60])  # two BGR pixels, one row (bottom-up), padded to 8 bytes
        header = b"BM" + struct.pack("<IHHI", 62, 0, 0, 54) + struct.pack("<IiiHHIIiiII", 40, 2, 1, 1, 24, 0, 8, 0, 0, 0, 0)
        self.assertEqual(imaging.decode(header + rgb + b"\0\0")[2], bytes([30, 20, 10, 255, 60, 50, 40, 255]))
        tga = struct.pack("<BBBHHBHHHHBB", 0, 0, 10, 0, 0, 0, 0, 0, 3, 1, 32, 0x28) + bytes([0x82, 1, 2, 3, 4])
        self.assertEqual(imaging.decode(tga, "x.tga")[2], bytes([3, 2, 1, 4]) * 3)

    def test_fit_resizes_to_powers_of_two_and_keeps_colour(self):
        width, height, pixels = imaging.fit(300, 70, bytes([200, 100, 50, 255]) * (300 * 70), 256)
        self.assertEqual((width, height), (256, 64))
        self.assertEqual(set(np.frombuffer(pixels, np.uint8).reshape(-1, 4)[:, 0].tolist()), {200})

    def test_damaged_and_oversized_images(self):
        with self.assertRaises(imaging.ImageError):
            imaging.decode(encode_png_rgba(4, 4, bytes(64))[:40])
        huge = _png(9000, 1, b"\0" * 9001, 0)
        with self.assertRaisesRegex(imaging.ImageError, "up to"):
            imaging.decode(huge)
        with self.assertRaisesRegex(imaging.ImageError, "not a PNG"):
            imaging.decode(b"hello", "notes.txt")


class ModelImportTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="studio-import-"))

    def test_obj_with_material_texture_found_by_name(self):
        (self.folder / "brick.png").write_bytes(encode_png_rgba(4, 4, bytes([200, 60, 40, 255]) * 16))
        (self.folder / "box.mtl").write_text("newmtl red\nKd 1 1 1\nmap_Kd -s 1 1 1 C:\\Users\\someone\\brick.png\nnewmtl blue\nKd 0.2 0.4 0.9\n")
        (self.folder / "box.obj").write_text("mtllib box.mtl\nv 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nv 0 0 1\nvt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\n"
                                             "usemtl red\nf 1/1 2/2 3/3 4/4\nusemtl blue\nf -5 -4 -1\n")
        result = imports.read_file(self.folder / "box.obj", imports.ImportOptions(units="metres", credit="Made by me"))
        self.assertEqual(result.warnings, [])
        model = next(iter(result.models.values()))
        self.assertEqual(len(model["mesh"]["triangles"]), 3)
        self.assertEqual(model["bounds"], {"min": [-8.0, 0.0, -8.0], "max": [8.0, 16.0, 8.0]})
        textured = [t for t in model["mesh"]["triangles"] if model["mesh"]["materials"][t["material"]]["texture"]]
        self.assertEqual([c["uv"] for c in textured[0]["corners"]], [[0.0, 1.0], [1.0, 1.0], [1.0, 0.0]])  # OBJ v flipped to rows
        blue = [t for t in model["mesh"]["triangles"] if not model["mesh"]["materials"][t["material"]]["texture"]]
        self.assertEqual(blue[0]["corners"][0]["colour"], [0.2, 0.4, 0.9, 1.0])
        self.assertEqual(model["source"]["import"]["credit"], "Made by me")
        self.assertNotIn(str(self.folder), json.dumps(model["source"]))
        again = imports.read_file(self.folder / "box.obj", imports.ImportOptions(units="metres", credit="Made by me"))
        self.assertEqual(sorted(again.models), sorted(result.models))

    def test_missing_texture_and_escaping_paths_are_reported(self):
        model_folder = self.folder / "model"
        model_folder.mkdir()
        (self.folder / "secret.png").write_bytes(encode_png_rgba(4, 4, bytes(64)))  # exists, but outside the model's folder
        self.folder = model_folder
        (self.folder / "a.mtl").write_text("newmtl m\nmap_Kd ../secret.png\n")
        (self.folder / "a.obj").write_text("mtllib a.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nusemtl m\nf 1 2 3\n")
        result = imports.read_file(self.folder / "a.obj")
        self.assertTrue(any("secret.png" in w for w in result.warnings))
        self.assertEqual(result.textures, {})

    def test_glb_round_trip_keeps_geometry(self):
        library = starter_library()
        tank_id = library.starter()["models"]["tank_placeholder"]
        tank = library.model(tank_id)
        textures = {}
        for material in tank.mesh["materials"]:
            texture = library.texture(material["texture"])
            textures[material["texture"]] = {"id": material["texture"], "width": texture.width, "height": texture.height, "rgba": texture.rgba,
                                             "wrap": texture.wrap}
        (self.folder / "tank.glb").write_bytes(gltf.mesh_glb({"id": tank_id, "schema": "btga-neutral", "schema_version": 1, "mesh": tank.mesh, "bounds": tank.bounds,
                                                              "source": tank.provenance}, textures))
        result = imports.read_file(self.folder / "tank.glb", imports.ImportOptions(units="studio units", ground=False))
        model = next(iter(result.models.values()))
        self.assertEqual(model["bounds"], tank.bounds)
        self.assertEqual(len(model["mesh"]["triangles"]), len(tank.mesh["triangles"]))
        self.assertEqual(len(result.textures), len(textures))

    def test_z_up_scale_and_limits(self):
        (self.folder / "z.obj").write_text("v 0 0 0\nv 2 0 0\nv 0 0 3\nf 1 2 3\n")
        model = next(iter(imports.read_file(self.folder / "z.obj", imports.ImportOptions(units="feet", up_axis="z", ground=False)).models.values()))
        self.assertAlmostEqual(model["bounds"]["max"][1], 3 * 4.8768, 3)
        with self.assertRaisesRegex(imports.ImportProblem, "units across"):
            imports.read_file(self.folder / "z.obj", imports.ImportOptions(units="metres", scale=5000.0))
        (self.folder / "model.fbx").write_bytes(b"Kaydara FBX Binary")
        with self.assertRaisesRegex(imports.ImportProblem, "Blender"):
            imports.read_file(self.folder / "model.fbx")
        with self.assertRaisesRegex(imports.ImportProblem, "group name"):
            imports.read_file(self.folder / "z.obj", imports.ImportOptions(group="a/b"))

    def test_picture_shapes_and_sky(self):
        (self.folder / "sign.png").write_bytes(encode_png_rgba(8, 8, bytes([0, 0, 255, 255]) * 64))
        for shape, triangles in (("ground tile", 2), ("upright panel", 2), ("box", 10)):
            result = imports.read_file(self.folder / "sign.png", imports.ImportOptions(shape=shape, size=(100, 50, 20)))
            self.assertEqual(result.triangles, triangles, shape)
        (self.folder / "pano.png").write_bytes(encode_png_rgba(64, 8, bytes([90, 140, 200, 255]) * 512))
        result = imports.read_file(self.folder / "pano.png", imports.ImportOptions(shape="sky panorama"))
        texture = next(iter(result.textures.values()))
        self.assertEqual(texture["sky"], {"horizon": 0.5, "vertical_degrees": 45.0})
        self.assertEqual(texture["wrap"], {"s": "clamp", "t": "clamp"})


class UserLibraryTests(unittest.TestCase):
    def _result(self, folder: Path, colour: int, name: str = "panel"):
        (folder / f"{name}.png").write_bytes(encode_png_rgba(4, 4, bytes([colour, 0, 0, 255]) * 16))
        return imports.read_file(folder / f"{name}.png", imports.ImportOptions(shape="upright panel", group="Signs", credit="CC0"))

    def test_add_dedupe_remove_and_reopen(self):
        folder = Path(tempfile.mkdtemp())
        user = imports.UserLibrary(folder / "lib")
        first = self._result(folder, 10)
        added = user.add(first)
        self.assertEqual(len(added), 2)
        self.assertEqual(user.add(first), [])
        models, textures = user.entries()
        self.assertEqual((len(models), len(textures)), (1, 1))
        self.assertEqual(models[0]["group"], "Signs")
        self.assertEqual(models[0]["credit"], "CC0")
        combined = LibrarySet([*starter_library().libraries, __import__("tools.btga_assets.studio", fromlist=["StudioLibrary"]).StudioLibrary.open(user.root)])
        model_id = next(iter(first.models))
        self.assertEqual(combined.model(model_id).mesh, first.models[model_id]["mesh"])
        texture_id = next(iter(first.textures))
        self.assertEqual(combined.texture(texture_id).rgba, first.textures[texture_id]["rgba"])
        removed = user.remove([model_id, texture_id])
        self.assertEqual(sorted(removed), sorted([model_id, texture_id]))
        self.assertEqual(user.entries(), ([], []))
        self.assertFalse((folder / "lib" / "catalog.partial").exists())

    def test_textures_used_by_kept_models_stay(self):
        folder = Path(tempfile.mkdtemp())
        user = imports.UserLibrary(folder / "lib")
        panel = self._result(folder, 10)
        tile = imports.read_file(folder / "panel.png", imports.ImportOptions(shape="ground tile"))
        user.add(panel)
        user.add(tile)
        texture_id = next(iter(panel.textures))
        self.assertEqual(user.remove([next(iter(panel.models)), texture_id]), [next(iter(panel.models))])
        self.assertEqual([t["id"] for t in user.entries()[1]], [texture_id])


class ProjectAndPackageTests(unittest.TestCase):
    def _setup(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "sign.png").write_bytes(encode_png_rgba(4, 4, bytes([0, 200, 0, 255]) * 16))
        (folder / "pano.png").write_bytes(encode_png_rgba(32, 4, bytes([90, 140, 200, 255]) * 128))
        user = imports.UserLibrary(folder / "lib")
        sign = imports.read_file(folder / "sign.png", imports.ImportOptions(shape="box", size=(64, 64, 64), credit="Sign by A. Person, CC BY 4.0"))
        pano = imports.read_file(folder / "pano.png", imports.ImportOptions(shape="sky panorama", credit="Panorama CC0"))
        user.add(sign)
        user.add(pano)
        from tools.btga_assets.studio import StudioLibrary

        library = LibrarySet([*starter_library().libraries, StudioLibrary.open(user.root)])
        project = projects.StudioProject("Imports", "studio")
        project.add_blank_map("main", "Main", 1024.0)
        document = project.open_map("main", library)
        placed = document.place_model(next(iter(sign.models)), (0.0, 0.0, 0.0))
        sky_texture = next(iter(pano.textures))
        project.set_map_sky("main", {"texture": sky_texture, **pano.textures[sky_texture]["sky"]})
        return folder, user, library, project, document, placed, sky_texture, sign

    def test_package_embeds_user_resources_with_credits_and_restores_them(self):
        folder, user, library, project, document, placed, sky_texture, sign = self._setup()
        data = package.build_package(project, library)
        read = package.read_package(data)
        model_id = next(iter(sign.models))
        self.assertIn(model_id, read.models)
        self.assertEqual(read.models[model_id]["source"]["import"]["credit"], "Sign by A. Person, CC BY 4.0")
        credits = {c["id"]: c["credit"] for c in read.manifest["credits"]}
        self.assertEqual(credits[sky_texture], "Panorama CC0")
        self.assertEqual(read.maps["main"]["environment"][0]["sky"]["texture"], sky_texture)
        self.assertEqual(data, package.build_package(project, library))  # deterministic
        other = imports.UserLibrary(Path(tempfile.mkdtemp()) / "lib")
        restored = other.add_package(data)
        self.assertIn(model_id, restored)
        self.assertIn(sky_texture, restored)
        self.assertTrue(any(t.get("sky") for t in other.entries()[1]))

    def test_missing_sky_texture_in_a_package_is_refused(self):
        folder, user, library, project, document, placed, sky_texture, sign = self._setup()
        import io
        import zipfile

        data = package.build_package(project, library)
        source = zipfile.ZipFile(io.BytesIO(data))
        files = {n: source.read(n) for n in source.namelist() if not n.endswith(sky_texture.replace(":", "_") + ".rgba")}
        manifest = json.loads(files["manifest.json"])
        manifest["files"] = [f for f in manifest["files"] if f["path"] in files]
        files["manifest.json"] = json.dumps(manifest).encode()
        with self.assertRaisesRegex(package.PackageError, "sky"):
            package.read_package(package._zip(files))

    def test_sky_validation_and_undo(self):
        folder, user, library, project, document, placed, sky_texture, sign = self._setup()
        with self.assertRaises(projects.ProjectError):
            project.set_map_sky("main", {"texture": sky_texture, "horizon": 2.0})
        history = project.definitions
        history.edit("Turn sky", lambda: project.set_map_sky("main", {**project.maps["main"].base["environment"][0]["sky"], "north": 45.0}))
        self.assertEqual(project.maps["main"].base["environment"][0]["sky"]["north"], 45.0)
        history.undo()
        self.assertEqual(project.maps["main"].base["environment"][0]["sky"]["north"], 0.0)
        history.edit("Remove sky", lambda: project.set_map_sky("main", None))
        self.assertNotIn("sky", document.scene.base["environment"][0])
        history.undo()
        self.assertIn("sky", document.scene.base["environment"][0])
        history.redo()
        history.edit("Sky again", lambda: project.set_map_sky("main", {"texture": sky_texture, "horizon": 0.5, "vertical_degrees": 45.0}))
        history.undo()  # back to a map that never had a sky
        self.assertNotIn("sky", project.maps["main"].base["environment"][0])
        self.assertNotIn("sky", document.scene.base["environment"][0])

    def test_missing_import_is_a_clear_error_and_drawn_as_a_stand_in(self):
        folder, user, library, project, document, placed, sky_texture, sign = self._setup()
        path = folder / "imports.btgaproj"
        projects.save_project(path, project)
        bare = starter_library()
        reopened = projects.load_project(path)
        from tools.btga_studio.core import diagnostics
        from tools.btga_studio.core.scene import MeshCache

        cache = MeshCache(bare)
        document = reopened.open_map("main", bare, cache)
        codes = {d.code for d in diagnostics.collect(document, bare)}
        self.assertIn("MISSING_IMPORT", codes)
        self.assertIn("MISSING_IMPORT", {p["code"] for p in reopened.problems(bare)})  # the sky picture
        mesh = cache.get(next(iter(sign.models)))
        self.assertEqual(mesh.triangle_count, 12)
        self.assertIn(next(iter(sign.models)), cache.missing)

    def test_generated_boxes_still_report_overlaps_with_authored_ones(self):
        from tools.btga_assets import overlay as overlay_module

        def box(identifier, x, generated):
            shape = {"id": identifier, "shape": "box", "translation": [x, 0.0, 0.0], "yaw": 0, "min": [-10.0, 0.0, -10.0], "max": [10.0, 5.0, 10.0],
                     "vertical": {"status": "proven"}, "overlay": {"edits": []}}
            return {**shape, "allow_overlap": True} if generated else shape

        scene = {"id": "s", "source": {"platform": "studio"}, "instances": [], "markers": [], "collision": [], "bounds": {}}
        for pair, expected in (((True, True), 0), ((True, False), 2), ((False, False), 2)):
            edited = {**scene, "collision": [box("a", 0.0, pair[0]), box("b", 5.0, pair[1])]}
            errors = overlay_module.validate(scene, overlay_module.Overlay("s", "studio"), edited=edited)
            self.assertEqual(sum(1 for e in errors if e["code"] == "COLLISION_OVERLAP"), expected, pair)

    def test_make_solid_adds_one_box_per_model(self):
        folder, user, library, project, document, placed, sky_texture, sign = self._setup()
        ids = document.add_collision_for([placed])
        record = document.record(ids[0])
        self.assertEqual(record["min"], [-32.0, 0.0, -32.0])
        self.assertEqual(record["max"], [32.0, 64.0, 32.0])
        document.undo()
        self.assertIsNone(document.scene.record(ids[0]))
        tile = document.place_model(library.starter()["models"]["ground_tile"], (0.0, 0.0, 0.0))
        with self.assertRaises(Exception):
            document.add_collision_for([tile])


class TeleporterTests(unittest.TestCase):
    def test_teleporter_needs_an_exit_of_the_right_kind(self):
        library = starter_library()
        templates = {t["source_kind"].removeprefix("studio_"): t for t in library.starter()["templates"]}
        project = projects.StudioProject("T", "studio")
        project.add_blank_map("main", "Main", 1024.0)
        document = project.open_map("main", library)
        pad = document.place_marker(templates["teleporter"], (0.0, 0.0, 0.0))
        from tools.btga_studio.core import diagnostics

        self.assertIn("TELEPORTER_WITHOUT_EXIT", {d.code for d in diagnostics.collect(document, library)})
        pickup = document.place_marker(templates["pickup"], (300.0, 0.0, 0.0))
        document.set_field(pad, "exit", pickup)
        self.assertIn("INVALID_FIELD", {d.code for d in diagnostics.collect(document, library) if d.target == pad})
        exit_marker = document.place_marker(templates["teleport_exit"], (600.0, 400.0, 0.0))
        document.set_field(pad, "exit", exit_marker)
        self.assertFalse([d for d in diagnostics.collect(document, library) if d.target == pad and d.severity == "error"])
        data = package.read_package(package.build_package(project, library))
        kinds = {m["source_kind"] for m in data.maps["main"]["records"]["markers"]}
        self.assertTrue({"studio_teleporter", "studio_teleport_exit"} <= kinds)

    def test_starter_ids_survive_the_version_bump(self):
        from tools.btga_studio.content import starter

        self.assertEqual(starter.STARTER_VERSION, 3)
        example = json.loads((ROOT / "examples" / "starter-example.btgaproj").read_text(encoding="utf-8"))
        used = {e["instance"]["model"] for m in example["maps"].values() for e in (m.get("overlay") or {}).get("edits", []) if e["op"] == "add_instance"}
        self.assertTrue(used)
        self.assertTrue(used <= set(starter_library()._models))


class GeoTests(unittest.TestCase):
    def test_projection_and_bearings(self):
        projection = geo.LocalProjection(45.0, -93.0)
        east, north = projection.forward(45.0, -92.99)
        self.assertAlmostEqual(float(east), 787.9, delta=1.0)
        lat, lon = projection.inverse(east, north)
        self.assertAlmostEqual(float(lon), -92.99, 9)
        bearing, distance = geo.bearing_distance(44.9779, -93.2694, 44.9481, -93.0958)
        self.assertAlmostEqual(bearing, 103.6, delta=0.5)
        self.assertAlmostEqual(distance, 14090, delta=60)

    def test_triangulation_with_hole_covers_the_area(self):
        outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
        hole = [(3, 3), (3, 7), (7, 7), (7, 3)]
        points = outer + hole
        triangles = geo.triangulate(outer, [hole])
        area = sum(abs(geo.signed_area([points[a], points[b], points[c]])) for a, b, c in triangles)
        self.assertAlmostEqual(area, 100 - 16, 6)
        concave = [(0, 0), (4, 0), (4, 4), (2, 1), (0, 4)]
        area = sum(abs(geo.signed_area([concave[a], concave[b], concave[c]])) for a, b, c in geo.triangulate(concave))
        self.assertAlmostEqual(area, abs(geo.signed_area(concave)), 6)

    def test_minimum_rectangle_of_a_rotated_box(self):
        angle = math.radians(30)
        corners = [(x * math.cos(angle) - y * math.sin(angle) + 5, x * math.sin(angle) + y * math.cos(angle) - 2)
                   for x, y in ((-4, -1), (4, -1), (4, 1), (-4, 1))]
        (cx, cy), found, half_length, half_width = geo.min_area_rectangle(corners)
        self.assertAlmostEqual(cx, 5, 6)
        self.assertAlmostEqual(cy, -2, 6)
        self.assertAlmostEqual(sorted([half_length, half_width])[1], 4, 6)
        self.assertAlmostEqual(math.sin(2 * found), math.sin(2 * angle) if half_length > half_width else -math.sin(2 * angle), 6)

    def test_rings_assemble_from_reversed_pieces(self):
        rings = geo.assemble_rings([[(0, 0), (1, 0)], [(1, 1), (1, 0)], [(1, 1), (0, 1), (0, 0)]])
        self.assertEqual(len(rings), 1)
        self.assertEqual(len(rings[0]), 5)

    def test_lengths_colours_and_levels(self):
        self.assertEqual(osm.length("40'"), 12.192)
        self.assertEqual(osm.length("12'6\""), 3.81)
        self.assertEqual(osm.length("12.5 m"), 12.5)
        self.assertIsNone(osm.length("tall"))
        self.assertEqual(osm.colour("#fff")[:3], (1.0, 1.0, 1.0))
        self.assertIsNone(osm.colour("sparkly"))
        self.assertEqual(osm.levels("3;4"), 3.0)

    def test_wikidata_heights_use_units_and_distrust_bad_heights(self):
        def row(qid, label, height=None, unit=None, floors=None):
            out = {"item": {"value": f"http://www.wikidata.org/entity/{qid}"}, "itemLabel": {"value": label}}
            if height is not None:
                out["height"] = {"value": str(height)}
                out["heightUnit"] = {"value": f"http://www.wikidata.org/entity/{unit}"}
            if floors is not None:
                out["floors"] = {"value": str(floors)}
            return out

        table = osm.heights_from_wikidata({"results": {"bindings": [row("Q1", "Tower", 100, "Q3710"), row("Q2", "Library", 400, "Q11573", 5),
                                                                    row("Q3", "Hall", floors=12)]}})
        self.assertEqual(table["wikidata:Q1"]["height"], 30.48)
        self.assertNotIn("height", table["wikidata:Q2"])
        self.assertEqual(table["Hall"]["building:levels"], 12.0)


def _terrarium(folder: Path, zoom: int, lat: float, lon: float, elevation) -> None:
    """Write the elevation tiles around a point; ``elevation(lat, lon)`` gives metres."""

    fx, fy = geo.tile_xy(lat, lon, zoom)
    for x in range(int(fx) - 1, int(fx) + 2):
        for y in range(int(fy) - 1, int(fy) + 2):
            py, px = np.mgrid[0:256, 0:256] + 0.5
            n = 2 ** zoom
            lons = (x + px / 256) / n * 360 - 180
            lats = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (y + py / 256) / n))))
            value = elevation(lats, lons) + 32768
            rgba = np.dstack([value // 256, value % 256, (value * 256) % 256, np.full_like(value, 255)]).astype(np.uint8)
            path = folder / str(zoom) / str(x) / f"{y}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(encode_png_rgba(256, 256, rgba.tobytes()))


CITY = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6">
 <bounds minlat="44.9970" minlon="-93.0040" maxlat="45.0030" maxlon="-92.9960"/>
 {nodes}
 <way id="1"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/><tag k="building" v="office"/><tag k="name" v="Glass Tower"/></way>
 <way id="2"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/><tag k="building:part" v="yes"/><tag k="height" v="300'"/></way>
 <way id="3"><nd ref="5"/><nd ref="6"/><nd ref="7"/><nd ref="8"/><nd ref="5"/><tag k="building" v="yes"/><tag k="building:levels" v="3"/></way>
 <way id="4"><nd ref="9"/><nd ref="10"/><tag k="highway" v="primary"/><tag k="name" v="Main Street"/></way>
 <way id="5"><nd ref="11"/><nd ref="12"/><nd ref="13"/><nd ref="14"/><nd ref="11"/><tag k="natural" v="water"/></way>
 <way id="6"><nd ref="15"/><nd ref="16"/><tag k="highway" v="primary"/><tag k="bridge" v="yes"/><tag k="layer" v="1"/></way>
 <way id="7"><nd ref="17"/><nd ref="18"/><tag k="highway" v="footway"/><tag k="bridge" v="covered"/><tag k="indoor" v="yes"/></way>
 <way id="8"><nd ref="19"/><nd ref="20"/><nd ref="21"/><nd ref="22"/><nd ref="19"/><tag k="building" v="stadium"/><tag k="name" v="Ballpark"/></way>
 <way id="9"><nd ref="23"/><nd ref="24"/><nd ref="25"/><nd ref="26"/><nd ref="23"/><tag k="leisure" v="pitch"/></way>
 <way id="10"><nd ref="27"/><nd ref="28"/><nd ref="29"/><nd ref="30"/><nd ref="27"/><tag k="building" v="house"/><tag k="wikidata" v="Q7"/></way>
 <way id="12"><nd ref="36"/><nd ref="37"/><nd ref="38"/><nd ref="39"/><nd ref="36"/><tag k="building" v="stadium"/><tag k="name" v="Open Park"/></way>
 <way id="13"><nd ref="40"/><nd ref="41"/><nd ref="42"/><nd ref="43"/><nd ref="40"/><tag k="building" v="yes"/><tag k="leisure" v="stadium"/><tag k="name" v="Arena"/></way>
 <way id="11"><nd ref="32"/><nd ref="33"/><nd ref="34"/><nd ref="35"/><nd ref="32"/><tag k="building" v="yes"/><tag k="height" v="10"/></way>
 <node id="31" lat="45.0010" lon="-92.9990"><tag k="natural" v="tree"/></node>
</osm>"""


def _city_nodes() -> str:
    """Nodes in metres around (45, -93): two buildings 30 m apart, a road, a river with a bridge, a skyway between them, a ballpark."""

    projection = geo.LocalProjection(45.0, -93.0)
    points = {1: (-40, 20), 2: (-10, 20), 3: (-10, 50), 4: (-40, 50), 5: (20, 20), 6: (50, 20), 7: (50, 50), 8: (20, 50),
              9: (-200, 0), 10: (200, 0), 11: (-200, -120), 12: (200, -120), 13: (200, -80), 14: (-200, -80),
              15: (0, -60), 16: (0, -140), 17: (-15, 35), 18: (25, 35),
              19: (-160, 60), 20: (-60, 60), 21: (-60, 160), 22: (-160, 160), 23: (-130, 90), 24: (-90, 90), 25: (-90, 130), 26: (-130, 130),
              27: (100, 100), 28: (110, 100), 29: (110, 110), 30: (100, 110),
              32: (240, -20), 33: (262, -20), 34: (262, -5), 35: (240, -5),  # crosses the map edge at 256 m
              36: (60, -240), 37: (160, -240), 38: (160, -160), 39: (60, -160),  # an open stadium with no field mapped
              40: (-240, -240), 41: (-180, -240), 42: (-180, -190), 43: (-240, -190)}  # an indoor arena (leisure=stadium only)
    out = []
    for number, (e, n) in points.items():
        lat, lon = projection.inverse(e, n)
        out.append(f'<node id="{number}" lat="{float(lat):.8f}" lon="{float(lon):.8f}"/>')
    return "\n ".join(out)


class CityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp(prefix="studio-city-"))
        (cls.folder / "city.osm").write_text(CITY.format(nodes=_city_nodes()), encoding="utf-8")
        projection = geo.LocalProjection(45.0, -93.0)

        def elevation(lats, lons):
            east, north = projection.forward(lats, lons)
            return np.round(250.0 + np.where(np.asarray(north) < -70, -12.0, 0.0) + np.asarray(east) * 0.01)  # river valley to the south

        _terrarium(cls.folder / "tiles", 15, 45.0, -93.0, elevation)
        cls.terrain = geo.Terrain(cls.folder / "tiles")
        cls.data = osm.read_osm(cls.folder / "city.osm")
        cls.options = osm.CityOptions(centre=(45.0, -93.0), half_extent_m=256.0, chunk_m=128.0, group="Test city",
                                      heights={"wikidata:Q7": {"height": 9.0, "source": "Wikidata Q7"}})
        cls.plan = osm.build_city(cls.data, cls.options, cls.terrain)

    def test_terrain_samples_elevation_tiles(self):
        value = self.terrain.elevation([45.0], [-93.0])[0]
        self.assertAlmostEqual(value, 250.0, delta=0.6)
        self.assertEqual(self.plan.reference_elevation, round(value, 1))

    def test_counts_heights_and_landmarks(self):
        counts = self.plan.counts
        self.assertEqual(counts["buildings"], 7)
        self.assertEqual(counts["parts"], 1)
        self.assertEqual(counts["bridges"], 1)
        self.assertEqual(counts["skyways"], 1)
        self.assertEqual(counts["trees"], 1)
        self.assertEqual(self.plan.estimated, 3)  # the stadiums and the arena have no height; the house gets one from the heights file
        tower = self.plan.landmarks["Glass Tower"]
        self.assertAlmostEqual(tower["height_m"], 91.44, delta=0.05)  # 300 ft from the part, not the outline
        self.assertIn("Ballpark", self.plan.landmarks)
        instance = next(i for i in self.plan.instances if i["model"] == tower["model"])
        top = max(p[1] for p in self.plan.result.models[tower["model"]]["mesh"]["positions"]) + instance["translation"][1]
        ground = (self.terrain.elevation([45.0], [-93.0])[0] - self.plan.reference_elevation)
        self.assertAlmostEqual(top / osm.SCALE, 91.44 + ground - 0.3, delta=0.7)  # base at the lowest ground under the outline

    def test_collision_boxes_follow_outlines_and_open_the_ballpark(self):
        boxes = self.plan.collision
        tower = [b for b in boxes if b["source"]["osm"] == "way/1"]
        self.assertEqual(len(tower), 1)
        self.assertAlmostEqual(tower[0]["max"][1], 91.44 * osm.SCALE, 1)
        self.assertAlmostEqual(abs(tower[0]["min"][0]) + abs(tower[0]["max"][0]), (30 - 0.5) * osm.SCALE, delta=2)
        self.assertFalse([b for b in boxes if b["source"]["osm"] == "way/11"])  # drawn, but not solid beyond the map edge
        self.assertEqual(sum(1 for b in boxes if b["source"]["osm"] == "way/12"), 24)  # open stands around a field Studio drew
        self.assertEqual(sum(1 for b in boxes if b["source"]["osm"] == "way/13"), 1)  # the indoor arena stays one solid block
        ballpark = [b for b in boxes if b["source"]["osm"] == "way/8"]
        self.assertEqual(len(ballpark), 24)
        field_centre = (-110 * osm.SCALE, 110 * osm.SCALE * -1)
        for box in ballpark:  # no stand box covers the middle of the field
            self.assertGreater(math.hypot(box["translation"][0] - field_centre[0], box["translation"][2] - field_centre[1]), 15 * osm.SCALE)

    def test_ground_is_painted_and_follows_the_valley(self):
        ground_models = [i for i in self.plan.instances if i["label"] == "ground"]
        self.assertEqual(len(ground_models), 16)
        heights = [p[1] for i in ground_models for p in self.plan.result.models[i["model"]]["mesh"]["positions"]]
        self.assertLess(min(heights), -10 * osm.SCALE)
        textures = [t for t in self.plan.result.textures.values() if t["label"].startswith("ground")]
        pitch = np.array(osm.GROUND["pitch"], np.int16)
        middle = next(t for t in textures if t["label"] == "ground 3 2")  # the chunk holding Open Park's middle
        pixels = np.frombuffer(middle["rgba"], np.uint8).reshape(-1, 4)[:, :3].astype(np.int16)
        self.assertGreater(int((np.abs(pixels - pitch).max(axis=1) <= 16).sum()), 1500)  # its field (partly in this chunk) is grass
        water = np.array(osm.GROUND["water"], np.uint8)
        self.assertTrue(any(np.all(np.frombuffer(t["rgba"], np.uint8).reshape(-1, 4)[:, :3] == water, axis=1).any() for t in textures))
        wet_norths = []
        for instance in ground_models:
            mesh = self.plan.result.models[instance["model"]]["mesh"]
            positions = np.asarray(mesh["positions"])
            for triangle in mesh["triangles"]:
                if mesh["materials"][triangle["material"]].get("surface") == "water":
                    corner = positions[[c["p"] for c in triangle["corners"]]].mean(axis=0)
                    wet_norths.append(-(corner[2] + instance["translation"][2]) / osm.SCALE)
        self.assertTrue(wet_norths)  # the river's ground is marked as water for driving
        self.assertTrue(all(-126 < n < -74 for n in wet_norths))  # and only the river (north -120 to -80)

    def test_barriers_stop_at_the_water(self):
        projection = geo.LocalProjection(45.0, -93.0)

        def point(e, n):
            lat, lon = projection.inverse(e, n)
            return float(lat), float(lon)

        fence = osm.OsmData(ways=[{"id": 950, "tags": {"barrier": "fence"}, "coords": [point(30, -40 - 10 * k) for k in range(13)]}],
                            sha256="fence", file="fence.json")
        builder = osm.CityBuilder(osm.merge(self.data, fence), osm.CityOptions(centre=(45.0, -93.0), half_extent_m=256.0, chunk_m=128.0,
                                                                                detail="high"), self.terrain)
        builder.paint()
        self.assertEqual(builder.is_water([(30, -100), (30, -60), (30, -140)]).tolist(), [True, False, False])
        models, counts = builder.furniture()
        self.assertEqual(counts["barriers"], 1)
        norths = [-(p[2] + origin_n) / osm.SCALE for model, (origin_e, origin_n) in [(m, (o[0] * osm.SCALE, -o[1] * osm.SCALE)) for m, o in models]
                  for p in builder.result.models[model]["mesh"]["positions"]]
        self.assertFalse(any(-118 < n < -82 for n in norths))  # the fence breaks off at both banks
        self.assertTrue(any(n > -80 for n in norths) and any(n < -120 for n in norths))
        self.assertIn("OpenStreetMap contributors", self.plan.credit)
        self.assertIn("USGS 3DEP", self.plan.credit)

    def test_plan_places_on_a_map_in_one_undo_step_and_exports(self):
        library = starter_library()
        user = imports.UserLibrary(Path(tempfile.mkdtemp()) / "lib")
        user.add(self.plan.result)
        from tools.btga_assets.studio import StudioLibrary

        combined = LibrarySet([*library.libraries, StudioLibrary.open(user.root)])
        project = projects.StudioProject("City", "studio")
        project.add_blank_map("city", "City", self.plan.half_extent)
        document = project.open_map("city", combined)
        osm.apply_plan(document, self.plan)
        self.assertEqual(len(document.undo_stack), 1)
        self.assertEqual(sum(1 for f, r in document.scene.records("collision")), len(self.plan.collision))
        from tools.btga_studio.core import diagnostics

        errors = [d for d in diagnostics.collect(document, combined) if d.severity == "error"]
        self.assertEqual(errors, [])
        data = package.read_package(package.build_package(project, combined))
        self.assertTrue(any("OpenStreetMap" in c["credit"] for c in data.manifest["credits"]))
        document.undo()
        self.assertEqual(sum(1 for _ in document.scene.records("instances")), 0)

    def test_high_detail_adds_street_furniture_parapets_gables_and_finer_ground(self):
        projection = geo.LocalProjection(45.0, -93.0)

        def point(e, n):
            lat, lon = projection.inverse(e, n)
            return float(lat), float(lon)

        lamp, signal = point(-100, -20), point(-80, -20)
        a, b, c = point(-150, 60), point(-130, 60), point(-130, 75)
        extra = osm.OsmData(nodes=[{"id": 900, "tags": {"highway": "street_lamp"}, "lat": lamp[0], "lon": lamp[1]},
                                   {"id": 901, "tags": {"highway": "traffic_signals"}, "lat": signal[0], "lon": signal[1]}],
                            ways=[{"id": 902, "tags": {"barrier": "fence"}, "coords": [a, b, c]},
                                  {"id": 903, "tags": {"building": "house", "roof:shape": "gabled", "height": "9", "roof:height": "3"},
                                   "coords": [point(150, 150), point(170, 150), point(170, 160), point(150, 160), point(150, 150)]}],
                            sha256="extra", file="extra.json")
        data = osm.merge(self.data, extra)
        self.assertEqual(len(osm.merge(data, extra).nodes), len(data.nodes))  # merging twice keeps one copy
        common = dict(centre=(45.0, -93.0), half_extent_m=256.0, chunk_m=128.0, group="Test city")
        standard = osm.build_city(data, osm.CityOptions(**common), self.terrain)
        high = osm.build_city(data, osm.CityOptions(**common, detail="high", ground_metres_per_pixel=0.5, terrain_spacing_m=8.0), self.terrain)
        self.assertNotIn("lamps", standard.counts)
        self.assertEqual((high.counts["lamps"], high.counts["signals"], high.counts["barriers"]), (1, 1, 1))
        self.assertGreater(high.counts["triangles"], standard.counts["triangles"])
        ground = [t for t in high.result.textures.values() if t["label"].startswith("ground")]
        self.assertEqual({t["width"] for t in ground}, {256})  # 0.5 m per pixel over 128 m chunks
        house = next(i for i in high.instances if i["label"].startswith("buildings") and
                     any(p[1] > 8.5 * osm.SCALE for p in high.result.models[i["model"]]["mesh"]["positions"]))
        positions = np.asarray(high.result.models[house["model"]]["mesh"]["positions"])
        ridge = positions[np.abs(positions[:, 1] - positions[:, 1].max()) < 0.05]
        self.assertGreaterEqual(len(ridge), 2)  # a ridge line, not a pyramid's single apex
        self.assertGreater(np.ptp(ridge[:, [0, 2]], axis=0).max(), 15 * osm.SCALE)
        with self.assertRaisesRegex(imports.ImportProblem, "detail"):
            osm.CityBuilder(self.data, osm.CityOptions(**common, detail="ultra"))
        mixed = osm.build_city(data, osm.CityOptions(**common, detail="high", ground_metres_per_pixel=0.5, fine_ground_radius_m=100.0,
                                                     fine_ground_centre=(0.0, 0.0)), self.terrain)
        sizes = sorted({t["width"] for t in mixed.result.textures.values() if t["label"].startswith("ground")})
        self.assertEqual(sizes, [128, 256])  # sharp near the centre, 1 m per pixel further out

    def test_traffic_signals_stand_on_the_corners_not_in_the_road(self):
        projection = geo.LocalProjection(45.0, -93.0)

        def point(e, n):
            lat, lon = projection.inverse(e, n)
            return float(lat), float(lon)

        centre, middle = point(0, 0), point(0, -60)
        data = osm.OsmData(nodes=[{"id": 1, "tags": {"highway": "traffic_signals"}, "lat": centre[0], "lon": centre[1]},
                                  {"id": 2, "tags": {"highway": "traffic_signals"}, "lat": middle[0], "lon": middle[1]}],
                           ways=[{"id": 3, "tags": {"highway": "residential"}, "coords": [point(-100, 0), centre, point(100, 0)]},
                                 {"id": 4, "tags": {"highway": "residential"}, "coords": [point(0, -100), middle, centre, point(0, 100)]}],
                           sha256="signals", file="signals.json")
        builder = osm.CityBuilder(data, osm.CityOptions(centre=(45.0, -93.0), half_extent_m=256.0, chunk_m=128.0, detail="high"))
        corners = builder._signal_poles(data.nodes[0])
        self.assertEqual(len(corners), 4)
        for e, n, (ae, an) in corners:
            self.assertGreater(min(abs(e), abs(n)), osm.ROAD_WIDTH["residential"] / 2)  # off both carriageways
            self.assertLess(math.hypot(e + ae, n + an), math.hypot(e, n))  # the arm reaches towards the junction
        sides = builder._signal_poles(data.nodes[1])
        self.assertEqual(len(sides), 2)
        self.assertEqual(sorted(round(e) for e, n, _ in sides), [-6, 6])  # either kerb of the north-south road
        self.assertTrue(all(abs(n + 60) < 0.5 for _, n, _ in sides))
        lone = osm.OsmData(nodes=[{"id": 9, "tags": {"highway": "traffic_signals"}, "lat": centre[0], "lon": centre[1]}], ways=[], sha256="x", file="x")
        self.assertEqual(len(osm.CityBuilder(lone, builder.options)._signal_poles(lone.nodes[0])), 1)  # off any road: left where it was mapped

    def test_overpass_json_reads_like_xml(self):
        projection = geo.LocalProjection(45.0, -93.0)
        lat, lon = projection.inverse(np.array([0.0, 10.0, 10.0, 0.0]), np.array([0.0, 0.0, 10.0, 10.0]))
        geometry = [{"lat": float(a), "lon": float(b)} for a, b in zip(lat, lon)]
        document = {"elements": [{"type": "way", "id": 5, "tags": {"building": "yes", "height": "12"}, "geometry": geometry + geometry[:1]}]}
        path = self.folder / "one.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        plan = osm.build_city(osm.read_osm(path), osm.CityOptions(centre=(45.0, -93.0), half_extent_m=256.0, chunk_m=128.0, trees=False))
        self.assertEqual(plan.counts["buildings"], 1)
        self.assertEqual(plan.estimated, 0)

    def test_hostile_and_empty_inputs(self):
        path = self.folder / "bad.osm"
        path.write_text('<?xml version="1.0"?><!DOCTYPE osm [<!ENTITY a "b">]><osm>&a;</osm>', encoding="utf-8")
        with self.assertRaisesRegex(imports.ImportProblem, "entities"):
            osm.read_osm(path)
        path.write_text("<osm></osm>", encoding="utf-8")
        with self.assertRaisesRegex(imports.ImportProblem, "no map features"):
            osm.read_osm(path)
        with self.assertRaisesRegex(imports.ImportProblem, "half-size"):
            osm.CityBuilder(self.data, osm.CityOptions(centre=(45.0, -93.0), half_extent_m=5000.0))


class SkylineTests(unittest.TestCase):
    def test_distant_tower_appears_on_its_true_bearing(self):
        folder = Path(tempfile.mkdtemp(prefix="studio-sky-"))
        for zoom in (10,):
            _terrarium(folder, zoom, 45.0, -93.0, lambda lats, lons: np.full(np.shape(lats), 250.0))
        projection = geo.LocalProjection(45.0, -93.0)
        lat, lon = projection.inverse(np.array([20000.0, 20040.0, 20040.0, 20000.0]), np.array([0.0, 0.0, 40.0, 40.0]))
        coords = list(zip(lat.tolist(), lon.tolist()))
        features = osm.OsmData(ways=[{"id": 1, "tags": {"building": "yes", "height": "200"}, "coords": coords + coords[:1]}])
        options = skyline.SkylineOptions(centre=(45.0, -93.0), half_extent_m=1000.0, width=720, height=90, reach_m=24000.0, viewer_height_m=10.0)
        pixels, summary = skyline.render(options, geo.Terrain(folder), features)
        self.assertEqual(pixels.shape, (90, 720, 4))
        feature = summary["features"][0]
        self.assertAlmostEqual(feature["bearing"], 90.0, delta=0.6)
        top = math.degrees(math.atan2(250 + 200 - 260 - 20020 ** 2 / (2 * skyline.EARTH_RADIUS) * (1 - skyline.REFRACTION), 20020))
        self.assertAlmostEqual(feature["top_degrees"], top, delta=0.01)  # the curvature drop at 20 km is about 0.08 degrees
        column = int(90.0 / 360 * 720)
        sky_row = int(options.horizon * 90 - top * 90 / options.vertical_degrees) + 1
        self.assertNotEqual(tuple(pixels[sky_row, column, :3]), tuple(pixels[sky_row, column + 40, :3]))  # tower, then open sky
        self.assertEqual(sky.check({"texture": "user:texture:x", "horizon": options.horizon, "vertical_degrees": options.vertical_degrees})["north"], 0.0)

    def test_sky_band_geometry(self):
        vertices = sky.vertices({"texture": "t", "horizon": 0.75, "vertical_degrees": 45.0}, 1000.0)
        self.assertEqual(len(vertices), sky.SEGMENTS * (sky.RINGS + 2) * 6)
        self.assertTrue(np.allclose(np.linalg.norm(vertices[:, :3], axis=1), 1000.0, atol=0.05))  # a sphere: no gaps at any angle
        band_top = vertices[np.abs(vertices[:, 4]) < 1e-6][:, 1].min()
        self.assertAlmostEqual(float(band_top), 1000.0 * math.sin(math.radians(33.75)), 2)  # picture top row at +33.75 degrees
        north = vertices[(np.abs(vertices[:, 3]) < 1e-6) & (np.abs(vertices[:, 4] - 0.75) < 1e-6)]
        self.assertTrue(np.allclose(north[:, [0, 1, 2]], [0.0, 0.0, -1000.0], atol=1e-3))  # left edge faces north (-Z) on the horizon
        east = vertices[(np.abs(vertices[:, 3] - 0.25) < 1e-6) & (np.abs(vertices[:, 4] - 0.75) < 1e-6)]
        self.assertTrue(np.allclose(east[:, [0, 1, 2]], [1000.0, 0.0, 0.0], atol=1e-3))  # a quarter of the way in faces east (+X)


from tests.test_studio_authoring import _egl_available  # noqa: E402


@unittest.skipIf(not _egl_available(), "needs imgui-bundle, PyOpenGL and EGL (off-screen UI)")
class ImportWindowTests(unittest.TestCase):
    def test_import_window_library_sky_and_city(self):
        import os

        os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
        from tests.test_studio_product import TempHome
        from tools.btga_studio.core.settings import Settings
        from tools.btga_studio.fixtures import ScriptedDialogs, recording_launcher
        from tools.btga_studio.gui.app import StudioApp
        from tools.btga_studio.gui.offscreen import OffscreenUI

        with TempHome() as home, recording_launcher():
            folder = home / "my models ü"
            folder.mkdir()
            (folder / "crate.png").write_bytes(encode_png_rgba(8, 8, bytes([150, 110, 60, 255]) * 64))
            (folder / "sky ü.png").write_bytes(encode_png_rgba(64, 8, bytes([90, 140, 200, 255]) * 512))
            (folder / "broken.obj").write_text("v 0 0 0\nf 1 2 3\n")
            app = StudioApp(Settings.open())
            app.settings.preferences.welcome_seen = True
            ui = OffscreenUI(app, 1400, 900)
            self.addCleanup(ui.close)
            c = app.controller
            self.addCleanup(c.shutdown)
            app.show_hub = False
            c.add_blank_map("main", "Main", 2048.0)
            app.switch_map("main")
            app.open_import()
            app.dialogs = ScriptedDialogs(folder / "crate.png", folder / "broken.obj")
            app.import_state.update({"shape": 3, "group": "Props", "credit": "Made by me"})
            for _ in range(2):
                app.import_state["files"].append(app.dialogs.open_file("x", []))
            for _ in range(3):
                ui.frame()
            self.assertIn(folder / "crate.png", app.import_state["results"])
            self.assertIn("vertex that does not exist", app.import_state["errors"][folder / "broken.obj"])
            for _ in range(2):
                ui.frame()
            ui.click(self._centre(app, "import.go"), settle=3)
            self.assertIn("Added 2 item(s)", app.import_state["done"])
            self.assertEqual(app.filter_category, "Imported: Props")
            crate = next(e for e in c.index.catalog_entries if e.category == "Imported: Props")
            app.activate_asset(crate)
            self.assertIsNotNone(c.placement)
            # A sky panorama, chosen through the gameplay editor's Map tab.
            app.import_state.update({"files": [folder / "sky ü.png"], "shape": 4, "key": None})
            for _ in range(3):
                ui.frame()
            ui.click(self._centre(app, "import.go"), settle=3)
            app.show_import = False
            app.open_gameplay("Map")
            for _ in range(3):
                ui.frame()
            sky_texture = next(t["id"] for t in c.library.catalog["textures"] if t.get("sky"))
            c.project.definitions.edit("Change sky", lambda: c.project.set_map_sky("main", {"texture": sky_texture, "horizon": 0.5,
                                                                                               "vertical_degrees": 45.0}))
            for _ in range(2):
                ui.frame()
            self.assertIsNotNone(app.renderer.sky_record())
            self.assertIn("gameplay.sky", app.previous_anchors)
            draws_with_sky = app.renderer.draw_calls
            c.undo()
            for _ in range(2):
                ui.frame()
            self.assertIsNone(app.renderer.sky_record())
            self.assertEqual(app.renderer.draw_calls, draws_with_sky - 1)
            # The city section builds a map from OpenStreetMap data in the background.
            app.show_gameplay = False
            app.open_import()
            (folder / "town.osm").write_text(CITY.format(nodes=_city_nodes()), encoding="utf-8")
            app.dialogs = ScriptedDialogs(folder / "town.osm")
            app.choose_city_file(app.dialogs.open_file("x", []))
            self.assertAlmostEqual(app.city_state["lat"], 45.0, 2)
            app.city_state.update({"half": 256.0, "title": "Town"})
            app.start_city()
            for _ in range(600):
                ui.frame()
                if app.city_state["summary"] or app.city_state["error"]:
                    break
                __import__("time").sleep(0.02)
            self.assertIsNone(app.city_state["error"])
            self.assertIn("7 buildings", app.city_state["summary"])
            self.assertEqual(c.active_map, "town")
            self.assertIn("OpenStreetMap contributors", c.project.project["description"])
            c.undo()
            self.assertEqual(sum(1 for _ in c.document.scene.records("instances")), 0)

    @staticmethod
    def _centre(app, name):
        x0, y0, x1, y1 = app.previous_anchors[name]
        return ((x0 + x1) / 2, (y0 + y1) / 2)


if __name__ == "__main__":
    unittest.main()
