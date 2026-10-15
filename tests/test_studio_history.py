"""Undo/redo for gameplay definitions, map settings, minimap settings and prefabs, and its interplay with map-object history."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

from tests.test_studio_authoring import _egl_available, starter_library

REQUIRES = "install tools/btga_studio/requirements.txt (numpy) to run the history tests"


def _example(temp: Path):
    from tools.btga_studio.core import hub
    from tools.btga_studio.core import project as projects

    library = starter_library()
    path, _ = hub.create(library, "History ü", "example", directory=temp)
    project = projects.load_project(path)
    return library, path, project


@unittest.skipIf(np is None, REQUIRES)
class DefinitionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="history ü ")
        self.library, self.path, self.project = _example(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def assert_round_trip(self, label, mutate):
        from tools.btga_studio.core.history import capture

        defs = self.project.definitions
        before = capture(self.project)
        self.assertTrue(defs.edit(label, mutate), label)
        after = capture(self.project)
        self.assertNotEqual(before, after)
        defs.undo()
        self.assertEqual(capture(self.project), before, label)
        self.assertFalse(self.project.dirty, label)  # back at the saved state
        defs.redo()
        self.assertEqual(capture(self.project), after, label)
        self.assertTrue(self.project.dirty, label)
        defs.undo()

    def test_every_family_undoes_and_redoes(self):
        p = self.project
        mission = next(iter(p.missions))
        tank = next(iter(p.tanks))
        gang = next(iter(p.gangs))
        arena = next(iter(p.multiplayer))
        minigame = next(iter(p.minigames))
        blank = next(k for k, e in p.maps.items() if e.kind == "blank")
        cases = [
            ("project title", lambda: p.project.__setitem__("title", "Renamed ü")),
            ("description", lambda: p.project.__setitem__("description", "Αρένα")),
            ("mission title", lambda: p.missions[mission].__setitem__("title", "Новая миссия")),
            ("mission objectives", lambda: p.missions[mission].__setitem__("objectives", [])),
            ("new mission", lambda: p.define("missions", "extra", {"title": "Extra", "map": blank})),
            ("delete mission", lambda: p.missions.pop(mission)),
            ("campaign order", lambda: p.campaigns[0]["missions"].append(mission)),
            ("new campaign", lambda: p.campaigns.append({"id": "second", "title": "Second", "missions": []})),
            ("multiplayer players", lambda: p.multiplayer[arena].__setitem__("max_players", 2)),
            ("multiplayer modes", lambda: p.multiplayer[arena].__setitem__("modes", ["team deathmatch"])),
            ("minigame rules", lambda: p.minigames[minigame].setdefault("rules", {}).__setitem__("targets", 9)),
            ("tank stats", lambda: p.tanks[tank]["stats"].__setitem__("armor", 300)),
            ("delete tank", lambda: p.tanks.pop(tank)),
            ("gang colour", lambda: p.gangs[gang].__setitem__("colour", [1, 2, 3])),
            ("gang tanks", lambda: p.gangs[gang].__setitem__("tanks", [])),
            ("map title", lambda: p.set_map_settings(blank, title="日本語のマップ")),
            ("map size", lambda: p.set_map_settings(blank, 2048.0)),
            ("sky colour", lambda: p.set_map_settings(blank, None, [10, 20, 30])),
            ("minimap", lambda: p.maps[blank].minimap.__setitem__("width", 32)),
        ]
        for label, mutate in cases:
            with self.subTest(label):
                self.assert_round_trip(label, mutate)

    def test_open_map_settings_follow_undo(self):
        p = self.project
        blank = next(k for k, e in p.maps.items() if e.kind == "blank")
        document = p.open_map(blank, self.library)
        before = document.scene.bounds["x"][1]
        p.definitions.edit("map size", lambda: p.set_map_settings(blank, 4096.0))
        self.assertEqual(document.scene.bounds["x"][1], 4096.0)
        p.definitions.undo()
        self.assertEqual(document.scene.bounds["x"][1], before)

    def test_coalescing_invalid_and_noop_edits(self):
        from tools.btga_studio.core import history
        from tools.btga_studio.core import project as projects

        p, defs = self.project, self.project.definitions
        for text in ("N", "Ne", "New", "New t", "New title"):
            defs.edit("Rename project", lambda t=text: p.project.__setitem__("title", t), coalesce="text:title")
        self.assertEqual(len(defs.undo_stack), 1)
        defs.seal()
        defs.edit("Rename project", lambda: p.project.__setitem__("title", "Again"), coalesce="text:title")
        self.assertEqual(len(defs.undo_stack), 2)
        with mock.patch.object(history.time, "monotonic", return_value=time.monotonic() + history.COALESCE_SECONDS + 1):
            defs.edit("Rename project", lambda: p.project.__setitem__("title", "Later"), coalesce="text:title")
        self.assertEqual(len(defs.undo_stack), 3)
        defs.undo()
        defs.undo()
        defs.undo()
        self.assertEqual(p.project["title"], "History ü")
        with self.assertRaises(projects.ProjectError):
            defs.edit("Rename project", lambda: p.project.__setitem__("title", projects._text("x" * 500, "title")))
        with self.assertRaises(KeyError):
            defs.edit("half-done edit", lambda: (p.project.__setitem__("title", "half"), p.missions["missing"]))
        self.assertEqual(p.project["title"], "History ü")  # a failed edit is rolled back and never recorded
        self.assertFalse(defs.edit("no change", lambda: p.project.__setitem__("title", "History ü")))
        self.assertEqual(defs.undo_stack, [])
        self.assertEqual(len(defs.redo_stack), 3)

    def test_save_reopen_keeps_history_and_unknown_data(self):
        from tools.btga_studio.core import project as projects

        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["future_feature"] = {"kept": ["Ω", 1]}
        mission = next(iter(data["missions"]))
        data["missions"][mission]["engine_hint"] = "raw"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        p = projects.load_project(self.path)
        p.definitions.edit("Rename", lambda: p.project.__setitem__("title", "Saved title"))
        p.definitions.edit("Rename mission", lambda: p.missions[mission].__setitem__("title", "Saved mission"))
        p.definitions.undo()
        projects.save_project(self.path, p)
        first = self.path.read_bytes()
        reopened = projects.load_project(self.path)
        self.assertEqual(projects.dumps(reopened).encode("utf-8"), first)  # deterministic
        self.assertEqual([e.label for e in reopened.definitions.undo_stack], ["Rename"])
        self.assertEqual([e.label for e in reopened.definitions.redo_stack], ["Rename mission"])
        self.assertFalse(reopened.dirty)
        reopened.definitions.redo()
        self.assertEqual(reopened.missions[mission]["title"], "Saved mission")
        reopened.definitions.undo()
        reopened.definitions.undo()
        self.assertEqual(reopened.project["title"], "History ü")
        self.assertEqual(reopened.unknown["future_feature"], {"kept": ["Ω", 1]})
        self.assertEqual(reopened.missions[mission]["engine_hint"], "raw")
        damaged = json.loads(first)
        damaged["definition_history"] = {"undo": [{"label": 3}, "junk"], "redo": "x"}
        self.path.write_text(json.dumps(damaged), encoding="utf-8")
        self.assertEqual(projects.load_project(self.path).definitions.undo_stack, [])  # a damaged history is dropped, not fatal

    def test_autosave_and_migration_are_consistent(self):
        from tools.btga_studio.core import project as projects

        p = self.project
        p.definitions.edit("Rename", lambda: p.project.__setitem__("title", "Autosaved"))
        with tempfile.TemporaryDirectory() as temp:
            saver = projects.Autosaver(Path(temp), True, 0)
            recovered = projects.load_project(saver.write(p))
            self.assertEqual(recovered.project["title"], "Autosaved")
            self.assertEqual(len(recovered.definitions.undo_stack), 1)
        legacy = {"schema": "btga-studio-project", "schema_version": 1, "scene": "studio:scene:x", "title": "Old"}
        self.assertEqual(projects.dumps(projects.loads_project(json.dumps(legacy))), projects.dumps(projects.loads_project(json.dumps(legacy))))

    def test_prefab_saves_and_deletes_undo(self):
        from tools.btga_studio.core.prefabs import PrefabLibrary

        defs = self.project.definitions
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "guard_post.json"
            path.write_text("v1", encoding="utf-8")
            defs.record_file("Save prefab", path, None, "v1")
            defs.undo()
            self.assertFalse(path.exists())
            defs.redo()
            self.assertEqual(path.read_text(encoding="utf-8"), "v1")
            path.unlink()
            defs.record_file("Delete prefab", path, "v1", None)
            defs.undo()
            self.assertEqual(path.read_text(encoding="utf-8"), "v1")
            self.assertTrue(PrefabLibrary(Path(temp)).directory.is_dir())

    def test_map_and_definition_histories_interleave_without_corruption(self):
        from tools.btga_studio.core.controller import EditorController
        from tools.btga_studio.core.settings import Settings

        with tempfile.TemporaryDirectory() as home:
            os.environ["BTGA_STUDIO_HOME"] = home
            c = EditorController(Settings.open())
            try:
                c.project = self.project
                key = next(k for k, e in self.project.maps.items() if e.kind == "blank")
                c.switch_map(key)
                cube = c.library.starter()["models"]["cube"]
                overlay_before = c.document.overlay.to_json()
                a = c.document.place_model(cube, (0.0, 0.0, 0.0))
                self.project.definitions.edit("Rename", lambda: self.project.project.__setitem__("title", "B"))
                b = c.document.place_model(cube, (64.0, 0.0, 0.0))
                order = []
                for _ in range(3):
                    result = c.undo()
                    order.append(getattr(result, "label", None))
                self.assertEqual(order, ["Place model", "Rename", "Place model"])
                self.assertEqual(c.document.overlay.to_json(), overlay_before)
                self.assertEqual(self.project.project["title"], "History ü")
                self.assertFalse(self.project.dirty)
                redo = [getattr(c.redo(), "label", None) for _ in range(3)]
                self.assertEqual(redo, ["Place model", "Rename", "Place model"])
                self.assertIsNotNone(c.document.scene.record(a))
                self.assertIsNotNone(c.document.scene.record(b))
                self.assertEqual(self.project.project["title"], "B")
            finally:
                c.shutdown()


@unittest.skipIf(np is None or not _egl_available(), "needs EGL (off-screen UI)")
class GameplayEditorUndoTests(unittest.TestCase):
    def test_typing_is_one_transaction_and_cancel_records_nothing(self):
        os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
        from imgui_bundle import imgui

        from tools.btga_studio.core.settings import Settings
        from tools.btga_studio.gui.app import StudioApp
        from tools.btga_studio.gui.offscreen import OffscreenUI

        with tempfile.TemporaryDirectory() as home:
            os.environ["BTGA_STUDIO_HOME"] = home
            app = StudioApp(Settings.open())
            ui = OffscreenUI(app, 1600, 960)
            try:
                app.open_example()
                app.open_gameplay("Map")
                for _ in range(3):
                    ui.frame()
                defs = app.controller.project.definitions
                # Edit the project name through the real widget: click the field, type, then click elsewhere.
                field = app.anchors.get("gameplay.project-name")
                self.assertIsNotNone(field)
                centre = ((field[0] + field[2]) / 2, (field[1] + field[3]) / 2)
                ui.click(centre)
                ui.frame(mouse=centre, keys={imgui.Key.mod_ctrl: True, imgui.Key.a: True})
                ui.frame(mouse=centre, keys={imgui.Key.mod_ctrl: False, imgui.Key.a: False})
                for character in "Ελλάδα":
                    ui.frame(mouse=centre, text=character)
                ui.click((5, 950))
                self.assertEqual(app.controller.project.project["title"], "Ελλάδα")
                self.assertEqual([e.label for e in defs.undo_stack], ["Rename project"])
                app.open_gameplay("Tanks & gangs")
                ui.frame()
                app.undo()
                ui.frame()
                ui.frame()  # Dear ImGui switches tabs on the next frame
                self.assertEqual(app.controller.project.project["title"], "Starter example")
                self.assertTrue(app.show_gameplay)
                self.assertEqual(app.gameplay_tab, 0)  # undo brings back the tab where the change was made
                mission = next(iter(app.controller.project.missions))
                app.confirm("Delete mission", "?", "Delete", lambda: app.delete_definition("missions", mission))
                app.confirmation = None  # the user pressed Cancel
                ui.frame()
                self.assertEqual(defs.undo_stack, [])
                self.assertEqual(app.frame_errors, 0)
            finally:
                ui.close()


if __name__ == "__main__":
    unittest.main()
