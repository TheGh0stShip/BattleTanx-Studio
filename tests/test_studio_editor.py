"""BTGA Studio editor tests on synthetic fixtures (no game data, no windows).

Requires the editor dependencies (tools/btga_studio/requirements.txt).  The
off-screen UI tests additionally need an EGL-capable Mesa and are skipped
otherwise.
"""

from __future__ import annotations

import gc
import json
import math
import os
import random
import shutil
import tempfile
import time
import tracemalloc
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover - reported as a skip
    np = None

ROOT = Path(__file__).resolve().parents[1]
REQUIRES = "install tools/btga_studio/requirements.txt (numpy) to run the editor tests"


def sample_root() -> Path:
    if not hasattr(sample_root, "path"):
        from tools.btga_studio.samples import write_sample_catalog

        sample_root.path = Path(tempfile.mkdtemp(prefix="btga-studio-sample-"))
        write_sample_catalog(sample_root.path)
    return sample_root.path


def library():
    from tools.btga_assets.studio import StudioLibrary

    return StudioLibrary.open(sample_root())


def level_id(lib, platform="n64_btga"):
    return next(l.id for l in lib.levels() if l.platform == platform)


def document(platform="n64_btga"):
    from tools.btga_studio.core.document import EditorDocument
    from tools.btga_studio.core.scene import EditorScene

    lib = library()
    return EditorDocument(EditorScene(lib, level_id(lib, platform)))


@unittest.skipIf(np is None, REQUIRES)
class CameraTests(unittest.TestCase):
    def setUp(self):
        from tools.btga_studio.core import geometry as g

        self.g = g

    def test_forward_right_and_movement(self):
        camera = self.g.Camera(speed=100)
        np.testing.assert_allclose(camera.forward(), [0, 0, -1], atol=1e-12)
        np.testing.assert_allclose(camera.right(), [1, 0, 0], atol=1e-12)
        camera.move(1.0, forward=1)
        np.testing.assert_allclose(camera.position, [0, 0, -100], atol=1e-9)
        camera.move(1.0, strafe=1, fast=True)
        np.testing.assert_allclose(camera.position, [400, 0, -100], atol=1e-9)
        camera.move(0.5, vertical=1)
        self.assertAlmostEqual(camera.position[1], 50)
        camera.pitch = math.radians(-60)
        before = camera.position.copy()
        camera.move(1.0, forward=1)
        self.assertAlmostEqual(camera.position[1], before[1])  # WASD stays horizontal

    def test_look_clamps_pitch_and_speed(self):
        camera = self.g.Camera()
        camera.look(0, -1e6)
        self.assertAlmostEqual(camera.pitch, camera.max_pitch)
        camera.look(100, 0)
        self.assertLess(camera.yaw, 0)  # mouse right turns right (clockwise from above)
        camera.adjust_speed(-1000)
        self.assertEqual(camera.speed, 1.0)

    def test_projection_and_screen_ray_agree(self):
        camera = self.g.Camera(position=np.array([10.0, 50.0, 300.0]), yaw=0.3, pitch=-0.2)
        point = np.array([40.0, 0.0, -100.0])
        x, y = self.g.project(camera, point, 800, 600)
        ray = self.g.screen_ray(camera, x, y, 800, 600)
        offset = point - ray.origin
        distance = np.linalg.norm(offset - (offset @ ray.direction) * ray.direction)
        self.assertLess(distance, 1e-6)
        centre = self.g.screen_ray(camera, 399.5, 299.5, 800, 600)
        np.testing.assert_allclose(centre.direction, camera.forward(), atol=1e-9)

    def test_screen_y_points_down(self):
        camera = self.g.Camera()
        top = self.g.screen_ray(camera, 400, 0, 800, 600)
        self.assertGreater(top.direction[1], 0)

    def test_yaw_matrix_direction(self):
        np.testing.assert_allclose(self.g.yaw_matrix3(0x4000) @ [1, 0, 0], [0, 0, -1], atol=1e-12)
        np.testing.assert_allclose(self.g.to_local([0, 0, -10], [0, 0, 0], 0x4000), [10, 0, 0], atol=1e-9)
        self.assertEqual(self.g.yaw_units(math.pi / 2), 0x4000)

    def test_snapping(self):
        self.assertEqual(self.g.snap(37, 16), 32)
        self.assertEqual(self.g.snap(37, 0), 37)
        self.assertEqual(self.g.snap_yaw(0x1000, 45), 0)
        self.assertEqual(self.g.snap_yaw(0x1800, 45), 0x2000)
        self.assertEqual(self.g.snap_yaw(0x0B00, 15), 0x0AAB)
        self.assertEqual(self.g.snap_yaw(0x3000, 90), 0x4000)
        self.assertEqual(self.g.snap_yaw(0x1234, 0), 0x1234)


@unittest.skipIf(np is None, REQUIRES)
class PickingTests(unittest.TestCase):
    def setUp(self):
        from tools.btga_studio.core import geometry as g

        self.g = g
        self.doc = document()
        self.scene = self.doc.scene

    def down(self, x, z, height=2000.0):
        return self.g.Ray(np.array([x, height, z], float), np.array([0.0, -1.0, 0.0]))

    def test_triangles_nearest_and_lowest_index(self):
        a = np.array([[0, 0, 0], [0, 0, 0]], float)
        b = np.array([[1, 0, 0], [1, 0, 0]], float)
        c = np.array([[0, 0, 1], [0, 0, 1]], float)
        ray = self.g.Ray(np.array([0.2, 5.0, 0.2]), np.array([0.0, -1.0, 0.0]))
        self.assertEqual(self.g.ray_triangles(ray, a, b, c), (5.0, 0))
        stacked = self.g.ray_triangles(ray, a + [[0, 0, 0], [0, 2, 0]], b + [[0, 0, 0], [0, 2, 0]], c + [[0, 0, 0], [0, 2, 0]])
        self.assertEqual(stacked, (3.0, 1))  # the nearer, higher triangle wins
        miss = self.g.Ray(np.array([0.9, 5.0, 0.9]), np.array([0.0, -1.0, 0.0]))
        self.assertIsNone(self.g.ray_triangles(miss, a, b, c))

    def test_mesh_collision_and_marker_hits(self):
        hits = self.scene.pick(self.down(-400, -200))
        families = [(h.family, h.id) for h in hits]
        self.assertEqual(families[0], ("collision", "n64_btga:placement:building0:collision"))
        self.assertIn(("instances", "n64_btga:placement:building0"), families)
        self.assertEqual(hits[0].distance, 2000 - 240)
        marker = self.scene.pick(self.down(-600, -600))
        self.assertEqual(marker[0].id, "n64_btga:marker:spawner")
        ground = self.scene.pick(self.down(1000, 1000))
        self.assertEqual([h.id for h in ground], ["n64_btga:placement:ground"])
        self.assertAlmostEqual(ground[0].distance, 2000)

    def test_rotated_instance_is_picked_in_local_space(self):
        # building1 is at (400, 200) with yaw 0x2000 (45 degrees); its corner is at radius 141.
        inside = self.scene.pick(self.down(400 + 135, 200), {"instances"})
        self.assertEqual(inside[0].id, "n64_btga:placement:building1")
        outside = self.scene.pick(self.down(400 + 100, 200 + 100), {"instances"})
        self.assertEqual(outside[0].id, "n64_btga:placement:ground")

    def test_tie_breaking_is_deterministic(self):
        from tools.btga_studio.core.scene import Hit

        hits = [Hit("b", "instances", 5.0, (0, 0, 0)), Hit("a", "instances", 5.0, (0, 0, 0)), Hit("z", "markers", 5.00001, (0, 0, 0)),
                Hit("c", "collision", 5.0, (0, 0, 0))]
        self.assertEqual([h.id for h in sorted(hits, key=Hit.key)], ["z", "c", "a", "b"])

    def test_layer_filter(self):
        hits = self.scene.pick(self.down(-400, -200), {"instances"})
        self.assertTrue(all(h.family == "instances" for h in hits))


@unittest.skipIf(np is None, REQUIRES)
class EditingTests(unittest.TestCase):
    def setUp(self):
        self.doc = document()

    def test_drag_is_one_transaction_and_cancel_restores(self):
        doc = self.doc
        target = "n64_btga:placement:building0"
        before = json.dumps(doc.record(target), sort_keys=True)
        doc.begin_transform([target])
        for step in range(50):
            doc.update_transform((step, 0, 0), step * 10)
        doc.cancel_transform()
        self.assertEqual(json.dumps(doc.record(target), sort_keys=True), before)
        self.assertEqual(doc.undo_stack, [])
        doc.begin_transform([target])
        for step in range(50):
            doc.update_transform((step, 0, 0))
        doc.commit_transform()
        self.assertEqual(len(doc.undo_stack), 1)
        self.assertEqual(len(doc.overlay.edits), 1)
        self.assertEqual(doc.record(target)["translation"], [-351.0, 0.0, -200.0])
        doc.undo()
        self.assertEqual(json.dumps(doc.record(target), sort_keys=True), before)

    def test_multi_selection_rotates_about_pivot(self):
        doc = self.doc
        a, b = "n64_btga:marker:start_a", "n64_btga:marker:start_b"  # (-900, 800) and (900, 800)
        doc.selection = [a, b]
        doc.begin_transform()
        doc.update_transform((0, 0, -600), 0x4000)  # pivot (0, 0, 800); +90 degrees maps (-900, 0) to (0, 900)
        doc.commit_transform()
        np.testing.assert_allclose(doc.record(a)["translation"], [0, 0, 800 + 900 - 600], atol=1e-6)
        np.testing.assert_allclose(doc.record(b)["translation"], [0, 0, 800 - 900 - 600], atol=1e-6)
        self.assertEqual(doc.record(a)["yaw"], 0x8000)
        self.assertEqual(len(doc.undo_stack), 1)

    def test_undo_redo_keeps_ids_and_dirty_state(self):
        doc = self.doc
        new = doc.place_model("n64_btga:model:tower", (100, 0, 100))
        doc.set_transform(new, [200, 0, 200])
        self.assertTrue(doc.dirty)
        doc.mark_saved()
        self.assertFalse(doc.dirty)
        doc.undo()
        self.assertTrue(doc.dirty)
        doc.undo()
        self.assertIsNone(doc.scene.record(new))
        doc.redo()
        doc.redo()
        self.assertEqual(doc.record(new)["translation"], [200.0, 0.0, 200.0])
        self.assertFalse(doc.dirty)
        again = doc.place_model("n64_btga:model:tower", (0, 0, 0))
        self.assertNotEqual(again, new)

    def test_user_ids_are_stable_across_documents(self):
        first = document().place_model("n64_btga:model:tower", (0, 0, 0))
        second = document().place_model("n64_btga:model:tower", (10, 0, 10))
        self.assertEqual(first, second)
        self.assertRegex(first, r"^user:[0-9a-f]{12}:0$")

    def test_duplicate_delete_and_disable(self):
        doc = self.doc
        doc.selection = ["n64_btga:marker:pickup"]
        copies = doc.duplicate()
        self.assertTrue(doc.record(copies[0])["source"]["user_created"])
        self.assertEqual(doc.record(copies[0])["raw_hex"], doc.record("n64_btga:marker:pickup")["raw_hex"])
        doc.delete(copies)
        self.assertIsNone(doc.scene.record(copies[0]))
        doc.delete(["n64_btga:marker:pickup"])
        self.assertTrue(doc.record("n64_btga:marker:pickup")["disabled"])
        self.assertEqual([e["op"] for e in doc.overlay.edits], ["add_marker", "delete", "disable"])

    def test_view_matches_full_apply(self):
        from tools.btga_assets import overlay as ov

        doc = self.doc
        rng = random.Random(7)
        ids = [r["id"] for f, r in doc.scene.records() if f != "collision" and "raw" not in r["id"]]
        for _ in range(200):
            roll = rng.random()
            if roll < 0.4:
                doc.set_transform(rng.choice(ids), [rng.uniform(-900, 900), 0, rng.uniform(-900, 900)], rng.randrange(65536))
            elif roll < 0.6:
                doc.duplicate([rng.choice(ids)], (5, 0, 5))
            elif roll < 0.8:
                doc.undo()
            else:
                doc.redo()
        full = ov.apply(doc.scene.base, doc.overlay)
        expected = {r["id"]: r for family in ("instances", "markers", "collision") for r in full[family]}
        self.assertEqual(set(expected), set(doc.scene.view))
        for key, record in expected.items():
            self.assertEqual(record, doc.scene.view[key][1], key)

    def test_placement_limits_and_snapping(self):
        from tools.btga_studio.core.document import EditError

        doc = self.doc
        with self.assertRaises(EditError):
            doc.place_model("n64_btga:model:tower", (5000, 0, 0))
        placed = doc.place_model("n64_btga:model:tower", (37, 0, 70), grid=32)
        self.assertEqual(doc.record(placed)["translation"], [32.0, 0.0, 64.0])
        with self.assertRaises(EditError):
            doc.set_transform(placed, [0, 0, 9000])
        planar = document("ps1")
        self.assertIn("collision for this object is unknown or planar on this platform",
                      planar.placement((0, 0, 0), template={"collision_status": "none_stored"}).warnings)

    def test_unresolved_records_are_protected(self):
        from tools.btga_studio.core.document import EditError

        doc = self.doc
        raw = "n64_btga:marker:raw"
        for action in (lambda: doc.set_transform(raw, [0, 0, 0]), lambda: doc.delete([raw]), lambda: doc.duplicate([raw]),
                       lambda: doc.set_field(raw, "x", 1), lambda: doc.place_marker(doc.record(raw), (0, 0, 0))):
            with self.assertRaises(EditError):
                action()
        self.assertEqual(doc.overlay.edits, [])

    def test_field_validation_happens_before_the_transaction(self):
        from tools.btga_studio.core.document import EditError

        doc = self.doc
        target = "n64_btga:marker:spawner"
        for value in (0x8000, 0, -1, "x"):
            with self.assertRaises(EditError):
                doc.set_field(target, "unit_type_mask", value)
        doc.set_field(target, "spawn_budget", 0xFFFF)  # sentinel
        doc.set_field(target, "unit_type_mask", 0b111)
        self.assertEqual(len(doc.undo_stack), 2)
        with self.assertRaises(EditError):
            doc.set_field("n64_btga:marker:hazard", "hazard_size", 3)  # closed enum


@unittest.skipIf(np is None, REQUIRES)
class InspectorTests(unittest.TestCase):
    def test_controls_from_schema(self):
        from tools.btga_studio.core import inspector as insp
        from tools.btga_studio.core.document import EditError

        doc = document()
        model = insp.describe(doc.record("n64_btga:marker:spawner"), "markers")
        self.assertEqual(model.title, "Unit generator")
        kinds = {c.name: c.kind for c in model.controls}
        self.assertEqual((kinds["unit_type_mask"], kinds["spawn_mode"], kinds["owner_slot"], kinds["translation"], kinds["yaw"]),
                         ("bitmask", "enum", "slot", "position", "angle"))
        mask = next(c for c in model.controls if c.name == "unit_type_mask")
        self.assertEqual(insp.parse_value(mask, "0x5"), 5)
        with self.assertRaises(EditError):
            insp.parse_value(mask, "0x8000")
        with self.assertRaises(EditError):
            insp.parse_value(mask, "five")
        raw = insp.describe(doc.record("n64_btga:marker:raw"), "markers")
        self.assertIsNotNone(raw.read_only_reason)
        self.assertTrue(all(not c.editable for c in raw.controls))
        collision = insp.describe(document("ps1").record("ps1:placement:building0:collision"), "collision")
        self.assertIn("planar", " ".join(collision.notes))
        self.assertEqual(insp.describe(doc.record("n64_btga:placement:tower"), "instances").kind, "prop")
        self.assertEqual(insp.describe_environment(doc.scene.base["environment"][0]).controls[0].kind, "colour")
        self.assertEqual(insp.describe_minimap(doc.scene.base["minimap"]).kind, "minimap")
        self.assertEqual(insp.describe_nav_point(doc.scene.navigation, 1).kind, "navigation")
        for title, record_id in (("Spawn / team start", "n64_btga:marker:start_a"), ("Pickup", "n64_btga:marker:pickup"),
                                 ("Objective", "n64_btga:marker:objective"), ("Hazard", "n64_btga:marker:hazard")):
            self.assertEqual(insp.describe(doc.record(record_id), "markers").title, title)

    def test_unresolved_field_is_not_editable(self):
        from tools.btga_studio.core import inspector as insp

        spec = {"value": 1, "type": "u8", "offset": 3, "range": [0, 255], "evidence": "unresolved", "meaning": "?"}
        control = insp.control_for("byte_3", spec, None)
        self.assertFalse(control.editable)
        self.assertIn("unresolved", control.reason)


@unittest.skipIf(np is None, REQUIRES)
class ProjectTests(unittest.TestCase):
    def test_round_trip_is_deterministic(self):
        from tools.btga_studio.core import project

        doc = document()
        doc.place_model("n64_btga:model:tower", (100, 0, 100), yaw=0x1234)
        doc.set_field("n64_btga:marker:spawner", "spawn_budget", 3)
        with tempfile.TemporaryDirectory() as temp:
            first, second = Path(temp) / "a.btgaproj", Path(temp) / "b.btgaproj"
            project.save(first, doc)
            reloaded = project.load(first, library())
            project.save(second, reloaded)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            text = first.read_text()
            self.assertNotIn(temp, text)
            self.assertNotIn("time", json.loads(text))
            self.assertEqual(json.loads(text)["schema_version"], 2)
            self.assertEqual(reloaded.record("n64_btga:marker:spawner")["fields"]["spawn_budget"]["value"], 3)
            self.assertFalse(reloaded.dirty)
            self.assertEqual(reloaded.record("n64_btga:marker:spawner")["source"], {"synthetic": True})

    def test_source_mismatch_is_refused(self):
        from tools.btga_studio.core import project

        doc = document()
        text = project.dumps_document(doc)
        data = json.loads(text)
        data["maps"]["main"]["source_fingerprint"] = "0" * 64
        with self.assertRaises(project.ProjectMismatch):
            project.loads(json.dumps(data), library())
        data = json.loads(text)
        data["maps"]["main"]["scene"] = "n64_btga:scene:missing"
        with self.assertRaises(project.ProjectMismatch):
            project.loads(json.dumps(data), library())
        with self.assertRaises(project.ProjectError):
            project.loads("{}", library())

    def test_autosave_is_off_by_default_and_writes_when_enabled(self):
        from tools.btga_studio.core import project

        doc = document()
        doc.place_model("n64_btga:model:tower", (0, 0, 0))
        clock = [0.0]
        with tempfile.TemporaryDirectory() as temp:
            off = project.Autosaver(Path(temp), clock=lambda: clock[0])
            clock[0] = 1000
            self.assertIsNone(off.tick(doc))
            on = project.Autosaver(Path(temp), enabled=True, interval=60, clock=lambda: clock[0])
            self.assertIsNone(on.tick(doc))
            clock[0] = 1100
            path = on.tick(doc)
            self.assertTrue(path.is_file())
            on.discard(doc)
            self.assertFalse(path.exists())

    def test_preview_package(self):
        from tools.btga_studio.core import project

        doc = document()
        with tempfile.TemporaryDirectory() as temp:
            written = project.export_preview(Path(temp), doc, library())
            self.assertEqual(sorted(p.name for p in written), ["README.txt", "edited_scene.json", "plan.png", "project.btgaproj", "validation.json"])


@unittest.skipIf(np is None, REQUIRES)
class DiagnosticsTests(unittest.TestCase):
    def codes(self, doc):
        from tools.btga_studio.core import diagnostics

        return {d.code: d for d in diagnostics.collect(doc, library())}

    def test_clean_sample_and_detected_problems(self):
        from tools.btga_studio.core.controller import EditorController
        from tools.btga_studio.core.settings import Settings

        doc = document()
        self.assertEqual(set(self.codes(doc)), set())
        doc.set_transform("n64_btga:marker:start_b", [-890, 0, 800])
        doc.set_transform("n64_btga:marker:start_a", [-400, 0, -200])
        doc.overlay.add({"op": "move", "target": "n64_btga:marker:raw", "translation": [0, 0, 0]})
        doc.overlay.add({"op": "set_field", "target": "n64_btga:marker:spawner", "field": "unit_type_mask", "value": 0})
        doc.scene.refresh(doc.overlay)
        codes = self.codes(doc)
        self.assertEqual(codes["SPAWN_IN_COLLISION"].target, "n64_btga:marker:start_a")
        self.assertIn("UNRESOLVED_MODIFIED", codes)
        self.assertEqual(codes["INVALID_SPAWNER_MASK"].target, "n64_btga:marker:spawner")
        doc.set_transform("n64_btga:marker:start_a", [-900, 0, 800])
        self.assertIn("OVERLAPPING_STARTS", self.codes(doc))
        with tempfile.TemporaryDirectory() as temp:
            os.environ["BTGA_STUDIO_HOME"] = temp
            controller = EditorController(Settings.open())
            controller.open_catalog(sample_root())
            controller.new_project(doc.scene.level_id)
            controller.document.camera = None
            controller.frame_record(codes["INVALID_SPAWNER_MASK"].target)
            self.assertEqual(controller.document.selection, ["n64_btga:marker:spawner"])
            controller.shutdown()

    def test_dangling_reference(self):
        doc = document()
        doc.overlay.add({"op": "add_instance", "instance": {"model": "n64_btga:model:missing", "translation": [0, 0, 0], "yaw": 0,
                                                             "category": "model", "source": {"user_created": True}}})
        doc.scene.refresh(doc.overlay)
        self.assertIn("DANGLING_REFERENCE", self.codes(doc))


@unittest.skipIf(np is None, REQUIRES)
class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_keys_change_with_every_input(self):
        from tools.btga_studio.core import thumbnails as th

        base = th.cache_key("m", 96, 96)
        self.assertEqual(base, th.cache_key("m", 96, 96))
        for other in (th.cache_key("n", 96, 96), th.cache_key("m", 64, 64), th.cache_key("m", 96, 96, renderer="r2"),
                      th.cache_key("m", 96, 96, preset="top")):
            self.assertNotEqual(base, other)

    def test_eviction_corruption_and_stale_format(self):
        from tools.btga_studio.core import thumbnails as th

        cache = th.ThumbnailCache(self.temp, max_bytes=3 * (th.HEADER.size + 16))
        for name in "abcd":
            cache.put(name * 64, 2, 2, bytes(16))
        self.assertEqual(len(cache), 3)
        self.assertIsNone(cache.get("a" * 64))
        self.assertEqual(cache.evictions, 1)
        self.assertEqual(cache.get("b" * 64), (2, 2, bytes(16)))
        path = cache.path("c" * 64)
        path.write_bytes(path.read_bytes()[:-1] + b"\x01")
        self.assertIsNone(cache.get("c" * 64))
        self.assertFalse(path.exists())
        self.assertEqual(cache.corrupt, 1)
        data = bytearray(cache.path("d" * 64).read_bytes())
        data[4] = 9  # unknown format version
        cache.path("d" * 64).write_bytes(bytes(data))
        self.assertIsNone(cache.get("d" * 64))
        reopened = th.ThumbnailCache(self.temp, max_bytes=10_000)
        self.assertEqual(len(reopened), 1)

    def test_service_priority_limit_cancel_and_failures(self):
        from tools.btga_studio.core import thumbnails as th

        started = []

        class Immediate:
            def submit(self, fn, *args):
                from concurrent.futures import Future

                started.append(args[1])
                future = Future()
                try:
                    future.set_result(fn(*args))
                except Exception as error:
                    future.set_exception(error)
                return future

            def shutdown(self, **_):
                pass

        def render(root, model_id, size):
            if model_id == "bad":
                raise ValueError("broken model")
            return size, size, bytes(size * size * 4)

        service = th.ThumbnailService(self.temp, th.ThumbnailCache(self.temp / "c"), workers=2, executor=Immediate(), render=render)
        for name, priority in (("low", 50), ("high", 1), ("mid", 10), ("bad", 5)):
            self.assertIsNone(service.request(name, 4, priority))
        service.pump()
        self.assertEqual(started, ["high", "bad"])  # priority order, two workers
        service.cancel_older_than()
        self.assertEqual(service.pending, 2)  # in flight are kept, queued dropped
        service.pump()
        service.pump()
        self.assertEqual(started, ["high", "bad"])
        self.assertIn(th.cache_key("bad", 4, 4), service.failed)
        self.assertEqual(service.request("high", 4)[0], 4)
        self.assertIsNone(service.request("bad", 4))
        self.assertEqual(service.cancelled, 2)

    def test_real_concurrent_rendering_and_lookup_latency(self):
        from tools.btga_studio.core import thumbnails as th

        cache = th.ThumbnailCache(self.temp)
        service = th.ThumbnailService(sample_root(), cache, workers=2, executor=ThreadPoolExecutor(2))
        models = sorted(library()._models)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            results = [service.request(m, 24) for m in models]
            service.pump()
            if all(results):
                break
            time.sleep(0.02)
        self.assertTrue(all(results))
        service.shutdown()
        fresh = th.ThumbnailService(sample_root(), th.ThumbnailCache(self.temp), workers=1)
        timings = []
        for model in models * 10:
            fresh._memory.clear()
            start = time.perf_counter()
            self.assertIsNotNone(fresh.request(model, 24))
            timings.append(time.perf_counter() - start)
        timings.sort()
        self.assertLess(timings[int(len(timings) * 0.95)], 0.020)


@unittest.skipIf(np is None, REQUIRES)
class LazyAndMemoryTests(unittest.TestCase):
    def test_browser_reads_only_the_catalog(self):
        from tools.btga_studio.core.browser import AssetIndex

        lib = library()
        reads = []
        original = lib._read
        lib._read = lambda relative: reads.append(relative) or original(relative)
        index = AssetIndex(lib)
        for platform in (None, "ps1", "n64_bt1"):
            for kind in (None, "model", "texture"):
                index.filter("t", platform, kind)
        self.assertEqual(reads, [])
        doc = document()
        index.set_map(doc.scene)
        gameplay = index.filter(kind="gameplay", map_only=True)
        self.assertTrue(any(e.category == "Unit generators" for e in gameplay))
        self.assertFalse(next(e for e in gameplay if e.id.endswith(":raw")).editable)

    @unittest.skipIf(os.environ.get("BTGA_MUTATION_GATE") == "1", "slow soak is covered by the full suite")
    def test_thousand_edits_ten_cycles_plateau(self):
        import importlib

        benchmark = importlib.import_module("tools.benchmark_studio_editor")
        from tools.btga_studio.core.document import EditorDocument
        from tools.btga_studio.core.scene import EditorScene, MeshCache

        lib = library()
        cache = MeshCache(lib)
        rng = random.Random(3)
        tracemalloc.start()
        heaps = []
        for cycle in range(10):
            doc = EditorDocument(EditorScene(lib, level_id(lib), cache))
            benchmark.mixed_edits(doc, 1000, rng)
            while doc.undo_stack:
                doc.undo()
            self.assertEqual(doc.overlay.edits, [])
            del doc
            gc.collect()
            heaps.append(tracemalloc.get_traced_memory()[0])
        tracemalloc.stop()
        second = heaps[5:]
        self.assertLess(max(second) - min(second), 64 * 1024, heaps)


@unittest.skipIf(np is None, REQUIRES)
class SettingsAndInputTests(unittest.TestCase):
    def test_preferences_are_separate_from_projects(self):
        from tools.btga_studio.core import project
        from tools.btga_studio.core.settings import Settings

        with tempfile.TemporaryDirectory() as temp:
            os.environ["BTGA_STUDIO_HOME"] = temp
            settings = Settings.open()
            self.assertEqual(settings.directory, Path(temp))
            settings.preferences.wireframe = True
            settings.preferences.add_recent("a.btgaproj")
            settings.save()
            again = Settings.open()
            self.assertTrue(again.preferences.wireframe)
            doc = document()
            text = project.dumps_document(doc)
            self.assertNotIn("wireframe", text)
            self.assertNotIn("overlay", settings.preferences_file.read_text())

    def test_commands_are_blocked_while_typing(self):
        from tools.btga_studio.gui.input import InputState, commands

        typing = InputState(viewport_hovered=True, viewport_focused=True, text_input=True, keys_down={"W", "Ctrl"},
                            keys_pressed={"Z", "F", "Delete"}, left_clicked=True)
        self.assertEqual(commands(typing), [])
        moving = InputState(viewport_hovered=True, keys_down={"W", "Shift"})
        self.assertEqual([(c.name, c.args["forward"], c.args["fast"]) for c in commands(moving)], [("move", 1, True)])
        elsewhere = InputState(viewport_hovered=False, keys_down={"W"}, keys_pressed={"Delete"})
        self.assertEqual(commands(elsewhere), [])
        undo = InputState(keys_down={"Ctrl"}, keys_pressed={"Z"})
        self.assertEqual([c.name for c in commands(undo)], ["undo"])
        redo = InputState(keys_down={"Ctrl", "Shift"}, keys_pressed={"Z"})
        self.assertEqual([c.name for c in commands(redo)], ["redo"])
        gizmo = InputState(viewport_hovered=True, left_clicked=True, gizmo_active=True)
        self.assertEqual(commands(gizmo), [])
        click = InputState(viewport_hovered=True, left_clicked=True, keys_down={"Ctrl"}, mouse=(3, 4))
        self.assertEqual([(c.name, c.args["toggle"]) for c in commands(click)], [("click", True)])  # Ctrl+click toggles
        click = InputState(viewport_hovered=True, left_clicked=True, mouse=(3, 4))
        self.assertEqual([(c.name, c.args["x"]) for c in commands(click)], [("click", 3)])


@unittest.skipIf(np is None, REQUIRES)
class ControllerTests(unittest.TestCase):
    def setUp(self):
        from tools.btga_studio.core.controller import EditorController
        from tools.btga_studio.core.settings import Settings

        self.temp = tempfile.mkdtemp()
        os.environ["BTGA_STUDIO_HOME"] = self.temp
        self.controller = EditorController(Settings.open())
        self.controller.open_catalog(sample_root())
        self.controller.new_project(level_id(self.controller.library))

    def tearDown(self):
        self.controller.shutdown()
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_click_select_toggle_clear_and_place(self):
        from tools.btga_studio.core import geometry as g

        c = self.controller
        c.viewport = (800, 600)
        c.camera = g.Camera(position=np.array([-400.0, 3000.0, -200.0]), pitch=-math.pi / 2 + 1e-6)
        c.click(399.5, 299.5)
        self.assertEqual(c.document.selection, ["n64_btga:placement:building0:collision"])
        c.camera.position = np.array([-600.0, 3000.0, -600.0])
        c.click(399.5, 299.5, toggle=True)
        self.assertEqual(len(c.document.selection), 2)
        c.camera.position = np.array([5000.0, 3000.0, 5000.0])
        c.click(399.5, 299.5)
        self.assertEqual(c.document.selection, [])
        c.camera.position = np.array([100.0, 3000.0, 100.0])
        c.begin_placement("model", "n64_btga:model:tower")
        c.hover(399.5, 299.5)
        self.assertTrue(c.preview["valid"])
        new = c.place_at(399.5, 299.5)
        np.testing.assert_allclose(c.document.record(new)["translation"], [100, 0, 100], atol=0.01)
        c.cancel()
        self.assertIsNone(c.placement)

    def test_gizmo_flow_and_duplicate_drag(self):
        from tools.btga_studio.core import geometry as g

        c = self.controller
        c.document.selection = ["n64_btga:placement:tower"]
        self.assertTrue(c.gizmo_begin(duplicate=True))
        moved = g.model_matrix([520.0, 0.0, -500.0], 0x4000)
        c.gizmo_update(moved)
        c.gizmo_end()
        records = [r for f, r in c.document.scene.records("instances") if r["model"].endswith(":tower")]
        self.assertEqual(len(records), 2)
        duplicate = next(r for r in records if r.get("source", {}).get("user_created"))
        np.testing.assert_allclose(duplicate["translation"], [520, 0, -500], atol=1e-6)
        self.assertEqual(duplicate["yaw"], 0x4000)
        self.assertEqual(len(c.document.undo_stack), 2)

    def test_async_diagnostics(self):
        c = self.controller
        c.document.overlay.add({"op": "set_field", "target": "n64_btga:marker:spawner", "field": "unit_type_mask", "value": 0})
        c.document.scene.refresh(c.document.overlay)
        c.request_diagnostics(immediate=True)
        c.poll_diagnostics(wait=True)
        self.assertIn("INVALID_SPAWNER_MASK", {d.code for d in c.diagnostics})


def _egl_available() -> bool:
    try:
        import imgui_bundle  # noqa: F401
        from OpenGL import EGL  # noqa: F401
    except Exception:
        return False
    return os.name == "posix"


@unittest.skipIf(np is None or not _egl_available(), "needs imgui-bundle, PyOpenGL and EGL (off-screen UI)")
class OffscreenUITests(unittest.TestCase):
    """Runs the full editor UI against an off-screen framebuffer: no window is created."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
        from tools.btga_studio.core.settings import Settings
        from tools.btga_studio.gui.app import StudioApp
        from tools.btga_studio.gui.offscreen import OffscreenUI

        cls.temp = tempfile.mkdtemp()
        os.environ["BTGA_STUDIO_HOME"] = cls.temp
        cls.app = StudioApp(Settings.open(), sample_root())
        cls.ui = OffscreenUI(cls.app, 1280, 800)

    @classmethod
    def tearDownClass(cls):
        cls.ui.close()
        shutil.rmtree(cls.temp, ignore_errors=True)

    def test_open_map_select_and_type_without_triggering_shortcuts(self):
        from imgui_bundle import imgui

        app, ui = self.app, self.ui
        app.controller.new_project(level_id(app.controller.library))
        app._load_into_renderer()
        for _ in range(3):
            ui.frame()
        self.assertGreater(app.renderer.draw_calls, 0)
        before = app.controller.camera.position.copy()
        # Click the asset search box (bottom-left panel) and type "wasd".
        ui.frame(mouse=(200, 0.72 * 800 + 30), buttons=(True, False))
        ui.frame(mouse=(200, 0.72 * 800 + 30), buttons=(False, False))
        for character in "wasd":
            ui.frame(mouse=(200, 0.72 * 800 + 30), keys={imgui.Key.w: True}, text=character)
        ui.frame(keys={imgui.Key.w: False})
        np.testing.assert_allclose(app.controller.camera.position, before)
        self.assertEqual(app.search, "wasd")
        app.search = ""
        # Click the middle of the viewport (no text focus): selection happens through picking.
        app.controller.document.selection = []
        centre = (0.18 * 1280 + 0.58 * 1280 / 2, 0.03 * 800 + 0.69 * 800 / 2)
        ui.frame(mouse=centre, buttons=(True, False))
        ui.frame(mouse=centre, buttons=(False, False))
        ui.frame(mouse=centre)
        self.assertTrue(app.controller.document.selection)
        with tempfile.TemporaryDirectory() as temp:
            ui.screenshot(Path(temp) / "ui.png")
            self.assertGreater((Path(temp) / "ui.png").stat().st_size, 10_000)


if __name__ == "__main__":
    unittest.main()
