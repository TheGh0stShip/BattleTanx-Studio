"""Drive Preview: ground following, ramps, solid objects, water, map limits and area notices (synthetic, no game data)."""

from __future__ import annotations

import math
import os
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_studio_authoring import _egl_available, starter_library  # noqa: E402
from tools.btga_studio.core import drive  # noqa: E402
from tools.btga_studio.core import geometry as g  # noqa: E402
from tools.btga_studio.core import project as projects  # noqa: E402


def course():
    """A 2 km blank map: ground tiles, a ramp at z = -1500, a solid wall at x = 1200, a teleporter and a pickup."""

    library = starter_library()
    models = library.starter()["models"]
    templates = {t["source_kind"].removeprefix("studio_"): t for t in library.starter()["templates"]}
    project = projects.StudioProject("Drive", "studio")
    project.add_blank_map("main", "Main", 4096.0)
    document = project.open_map("main", library)
    for x in range(-3840, 4096, 512):
        for z in range(-3840, 4096, 512):
            document.place_model(models["ground_tile"], (float(x), 0.0, float(z)))
    ramp = document.place_model(models["ramp"], (0.0, 0.0, -1500.0))
    document.begin_transform([ramp])
    document.update_transform(scale=(4.0, 4.0, 4.0))  # 800 units long, 400 high: a 26.6 degree slope
    document.commit_transform()
    wall = document.place_model(models["wall"], (1200.0, 0.0, 0.0), 0x4000)  # runs along z
    document.add_collision_for([wall])
    document.place_model(models["wall"], (-1200.0, 0.0, -2500.0), 0x4000)  # no collision box: its geometry alone must stop a tank
    tower = document.place_model(models["building_tall"], (2500.0, 0.0, -2500.0))  # 640 units (40 m) tall, 180 square
    exit_marker = document.place_marker(templates["teleport_exit"], (-3000.0, 0.0, 3000.0), 0x8000, values={"label": "the far corner"})
    document.place_marker(templates["teleporter"], (0.0, 0.0, 1500.0), values={"exit": exit_marker, "radius": 90})
    document.place_marker(templates["pickup"], (-1500.0, 0.0, 0.0), values={"pickup_type": 2, "respawn_seconds": 30})
    return document


class DriveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = course()

    def make(self, x=0.0, z=0.0, yaw=0.0, **settings):
        return drive.DrivePreview(self.document.scene, drive.DriveSettings(**settings), (x, 0.0, z), yaw)

    def run_for(self, tank, seconds, throttle=1.0, steer=0.0, brake=False):
        events = []
        for _ in range(int(seconds * 60)):
            events += tank.step(1 / 60, throttle, steer, brake)
        return events

    def test_accelerates_to_top_speed_on_flat_ground(self):
        tank = self.make(0.0, 500.0, yaw=0.0)  # facing -Z
        self.run_for(tank, 4.0)
        self.assertAlmostEqual(tank.speed_kmh(), 14.0 * 3.6, delta=0.5)
        self.assertLess(tank.state.position[2], 500.0 - 14 * 16 * 2)  # moved forward (-Z)
        self.assertAlmostEqual(tank.state.position[1], 0.0, delta=0.01)  # on the ground tiles
        self.run_for(tank, 1.5, throttle=0.0, brake=True)
        self.assertEqual(tank.state.speed, 0.0)

    def test_turning_follows_the_scene_yaw_convention(self):
        tank = self.make(0.0, 0.0, yaw=0.0)
        self.run_for(tank, 1.0, throttle=0.0, steer=1.0)  # steer left without moving
        self.assertAlmostEqual(math.degrees(tank.state.yaw), 90.0, delta=1.0)
        forward = tank.forward()
        self.assertTrue(np.allclose(forward, [-1.0, 0.0, 0.0], atol=0.02))  # yaw +90 degrees faces -X, like the camera

    def test_drives_up_the_ramp(self):
        tank = self.make(0.0, -800.0, yaw=0.0)  # the ramp foot is at z = -1100, rising towards -Z
        self.run_for(tank, 4.2)
        self.assertGreater(tank.state.position[1], 200.0)  # well up the 400-unit ramp
        self.assertGreater(tank.state.pitch, math.radians(15))

    def test_too_steep_stops_the_tank(self):
        tank = self.make(0.0, -800.0, yaw=0.0, max_slope_deg=10.0, step_m=0.2)
        events = self.run_for(tank, 4.0)
        self.assertLess(tank.state.position[1], 10.0)
        self.assertIn("Too steep", {e.text for e in events})

    def test_solid_wall_blocks(self):
        tank = self.make(900.0, 0.0, yaw=-math.pi / 2)  # facing +X, towards the wall at x = 1200
        events = self.run_for(tank, 4.0)
        radius = 3.6 * 16 / 2
        self.assertLess(tank.state.position[0], 1200.0 - 10.0 - radius + 1.0)
        self.assertIn("blocked", {e.kind for e in events})

    def test_solid_geometry_without_a_collision_box_blocks(self):
        tank = self.make(-1500.0, -2500.0, yaw=-math.pi / 2)  # facing +X, towards the box-less wall at x = -1200
        events = self.run_for(tank, 4.0)
        self.assertLess(tank.state.position[0], -1200.0 - 10.0 - 3.6 * 16 / 2 + 1.0)
        self.assertIn("blocked", {e.kind for e in events})

    def test_driving_off_a_tall_roof_lands_on_the_ground(self):
        tank = drive.DrivePreview(self.document.scene, drive.DriveSettings(), (2500.0, 640.0, -2500.0), -math.pi / 2)  # roof, facing +X
        self.assertAlmostEqual(tank.state.position[1], 640.0, delta=0.5)
        events = self.run_for(tank, 5.0)
        self.assertTrue(tank.state.on_ground)
        self.assertAlmostEqual(tank.state.position[1], 0.0, delta=0.5)  # on the ground tiles, not under the map
        self.assertGreater(tank.state.position[0], 2500.0 + 90.0)
        self.assertNotIn("fell", {e.kind for e in events})

    def test_water_stops_the_tank_at_the_bank(self):
        tank = self.make(-2000.0, 500.0, yaw=0.0)  # facing -Z; ground from z = 0 to -800 is marked as a river below
        centres = tank.heights.tris.mean(axis=1)
        tank.heights.water = (centres[:, 2] < 0.0) & (centres[:, 2] > -800.0) & (centres[:, 0] < -1000.0)
        events = self.run_for(tank, 5.0)
        self.assertGreater(tank.state.position[2], -30.0)
        self.assertIn("Water: tanks cannot drive into the river", {e.text for e in events})
        self.run_for(tank, 2.0, throttle=-1.0)  # and can back away
        self.assertGreater(tank.state.position[2], 100.0)

    def test_map_limits_hold(self):
        tank = self.make(0.0, -3500.0, yaw=0.0)
        self.run_for(tank, 6.0)
        self.assertGreaterEqual(tank.state.position[2], -4096.0 + 7.5 * 16 / 2 - 1e-6)

    def test_teleporter_pad_is_reported_not_executed(self):
        tank = self.make(0.0, 1800.0, yaw=0.0)  # drive -Z onto the pad at z = 1500
        for _ in range(int(3.0 * 60)):
            events = tank.step(1 / 60, 1.0, 0.0, False)
            if any(e.kind == "notice" for e in events):
                break
        notices = [e for e in events if e.kind == "notice"]
        self.assertEqual(len(notices), 1)
        self.assertIn("Teleporter pad to the far corner", notices[0].text)
        self.assertIn("the preview does not", notices[0].text)
        self.assertLess(math.dist(tank.state.position[[0, 2]], (0.0, 1500.0)), 200.0)  # still at the pad: nothing was teleported
        self.assertTrue(tank.jump_to_exit())  # the explicit editor jump
        self.assertLess(math.dist(tank.state.position[[0, 2]], (-3000.0, 3000.0)), 1.0)
        self.assertEqual(tank.events[-1].kind, "jumped")
        self.assertIn("not a game teleport", tank.events[-1].text)

    def test_pickup_is_reported_not_collected(self):
        tank = self.make(-1300.0, 0.0, yaw=math.pi / 2)  # facing -X, towards the pickup at x = -1500
        events = self.run_for(tank, 2.0)
        notices = [e.text for e in events if e.kind == "notice"]
        self.assertEqual(len(notices), 1)
        self.assertIn("not collected", notices[0])
        self.assertFalse(hasattr(tank.state, "collected"))
        self.assertFalse(tank.jump_to_exit())  # not on a teleporter pad

    def test_reset_returns_to_the_start_and_the_chase_camera_follows(self):
        tank = self.make(0.0, 0.0)
        self.run_for(tank, 2.0)
        camera = g.Camera()
        tank.chase_camera(camera, 0.0)
        behind = tank.state.position - tank.forward() * 14 * 16
        self.assertLess(math.dist(camera.position[[0, 2]], behind[[0, 2]]), 1.0)
        self.assertTrue(np.allclose(camera.forward()[[0, 2]] / np.linalg.norm(camera.forward()[[0, 2]]), tank.forward()[[0, 2]], atol=0.01))
        tank.reset()
        self.assertTrue(np.allclose(tank.state.position[[0, 2]], [0.0, 0.0]))
        translation, yaw, scale = tank.tank_transform(110.0)
        self.assertAlmostEqual(scale[0], 7.5 * 16 / 110.0)


class DrivePreviewHonestyTests(unittest.TestCase):
    def test_the_disclaimer_names_what_is_not_simulated(self):
        from tools.btga_studio.gui.drive_preview import DISCLAIMER

        text = " ".join(DISCLAIMER).lower()
        self.assertIn("approximation", text)
        for word in ("physics", "ai", "combat", "destruction", "pickups", "scripting", "test in game"):
            self.assertIn(word, text)
        self.assertNotIn("test drive", text)

    def test_no_user_facing_test_drive_wording_remains(self):
        for path in (ROOT / "tools" / "btga_studio").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("Test drive", text, path.name)
            self.assertNotIn("test drive", text.lower().replace("contract test", ""), path.name)


class DriveTankModelTests(unittest.TestCase):
    """Test drive shows the game's own tank when the user's cartridge catalog is loaded, else the starter stand-in."""

    def parts(self, models):
        from types import SimpleNamespace

        from tools.btga_studio.gui.drive_preview import DrivePreviewUI

        library = SimpleNamespace(_models=dict.fromkeys(models), starter=lambda: {"models": {"tank_placeholder": "studio:model:tank"}})
        host = DrivePreviewUI()
        host.controller = SimpleNamespace(library=library)
        return host.drive_tank_parts()

    def test_game_tank_when_the_cartridge_catalog_is_loaded(self):
        from tools.btga_studio.gui.drive_preview import GAME_TANK_LENGTH, GAME_TANK_PARTS

        self.assertEqual(self.parts(GAME_TANK_PARTS), (list(GAME_TANK_PARTS), GAME_TANK_LENGTH, 0x8000))

    def test_stand_in_without_the_game(self):
        from tools.btga_studio.gui.drive_preview import GAME_TANK_PARTS

        self.assertEqual(self.parts(GAME_TANK_PARTS[:1]), (["studio:model:tank"], 110.0, 0))


    def test_lighting_normals_decoded_as_colours_are_shaded_grey(self):
        from tools.btga_assets import mesh as meshmod

        def corner(colour):
            return {"p": 0, "uv": None, "colour": colour, "normal": None}

        up, side = [0.0, 127 / 255, 0.0, 1.0], [129 / 255, 0.0, 0.0, 1.0]  # +Y and -X as signed bytes
        mesh = {"positions": [[0, 0, 0]], "lods": [],
                "materials": [{"id": "n64_btga:texture:a|cull01|lit0", "vertex_colour": "shade RGBA, 255 = 1.0"},
                              {"id": "n64_btga:texture:b|cull01|lit0", "vertex_colour": "shade RGBA, 255 = 1.0"}],
                "triangles": [{"material": 0, "corners": [corner(up), corner(up), corner(side)]},
                              {"material": 1, "corners": [corner([0.5, 0.2, 0.1, 1.0])] * 3}]}  # real colours stay
        lit = meshmod.with_game_lighting(mesh)
        top, edge = lit["triangles"][0]["corners"][0]["colour"], lit["triangles"][0]["corners"][2]["colour"]
        self.assertEqual(top[0], top[1])
        self.assertEqual(top[1], top[2])  # grey, not green
        self.assertGreater(top[0], edge[0])  # the lit top is brighter than the side facing away
        self.assertEqual(lit["triangles"][1]["corners"][0]["colour"], [0.5, 0.2, 0.1, 1.0])
        self.assertEqual(mesh["triangles"][0]["corners"][0]["colour"], up)  # the input is unchanged


@unittest.skipIf(not _egl_available(), "needs imgui-bundle, PyOpenGL and EGL (off-screen UI)")
class DriveWindowTests(unittest.TestCase):
    def test_toolbar_drive_moves_the_tank_and_restores_the_camera(self):
        os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
        from tests.test_studio_product import TempHome
        from tools.btga_studio.core.settings import Settings
        from tools.btga_studio.fixtures import recording_launcher
        from tools.btga_studio.gui.app import StudioApp
        from tools.btga_studio.gui.offscreen import OffscreenUI

        with TempHome(), recording_launcher():
            app = StudioApp(Settings.open())
            app.settings.preferences.welcome_seen = True
            ui = OffscreenUI(app, 1280, 800)
            self.addCleanup(ui.close)
            c = app.controller
            self.addCleanup(c.shutdown)
            app.show_hub = False
            c.add_blank_map("main", "Main", 2048.0)
            app.switch_map("main")
            library = c.library
            for x in range(-1536, 2048, 512):
                for z in range(-1536, 2048, 512):
                    c.document.place_model(library.starter()["models"]["ground_tile"], (float(x), 0.0, float(z)))
            templates = {t["source_kind"].removeprefix("studio_"): t for t in library.starter()["templates"]}
            start = c.document.place_marker(templates["team_start"], (100.0, 0.0, 200.0))
            c.document.selection = [start]
            app._load_into_renderer()
            for _ in range(3):
                ui.frame()
            before = c.camera.position.copy()
            x0, y0, x1, y1 = app.previous_anchors["toolbar.drive"]
            ui.click(((x0 + x1) / 2, (y0 + y1) / 2), settle=2)
            self.assertIsNotNone(app.drive)
            self.assertTrue(np.allclose(app.drive.state.position[[0, 2]], [100.0, 200.0]))
            self.assertEqual(c.preferences.layers["collision"], True)  # the authoring overlays are switched on for the preview
            app.drive_input = (1.0, 0.0, False)
            for _ in range(60):
                ui.frame()
            self.assertGreater(app.drive.state.distance, 50.0)
            self.assertEqual(len(app.renderer.extras), 1)
            draws = app.renderer.draw_calls
            self.assertGreater(draws, 1)
            app.drive_input = None
            ui.press("escape")
            ui.frame()
            self.assertIsNone(app.drive)
            self.assertTrue(np.allclose(c.camera.position, before))
            self.assertEqual(app.renderer.extras, [])
            self.assertEqual(c.document.overlay.edits[-1]["op"], "add_marker")  # driving changed nothing


if __name__ == "__main__":
    unittest.main()
