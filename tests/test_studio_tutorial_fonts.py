"""Guided tour (logic, persistence, on-screen pointer) and bundled Unicode fonts (discovery, integrity, licences, rendering)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

from tests.test_studio_authoring import _egl_available

ROOT = Path(__file__).resolve().parents[1]
FONT_DIR = ROOT / "private-work" / "toolchains" / "fonts"
UNICODE_NAMES = ("Café Crème Élan", "Ελληνική Αρένα", "Кириллица Карта", "日本語のマップ", "한국어 지도", "中文地图", "Plane 1 𝔅 emoji 😀")


def fonts_available() -> bool:
    from tools.btga_studio.core import fontset

    with mock.patch.dict(os.environ, {"BTGA_STUDIO_FONT_DIR": str(FONT_DIR)}):
        return not fontset.resolve()[1]


class TutorialLogicTests(unittest.TestCase):
    def facts(self, **values):
        base = {"project": True, "camera": ((0, 0, 0), 0, 0), "selection": 0, "transforms": 0, "spawns": 0, "pickups": 0,
                "objectives": 0, "gameplay_open": False, "validated": False, "checks": 0, "saved": False, "saves": 0, "exports": 0}
        base.update(values)
        return base

    def test_steps_progress_only_on_new_actions_and_persist(self):
        from tools.btga_studio.core.settings import Settings
        from tools.btga_studio.core.tutorial import STEPS, Tutorial

        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(Path(temp))
            tour = Tutorial(settings.preferences)
            self.assertFalse(tour.active)
            tour.start()
            self.assertTrue(tour.update(self.facts()))  # a project is open: step 1 done
            self.assertEqual(tour.step.key, "look")
            self.assertFalse(tour.update(self.facts(spawns=3)))  # first frame of a step records the baseline
            self.assertFalse(tour.update(self.facts(spawns=3)))
            self.assertTrue(tour.update(self.facts(spawns=3, camera=((1, 0, 0), 0, 0))))
            self.assertTrue(tour.update(self.facts(selection=1)) or tour.update(self.facts(selection=1)))
            self.assertEqual(tour.step.key, "move")
            tour.update(self.facts(transforms=2))
            self.assertFalse(tour.update(self.facts(transforms=2)))  # earlier moves do not count
            self.assertTrue(tour.update(self.facts(transforms=3)))
            settings.save()
            probe = Tutorial(settings.preferences)
            probe.preferences.tutorial_step = 8
            probe.update(self.facts(validated=True))
            self.assertFalse(probe.update(self.facts(validated=True)))  # a clean map alone does not complete the check step
            self.assertTrue(probe.update(self.facts(validated=True, checks=1)))
            settings.preferences.tutorial_step = 4
            reopened = Settings(Path(temp))
            reopened.load()
            again = Tutorial(reopened.preferences)
            self.assertEqual(again.step.key, "spawn")  # progress is per user, in preferences
            again.back()
            self.assertEqual(again.step.key, "move")
            again.stop()
            self.assertFalse(again.active)
            self.assertIsNone(again.step)
            again.start(from_beginning=False)
            self.assertEqual(again.step.key, "move")
            for _ in range(len(STEPS)):
                again.skip()
            self.assertTrue(again.finished and reopened.preferences.tutorial_done)
            self.assertFalse(again.active)
            again.start(from_beginning=False)
            self.assertEqual(again.step.key, "open")  # finished: starts over

    def test_badge_stays_on_the_target_corner_and_on_screen(self):
        try:
            from tools.btga_studio.gui.windows import badge_position
        except ImportError:  # pragma: no cover - imgui-bundle missing
            self.skipTest("needs imgui-bundle")
        x, y, end = badge_position((296, 80, 1208, 686), (1394, 800), (8, 15), (1600, 960))
        self.assertTrue(296 < x < 1208 - 8 and 80 < y < 686 - 15)  # inside a large target, at the bottom
        self.assertGreater(y, 600)
        x, y, end = badge_position((966, 744, 1048, 760), (1394, 800), (8, 15), (1600, 960))
        self.assertEqual(end, (1052, 764))  # the corner nearest the tour window
        self.assertTrue(abs(x + 4 - 1052) <= 1 and abs(y + 7.5 - 764) <= 1)
        x, y, _ = badge_position((1590, 950, 1600, 960), (0, 0), (8, 15), (1600, 960))
        self.assertTrue(0 <= x <= 1600 - 8 and 0 <= y <= 960 - 15)

    def test_texts_have_no_internal_terms(self):
        from tools.btga_studio.core.tutorial import STEPS

        for step in STEPS:
            for word in ("user:", "studio:", "overlay", "record", "json", "catalog", "schema"):
                self.assertNotIn(word, (step.title + step.text + step.hidden_hint).lower(), step.key)


class FontTests(unittest.TestCase):
    def test_manifest_licences_and_integrity(self):
        from tools.btga_studio.core import fontset

        entries = fontset.manifest()
        self.assertEqual([e.role for e in entries][0], "primary")
        for entry in entries:
            self.assertTrue((fontset.MANIFEST_DIR / entry.license_file).is_file())
            self.assertRegex(entry.sha256, "^[0-9a-f]{64}$")
            self.assertIn(entry.license.split()[0], ("Bitstream", "Apache", "SIL"))
            self.assertTrue(entry.package_url.startswith("https://archive.ubuntu.com/ubuntu/"))
            self.assertRegex(entry.package_sha256, "^[0-9a-f]{64}$")
            self.assertFalse(Path(entry.package_path).is_absolute())
        self.assertIn("SHA-256", fontset.notices())
        with tempfile.TemporaryDirectory() as temp:
            fake = Path(temp) / entries[0].file
            fake.write_bytes(b"\0" * entries[0].size)  # right size, wrong contents
            with mock.patch.dict(os.environ, {"BTGA_STUDIO_FONT_DIR": temp}):
                self.assertNotEqual(fontset.locate(entries[0], Path(temp)), fake)
            with mock.patch.object(fontset, "candidates", return_value=[fake]):
                self.assertIsNone(fontset.locate(entries[0]))
                with self.assertRaises(FileNotFoundError):
                    fontset.stage(Path(temp) / "out")

    @unittest.skipUnless(FONT_DIR.is_dir(), "stage the fonts into private-work/toolchains/fonts (see docs) to run")
    def test_stage_copies_fonts_and_licences(self):
        from tools.btga_studio.core import fontset

        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"BTGA_STUDIO_FONT_DIR": str(FONT_DIR)}):
            written = {p.name for p in fontset.stage(Path(temp))}
            for entry in fontset.manifest():
                self.assertIn(entry.file, written)
                self.assertIn(entry.license_file, written)
            self.assertIn("fonts.json", written)


@unittest.skipIf(np is None or not _egl_available(), "needs EGL (off-screen UI)")
class UnicodeAndTourUITests(unittest.TestCase):
    def _app(self, home):
        os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
        from tools.btga_studio.core.settings import Settings
        from tools.btga_studio.gui.app import StudioApp
        from tools.btga_studio.gui.offscreen import OffscreenUI

        os.environ["BTGA_STUDIO_HOME"] = str(home)
        app = StudioApp(Settings.open())
        return app, OffscreenUI(app, 1600, 960)

    @unittest.skipUnless(FONT_DIR.is_dir(), "stage the fonts into private-work/toolchains/fonts to run")
    def test_unicode_names_render_and_round_trip(self):
        from imgui_bundle import imgui

        from tools.btga_studio.core import hub
        from tools.btga_studio.core import project as projects

        with tempfile.TemporaryDirectory(prefix="ユニコード Ελλάδα ") as home, mock.patch.dict(os.environ, {"BTGA_STUDIO_FONT_DIR": str(FONT_DIR)}):
            app, ui = self._app(Path(home))
            try:
                self.assertEqual(len(ui.font_status.loaded), 3, ui.font_status)
                font = imgui.get_io().fonts.fonts[0]
                for name in UNICODE_NAMES:
                    path, _ = hub.create(app.controller.library, name, "blank", 1024.0, directory=app.settings.projects_directory)
                    self.assertEqual(path.stem, name)
                    self.assertEqual(projects.load_project(path).project["title"], name)
                    app.settings.preferences.add_recent(str(path))
                    if name != UNICODE_NAMES[-1]:
                        baked = font.get_font_baked(15.0)
                        for character in name.replace(" ", ""):
                            self.assertIsNotNone(baked.find_glyph_no_fallback(ord(character)), f"{name}: {character!r}")
                app.open_hub()
                for _ in range(3):
                    ui.frame()
                self.assertEqual(app.frame_errors, 0)
            finally:
                ui.close()

    def test_without_fonts_the_editor_still_works(self):
        from tools.btga_studio.core import fontset

        with tempfile.TemporaryDirectory() as home, mock.patch.object(fontset, "candidates", return_value=[]):
            app, ui = self._app(Path(home))
            try:
                self.assertFalse(ui.font_status.unicode)
                self.assertTrue(any("built-in font" in p for p in ui.font_status.problems))
                app.open_example()
                ui.frame()
                self.assertEqual(app.frame_errors, 0)
            finally:
                ui.close()

    def test_tour_points_at_controls_and_adapts_when_hidden(self):
        with tempfile.TemporaryDirectory() as home:
            app, ui = self._app(Path(home))
            try:
                app.start_tour()
                ui.frame()
                self.assertEqual(app.tutorial.step.key, "open")
                app.open_example()
                for _ in range(3):
                    ui.frame()
                self.assertEqual(app.tutorial.step.key, "look")
                self.assertIn("viewport", app.previous_anchors)
                c = app.controller
                c.camera.yaw += 0.5
                ui.frame()
                ui.frame()
                self.assertEqual(app.tutorial.step.key, "select")
                app.tutorial.skip()
                app.tutorial.skip()  # move
                self.assertEqual(app.tutorial.step.key, "spawn")
                ui.frame()
                templates = {t["source_kind"]: t["id"] for t in c.library.starter()["templates"]}
                c.begin_placement("marker", templates["studio_player_spawn"])
                c.place_at(c.viewport[0] / 2, c.viewport[1] / 2)
                c.placement = None
                ui.frame()
                ui.frame()
                self.assertEqual(app.tutorial.step.key, "pickup")
                app.anchors.clear()
                app.previous_anchors.clear()
                original = app.panel_assets
                app.panel_assets = lambda: None  # the Assets panel is closed
                ui.frame()
                ui.frame()
                self.assertNotIn("assets", app.previous_anchors)
                app.panel_assets = original
                app.tutorial.stop()
                ui.frame()
                self.assertFalse(app.tutorial.active)
                self.assertEqual(app.settings.preferences.tutorial_step, 5)
                self.assertEqual(app.frame_errors, 0)
            finally:
                ui.close()


if __name__ == "__main__":
    unittest.main()
