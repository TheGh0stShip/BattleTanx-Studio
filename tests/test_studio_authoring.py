"""Authoring workflow tests: projects, packages, sources, starter content (synthetic; no game data).

Requires tools/btga_studio/requirements.txt.  Real-source tests are gated on
BTGA_N64_ROM, BTGA_BT1_ROM, BTGA_PS1_EXTRACTED and BTGA_PS1_IMAGE.
"""

from __future__ import annotations

import copy
import gc
import hashlib
import io
import json
import os
import shutil
import struct
import tempfile
import tracemalloc
import unittest
import zipfile
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

ROOT = Path(__file__).resolve().parents[1]
REQUIRES = "install tools/btga_studio/requirements.txt (numpy) to run the authoring tests"
_STARTER = {}


def starter_root() -> Path:
    if "root" not in _STARTER:
        from tools.btga_studio.content.starter import ensure_starter_catalog

        _STARTER["root"] = ensure_starter_catalog(Path(tempfile.mkdtemp(prefix="btga-starter-")))
    return _STARTER["root"]


def starter_library():
    from tools.btga_studio.core.library import LibrarySet

    return LibrarySet.open_all([starter_root()])


def example_project():
    from tools.btga_studio.content.examples import build_example

    return build_example(starter_library())


@unittest.skipIf(np is None, REQUIRES)
class StarterContentTests(unittest.TestCase):
    def test_starter_is_deterministic_and_original(self):
        from tools.btga_studio.content.starter import write_starter_catalog

        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            write_starter_catalog(Path(a))
            write_starter_catalog(Path(b))
            files = sorted(p.relative_to(a) for p in Path(a).rglob("*") if p.is_file())
            self.assertEqual(files, sorted(p.relative_to(b) for p in Path(b).rglob("*") if p.is_file()))
            for relative in files:
                self.assertEqual((Path(a) / relative).read_bytes(), (Path(b) / relative).read_bytes(), relative)
            catalog = json.loads((Path(a) / "catalog.json").read_text())
            self.assertTrue(all(m["platform"] == "studio" for m in catalog["models"]))
            self.assertTrue(all(s.get("platform") == "studio" for s in catalog["sources"]))
            self.assertTrue(all(m["id"].startswith("studio:model:") for m in catalog["models"]))
            labels = {m["label"] for m in catalog["models"]}
            for name in ("ground_tile", "cube", "ramp", "wall", "road_straight", "building_small", "marker_spawn", "marker_pickup",
                         "marker_hazard", "marker_objective"):
                self.assertIn(name, labels)

    def test_example_project_matches_committed_file(self):
        from tools.btga_studio.core.project import dumps

        committed = (ROOT / "examples" / "starter-example.btgaproj").read_text(encoding="utf-8")
        self.assertEqual(dumps(example_project()), committed)

    def test_editor_runs_without_any_rom(self):
        from tools.btga_studio.core.controller import EditorController
        from tools.btga_studio.core.settings import Settings

        with tempfile.TemporaryDirectory() as temp:
            os.environ["BTGA_STUDIO_HOME"] = temp
            controller = EditorController(Settings.open())
            self.assertEqual({l.platform for l in controller.library.levels()}, {"studio"})
            controller.add_blank_map("first", "First map", 1024)
            starter = controller.library.starter()
            new = controller.document.place_model(starter["models"]["cube"], (10.0, 0.0, 10.0))
            self.assertTrue(controller.document.record(new)["source"]["user_created"])
            template = next(t for t in starter["templates"] if t["source_kind"] == "studio_team_start")
            controller.begin_placement("marker", template["id"])
            self.assertIsNotNone(controller.placement)
            controller.shutdown()


@unittest.skipIf(np is None, REQUIRES)
class ProjectFormatTests(unittest.TestCase):
    def test_round_trip_unknown_fields_and_definitions(self):
        from tools.btga_studio.core import project as p

        project = example_project()
        text = p.dumps(project)
        data = json.loads(text)
        data["future_section"] = {"keep": [1, 2, 3]}
        data["maps"]["arena"]["mystery"] = "value"
        data["missions"]["first_strike"]["custom"] = {"nested": True}
        reloaded = p.loads_project(json.dumps(data))
        again = json.loads(p.dumps(reloaded))
        self.assertEqual(again["future_section"], {"keep": [1, 2, 3]})
        self.assertEqual(again["maps"]["arena"]["extra"]["_unknown"]["mystery"], "value")
        self.assertEqual(again["missions"]["first_strike"]["custom"], {"nested": True})
        self.assertEqual(sorted(again["maps"]), ["arena", "target_range", "training_grounds"])
        self.assertEqual(again["campaigns"][0]["missions"], ["first_strike"])
        self.assertEqual(sorted(again["tanks"]), ["heavy", "scout"])
        self.assertEqual(p.dumps(p.loads_project(p.dumps(reloaded))), p.dumps(reloaded))
        self.assertNotIn("/home/", text)
        self.assertNotIn("\\", text)

    def test_migration_and_version_rules(self):
        from tools.btga_studio.core import project as p

        v1 = {"schema": "btga-studio-project", "schema_version": 1, "scene": "studio:scene:starter_showcase", "platform": "studio",
              "source_fingerprint": "ab" * 32, "title": "Old", "overlay": {"schema": "btga-overlay", "schema_version": 1,
                                                                            "scene": "studio:scene:starter_showcase", "platform": "studio",
                                                                            "next_user_index": 0, "edits": []}}
        migrated = p.loads_project(json.dumps(v1))
        self.assertEqual(list(migrated.maps), ["main"])
        self.assertEqual(migrated.maps["main"].source_fingerprint, "ab" * 32)
        self.assertEqual(json.loads(p.dumps(migrated))["schema_version"], 2)
        with self.assertRaises(p.FutureVersion):
            p.loads_project(json.dumps({"schema": "btga-studio-project", "schema_version": 99}))
        with self.assertRaises(p.ProjectError):
            p.loads_project(json.dumps({"schema": "something-else", "schema_version": 2}))
        with self.assertRaises(p.ProjectError):
            p.loads_project("not json")
        bad = json.loads(p.dumps(example_project()))
        bad["maps"]["Bad Key"] = bad["maps"].pop("arena")
        with self.assertRaises(p.ProjectError):
            p.loads_project(json.dumps(bad))
        with self.assertRaises(p.ProjectError):
            p.StudioProject("x").add_blank_map("UPPER", "x")

    def test_history_and_view_state_survive_save_and_reload(self):
        from tools.btga_studio.core import project as p

        library = starter_library()
        project = example_project()
        document = project.open_map("arena", library)
        cube = library.starter()["models"]["cube"]
        first = document.place_model(cube, (100.0, 0.0, 100.0))
        document.set_transform(first, [200.0, 0.0, 200.0], 0x4000)
        second = document.place_model(cube, (-100.0, 0.0, -100.0))
        document.undo()
        document.hide([first])
        document.lock([first])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "proj ü ț.btgaproj"
            p.save_project(path, project)
            reloaded = p.load_project(path)
            self.assertFalse(reloaded.dirty)
            doc2 = reloaded.open_map("arena", library)
            self.assertFalse(doc2.dirty)
            self.assertIn(first, doc2.hidden)
            self.assertIn(first, doc2.locked)
            self.assertEqual(doc2.record(first)["translation"], [200.0, 0.0, 200.0])
            self.assertIsNone(doc2.scene.record(second))
            doc2.redo()  # the undone placement comes back with the same ID
            self.assertEqual(doc2.record(second)["translation"], [-100.0, 0.0, -100.0])
            doc2.undo()
            doc2.undo()
            self.assertEqual(doc2.record(first)["translation"], [100.0, 0.0, 100.0])
            doc2.undo()
            self.assertIsNone(doc2.scene.record(first))
            self.assertTrue(reloaded.dirty)

    def test_no_asset_loss_after_edit_save_reload(self):
        from tools.btga_studio.core import project as p

        library = starter_library()
        project = example_project()
        before = {}
        for key in project.maps:
            document = project.open_map(key, library)
            before[key] = json.dumps(document.scene.edited_scene(document.overlay), sort_keys=True)
        with tempfile.TemporaryDirectory() as temp:
            p.save_project(Path(temp) / "a.btgaproj", project)
            reloaded = p.load_project(Path(temp) / "a.btgaproj")
            for key in reloaded.maps:
                document = reloaded.open_map(key, library)
                self.assertEqual(json.dumps(document.scene.edited_scene(document.overlay), sort_keys=True), before[key], key)

    def test_problems_name_object_and_field(self):
        project = example_project()
        project.define("missions", "broken", {"title": "Broken", "map": "nowhere", "objectives": []})
        project.define("gangs", "ghosts", {"title": "Ghosts", "tanks": ["missing_tank"]})
        project.define("tanks", "odd", {"title": "Odd", "model": "studio:model:missing"})
        problems = {(x["code"], x["target"], x["field"]) for x in project.problems(starter_library())}
        self.assertIn(("MISSING_MAP", "missions:broken", "map"), problems)
        self.assertIn(("MISSING_TANK", "gangs:ghosts", "tanks"), problems)
        self.assertIn(("MISSING_MODEL", "tanks:odd", "model"), problems)

    def test_imported_map_mismatch_is_refused(self):
        from tools.btga_studio.core import project as p

        library = starter_library()
        project = p.StudioProject("Imported")
        entry = project.add_imported_map("showcase", library, "studio:scene:starter_showcase")
        entry.source_fingerprint = "0" * 64
        with self.assertRaises(p.ProjectMismatch):
            project.open_map("showcase", library)
        entry.scene = "n64_btga:scene:missing"
        with self.assertRaises(p.ProjectMismatch):
            project.open_map("showcase", library)

    def test_autosave_and_crash_recovery(self):
        from tools.btga_studio.core import project as p
        from tools.btga_studio.core.controller import EditorController
        from tools.btga_studio.core.settings import Settings

        with tempfile.TemporaryDirectory() as temp:
            os.environ["BTGA_STUDIO_HOME"] = temp
            settings = Settings.open()
            settings.preferences.autosave_enabled = True
            settings.preferences.autosave_seconds = 15
            controller = EditorController(settings)
            controller.add_blank_map("crash", "Crash test", 1024)
            controller.document.place_model(controller.library.starter()["models"]["cube"], (1.0, 0.0, 1.0))
            controller.autosaver._last -= 1000
            controller.tick()
            recoveries = controller.recoveries()
            self.assertEqual(len(recoveries), 1)
            controller.shutdown()
            # "Crash": a new controller finds the recovery and restores it as unsaved work.
            fresh = EditorController(Settings.open())
            fresh.restore_recovery(fresh.recoveries()[0]["path"])
            self.assertTrue(fresh.dirty)
            self.assertIsNone(fresh.project.path)
            self.assertEqual(len(fresh.document.scene.user), 1)
            fresh.save(Path(temp) / "saved.btgaproj")
            self.assertFalse(fresh.dirty)
            self.assertEqual(fresh.recoveries(), [])
            fresh.shutdown()


@unittest.skipIf(np is None, REQUIRES)
class PackageTests(unittest.TestCase):
    def build(self, project=None):
        from tools.btga_studio.core.package import build_package

        return build_package(project or example_project(), starter_library(), "test")

    def test_deterministic_and_readable(self):
        from tools.btga_studio.core.package import read_package

        first, second = self.build(), self.build()
        self.assertEqual(first, second)
        package = read_package(first)
        self.assertEqual(sorted(package.maps), ["arena", "target_range", "training_grounds"])
        self.assertEqual(package.manifest["schema_versions"]["project"], 2)
        self.assertEqual(package.manifest["sources"], [])
        self.assertTrue(package.models)
        self.assertTrue(all(model_id.startswith("studio:") for model_id in package.models))
        training = package.maps["training_grounds"]
        self.assertEqual(len(training["navigation"]["points"]), 3)
        self.assertTrue(all(record["translation"] for record in training["records"]["markers"]))
        names = zipfile.ZipFile(io.BytesIO(first)).namelist()
        self.assertEqual(names, sorted(names))
        self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in zipfile.ZipFile(io.BytesIO(first)).infolist()))

    def test_unknown_bytes_and_fields_travel(self):
        from tools.btga_studio.core.package import read_package

        project = example_project()
        project.unknown["future_section"] = {"x": 1}
        document = project.open_map("arena", starter_library())
        marker = next(r for f, r in document.scene.records("markers"))
        package = read_package(self.build(project))
        self.assertEqual(package.project["unknown"]["future_section"], {"x": 1})
        records = {r["id"]: r for r in package.maps["arena"]["records"]["markers"]}
        self.assertEqual(records[marker["id"]].get("raw_hex"), marker.get("raw_hex"))

    def rewrite(self, data: bytes, mutate) -> bytes:
        archive = zipfile.ZipFile(io.BytesIO(data))
        files = {info.filename: archive.read(info.filename) for info in archive.infolist()}
        mutate(files)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as writer:
            for name, content in files.items():
                writer.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), content)
        return out.getvalue()

    def test_malformed_packages_are_refused(self):
        from tools.btga_studio.core import package as pk

        good = self.build()

        def manifest(files):
            return json.loads(files["manifest.json"])

        def tamper_map(files):
            files["maps/arena.json"] = files["maps/arena.json"].replace(b"Square", b"Round!")

        def traversal(files):
            files["../evil.json"] = b"{}"

        def missing_manifest(files):
            del files["manifest.json"]

        def future(files):
            m = manifest(files)
            m["schema_version"] = 99
            files["manifest.json"] = json.dumps(m).encode()

        def extra_file(files):
            files["maps/unlisted.json"] = b"{}"

        def malformed_file_list(files):
            m = manifest(files)
            m["files"] = ["not an entry"]
            files["manifest.json"] = json.dumps(m).encode()

        def bad_texture(files):
            name = next(n for n in files if n.startswith("resources/textures/"))
            files[name] = files[name][:-4]
            m = manifest(files)
            for entry in m["files"]:
                if entry["path"] == name:
                    entry["size"], entry["sha256"] = len(files[name]), hashlib.sha256(files[name]).hexdigest()
            files["manifest.json"] = json.dumps(m).encode()

        def out_of_bounds(files):
            data = json.loads(files["maps/arena.json"])
            data["records"]["markers"][0]["translation"] = [99999.0, 0.0, 0.0]
            files["maps/arena.json"] = json.dumps(data).encode()
            m = manifest(files)
            for entry in m["files"]:
                if entry["path"] == "maps/arena.json":
                    entry["size"], entry["sha256"] = len(files["maps/arena.json"]), hashlib.sha256(files["maps/arena.json"]).hexdigest()
            files["manifest.json"] = json.dumps(m).encode()

        for mutate in (tamper_map, traversal, missing_manifest, future, extra_file, malformed_file_list, bad_texture, out_of_bounds):
            with self.assertRaises(pk.PackageError, msg=mutate.__name__):
                pk.read_package(self.rewrite(good, mutate))
        with self.assertRaises(pk.PackageError):
            pk.read_package(b"not a zip")
        link = io.BytesIO()
        with zipfile.ZipFile(link, "w") as writer:
            info = zipfile.ZipInfo("manifest.json")
            info.external_attr = 0o120777 << 16
            writer.writestr(info, b"target")
        with self.assertRaises(pk.PackageError):
            pk.read_package(link.getvalue())
        original = dict(pk.LIMITS)
        try:
            pk.LIMITS["package_bytes"] = 1000
            with self.assertRaises(pk.PackageError):
                pk.read_package(good)
            with self.assertRaises(pk.PackageError):
                self.build()
        finally:
            pk.LIMITS.update(original)

        compressed = io.BytesIO()
        payload = b"x" * 2000
        manifest_data = json.dumps({
            "schema": pk.PACKAGE_SCHEMA,
            "schema_version": pk.PACKAGE_VERSION,
            "files": [{"path": "project.json", "size": len(payload),
                       "sha256": hashlib.sha256(payload).hexdigest()}],
        }).encode()
        with zipfile.ZipFile(compressed, "w", zipfile.ZIP_DEFLATED) as writer:
            writer.writestr("manifest.json", manifest_data)
            writer.writestr("project.json", payload)
        self.assertLess(len(compressed.getvalue()), 1000)
        try:
            pk.LIMITS["package_bytes"] = 1000
            with self.assertRaisesRegex(pk.PackageError, "expands beyond"):
                pk.read_package(compressed.getvalue())
        finally:
            pk.LIMITS.update(original)

    def test_export_validation_blocks_with_object_and_field(self):
        from tools.btga_studio.core import package as pk

        library = starter_library()
        project = example_project()
        document = project.open_map("arena", library)
        objective_template = next(t for t in library.starter()["templates"] if t["source_kind"] == "studio_objective")
        objective = document.place_marker(objective_template, (0.0, 0.0, 0.0))
        document.overlay.add({"op": "set_field", "target": objective, "field": "target", "value": "user:missing:9"})
        document.scene.refresh(document.overlay)
        report = pk.validate_for_export(project, library)
        self.assertIn(("DANGLING_REFERENCE", objective, "target", "arena"), {(e["code"], e["target"], e["field"], e["map"]) for e in report.errors})
        with self.assertRaises(pk.PackageError):
            pk.build_package(project, library)
        scaled = example_project()
        scaled.project["target"]["game"] = "n64_btga"
        report = pk.validate_for_export(scaled, library)
        self.assertTrue(any(e["code"] == "UNSUPPORTED_FEATURE" for e in report.errors + report.warnings))


@unittest.skipIf(np is None, REQUIRES)
class EditingFeatureTests(unittest.TestCase):
    def setUp(self):
        self.library = starter_library()
        self.project = example_project()
        self.document = self.project.open_map("arena", self.library)
        self.cube = self.library.starter()["models"]["cube"]

    def test_scale_gizmo_transaction_and_scaled_picking(self):
        from tools.btga_studio.core import geometry as g

        doc = self.document
        cube = doc.place_model(self.cube, (0.0, 0.0, 600.0))
        doc.begin_transform([cube])
        doc.update_transform(scale=(2.0, 3.0, 2.0))
        doc.cancel_transform()
        self.assertNotIn("scale", doc.record(cube))
        doc.begin_transform([cube])
        for factor in (1.5, 2.0, 3.0):
            doc.update_transform(scale=(factor, factor, factor))
        doc.commit_transform()
        self.assertEqual(doc.record(cube)["scale"], [3.0, 3.0, 3.0])
        self.assertEqual(len([t for t in doc.undo_stack if t.label == "Transform"]), 1)
        ray = g.Ray(np.array([0.0, 1000.0, 600.0]), np.array([0.0, -1.0, 0.0]))
        hit = next(h for h in doc.scene.pick(ray, {"instances"}) if h.id == cube)
        self.assertAlmostEqual(hit.distance, 1000.0 - 300.0)
        from tools.btga_studio.core.document import EditError

        doc.begin_transform([cube])
        with self.assertRaises(EditError):
            doc.update_transform(scale=(0.0, 1.0, 1.0))
        doc.cancel_transform()

    def test_copy_paste_is_deterministic_and_lands_at_point(self):
        doc = self.document
        first = doc.place_model(self.cube, (100.0, 0.0, 100.0))
        second = doc.place_model(self.cube, (300.0, 0.0, 100.0))
        doc.selection = [second, first]
        text = doc.copy()
        self.assertEqual(text, doc.copy([first, second]))
        pasted = doc.paste(text, (0.0, 0.0, -500.0))
        positions = sorted(doc.record(i)["translation"] for i in pasted)
        self.assertEqual(positions, [[-100.0, 0.0, -500.0], [100.0, 0.0, -500.0]])
        self.assertEqual(doc.selection, pasted)
        from tools.btga_studio.core.document import EditError

        with self.assertRaises(EditError):
            doc.paste("{}")

    def test_hide_isolate_lock(self):
        from tools.btga_studio.core.document import EditError

        doc = self.document
        doc.selection = []
        ids = [r["id"] for f, r in doc.scene.records()][:3]
        doc.isolate([ids[0]])
        self.assertNotIn(ids[0], doc.hidden)
        self.assertIn(ids[1], doc.hidden)
        doc.select(ids[1])
        self.assertEqual(doc.selection, [])
        doc.unhide_all()
        doc.lock([ids[2]])
        doc.select(ids[2])
        self.assertEqual(doc.selection, [])
        with self.assertRaises(EditError):
            doc.set_transform(ids[2], [0.0, 0.0, 0.0])
        doc.unlock_all()
        doc.set_transform(ids[2], [0.0, 0.0, 0.0])

    def test_controller_box_select_paste_and_switching(self):
        from tools.btga_studio.core import geometry as g
        from tools.btga_studio.core.controller import EditorController
        from tools.btga_studio.core.settings import Settings

        with tempfile.TemporaryDirectory() as temp:
            os.environ["BTGA_STUDIO_HOME"] = temp
            c = EditorController(Settings.open())
            c.project = example_project()
            c.switch_map("arena")
            c.viewport = (800, 600)
            c.camera = g.Camera(position=np.array([0.0, 5000.0, 0.0]), pitch=-math_pi_2())
            chosen = c.box_select((0, 0, 800, 600))
            self.assertEqual(sorted(chosen), sorted(c.document.selection))
            self.assertGreater(len(chosen), 10)
            c.box_select((0, 0, 1, 1))
            self.assertEqual(c.document.selection, [])
            c.document.selection = [chosen[0]]
            c.copy()
            c.hover(400, 300)
            pasted = c.paste()
            self.assertEqual(len(pasted), 1)
            tracemalloc.start()
            heaps = []
            for cycle in range(12):
                c.switch_map(["arena", "target_range", "training_grounds"][cycle % 3])
                gc.collect()
                heaps.append(tracemalloc.get_traced_memory()[0])
            tracemalloc.stop()
            self.assertLess(max(heaps[6:]) - min(heaps[6:]), 512 * 1024, heaps)
            self.assertLessEqual(c.mesh_cache.triangles, c.mesh_cache.limit)
            c.shutdown()


def math_pi_2() -> float:
    import math

    return math.pi / 2 - 1e-6


@unittest.skipIf(np is None, REQUIRES)
class BindingTests(unittest.TestCase):
    def test_bindings(self):
        from tools.btga_studio.gui.input import InputState, commands, validate_bindings

        with self.assertRaises(ValueError):
            validate_bindings({"forward": "S"})  # S is already "back"
        with self.assertRaises(ValueError):
            validate_bindings({"fly": "X"})
        with self.assertRaises(ValueError):
            validate_bindings({"forward": "Space"})
        bindings = validate_bindings({"forward": "I", "isolate": "K"})
        state = InputState(viewport_hovered=True, keys_down={"I"}, bindings=bindings)
        self.assertEqual(commands(state)[0].name, "move")
        typing = InputState(viewport_hovered=True, text_input=True, keys_down={"I"}, keys_pressed={"K", "Delete"}, bindings=bindings)
        self.assertEqual(commands(typing), [])
        box = InputState(viewport_hovered=True, box=(1, 2, 3, 4), keys_down={"Shift"})
        self.assertEqual([(c.name, c.args["add"]) for c in commands(box)], [("box_select", True)])
        copy_ = InputState(keys_down={"Ctrl"}, keys_pressed={"C"})
        self.assertEqual([c.name for c in commands(copy_)], ["copy"])


# ----------------------------------------------------------------- sources --

def synthetic_rom(seed: int = 1, size: int = 1 << 20) -> bytes:
    body = hashlib.sha256(str(seed).encode()).digest() * (size // 32)
    return b"\x80\x37\x12\x40" + body[4:size]


def iso_image(files: dict[str, bytes], raw: bool = False) -> bytes:
    """A minimal ISO 9660 image (root directory plus one level of folders)."""

    sectors: dict[int, bytes] = {}
    next_sector = 20

    def record(name: bytes, extent: int, size: int, directory: bool) -> bytes:
        length = 33 + len(name) + (len(name) + 1) % 2
        data = bytearray(length)
        data[0] = length
        struct.pack_into("<I", data, 2, extent)
        struct.pack_into(">I", data, 6, extent)
        struct.pack_into("<I", data, 10, size)
        struct.pack_into(">I", data, 14, size)
        data[25] = 2 if directory else 0
        data[32] = len(name)
        data[33 : 33 + len(name)] = name
        return bytes(data)

    def place(content: bytes) -> int:
        nonlocal next_sector
        start = next_sector
        for index in range(0, max(1, len(content)), 2048):
            sectors[next_sector] = content[index : index + 2048].ljust(2048, b"\0")
            next_sector += 1
        return start

    tree: dict[str, dict] = {"": {}}
    for path in files:
        parts = path.split("/")
        for depth in range(1, len(parts)):
            tree.setdefault("/".join(parts[:depth]), {})
    def build_directory(prefix: str) -> tuple[int, int]:
        entries = []
        for path, content in sorted(files.items()):
            folder, _, name = path.rpartition("/")
            if folder == prefix:
                entries.append(record(name.encode() + b";1", place(content), len(content), False))
        for folder in sorted(tree):
            if folder and folder.rpartition("/")[0] == prefix:
                extent, size = build_directory(folder)
                entries.append(record(folder.rpartition("/")[2].encode(), extent, size, True))
        body = record(b"\x00", 0, 2048, True) + record(b"\x01", 0, 2048, True) + b"".join(entries)
        extent = place(body)
        return extent, 2048 * ((len(body) + 2047) // 2048)

    root_extent, root_size = build_directory("")
    pvd = bytearray(2048)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[40:72] = b"TESTDISC".ljust(32)
    pvd[156:156 + 34] = record(b"\x00", root_extent, root_size, True)
    sectors[16] = bytes(pvd)
    terminator = bytearray(2048)
    terminator[0], terminator[1:6] = 255, b"CD001"
    sectors[17] = bytes(terminator)
    total = max(sectors) + 1
    out = bytearray()
    for number in range(total):
        data = sectors.get(number, bytes(2048))
        if raw:
            header = bytes([0] + [0xFF] * 10 + [0]) + bytes([0, 2, 0, 2]) + bytes(8)
            out += header + data + bytes(280)
        else:
            out += data
    return bytes(out)


@unittest.skipIf(np is None, REQUIRES)
class SourceManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="btga sources ü "))

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_n64_identification_and_byte_orders(self):
        from tools.btga_studio.core import sources

        rom = synthetic_rom()
        digest = hashlib.sha1(rom).hexdigest()
        (self.temp / "unknown game.z64").write_bytes(rom)
        unknown = sources.identify(self.temp / "unknown game.z64")
        self.assertEqual(unknown.status, "unknown")
        self.assertIn("not a supported revision", unknown.detail)
        swapped = bytearray(rom)
        swapped[0::2], swapped[1::2] = rom[1::2], rom[0::2]
        (self.temp / "ром v64.v64").write_bytes(bytes(swapped))
        little = bytearray(len(rom))
        little[0::4], little[1::4], little[2::4], little[3::4] = rom[3::4], rom[2::4], rom[1::4], rom[0::4]
        (self.temp / "rom.n64").write_bytes(bytes(little))
        original = dict(sources.N64_REVISIONS)
        try:
            sources.N64_REVISIONS[digest] = ("n64_bt1", "Synthetic test ROM")
            for name, order in (("unknown game.z64", "z64"), ("ром v64.v64", "v64"), ("rom.n64", "n64")):
                found = sources.identify(self.temp / name)
                self.assertEqual((found.status, found.sha1, found.platform), ("supported", digest, "n64_bt1"), name)
                self.assertIn(order, found.detail)
        finally:
            sources.N64_REVISIONS.clear()
            sources.N64_REVISIONS.update(original)
        (self.temp / "junk.z64").write_bytes(b"not a rom at all")
        with self.assertRaises(sources.SourceError):
            sources.identify(self.temp / "junk.z64")

    def test_registry_missing_mismatch_relocate_and_search(self):
        from tools.btga_studio.core import sources

        rom = synthetic_rom(2)
        path = self.temp / "my roms" / "Battle Tanx ü.z64"
        path.parent.mkdir()
        path.write_bytes(rom)
        original = dict(sources.N64_REVISIONS)
        sources.N64_REVISIONS[hashlib.sha1(rom).hexdigest()] = ("n64_btga", "Synthetic")
        try:
            registry = sources.SourceRegistry(self.temp / "data")
            source = registry.register(path)
            self.assertEqual(source.status, "supported")
            self.assertEqual(Path(source.path), path.resolve())
            again = sources.SourceRegistry(self.temp / "data")
            self.assertEqual(again.sources[source.id].sha256, source.sha256)
            moved = self.temp / "elsewhere" / "renamed.z64"
            moved.parent.mkdir()
            path.rename(moved)
            self.assertEqual(registry.verify(source.id).status, "missing")
            self.assertEqual(registry.search(self.temp), {source.id: moved})
            other = self.temp / "other.z64"
            other.write_bytes(synthetic_rom(3))
            with self.assertRaises(sources.SourceError):
                registry.relocate(source.id, other)
            self.assertEqual(registry.relocate(source.id, moved).status, "supported")
            moved.write_bytes(synthetic_rom(4))
            self.assertEqual(registry.verify(source.id).status, "mismatched")
            with self.assertRaises(sources.SourceError):
                registry.build_catalog(source.id)
            text = (self.temp / "data" / "sources.json").read_text(encoding="utf-8")
            self.assertNotIn("rgba", text)
            registry.sources[source.id].catalog = "../../outside"
            self.assertEqual(registry.catalog_roots(), [])  # an unsafe name is skipped, never followed (and never crashes startup)
            self.assertEqual(registry.catalog_state(registry.sources[source.id]), ("damaged", 0))
            outside = self.temp / "outside"
            outside.mkdir()
            with self.assertRaisesRegex(sources.SourceError, "unsafe catalog"):
                registry.remove(source.id)
            self.assertTrue(outside.is_dir())
            self.assertIn(source.id, registry.sources)
        finally:
            sources.N64_REVISIONS.clear()
            sources.N64_REVISIONS.update(original)

    def test_ps1_folder_and_images(self):
        from tools.btga_assets.iso9660 import DiscImage, IsoError
        from tools.btga_studio.core import sources

        executable = b"PS-X EXE synthetic"
        digest = hashlib.sha256(executable).hexdigest()
        files = {"SLUS_010.44": executable, "SYSTEM.CNF": b"BOOT=cdrom:\\SLUS_010.44;1", "ASSETS/ENUMS.MMM": b"x",
                 "ASSETS/LONDON1.BIN": b"y", "ASSETS/LONDON1.MMM": b"z"}
        original = dict(sources.PS1_EXECUTABLES)
        sources.PS1_EXECUTABLES[digest] = ("ps1", "Synthetic PS1", "SLUS_010.44")
        try:
            folder = self.temp / "extracted disc"
            for name, content in files.items():
                (folder / name).parent.mkdir(parents=True, exist_ok=True)
                (folder / name).write_bytes(content)
            self.assertEqual(sources.identify(folder).status, "supported")
            (folder / "ASSETS" / "ENUMS.MMM").unlink()
            incomplete = sources.identify(folder)
            self.assertEqual(incomplete.status, "incomplete")
            self.assertIn("ASSETS/ENUMS.MMM", incomplete.detail)
            for raw in (False, True):
                image = self.temp / f"disc {'raw' if raw else 'iso'} ü.{'bin' if raw else 'iso'}"
                image.write_bytes(iso_image(files, raw))
                with DiscImage(image) as disc:
                    self.assertEqual(disc.raw, raw)
                    self.assertEqual(sorted(e.path for e in disc.entries() if not e.directory), sorted(files))
                    extracted = disc.extract(self.temp / f"out{raw}")
                    self.assertEqual(len(extracted), len(files))
                self.assertEqual(sources.identify(image).status, "supported")
            cue = self.temp / "disc ü.cue"
            cue.write_text('FILE "disc raw ü.bin" BINARY\n  TRACK 01 MODE2/2352\n', encoding="utf-8")
            self.assertEqual(sources.identify(cue).status, "supported")
            broken_cue = self.temp / "broken.cue"
            broken_cue.write_text('FILE "unterminated.bin BINARY\n', encoding="utf-8")
            with self.assertRaisesRegex(sources.SourceError, "unterminated"):
                sources.identify(broken_cue)
            (self.temp / "escape.cue").write_text('FILE "../outside.bin" BINARY\n', encoding="utf-8")
            with self.assertRaises(sources.SourceError):
                sources.identify(self.temp / "escape.cue")
            (self.temp / "tiny.iso").write_bytes(bytes(4096))
            with self.assertRaises(sources.SourceError):
                sources.identify(self.temp / "tiny.iso")
            evil = iso_image({"../evil": b"x"})
            (self.temp / "evil.iso").write_bytes(evil)
            with DiscImage(self.temp / "evil.iso") as disc:
                with self.assertRaises(IsoError):
                    disc.entries()
        finally:
            sources.PS1_EXECUTABLES.clear()
            sources.PS1_EXECUTABLES.update(original)


def _egl_available() -> bool:
    try:
        import imgui_bundle  # noqa: F401
        from OpenGL import EGL  # noqa: F401
    except Exception:
        return False
    return os.name == "posix"


@unittest.skipIf(np is None or not _egl_available(), "needs imgui-bundle, PyOpenGL and EGL (off-screen UI)")
class OffscreenWorkflowTests(unittest.TestCase):
    def test_wizard_blank_map_placement_and_windows(self):
        os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
        from tools.btga_studio.core.settings import Settings
        from tools.btga_studio.gui.app import StudioApp
        from tools.btga_studio.gui.offscreen import OffscreenUI

        with tempfile.TemporaryDirectory() as temp:
            os.environ["BTGA_STUDIO_HOME"] = temp
            app = StudioApp(Settings.open())
            ui = OffscreenUI(app, 1280, 800)
            try:
                app.show_new_project = True
                ui.frame()
                app.wizard.update({"title": "Wizard project", "key": "wizard_map", "map_title": "Wizard map", "size": 1024.0, "map_kind": 0})
                app.controller.new_studio_project("Wizard project")
                self.assertTrue(app.create_map_from_wizard())
                app.show_new_project = False
                for window in ("show_sources", "show_definitions", "show_controls", "show_new_map"):
                    setattr(app, window, True)
                    ui.frame()
                    setattr(app, window, False)
                cube = app.controller.library.starter()["models"]["cube"]
                app.try_placement("model", cube)
                app.controller.viewport = (900, 560)
                new = app.controller.place_at(450, 280)
                self.assertIsNotNone(new)
                for _ in range(3):
                    ui.frame()
                self.assertGreater(app.renderer.draw_calls, 0)
                self.assertTrue(app.controller.dirty)
            finally:
                ui.close()


@unittest.skipIf(np is None or not _egl_available(), "needs PyOpenGL and EGL (off-screen rendering)")
class OverheadMinimapTests(unittest.TestCase):
    def test_overview_orientation_matches_game_minimaps(self):
        os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
        from tools.btga_studio.core import geometry as g
        from tools.btga_studio.core.project import StudioProject
        from tools.btga_studio.gui.headless import create_context
        from tools.btga_studio.gui.renderer import ViewportRenderer

        create_context()
        library = starter_library()
        project = StudioProject("Orientation")
        project.add_blank_map("probe", "Probe", 1024)
        document = project.open_map("probe", library)
        document.place_model(library.starter()["models"]["building_small"], (600.0, 0.0, -600.0))
        renderer = ViewportRenderer(library)
        renderer.load_scene(document.scene)
        renderer.layers = {k: False for k in renderer.layers} | {"geometry": True}
        renderer.background = (0.0, 0.0, 0.0)
        texture, camera = renderer.render_top_down(64)
        renderer.use_target("minimap")
        rows = renderer.read_pixels()
        renderer.use_target("main")
        lit = [(i % 64, i // 64) for i in range(64 * 64) if sum(rows[4 * i : 4 * i + 3]) > 60]
        self.assertTrue(lit)
        mean_x = sum(p[0] for p in lit) / len(lit)
        mean_y = sum(p[1] for p in lit) / len(lit)
        expected = camera.to_pixel(600.0, -600.0, 64, 64)
        self.assertLess(abs(mean_x - expected[0]), 3)
        self.assertLess(abs(mean_y - expected[1]), 3)
        self.assertGreater(mean_x, 32)  # +x is right
        self.assertLess(mean_y, 32)  # -z is up, as in the games' minimaps
        self.assertEqual(g.TopDownCamera((-1, 1), (-1, 1)).to_pixel(-1, -1, 10, 10), (0.0, 0.0))


# ------------------------------------------------------------- real sources --

@unittest.skipUnless(os.environ.get("BTGA_BT1_ROM") and os.environ.get("BTGA_STUDIO_BUILD_TESTS"),
                     "set BTGA_BT1_ROM and BTGA_STUDIO_BUILD_TESTS=1 to register and build a real BattleTanx catalog")
class RealSourceBuildTests(unittest.TestCase):
    def test_register_build_and_import_battletanx(self):
        from tools.btga_studio.core.controller import EditorController
        from tools.btga_studio.core.package import build_package, read_package
        from tools.btga_studio.core.settings import Settings

        with tempfile.TemporaryDirectory(prefix="studio real ü ") as temp:
            os.environ["BTGA_STUDIO_HOME"] = temp
            c = EditorController(Settings.open())
            source = c.add_source(Path(os.environ["BTGA_BT1_ROM"]))
            self.assertEqual(source.status, "supported")
            job = c.build_catalog(source.id, wait=True)
            self.assertIsNone(job.error)
            first = (Path(temp) / "catalogs" / source.catalog / "catalog.json").read_bytes()
            c.build_catalog(source.id, wait=True)
            self.assertEqual((Path(temp) / "catalogs" / c.registry.sources[source.id].catalog / "catalog.json").read_bytes(), first)
            level = next(l for l in c.library.levels() if l.platform == "n64_bt1")
            c.new_studio_project("Real", "n64_bt1")
            c.add_imported_map("imported", level.id)
            template = next(t for t in c.library.starter()["templates"] if t["source_kind"] == "studio_pickup")
            c.document.place_marker(template, (0.0, 0.0, 0.0))
            package = read_package(build_package(c.project, c.library))
            self.assertEqual(package.manifest["sources"][0]["rom_sha1"], "535860d941738ac1210c20a9b80114fea0e0ff17")
            text = json.dumps(package.maps)
            self.assertNotIn("geometry_offset", text)  # source geometry is referenced, not embedded
            c.shutdown()


@unittest.skipUnless(os.environ.get("BTGA_PS1_IMAGE"), "set BTGA_PS1_IMAGE to the PS1 CUE or BIN to identify a real disc image")
class RealPS1ImageTests(unittest.TestCase):
    def test_identify_real_disc_image(self):
        from tools.btga_studio.core import sources

        found = sources.identify(Path(os.environ["BTGA_PS1_IMAGE"]))
        self.assertEqual((found.platform, found.status), ("ps1", "supported"))


if __name__ == "__main__":
    unittest.main()
