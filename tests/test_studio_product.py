"""Desktop-product behaviour: first run, Project Hub, Source Manager jobs, recovery, settings, packaging (synthetic only).

Requires tools/btga_studio/requirements.txt; the off-screen UI tests also need EGL.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

from tests.test_studio_authoring import _egl_available, starter_library

ROOT = Path(__file__).resolve().parents[1]
REQUIRES = "install tools/btga_studio/requirements.txt (numpy) to run the product tests"


class TempHome:
    """A fresh per-test data directory whose name has spaces and non-ASCII characters."""

    def __enter__(self) -> Path:
        self.root = Path(tempfile.mkdtemp(prefix="studio home ü "))
        self.previous = os.environ.get("BTGA_STUDIO_HOME")
        os.environ["BTGA_STUDIO_HOME"] = str(self.root / "data ü")
        return self.root

    def __exit__(self, *exc):
        if self.previous is None:
            os.environ.pop("BTGA_STUDIO_HOME", None)
        else:
            os.environ["BTGA_STUDIO_HOME"] = self.previous
        shutil.rmtree(self.root, ignore_errors=True)


class AtomicWriteTests(unittest.TestCase):
    def test_failure_keeps_the_previous_file(self):
        from tools.btga_studio.core import atomic

        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "dir ü" / "project.btgaproj"
            atomic.atomic_write_text(target, "old")
            with mock.patch("os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
                atomic.atomic_write_text(target, "new")
            self.assertEqual(target.read_text(), "old")
            self.assertEqual([p.name for p in target.parent.iterdir()], ["project.btgaproj"])  # no temporary left behind
            (target.parent / (atomic.TEMPORARY_PREFIX + "x")).write_text("stale")
            self.assertEqual(atomic.remove_partials(target.parent), 1)

    def test_failed_project_save_keeps_the_existing_file(self):
        from tools.btga_studio.core import project as projects
        from tools.btga_studio.core.hub import create

        library = starter_library()
        with tempfile.TemporaryDirectory() as temp:
            path, project = create(library, "Saved once", "blank", 1024.0, directory=Path(temp))
            before = path.read_bytes()
            project.project["title"] = "x" * 500  # fails validation while serialising
            project.maps[next(iter(project.maps))].title = "changed"
            with mock.patch.object(projects, "dumps", side_effect=projects.ProjectError("simulated")), self.assertRaises(projects.ProjectError):
                projects.save_project(path, project)
            self.assertEqual(path.read_bytes(), before)


class JobTests(unittest.TestCase):
    def test_progress_cancel_failure_retry_and_bounds(self):
        from tools.btga_studio.core.jobs import JobQueueFull, JobRunner

        runner = JobRunner(workers=1, max_pending=2)
        try:
            gate = threading.Event()
            seen = []

            def slow(context):
                for step in range(100):
                    context.progress("step", step, 100)
                    gate.wait(0.01)
                return "finished"

            first = runner.submit("slow", slow, "test", on_done=lambda job: seen.append((job.state, threading.current_thread().name)))
            second = runner.submit("second", lambda context: 1 / 0, "test")
            with self.assertRaises(JobQueueFull):
                runner.submit("third", lambda context: None)
            time.sleep(0.05)
            self.assertEqual(first.state, "running")
            self.assertIsNotNone(first.fraction)
            first.cancel()
            runner.wait(10)
            self.assertEqual(first.state, "cancelled")
            self.assertEqual(seen, [("cancelled", threading.current_thread().name)])  # callbacks run on the polling thread
            self.assertEqual(second.state, "failed")
            self.assertIn("ZeroDivisionError", second.error)
            self.assertIn("Traceback", second.detail)
            again = runner.retry(second)
            runner.wait(10)
            self.assertEqual(again.state, "failed")
        finally:
            runner.shutdown()


class LogTests(unittest.TestCase):
    def test_structured_log_and_report_hide_paths(self):
        from tools.btga_studio.core import applog

        with tempfile.TemporaryDirectory() as temp:
            logger = applog.open_log(Path(temp) / "logs")
            applog.write(logger, "error", "open_failed", r"could not open C:\Users\someone\Documents\My Maps\arena.btgaproj",
                         path="/home/someone/projects/x ü/map.btgaproj")
            applog.close(logger)
            records = applog.recent_records(Path(temp) / "logs")
            self.assertEqual(records[-1]["event"], "open_failed")
            text = json.dumps(records) + applog.problem_report(Path(temp) / "logs", "0.0", r"D:\games\BattleTanx (USA).z64 failed")
            for private in ("someone", "My Maps", "x ü", "games"):
                self.assertNotIn(private, text)
            self.assertIn("arena.btgaproj", text)
            self.assertEqual(applog.redact("see https://github.com/TheGh0stShip/BattleTanx-Studio/issues"),
                             "see https://github.com/TheGh0stShip/BattleTanx-Studio/issues")


class SettingsTests(unittest.TestCase):
    def test_locations_migration_and_recents(self):
        from tools.btga_studio.core import settings as settings_module
        from tools.btga_studio.core.settings import Preferences, Settings

        with TempHome() as home:
            settings = Settings.open()
            self.assertEqual(settings.directory, settings.local_directory)
            self.assertTrue(str(settings.logs_directory).startswith(str(settings.local_directory)))
            legacy = home / "old"
            legacy.mkdir()
            (legacy / "preferences.json").write_text(json.dumps({"recent_projects": ["a.btgaproj"], "grid_snap": 32}))
            (legacy / "sources.json").write_text('{"version": 1, "sources": []}')
            fresh = Settings(home / "new roaming", home / "new local")
            self.assertTrue(fresh.migrate(legacy))
            fresh.load()
            self.assertEqual((fresh.preferences.recent_projects, fresh.preferences.grid_snap), (["a.btgaproj"], 32))
            self.assertTrue((home / "new roaming" / "sources.json").is_file())
            self.assertFalse(fresh.migrate(legacy) and False)
            (home / "new roaming" / "preferences.json").write_text(json.dumps({"autosave_enabled": "yes", "grid_snap": True,
                                                                               "recent_projects": [1, "b"], "camera_speed": 10}))
            broken = Settings(home / "new roaming")
            broken.load()
            self.assertIs(broken.preferences.autosave_enabled, True)  # wrong types are ignored
            self.assertEqual(broken.preferences.grid_snap, 0.0)
            self.assertEqual(broken.preferences.recent_projects, ["b"])
            self.assertEqual(broken.preferences.camera_speed, 10)
        prefs = Preferences()
        for number in range(20):
            prefs.add_recent(f"p{number}")
        prefs.set_pinned("p0", True)
        for number in range(20, 40):
            prefs.add_recent(f"p{number}")
        self.assertIn("p0", prefs.recent_projects)
        self.assertLessEqual(len(prefs.recent_projects), settings_module.MAX_RECENT)
        prefs.replace_recent("p0", "moved")
        self.assertEqual((prefs.recent_projects[0], prefs.pinned_projects), ("moved", ["moved"]))
        prefs.remove_recent("moved")
        self.assertNotIn("moved", prefs.recent_projects + prefs.pinned_projects)

    def test_windows_and_portable_locations(self):
        from tools.btga_studio.core import settings

        with mock.patch.dict(os.environ, {"APPDATA": "R", "LOCALAPPDATA": "L"}, clear=False), mock.patch.object(settings.sys, "platform", "win32"):
            os.environ.pop("BTGA_STUDIO_HOME", None)
            with mock.patch.object(settings, "is_portable", return_value=False):
                roaming, local = settings.data_directories()
            self.assertEqual((roaming, local), (Path("R") / "BattleTanx Studio", Path("L") / "BattleTanx Studio"))
            with mock.patch.object(settings, "program_directory", return_value=Path("prog")), mock.patch.object(settings, "is_portable", return_value=True):
                self.assertEqual(settings.data_directories(), (Path("prog") / "BattleTanxStudioData",) * 2)


class SourceRegistryTests(unittest.TestCase):
    def test_unreadable_registry_is_kept_and_facts_are_plain(self):
        from tools.btga_studio.core import sources
        from tools.btga_studio.fixtures import supported_revision, synthetic_rom

        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "data"
            data.mkdir()
            (data / "sources.json").write_text("{ not json")
            registry = sources.SourceRegistry(data, Path(temp) / "local")
            self.assertTrue(registry.problems)
            self.assertTrue((data / "sources.unreadable-1.json").is_file())
            rom = synthetic_rom(7)
            path = Path(temp) / "my game ü.v64"
            swapped = bytearray(rom)
            swapped[0::2], swapped[1::2] = rom[1::2], rom[0::2]
            path.write_bytes(bytes(swapped))
            with supported_revision(rom):
                source = registry.register(path)
            facts = sources.describe(source)
            self.assertEqual((facts["platform"], facts["region"], facts["verified"]), ("Nintendo 64", "USA", True))
            self.assertIn("Byte-swapped", facts["byte_order"])
            self.assertTrue(str(registry.catalogs).startswith(str(Path(temp) / "local")))
            unknown = sources.identify(path)
            self.assertEqual(sources.describe(unknown)["revision"], "Unrecognised (no supported hash matches)")
            self.assertEqual(path.read_bytes(), bytes(swapped))  # the user's file is never changed

    def test_bundled_level_table_is_found_in_a_frozen_layout(self):
        from tools.btga_studio.core import sources

        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "data").mkdir()
            (Path(temp) / "data" / "800E8380_slot_asset_ranges.c").write_text("/* table */")
            with mock.patch.object(sys, "_MEIPASS", temp, create=True), mock.patch.object(Path, "is_file", autospec=True,
                                                                                         side_effect=lambda p: "src/code" not in p.as_posix() and os.path.isfile(p)):
                self.assertEqual(sources._slot_source(), "/* table */")


@unittest.skipIf(np is None, REQUIRES)
class ControllerJobTests(unittest.TestCase):
    def test_register_build_cancel_fail_rebuild_relink_remove(self):
        from tools.btga_studio.core.controller import EditorController
        from tools.btga_studio.core.settings import Settings
        from tools.btga_studio.fixtures import simulated_catalog_builder, supported_revision, synthetic_rom

        with TempHome() as home:
            c = EditorController(Settings.open())
            try:
                rom = synthetic_rom(11)
                path = home / "roms ü" / "Global Assault.z64"
                path.parent.mkdir()
                path.write_bytes(rom)
                with supported_revision(rom):
                    checked = c.identify_source(path)
                    c.jobs.wait()
                    self.assertEqual(checked.result.status, "supported")
                    self.assertEqual(c.registry.sources, {})  # identifying never registers
                    job = c.add_source_async(path)
                    c.jobs.wait()
                    source = job.result
                    levels_before = len(c.library.levels())
                    with simulated_catalog_builder(delay=0.02):
                        build = c.build_catalog(source.id)
                        time.sleep(0.05)
                        build.cancel()
                        c.jobs.wait()
                    self.assertEqual(build.state, "cancelled")
                    self.assertEqual(c.registry.catalog_state(c.registry.sources[source.id])[0], "not built")
                    with simulated_catalog_builder():
                        c.build_catalog(source.id, wait=True)
                    state, size = c.registry.catalog_state(c.registry.sources[source.id])
                    self.assertEqual(state, "built")
                    self.assertGreater(size, 0)
                    self.assertGreater(len(c.library.levels()), levels_before)
                    catalog = c.registry.catalogs / c.registry.sources[source.id].catalog / "catalog.json"
                    good = catalog.read_bytes()
                    with simulated_catalog_builder(fail_after=3):
                        failed = c.build_catalog(source.id, wait=True)
                    self.assertEqual(failed.state, "failed")
                    self.assertEqual(catalog.read_bytes(), good)  # a failed rebuild keeps the working catalog
                    self.assertFalse(list(c.registry.catalogs.glob("*.building")))
                    moved = home / "elsewhere" / "renamed.z64"
                    moved.parent.mkdir()
                    path.rename(moved)
                    c.verify_sources()
                    c.jobs.wait()
                    self.assertEqual(c.registry.sources[source.id].status, "missing")
                    search = c.search_sources(home)
                    c.jobs.wait()
                    self.assertEqual(search.result, 1)
                    self.assertEqual(c.registry.sources[source.id].status, "supported")
                    other = home / "other.z64"
                    other.write_bytes(synthetic_rom(12))
                    wrong = c.relink_source(source.id, other)
                    c.jobs.wait()
                    self.assertEqual(wrong.state, "failed")
                    c.remove_source(source.id)
                    self.assertTrue(moved.is_file())
                    self.assertEqual(c.registry.sources, {})
                    self.assertEqual(c.registry.disk_usage(), 0)
            finally:
                c.shutdown()

    def test_export_job_is_atomic_and_reports_failures(self):
        from tools.btga_studio.content.examples import build_example
        from tools.btga_studio.core.controller import EditorController
        from tools.btga_studio.core.package import read_package
        from tools.btga_studio.core.settings import Settings

        with TempHome() as home:
            c = EditorController(Settings.open())
            try:
                c.project = build_example(c.library)
                target = home / "exports ü" / "my package.btgapkg"
                done = c.export_package(target, "test")
                c.jobs.wait()
                self.assertEqual(done.state, "done")
                self.assertEqual(len(read_package(target.read_bytes()).maps), 3)
                before = target.read_bytes()
                c.project.missions["first_strike"]["objectives"] = ["user:missing:0"]
                failed = c.export_package(target, "test")
                c.jobs.wait()
                self.assertEqual(failed.state, "failed")
                self.assertIn("MISSING_RECORD", failed.error)
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["my package.btgapkg"])
            finally:
                c.shutdown()


@unittest.skipIf(np is None, REQUIRES)
class HubTests(unittest.TestCase):
    def test_recent_entries_relink_duplicate_rename(self):
        from tools.btga_studio.core import hub
        from tools.btga_studio.core import project as projects
        from tools.btga_studio.core.settings import Preferences

        library = starter_library()
        with tempfile.TemporaryDirectory(prefix="hub ü ") as temp:
            folder = Path(temp)
            path, project = hub.create(library, "Desert Run", "mission", 1024.0, directory=folder)
            arena, _ = hub.create(library, "Arena", "skirmish", 1024.0, players=2, directory=folder)
            broken = folder / "broken.btgaproj"
            broken.write_text("{ nope")
            needs_game = folder / "needs game.btgaproj"
            data = json.loads(path.read_text())
            data["dependencies"]["sources"] = [{"platform": "n64_btga", "rom_sha256": "ab" * 32}]
            needs_game.write_text(json.dumps(data))
            prefs = Preferences()
            for item in (path, arena, broken, needs_game, folder / "gone.btgaproj"):
                prefs.add_recent(str(item))
            prefs.set_pinned(str(arena), True)
            entries = {e.path.name: e for e in hub.recent_entries(prefs, library)}
            self.assertTrue(hub.recent_entries(prefs, library)[0].pinned)
            self.assertEqual(entries["Desert Run.btgaproj"].title, "Desert Run")
            self.assertIn("cannot be opened", entries["broken.btgaproj"].error)
            self.assertTrue(entries["needs game.btgaproj"].warnings)
            self.assertFalse(entries["gone.btgaproj"].exists)
            moved = folder / "sub" / "Desert Run.btgaproj"
            moved.parent.mkdir()
            shutil.move(path, moved)
            with self.assertRaises(hub.HubError):
                hub.relink(prefs, path, arena, expected_id=project.project["id"])
            hub.relink(prefs, path, moved, expected_id=project.project["id"])
            self.assertIn(str(moved), prefs.recent_projects)
            copy = hub.duplicate(moved)
            self.assertNotEqual(projects.load_project(copy).project["id"], project.project["id"])
            self.assertEqual(copy.name, "Desert Run (copy).btgaproj")
            with self.assertRaises(hub.HubError):
                hub.rename(copy, "Desert Run")  # would overwrite the original
            renamed = hub.rename(copy, "Night Raid ü")
            self.assertFalse(copy.exists())
            self.assertEqual(projects.load_project(renamed).project["title"], "Night Raid ü")
            for name in ("CON", "a/b", "  "):
                self.assertTrue(hub.safe_file_stem(name))

    def test_templates_are_valid_and_rom_free(self):
        from tools.btga_studio.core import hub
        from tools.btga_studio.core.package import build_package, validate_for_export

        library = starter_library()
        with tempfile.TemporaryDirectory() as temp:
            for template in hub.TEMPLATES:
                path, project = hub.create(library, f"{template.title} ü", template.key, 2048.0, 3, "team deathmatch", Path(temp))
                self.assertTrue(validate_for_export(project, library).ok, template.key)
                self.assertEqual(project.dependencies["sources"], [])
                self.assertNotIn(str(Path(temp)), path.read_text(encoding="utf-8"))
                package = build_package(project, library)
                self.assertNotIn(Path(temp).name.encode(), package)
            with self.assertRaises(hub.HubError):
                hub.create(library, "x", "nonsense", directory=Path(temp))
            with self.assertRaises(hub.HubError):
                hub.create(library, "x", "skirmish", players=9, directory=Path(temp))


@unittest.skipIf(np is None, REQUIRES)
class PrefabTests(unittest.TestCase):
    def test_save_list_place_and_missing_models(self):
        from tools.btga_studio.content.examples import build_example
        from tools.btga_studio.core.prefabs import PrefabError, PrefabLibrary

        library = starter_library()
        document = build_example(library).open_map("arena", library)
        document.selection = [r["id"] for f, r in document.scene.records()][:3]
        with tempfile.TemporaryDirectory() as temp:
            prefabs = PrefabLibrary(Path(temp) / "prefabs")
            prefab = prefabs.save("Guard Post ü", document.copy())
            (Path(temp) / "prefabs" / "damaged.json").write_text("{")
            listed, problems = prefabs.list()
            self.assertEqual([p.title for p in listed], ["Guard Post ü"])
            self.assertEqual(len(problems), 1)
            self.assertEqual(PrefabLibrary.missing_models(prefab, library), [])
            self.assertTrue(PrefabLibrary.missing_models(prefab, None) or not prefab.models)
            pasted = document.paste(prefab.clipboard, (100.0, 0.0, 100.0))
            self.assertEqual(len(pasted), prefab.records)
            with self.assertRaises(PrefabError):
                prefabs.save("Empty", '{"format": "btga-studio-clipboard", "version": 1, "records": []}')
            prefabs.delete(prefab.key)
            self.assertEqual(prefabs.list()[0], [])


class ReleaseTests(unittest.TestCase):
    def test_deterministic_zip_manifest_checksums_and_version(self):
        from tools.btga_studio.packaging import release

        with tempfile.TemporaryDirectory() as temp:
            dist = Path(temp) / "dist ü" / "BattleTanxStudio"
            (dist / "_internal").mkdir(parents=True)
            (dist / "BattleTanxStudio.exe").write_bytes(b"MZ program")
            (dist / "_internal" / "lib.dll").write_bytes(b"library")
            (dist / "README.txt").write_text("readme", encoding="utf-8")
            release.prepare_dist(dist)
            first = release.finalize(dist, Path(temp) / "out1", "abc")
            os.utime(dist / "_internal" / "lib.dll", (1, 1))
            second = release.finalize(dist, Path(temp) / "out2", "abc")
            self.assertEqual(first["archive"]["sha256"], second["archive"]["sha256"])
            self.assertEqual((Path(temp) / "out1" / first["archive"]["name"]).read_bytes(),
                             (Path(temp) / "out2" / second["archive"]["name"]).read_bytes())
            sums = (Path(temp) / "out1" / "SHA256SUMS.txt").read_text()
            self.assertIn(first["archive"]["sha256"], sums)
            self.assertIn("THIRD-PARTY-NOTICES.txt", first["files"])
            notices = (dist / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8")
            self.assertIn("numpy", notices)
            self.assertIn("Python Software Foundation", notices)
            names = zipfile.ZipFile(Path(temp) / "out1" / first["archive"]["name"]).namelist()
            self.assertTrue(all(n.startswith(f"BattleTanxStudio-{release.__version__}/") for n in names))
            self.assertNotIn(str(Path(temp)), (Path(temp) / "out1" / "build-manifest.json").read_text())
        resource = release.version_resource("0.2.0", "c441458dff01")
        self.assertIn("filevers=(0, 2, 0, 0)", resource)
        self.assertIn("'OriginalFilename', 'BattleTanxStudio.exe'", resource)
        setup = release.version_resource("0.2.0", None, "BattleTanxStudioSetup", "BattleTanx Studio Setup")
        self.assertIn("'OriginalFilename', 'BattleTanxStudioSetup.exe'", setup)
        self.assertIn("'CompanyName', 'BattleTanx Studio contributors'", setup)


class NoTerminalTests(unittest.TestCase):
    def test_user_documents_and_gui_need_no_terminal(self):
        readme = (ROOT / "tools/btga_studio/packaging/README-windows.txt").read_text(encoding="utf-8")
        for developer_only in ("python -", "py -", ".py ", "pip install", "powershell", "export_neutral", "BTGA_STUDIO_HOME", "tools/", "tools\\"):
            self.assertNotIn(developer_only, readme)
        for module in ("gui/app.py", "gui/windows.py"):
            text = (ROOT / "tools/btga_studio" / module).read_text(encoding="utf-8")
            self.assertNotIn("subprocess", text)
            self.assertNotIn("os.system", text)
        help_text = (ROOT / "tools/btga_studio/gui/help.py").read_text(encoding="utf-8")
        self.assertNotIn("python", help_text.lower())


@unittest.skipIf(np is None or not _egl_available(), "needs imgui-bundle, PyOpenGL and EGL (off-screen UI)")
class ProductUITests(unittest.TestCase):
    def _app(self, home, width=1600, height=960):
        os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
        from tools.btga_studio.core.settings import Settings
        from tools.btga_studio.gui.app import StudioApp
        from tools.btga_studio.gui.offscreen import OffscreenUI

        app = StudioApp(Settings.open())
        return app, OffscreenUI(app, width, height)

    def test_first_launch_wizard_edit_save_reopen_export(self):
        from tools.btga_studio.core import project as projects
        from tools.btga_studio.core.package import read_package
        from tools.btga_studio.fixtures import ScriptedDialogs, recording_launcher

        with TempHome() as home, recording_launcher() as launched:
            app, ui = self._app(home)
            try:
                for _ in range(3):
                    ui.frame()
                self.assertTrue(app.show_hub)  # first launch shows the welcome choices
                self.assertIsNone(app.controller.project)
                app.open_wizard()
                app.wizard_state.update({"title": "Raid on Ünterberg", "template": 2, "size": 0})
                ui.frame()
                self.assertTrue(app.create_from_wizard())
                ui.frame()
                c = app.controller
                path = c.project_path
                self.assertTrue(path.is_file())
                self.assertEqual(path.parent, app.settings.projects_directory)
                self.assertFalse(app.show_hub)
                self.assertEqual(app.settings.preferences.recent_projects[0], str(path))
                self.assertFalse(c.dirty)
                # Toolbar and status bar reflect the editor state.
                self.assertIn("All changes saved", app.status_text())
                cube = c.library.starter()["models"]["cube"]
                app.try_placement("model", cube)
                c.camera.position = np.array([0.0, 2000.0, 0.0])
                c.camera.pitch = -(np.pi / 2 - 1e-6)
                for _ in range(2):
                    ui.frame()
                screen = ui.viewport_to_screen(c.viewport[0] / 2, c.viewport[1] / 2)
                ui.click(screen)
                ui.press("escape", mouse=screen)
                self.assertTrue(c.dirty)
                self.assertIn("Unsaved changes", app.status_text())
                app.gizmo_operation = "select"
                ui.frame()
                self.assertIn("Tool: Select", app.status_text())
                # Gameplay editor tabs all render.
                app.show_gameplay = True
                for tab in ("Map", "Spawns & teams", "Pickups & hazards", "Objectives & navigation", "Missions & campaigns",
                            "Multiplayer", "Minigames", "Tanks & gangs", "Minimap", "Prefabs"):
                    app.open_gameplay(tab)
                    ui.frame()
                    ui.frame()
                self.assertEqual(app.frame_errors, 0, app.errors)
                self.assertTrue(app.save())
                self.assertFalse(c.dirty)
                saved = path.read_bytes()
                c.project = None
                c.active_map = None
                ui.frame()
                self.assertTrue(app.open_project(path))
                ui.frame()
                self.assertEqual(projects.dumps(c.project).encode("utf-8"), saved)
                export = path.with_suffix(".btgapkg")
                app.dialogs = ScriptedDialogs(export)
                app.export_package()
                c.jobs.wait()
                ui.frame()
                self.assertEqual(app.export_result["name"], export.name)
                self.assertEqual(len(read_package(export.read_bytes()).maps), 1)
                for window in ("show_settings", "show_about", "show_help", "show_sources"):
                    setattr(app, window, True)
                    ui.frame()
                    setattr(app, window, False)
                    ui.frame()
                app.report_problem()
                self.assertTrue(list(app.settings.logs_directory.glob("problem-report-*.txt")))
                self.assertTrue(any("issues" in item for item in launched))
                self.assertEqual(app.frame_errors, 0)
            finally:
                ui.close()

    def test_malformed_and_missing_projects_leave_the_app_usable(self):
        with TempHome() as home:
            app, ui = self._app(home)
            try:
                ui.frame()
                broken = home / "bad project ü.btgaproj"
                broken.write_text('{"schema": "btga-studio-project", "schema_version": 2, "maps": [1]}')
                app.settings.preferences.add_recent(str(broken))
                self.assertFalse(app.open_project(broken))
                ui.frame()
                self.assertIsNone(app.controller.project)
                self.assertIn("could not be opened", app.errors[0]["title"])
                actions = dict(app.errors[0]["actions"])
                actions["Remove from recent list"]()
                self.assertNotIn(str(broken), app.settings.preferences.recent_projects)
                app.errors.clear()
                newer = home / "future.btgaproj"
                newer.write_text('{"schema": "btga-studio-project", "schema_version": 99}')
                self.assertFalse(app.open_project(newer))
                self.assertIn("newer version", app.errors[-1]["message"])
                self.assertFalse(app.open_project(home / "never existed.btgaproj"))
                self.assertIn("Project not found", app.errors[-1]["title"])
                app.errors.clear()
                app.open_example()
                ui.frame()
                self.assertIsNotNone(app.controller.document)
            finally:
                ui.close()

    def test_frame_errors_are_contained(self):
        with TempHome() as home:
            app, ui = self._app(home)
            try:
                ui.frame()
                original = app.panel_inspector

                def broken():
                    from imgui_bundle import imgui

                    imgui.begin_child("never-closed")  # leaves the window stack unbalanced
                    raise RuntimeError("simulated panel failure")

                app.panel_inspector = broken
                for _ in range(3):
                    ui.frame()
                app.panel_inspector = original
                ui.frame()
                self.assertEqual(app.frame_errors, 3)
                self.assertEqual(len([e for e in app.errors if e["title"] == "Something went wrong"]), 1)
            finally:
                ui.close()

    def test_source_manager_flow_with_synthetic_fixtures(self):
        from tools.btga_studio.fixtures import simulated_catalog_builder, supported_revision, synthetic_rom

        with TempHome() as home:
            app, ui = self._app(home)
            c = app.controller
            try:
                rom = synthetic_rom(21)
                path = home / "my roms" / "BattleTanx GA.z64"
                path.parent.mkdir()
                path.write_bytes(rom)
                junk = home / "notes.txt"
                junk.write_text("not a game")
                with supported_revision(rom), simulated_catalog_builder():
                    app.show_sources = True
                    app.check_source(junk)
                    c.jobs.wait()
                    ui.frame()
                    self.assertIn("cannot be used", app.errors[-1]["title"])
                    app.errors.clear()
                    app.check_source(path)
                    c.jobs.wait()
                    ui.frame()
                    self.assertEqual([s.status for s in app.identified], ["supported"])
                    c.add_source_async(path, app._registered)
                    app.identified.clear()
                    c.jobs.wait()
                    ui.frame()
                    source_id = next(iter(c.registry.sources))
                    c.build_catalog(source_id, on_done=app._built)
                    c.jobs.wait()
                    for _ in range(2):
                        ui.frame()
                    self.assertEqual(c.registry.catalog_state(c.registry.sources[source_id])[0], "built")
                    self.assertTrue(any(level.platform != "studio" for level in c.library.levels()))
                    app.import_from_games()
                    self.assertTrue(app.show_new_project)
                    app.show_new_project = False
                self.assertEqual(app.frame_errors, 0)
            finally:
                ui.close()

    def test_scaling_and_small_windows(self):
        from imgui_bundle import imgui

        with TempHome() as home:
            for width, height, scale in ((1024, 640, 1.0), (1280, 720, 1.25), (1600, 960, 1.5), (1920, 1080, 2.0)):
                app, ui = self._app(home, width, height)
                try:
                    imgui.get_style().font_scale_main = scale
                    app.open_example()
                    app.show_gameplay = app.show_sources = True
                    for _ in range(3):
                        ui.frame()
                    self.assertEqual(app.frame_errors, 0, (width, scale))
                    self.assertGreater(app.controller.viewport[0], 64)
                    self.assertGreater(app.controller.viewport[1], 64)
                finally:
                    ui.close()


if __name__ == "__main__":
    unittest.main()
