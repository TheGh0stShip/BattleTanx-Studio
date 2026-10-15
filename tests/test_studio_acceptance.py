"""End-to-end ROM-free acceptance: the real application, controller, input, project, scene and renderer off-screen.

The workflow is driven through Dear ImGui input events (mouse clicks in the
viewport, keyboard shortcuts) on the off-screen EGL framebuffer, so the
viewport hit-testing, input mapping, controller, document, project and
renderer layers all take part.  Only Studio starter content is used.

Also holds regression tests for defects found during acceptance review.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

from tests.test_studio_authoring import _egl_available, example_project, starter_library

REQUIRES = "install tools/btga_studio/requirements.txt to run the acceptance tests"


def _top_down_camera(height: float = 3200.0):
    from tools.btga_studio.core import geometry as g

    return g.Camera(position=np.array([0.0, height, 0.0]), pitch=-(math.pi / 2 - 1e-6))


def _structure(project) -> dict:
    """Project content without the live documents, for lossless round-trip comparison."""

    from tools.btga_studio.core import project as projects

    return json.loads(projects.dumps(project))


@unittest.skipIf(np is None or not _egl_available(), "needs PyOpenGL, imgui-bundle and EGL (off-screen rendering)")
class OffscreenAcceptanceTests(unittest.TestCase):
    def test_complete_rom_free_workflow_through_the_ui(self):
        from tools.btga_studio.core import project as projects
        from tools.btga_studio.core.package import build_package, read_package, validate_for_export
        from tools.btga_studio.core.settings import Settings
        from tools.btga_studio.gui.app import StudioApp
        from tools.btga_studio.gui.offscreen import OffscreenUI

        with tempfile.TemporaryDirectory() as temp:
            os.environ["BTGA_STUDIO_HOME"] = str(Path(temp) / "home dir ü")
            app = StudioApp(Settings.open())
            ui = OffscreenUI(app, 1600, 960)
            c = app.controller
            try:
                # Only original starter content is loaded; no source is registered.
                self.assertEqual({level.platform for level in c.library.levels()} - {"studio"}, set())
                self.assertEqual(len(c.registry.sources), 0)

                # 1-3: new project and blank map through the wizard.
                app.show_new_project = True
                ui.frame()
                app.wizard.update({"title": "Acceptance ü", "key": "field", "map_title": "Field map", "size": 2048.0, "map_kind": 0})
                self.assertTrue(app.guarded("new project", c.new_studio_project, app.wizard["title"], "studio"))
                self.assertTrue(app.create_map_from_wizard())
                app.show_new_project = False
                c.camera = _top_down_camera()
                ui.frame()
                ui.frame()
                document = c.document
                self.assertIsNotNone(document)

                # 4 and 15: placement by clicking in the viewport, with grid snapping.
                starter = c.library.starter()
                models = starter["models"]
                templates = {t["source_kind"].removeprefix("studio_"): t["id"] for t in starter["templates"]}
                c.preferences.grid_snap = 64.0
                plan = [("model", models["ground_tile"], (-700, -700)), ("model", models["road_straight"], (0, -700)),
                        ("model", models["wall"], (700, -700)), ("model", models["ramp"], (-700, 0)),
                        ("model", models["building_small"], (0, 0)), ("model", models["building_tall"], (700, 0)),
                        ("marker", templates["player_spawn"], (-700, 700)), ("marker", templates["pickup"], (-250, 700)),
                        ("marker", templates["hazard"], (250, 700)), ("marker", templates["objective"], (700, 700)),
                        ("marker", templates["nav_node"], (-350, 350)), ("marker", templates["nav_node"], (350, 350))]
                placed = []
                for kind, resource, (x, z) in plan:
                    before = set(document.scene.view)
                    app.try_placement(kind, resource)
                    screen = ui.world_to_screen((x + 10.0, 0.0, z - 10.0))  # off-grid on purpose
                    ui.click(screen)
                    ui.press("escape", mouse=screen)
                    self.assertIsNone(c.placement)
                    new = sorted(set(document.scene.view) - before)
                    self.assertEqual(len(new), 1, (kind, resource))
                    translation = document.record(new[0])["translation"]
                    self.assertEqual((translation[0] % 64.0, translation[2] % 64.0), (0.0, 0.0), translation)
                    placed.append(new[0])
                self.assertEqual(len(document.undo_stack), len(plan))
                building, tall = placed[4], placed[5]

                # 5 and 17: click selection and Ctrl+click multi-selection.
                c.preferences.grid_snap = 0.0
                ui.click(ui.world_to_screen(document.record(building)["translation"]))
                self.assertEqual(document.selection, [building])
                ui.click(ui.world_to_screen(document.record(tall)["translation"]), ctrl=True)
                self.assertEqual(sorted(document.selection), sorted([building, tall]))
                empty = ui.world_to_screen((1100.0, 0.0, 1100.0))
                ui.click(empty)  # empty space clears
                self.assertEqual(document.selection, [])

                # 6-8, 16 and 18: gizmo transactions about the group pivot with angle snapping.
                document.selection = [building, tall]
                pivot = np.mean([document.record(i)["translation"] for i in document.selection], axis=0)
                np.testing.assert_allclose(c.gizmo_matrix()[:3, 3], pivot)
                c.preferences.yaw_snap_degrees = 45
                self.assertTrue(c.gizmo_begin())
                start = c.gizmo_matrix()
                moved = start.copy()
                moved[:3, 3] += (128.0, 0.0, 0.0)
                c.gizmo_update(moved)
                c.gizmo_end()
                np.testing.assert_allclose(np.mean([document.record(i)["translation"] for i in document.selection], axis=0),
                                           pivot + (128.0, 0.0, 0.0))
                before_yaw = document.record(building)["yaw"]
                self.assertTrue(c.gizmo_begin())
                rotated = c._gizmo_start.copy()
                angle = math.radians(50.0)
                rotated[:3, :3] = np.array([[math.cos(angle), 0, math.sin(angle)], [0, 1, 0], [-math.sin(angle), 0, math.cos(angle)]])
                c.gizmo_update(rotated)
                c.gizmo_end()
                self.assertEqual((document.record(building)["yaw"] - before_yaw) & 0xFFFF, 0x2000)  # 50 degrees snapped to 45
                document.selection = [building]
                self.assertTrue(c.gizmo_begin())
                scaled = c._gizmo_start.copy()
                scaled[:3, :3] *= 2.0
                c.gizmo_update(scaled, scale_mode=True)
                c.gizmo_end()
                self.assertEqual(document.record(building)["scale"], [2.0, 2.0, 2.0])
                ui.frame()

                # 9-14: keyboard shortcuts in the viewport.
                centre = ui.viewport_to_screen(c.viewport[0] / 2, c.viewport[1] / 2)
                count = len(document.scene.view)
                ui.press("d", mouse=centre, ctrl=True)
                self.assertEqual(len(document.scene.view), count + 1)
                duplicate = document.selection[0]
                self.assertEqual(document.record(duplicate)["scale"], [2.0, 2.0, 2.0])
                ui.press("c", mouse=centre, ctrl=True)
                c.hover(*ui_local(ui, ui.world_to_screen((-1200.0, 0.0, -1200.0))))
                c.paste()
                pasted = document.selection[0]
                self.assertEqual(len(document.scene.view), count + 2)
                np.testing.assert_allclose(document.record(pasted)["translation"][::2], (-1200.0, -1200.0), atol=1.0)
                ui.press("h", mouse=centre)
                self.assertIn(pasted, document.hidden)
                ui.press("u", mouse=centre)
                self.assertEqual(document.hidden, set())
                document.selection = [building]
                ui.press("i", mouse=centre)
                self.assertNotIn(building, document.hidden)
                self.assertIn(tall, document.hidden)
                ui.press("u", mouse=centre)
                document.selection = [tall]
                ui.press("l", mouse=centre)
                self.assertIn(tall, document.locked)
                ui.click(ui.world_to_screen(document.record(tall)["translation"]))
                self.assertNotEqual(document.selection, [tall])  # locked objects are not pickable
                document.unlock_all()
                document.selection = [pasted]
                ui.press("delete", mouse=centre)
                self.assertIsNone(document.scene.record(pasted))

                # 19: typed inspector fields; invalid values never reach the overlay.
                from tools.btga_studio.core.document import EditError

                objective = placed[9]
                document.set_field(objective, "target", tall)
                document.set_field(objective, "label", "Hold the tower")
                edits = len(document.overlay.edits)
                with self.assertRaises(EditError):
                    document.set_field(objective, "label", 12)
                self.assertEqual(len(document.overlay.edits), edits)
                document.set_field(placed[10], "link_0", placed[11])

                # 26: undo and redo before saving.
                labels = [t.label for t in document.undo_stack]
                snapshot = document.overlay.to_json()
                for _ in range(3):
                    ui.press("z", mouse=centre, ctrl=True)
                self.assertEqual([t.label for t in document.undo_stack], labels[:-3])
                for _ in range(3):
                    ui.press("y", mouse=centre, ctrl=True)
                self.assertEqual(document.overlay.to_json(), snapshot)

                # 20-21: minimap and every definition family.
                project = c.project
                project.maps["field"].minimap = {"mode": "generated", "width": 96, "height": 96}
                project.define("missions", "capture", {"title": "Capture", "map": "field", "objectives": [objective]})
                project.campaigns.append({"id": "story", "title": "Story", "missions": ["capture"]})
                project.define("multiplayer", "field_dm", {"title": "Field deathmatch", "map": "field", "modes": ["deathmatch"], "max_players": 4})
                project.define("minigames", "field_race", {"title": "Field race", "map": "field", "rules": {"laps": 3}})
                project.define("tanks", "runner", {"title": "Runner", "model": models["tank_placeholder"], "stats": {"speed": 1.4}})
                project.define("gangs", "green", {"title": "Green", "colour": [40, 180, 60], "tanks": ["runner"]})
                for _ in range(3):
                    ui.frame()
                self.assertGreater(app.renderer.draw_calls, 0)
                self.assertTrue(c.dirty)
                self.assertIn(" *", app.title)

                # 22-25: save to a path with spaces and non-ASCII, close, reopen, nothing lost.
                path = Path(temp) / "my projects ü" / "acceptance map.btgaproj"
                c.save(path)
                ui.frame()
                self.assertFalse(c.dirty)
                self.assertNotIn(" *", app.title)
                saved = _structure(project)
                saved_bytes = path.read_bytes()
                c.close_map()
                c.project = None
                ui.frame()
                app.open_project(path)
                ui.frame()
                self.assertFalse(c.dirty)
                self.assertEqual(_structure(c.project), saved)
                self.assertEqual(projects.dumps(c.project).encode("utf-8"), saved_bytes)

                # 27: undo and redo after reopening.
                document = c.document
                after = document.overlay.to_json()
                ui.press("z", mouse=centre, ctrl=True)
                self.assertTrue(c.dirty)
                ui.press("y", mouse=centre, ctrl=True)
                self.assertEqual(document.overlay.to_json(), after)
                self.assertFalse(c.dirty)

                # 28-31: deterministic export read back by the strict reader.
                first = build_package(c.project, c.library, "acceptance")
                package = read_package(first)
                self.assertEqual(sorted(package.maps), ["field"])
                self.assertEqual(len(package.maps["field"]["records"]["markers"]), 6)
                second = build_package(projects.load_project(path), c.library, "acceptance")
                self.assertEqual(hashlib.sha256(first).hexdigest(), hashlib.sha256(second).hexdigest())
                self.assertEqual(first, second)

                # 32-33: deliberate validation failure names map, object and field; the UI frames the object.
                c.project.missions["capture"]["objectives"] = ["user:missing:0"]
                c.project.define("multiplayer", "broken", {"title": "Broken", "map": "field", "max_players": 9})
                report = validate_for_export(c.project, c.library)
                found = {(e["code"], e["target"], e["field"]) for e in report.errors}
                self.assertIn(("MISSING_RECORD", "missions:capture", "objectives"), found)
                self.assertIn(("INVALID_FIELD", "multiplayer:broken", "max_players"), found)
                c.request_diagnostics(immediate=True)
                c.poll_diagnostics(wait=True)
                c.frame_record(tall)
                self.assertEqual(document.selection, [tall])
                ui.frame()
            finally:
                ui.close()


def ui_local(ui, screen) -> tuple[float, float]:
    ox, oy = ui.app.viewport_origin
    return (screen[0] - ox, screen[1] - oy)


@unittest.skipIf(np is None, REQUIRES)
class AcceptanceRegressionTests(unittest.TestCase):
    """Defects found during the acceptance review; each test failed before its fix."""

    def setUp(self):
        self.library = starter_library()
        self.cube = self.library.starter()["models"]["cube"]

    def test_duplicate_keeps_instance_scale(self):
        document = example_project().open_map("arena", self.library)
        cube = document.place_model(self.cube, (0.0, 0.0, 600.0))
        document.begin_transform([cube])
        document.update_transform(scale=(2.0, 2.0, 2.0))
        document.commit_transform()
        copy_id = document.duplicate([cube])[0]
        self.assertEqual(document.record(copy_id)["scale"], [2.0, 2.0, 2.0])

    @unittest.skipIf(not _egl_available(), "the renderer module imports PyOpenGL")
    def test_navigation_overlay_draws_authored_nodes_and_links(self):
        from tools.btga_studio.gui.renderer import navigation_segments

        document = example_project().open_map("training_grounds", self.library)
        nodes = sorted(r["id"] for _, r in document.scene.records("markers") if r.get("category") == "navigation_node")
        self.assertEqual(len(nodes), 3)
        self.assertIsNone(document.scene.navigation)  # blank maps carry no source navigation table
        segments = navigation_segments(document.scene)
        crosses, links = 3 * 6, 4 * 2  # three nodes; links 0->1, 1->2, 1->0 and 2->1 (the patrol's way back)
        self.assertEqual(len(segments), crosses + links)
        hidden = next(n for n in nodes if document.record(n)["translation"][2] == 600.0)  # the first node (at the south end)
        self.assertEqual(len(navigation_segments(document.scene, {hidden})), 2 * 6 + 2 * 2)

    def test_user_placed_assets_use_their_model_for_thumbnails(self):
        from concurrent.futures import ThreadPoolExecutor

        from tools.btga_studio.core import thumbnails as th
        from tools.btga_studio.core.browser import AssetIndex

        document = example_project().open_map("arena", self.library)
        index = AssetIndex(self.library)
        index.set_map(document.scene)
        placed = [e for e in index.map_entries if e.source == "user" and e.kind == "model"]
        self.assertTrue(placed)
        for entry in placed:
            self.assertEqual(entry.model_id, document.record(entry.id)["model"])
        catalog = next(e for e in index.catalog_entries if e.kind == "model")
        self.assertEqual(catalog.model_id, catalog.id)
        with tempfile.TemporaryDirectory() as temp:
            service = th.ThumbnailService(self.library.root_for_model, th.ThumbnailCache(Path(temp)), workers=1,
                                          executor=ThreadPoolExecutor(1))
            try:
                self.assertIsNone(service.request(placed[0].id, 32))  # an instance ID is not a model
                service.pump()  # used to raise StudioError and stop the editor
                self.assertEqual(len(service.failed), 1)
            finally:
                service.shutdown()

    @unittest.skipIf(not _egl_available(), "needs EGL (off-screen UI)")
    def test_thumbnail_browser_survives_rom_free_projects(self):
        os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
        from tools.btga_studio.core.settings import Settings
        from tools.btga_studio.gui.app import StudioApp
        from tools.btga_studio.gui.offscreen import OffscreenUI

        with tempfile.TemporaryDirectory() as temp:
            os.environ["BTGA_STUDIO_HOME"] = temp
            app = StudioApp(Settings.open())
            ui = OffscreenUI(app, 1280, 800)
            try:
                app.open_example()
                app.thumbnail_mode = True
                for map_only in (True, False):
                    app.map_only = map_only
                    for _ in range(10):
                        ui.frame()
                user = next(e for e in app.controller.index.map_entries if e.source == "user" and e.kind == "model")
                app.activate_asset(user)
                self.assertEqual(app.controller.document.selection, [user.id])
                self.assertIsNone(app.controller.placement)
            finally:
                ui.close()

    @unittest.skipIf(not _egl_available(), "self_check imports the OpenGL renderer")
    def test_frozen_self_check_verifies_the_shipped_starter_example(self):
        import shutil
        import sys
        from unittest import mock

        from tools.btga_studio.gui.app import self_check

        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            program = Path(temp) / "BTGAStudio dir ü"
            (program / "samples").mkdir(parents=True)
            shutil.copy(root / "examples" / "starter-example.btgaproj", program / "samples" / "starter-example.btgaproj")
            report = Path(temp) / "report.json"
            bundled = Path(temp) / "bundle"
            (bundled / "data").mkdir(parents=True)
            for name in ("800E8380_slot_asset_ranges.c", "build.json"):
                (bundled / "data" / name).write_text("{}", encoding="utf-8")
            with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", str(program / "BTGAStudio.exe")), \
                    mock.patch.object(sys, "_MEIPASS", str(bundled), create=True):
                self.assertEqual(self_check(report), 0, report.read_text(encoding="utf-8"))
                self.assertTrue(json.loads(report.read_text(encoding="utf-8"))["steps"]["shipped_example"])
                (bundled / "data" / "build.json").unlink()
                self.assertEqual(self_check(report), 1)  # missing bundled data fails the packaged self-check
                (bundled / "data" / "build.json").write_text("{}", encoding="utf-8")
                (program / "samples" / "starter-example.btgaproj").write_text("{}", encoding="utf-8")
                self.assertEqual(self_check(report), 1)  # a damaged or missing example fails the packaged self-check

    def _controller(self, temp):
        from tools.btga_studio.core.controller import EditorController
        from tools.btga_studio.core.settings import Settings

        os.environ["BTGA_STUDIO_HOME"] = temp
        controller = EditorController(Settings.open())
        controller.project = example_project()
        controller.switch_map("arena")
        return controller

    def test_cancelled_or_empty_shift_drag_leaves_no_stacked_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            c = self._controller(temp)
            try:
                cube = c.document.place_model(self.cube, (0.0, 0.0, 600.0))
                records = len(c.document.scene.view)
                undo = [t.label for t in c.document.undo_stack]
                c.gizmo_begin(duplicate=True)
                self.assertEqual(len(c.document.scene.view), records + 1)
                c.cancel()  # Esc while dragging
                c.gizmo_update(c.gizmo_matrix())  # ImGuizmo keeps reporting until release; ignored
                c.gizmo_end()
                self.assertEqual(len(c.document.scene.view), records)
                self.assertEqual([t.label for t in c.document.undo_stack], undo)
                self.assertEqual(c.document.selection, [cube])
                c.gizmo_begin(duplicate=True)
                c.gizmo_end()  # released without moving
                self.assertEqual(len(c.document.scene.view), records)
                self.assertEqual(c.document.selection, [cube])
                c.gizmo_begin(duplicate=True)
                moved = c.gizmo_matrix().copy()
                moved[:3, 3] += (100.0, 0.0, 0.0)
                c.gizmo_update(moved)
                c.gizmo_end()
                self.assertEqual(len(c.document.scene.view), records + 1)
                self.assertEqual([t.label for t in c.document.undo_stack][-2:], ["Duplicate", "Transform"])
            finally:
                c.shutdown()

    def test_malformed_projects_raise_project_errors(self):
        from tools.btga_studio.core import project as projects

        header = {"schema": "btga-studio-project", "schema_version": 2}
        cases = [{"maps": [1]}, {"maps": {"a": 5}}, {"maps": {"a": {"kind": "blank", "base": {}}}}, {"project": "x"},
                 {"project": {"target": "x"}}, {"missions": [1]}, {"campaigns": {"a": 1}},
                 {"maps": {"a": {"kind": "blank", "scene": "s", "base": {"x": 1}, "view": []}}}]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(projects.ProjectError):
                projects.loads_project(json.dumps({**header, **case}))
        data = json.loads(projects.dumps(example_project()))
        for history in ({"undo": [{"edits": "x"}]}, {"undo": "x"}, [], {"redo": [{"edits": 3, "user_index_before": 0, "user_index_after": 1}]}):
            broken = json.loads(json.dumps(data))
            broken["maps"]["arena"]["history"] = history
            with self.subTest(history=history), self.assertRaises(projects.ProjectError):
                projects.loads_project(json.dumps(broken))
        broken = json.loads(json.dumps(data))
        broken["maps"]["arena"]["overlay"] = {"schema": "btga-overlay"}
        with self.assertRaises(projects.ProjectError):
            projects.loads_project(json.dumps(broken)).open_map("arena", self.library)

    def test_package_reader_fails_closed_with_package_errors(self):
        from tools.btga_studio.core import package as packages

        data = packages.build_package(example_project(), self.library)
        archive = zipfile.ZipFile(io.BytesIO(data))
        original = {info.filename: archive.read(info.filename) for info in archive.infolist()}
        map_name = "maps/arena.json"

        def rebuild(change, manifest_change=None):
            files = dict(original)
            change(files)
            manifest = json.loads(files["manifest.json"])
            manifest["files"] = [{"path": n, "sha256": hashlib.sha256(b).hexdigest(), "size": len(b)} for n, b in sorted(files.items())
                                 if n != "manifest.json"]
            if manifest_change:
                manifest_change(manifest)
            files["manifest.json"] = json.dumps(manifest).encode()
            return packages._zip(files)

        def edit_map(change):
            def apply(files):
                document = json.loads(files[map_name])
                change(document)
                files[map_name] = json.dumps(document).encode()
            return apply

        def drop(prefix):
            return lambda files: [files.pop(name) for name in list(files) if name.startswith(prefix)]

        cases = {
            "bounds not an object": rebuild(edit_map(lambda d: d.__setitem__("bounds", [1, 2]))),
            "bounds of three values": rebuild(edit_map(lambda d: d["bounds"].__setitem__("x", [1, 2, 3]))),
            "boolean bound": rebuild(edit_map(lambda d: d["bounds"].__setitem__("x", [False, 2]))),
            "records not an object": rebuild(edit_map(lambda d: d.__setitem__("records", [1]))),
            "record not an object": rebuild(edit_map(lambda d: d["records"].__setitem__("markers", [5]))),
            "unknown record family": rebuild(edit_map(lambda d: d["records"].__setitem__("scripts", []))),
            "map not an object": rebuild(lambda f: f.__setitem__(map_name, b"[1]")),
            "model not an object": rebuild(lambda f: [f.__setitem__(n, b"[1]") for n in list(f) if n.startswith("resources/models/")]),
            "missing model dependency": rebuild(drop("resources/models/")),
            "missing texture dependency": rebuild(drop("resources/textures/")),
            "manifest not an object": packages._zip({**original, "manifest.json": b"[]"}),
            "project not an object": rebuild(lambda f: f.__setitem__("project.json", b"[]")),
        }
        for version in (0, -1, True, "1", None, 2):
            cases[f"schema version {version!r}"] = rebuild(lambda f: None, lambda m, v=version: m.__setitem__("schema_version", v))
        corrupt = bytearray(data)
        position = corrupt.find(b'"key"')
        corrupt[position + 1] ^= 1
        cases["CRC mismatch"] = bytes(corrupt)
        for name, payload in cases.items():
            with self.subTest(case=name), self.assertRaises(packages.PackageError):
                packages.read_package(payload)
        packages.read_package(rebuild(lambda f: None))  # the rebuild helper itself yields a valid package


if __name__ == "__main__":
    unittest.main()
