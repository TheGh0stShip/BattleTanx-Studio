"""Editor controller: everything the GUI does, without the GUI.

The ImGui layer reads state from here and forwards ``Command`` objects from
gui.input; tests drive the same object headlessly.  The controller owns:

* the library set: the ROM-free starter catalog, catalogs built from the
  user's registered sources, and any catalog opened for this session;
* the open project (``StudioProject``) and its active map document;
* placement, selection (click, toggle, box), clipboard, view state,
  gizmo transactions, background diagnostics, autosave and recovery;
* the Source Manager and every long operation as background jobs
  (``core.jobs``) with progress, cancellation and logged failures.
"""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import applog
from . import diagnostics as diag
from . import geometry as g
from . import project as projects
from .browser import AssetIndex
from .atomic import atomic_write_bytes
from .document import EditError, EditorDocument, is_read_only
from .jobs import JobRunner
from .library import LibrarySet
from .scene import MeshCache, record_box
from .settings import Settings

LAYER_FAMILIES = {"geometry": "instances", "collision": "collision", "markers": "markers"}


@dataclass
class PlacementMode:
    kind: str  # "model" | "marker"
    id: str  # model id or template marker id
    template: dict | None = None
    yaw: int = 0


class EditorController:
    def __init__(self, settings: Settings, log=None, use_registry: bool = True, starter: bool = True, log_to_disk: bool = True):
        self.settings = settings
        self.preferences = settings.preferences
        self.library: LibrarySet | None = None
        self.catalog_path: Path | None = None
        self.extra_catalogs: list[Path] = []
        self.index: AssetIndex | None = None
        self.mesh_cache: MeshCache | None = None
        self.project: projects.StudioProject | None = None
        self.active_map: str | None = None
        self.camera = g.Camera(speed=self.preferences.camera_speed, look_sensitivity=self.preferences.look_sensitivity)
        self.viewport = (1280, 720)
        self.placement: PlacementMode | None = None
        self.preview: dict | None = None
        self.hover_point = None
        self.clipboard: str | None = None
        self.messages: list[tuple[str, str]] = []
        self.diagnostics: list[diag.Diagnostic] = []
        self.diagnostics_version = -1
        self._diag_pool = ThreadPoolExecutor(max_workers=1)
        self._diag_future = None
        self._diag_due = None
        self.autosaver = projects.Autosaver(settings.recovery_directory, self.preferences.autosave_enabled, self.preferences.autosave_seconds)
        self.log_sink = log
        self.timings: dict[str, float] = {}
        self.registry = None
        self.logger = applog.open_log(settings.logs_directory) if log_to_disk else None
        self.jobs = JobRunner(workers=2, max_pending=16, log=self._job_logged)
        self._starter_enabled = starter
        from .imports import UserLibrary

        self.user_library = UserLibrary(settings.local_directory / "user-library")
        if use_registry:
            from .sources import SourceRegistry

            self.registry = SourceRegistry(settings.directory, settings.local_directory)
            for problem in self.registry.problems:
                self.log("warning", problem)
        self.history_listener = None  # the GUI restores editor context after a keyboard undo/redo
        self._gizmo_start = None
        self._gizmo_duplicated = False
        self._gizmo_selection: list[str] = []
        self.refresh_library()

    # -- messages ---------------------------------------------------------------
    def log(self, level: str, text: str) -> None:
        applog.write(getattr(self, "logger", None), level, "message", text)
        self.messages.append((level, text))
        del self.messages[:-500]
        if self.log_sink:
            self.log_sink(level, text)

    # -- library ----------------------------------------------------------------
    def refresh_library(self) -> None:
        """Rebuild the library set from the starter catalog, registered sources and session catalogs."""

        from tools.btga_assets.studio import StudioLibrary

        start = time.perf_counter()
        roots = []
        if self._starter_enabled:
            from tools.btga_studio.content.starter import ensure_starter_catalog

            roots.append(ensure_starter_catalog(self.settings.starter_directory))
        if self.user_library.exists():
            roots.append(self.user_library.root)
        if self.registry is not None:
            roots += self.registry.catalog_roots()
        roots += [r for r in self.extra_catalogs if r not in roots]
        libraries = []
        for root in roots:
            try:
                libraries.append(StudioLibrary.open(root))
            except Exception as error:  # a broken catalog must not stop the editor
                self.log("error", f"catalog {Path(root).name} could not be opened: {error}")
        self.library = LibrarySet(libraries) if libraries else None
        self.index = AssetIndex(self.library) if self.library else None
        if self.index is not None:
            self.index.add_templates((self.library.starter() or {}).get("templates", []))
        self.mesh_cache = MeshCache(self.library) if self.library else None
        if self.project is not None:
            for entry in self.project.maps.values():
                if entry.document is not None:
                    entry.document.scene.library = self.library
                    entry.document.scene.meshes = self.mesh_cache
        self.timings["library"] = time.perf_counter() - start

    def open_catalog(self, path: Path) -> None:
        """Add a catalog for this session (for example one exported by export_neutral_assets.py)."""

        path = Path(path)
        if not (path / "catalog.json").is_file():
            raise EditError(f"{path.name} is not a catalog folder")
        start = time.perf_counter()
        if path not in self.extra_catalogs:
            self.extra_catalogs.append(path)
        self.catalog_path = path
        self.refresh_library()
        self.preferences.last_catalog = str(path)
        self.timings["catalog_open"] = time.perf_counter() - start
        self.log("info", f"catalog opened: {len(self.library.levels())} maps, {len(self.library.models())} models")

    # -- imported models and textures ------------------------------------------------------
    def read_import(self, path: Path, options) -> "ImportResult":
        """Read a model or picture for preview; nothing is stored (raises ImportProblem)."""

        from .imports import read_file

        start = time.perf_counter()
        result = read_file(Path(path), options)
        self.timings["import_read"] = time.perf_counter() - start
        return result

    def add_import(self, result) -> list[str]:
        """Store a previewed import in the user's library and reload the library."""

        added = self.user_library.add(result)
        self.refresh_library()
        self.log("info", f"imported {result.file}: {result.summary()}" + ("" if added else " (already in your library)"))
        for warning in result.warnings:
            self.log("warning", warning)
        return added

    def remove_imports(self, identifiers: list[str]) -> list[str]:
        removed = self.user_library.remove(identifiers)
        self.refresh_library()
        self.log("info", f"removed {len(removed)} imported item(s) from your library")
        return removed

    def import_package_resources(self, path: Path) -> list[str]:
        """Restore the imported models and textures a content package carries."""

        added = self.user_library.add_package(Path(path).read_bytes())
        self.refresh_library()
        self.log("info", f"{Path(path).name}: {len(added)} imported item(s) added to your library")
        return added

    def build_city_async(self, map_file: Path, options, elevation: Path | None = None, heights: Path | None = None, on_done=None):
        """Build a city from OpenStreetMap data in the background, store its models, then place it on a new map.

        The job result is the ``CityPlan``; placing happens on the UI thread in
        ``place_city`` so the map edit is one undoable step.
        """

        from . import geo, osm

        def work(context):
            context.progress("reading map data")
            data = osm.read_osm(Path(map_file))
            terrain = geo.Terrain(Path(elevation)) if elevation else None
            if heights:
                options.heights = {**osm.heights_from_wikidata(json.loads(Path(heights).read_text(encoding="utf-8"))), **options.heights}
            plan = osm.build_city(data, options, terrain, lambda stage: context.progress(f"building {stage}"))
            context.progress("saving models to your library")
            self.user_library.add(plan.result)
            return plan

        def done(job):
            if job.state == "done":
                self.refresh_library()
                self.place_city(job.result, options.title)
            if on_done:
                on_done(job)

        return self.jobs.submit(f"Building {options.title} from {Path(map_file).name}", work, "city", done)

    def place_city(self, plan, title: str, key: str | None = None) -> str:
        """Add a blank map sized to a city plan and place the city on it (one undoable step)."""

        from . import osm
        from .hub import slug

        if self.project is None:
            self.new_studio_project(title)
        key = key or slug(title)
        base = key
        number = 2
        while key in self.project.maps:
            key = f"{base}_{number}"
            number += 1
        self.project.add_blank_map(key, title, plan.half_extent)
        self.project.set_map_settings(key, None, (182, 196, 210))
        self.switch_map(key)
        osm.apply_plan(self.document, plan)
        self.document.selection = []
        description = self.project.project.get("description") or ""
        if plan.credit not in description:
            self.project.project["description"] = (description + "\n" if description else "") + plan.credit
        counts = plan.counts
        self.log("info", f"{title}: {counts['buildings']:,} buildings, {counts['bridges']} bridges, {counts['skyways']} skyway segments, "
                         f"{counts['trees']:,} trees, {counts['collision']:,} collision boxes")
        for warning in plan.result.warnings:
            self.log("warning", warning)
        self.frame_map()
        return key

    # -- projects and maps ---------------------------------------------------------------
    @property
    def document(self) -> EditorDocument | None:
        if self.project is None or self.active_map is None:
            return None
        entry = self.project.maps.get(self.active_map)
        return entry.document if entry else None

    def new_studio_project(self, title: str, target: str = "studio") -> projects.StudioProject:
        self.project = projects.StudioProject(title, target)
        self.active_map = None
        self.log("info", f"new project {title!r}")
        return self.project

    def add_blank_map(self, key: str, title: str, half_extent: float = 2048.0) -> None:
        if self.project is None:
            self.new_studio_project(title)
        self.project.add_blank_map(key, title, half_extent)
        self.switch_map(key)

    def add_imported_map(self, key: str, scene_id: str, title: str | None = None) -> None:
        platform = scene_id.split(":", 1)[0]
        if self.project is None:
            self.new_studio_project(title or self.library._levels[scene_id].get("name") or scene_id,
                                    platform if platform in projects.TARGET_GAMES else "studio")
        self.project.add_imported_map(key, self.library, scene_id, title)
        self.switch_map(key)

    def switch_map(self, key: str) -> None:
        start = time.perf_counter()
        if self.document is not None and self.document.transforming:
            self.document.cancel_transform()
        document = self.project.open_map(key, self.library, self.mesh_cache)
        self.active_map = key
        self.placement = None
        self.preview = None
        self.index.set_map(document.scene)
        self.frame_map()
        self.request_diagnostics(immediate=True)
        self.timings["map_open"] = time.perf_counter() - start

    def close_map(self) -> None:
        if self.project is not None and self.active_map is not None:
            self.project.close_map(self.active_map)
        self.active_map = None
        self.placement = None

    def new_project(self, level_id: str) -> None:
        """One step: a new project with one imported map."""

        self.project = None
        self.add_imported_map("main", level_id)

    def open_project(self, path: Path) -> None:
        project = projects.load_project(Path(path))
        self.project = project
        self.active_map = None
        self.preferences.add_recent(str(path))
        for problem in project.problems(self.library):
            self.log("warning", f"{problem['target']} ({problem['field']}): {problem['message']}")
        for key in sorted(project.maps):
            try:
                self.switch_map(key)
                break
            except projects.ProjectMismatch as error:
                self.log("error", str(error))
        self.log("info", f"project opened: {Path(path).name}")

    def save(self, path: Path | None = None) -> Path:
        if self.project is None:
            raise EditError("no open project")
        target = Path(path) if path else self.project.path
        if target is None:
            raise EditError("choose a file name (Save As)")
        self.project.compute_dependencies(self.library)
        projects.save_project(target, self.project)
        self.preferences.add_recent(str(target))
        self.autosaver.discard(self.project)
        self.log("info", f"saved {target.name}")
        return target

    @property
    def project_path(self) -> Path | None:
        return self.project.path if self.project else None

    @property
    def dirty(self) -> bool:
        return self.project is not None and self.project.dirty

    def recoveries(self) -> list[dict]:
        return self.autosaver.recoveries()

    def restore_recovery(self, path: Path) -> None:
        project = projects.load_project(Path(path))
        project.saved_text = None  # recovered work stays unsaved until the user saves it
        project.path = None
        self.project = project
        self.active_map = None
        for key in sorted(project.maps):
            try:
                self.switch_map(key)
                break
            except projects.ProjectMismatch as error:
                self.log("error", str(error))
        self.log("info", "recovered unsaved work; choose Save As to keep it")

    # -- camera ------------------------------------------------------------------
    def frame_map(self) -> None:
        if self.document:
            self.camera.frame(*self.document.scene.world_bounds())
            self.preferences.camera_speed = self.camera.speed

    def frame_selection(self) -> None:
        document = self.document
        if not document or not document.selection:
            return
        boxes = [document.scene.record_world_box(i) for i in document.selection if document.scene.record(i)]
        if boxes:
            lo = np.min([b[0] for b in boxes], axis=0)
            hi = np.max([b[1] for b in boxes], axis=0)
            pad = np.array([64.0, 64.0, 64.0])
            self.camera.frame(lo - pad, hi + pad)

    def frame_record(self, record_id: str) -> None:
        if self.document and self.document.scene.record(record_id):
            self.document.hidden.discard(record_id)
            self.document.select(record_id)
            self.frame_selection()

    # -- picking and selection ----------------------------------------------------------
    def pick_layers(self) -> set[str]:
        layers = self.preferences.layers
        return {family for layer, family in LAYER_FAMILIES.items() if layers.get(layer, True)}

    def ray(self, x: float, y: float) -> g.Ray:
        return g.screen_ray(self.camera, x, y, *self.viewport)

    def _pickable(self, record_id: str) -> bool:
        document = self.document
        if record_id in document.hidden or record_id in document.locked:
            return False
        if not self.preferences.layers.get("unresolved", True) and is_read_only(document.scene.record(record_id)[1]):
            return False
        return True

    def pick(self, x: float, y: float):
        start = time.perf_counter()
        hits = [h for h in self.document.scene.pick(self.ray(x, y), self.pick_layers(), limit=16) if self._pickable(h.id)]
        self.timings["pick"] = time.perf_counter() - start
        return hits

    def click(self, x: float, y: float, toggle: bool = False) -> None:
        if self.document is None:
            return
        if self.placement is not None:
            self.place_at(x, y)
            return
        hits = self.pick(x, y)
        self.document.select(hits[0].id if hits else None, toggle)

    def box_select(self, rect, add: bool = False) -> list[str]:
        """Select every visible, unlocked record whose centre projects inside the screen rectangle."""

        document = self.document
        if document is None:
            return []
        x0, x1 = sorted((rect[0], rect[2]))
        y0, y1 = sorted((rect[1], rect[3]))
        families = self.pick_layers()
        chosen = []
        for family, record in document.scene.records():
            if family not in families or not self._pickable(record["id"]):
                continue
            lo, hi = document.scene.record_world_box(record["id"])
            projected = g.project(self.camera, (lo + hi) / 2, *self.viewport)
            if projected and x0 <= projected[0] <= x1 and y0 <= projected[1] <= y1:
                chosen.append(record["id"])
        document.selection = sorted(set(document.selection) | set(chosen)) if add else chosen
        return chosen

    def select_all(self) -> None:
        if self.document:
            families = self.pick_layers()
            self.document.selection = [r["id"] for f, r in self.document.scene.records() if f in families and self._pickable(r["id"])]

    # -- placement ------------------------------------------------------------------------
    def template(self, template_id: str) -> dict | None:
        entry = self.document.scene.record(template_id) if self.document else None
        if entry is not None:
            return entry[1]
        return self.index.templates.get(template_id) if self.index else None

    def begin_placement(self, kind: str, resource_id: str) -> None:
        if self.document is None:
            raise EditError("open a map first")
        template = None
        if kind == "marker":
            template = self.template(resource_id)
            if template is None:
                raise EditError("template not available in this map or the starter set")
            reason = is_read_only(template)
            if reason:
                raise EditError(f"cannot place this record: {reason}")
        elif resource_id not in self.library._models:
            raise EditError("model not in any loaded catalog")
        self.placement = PlacementMode(kind, resource_id, template)

    def cancel(self) -> None:
        if self.document and self.document.transforming:
            self.document.cancel_transform()
            if self._gizmo_duplicated:  # a cancelled Shift-drag must not leave the stacked copy behind
                self._drop_gizmo_duplicate()
        self._gizmo_duplicated = False
        self.placement = None
        self.preview = None

    def placement_point(self, x: float, y: float):
        exclude = set(self.document.selection)
        found = self.document.scene.surface_point(self.ray(x, y), exclude) if self.preferences.surface_snap else None
        if found is None:
            point = g.ray_plane_y(self.ray(x, y), 0.0)
            found = (point, None) if point is not None else None
        if found is None:
            return None
        point, _ = found
        grid = self.preferences.grid_snap
        return np.array([g.snap(point[0], grid), point[1], g.snap(point[2], grid)])

    def hover(self, x: float, y: float) -> None:
        if self.document is None:
            self.preview = None
            return
        self.hover_point = self.placement_point(x, y)
        if self.placement is None:
            self.preview = None
            return
        point = self.hover_point
        if point is None:
            self.preview = None
            return
        if self.placement.kind == "model":
            mesh = self.document.scene.meshes.get(self.placement.id)
            lo, hi = mesh.lo, mesh.hi
        else:
            lo, hi = record_box(self.placement.template, "markers", None)
        bounds = self.document.scene.bounds
        inside = not bounds or (bounds["x"][0] <= point[0] <= bounds["x"][1] and bounds["z"][0] <= point[2] <= bounds["z"][1])
        self.preview = {"translation": [float(v) for v in point], "yaw": self.placement.yaw, "lo": list(map(float, lo)),
                        "hi": list(map(float, hi)), "valid": inside}

    def place_at(self, x: float, y: float) -> str | None:
        point = self.placement_point(x, y)
        if point is None:
            self.log("warning", "no surface under the cursor")
            return None
        yaw = g.snap_yaw(self.placement.yaw, self.preferences.yaw_snap_degrees)
        try:
            if self.placement.kind == "model":
                new_id = self.document.place_model(self.placement.id, point, yaw)
            else:
                new_id = self.document.place_marker(self.placement.template, point, yaw)
        except EditError as error:
            self.log("error", f"placement refused: {error}")
            return None
        if self.document.family(new_id) == "markers" and not self._collision_known():
            self.log("warning", "collision on this map is planar or unknown; the placed object has no proven collision height")
        self.request_diagnostics()
        return new_id

    def _collision_known(self) -> bool:
        return any(r.get("vertical", {}).get("status") == "proven" for _, r in self.document.scene.records("collision"))

    # -- edits ----------------------------------------------------------------------
    def run(self, label: str, action, *args, **kwargs):
        if self.document is None:
            return None
        start = time.perf_counter()
        try:
            result = action(*args, **kwargs)
        except EditError as error:
            self.log("error", f"{label}: {error}")
            return None
        self.timings[label] = time.perf_counter() - start
        self.request_diagnostics()
        return result

    # One Undo command for map objects and gameplay definitions: each transaction carries a number from the
    # shared sequence (core.history), so undo reverses the most recent action and redo the most recently undone.
    def _definitions(self):
        return self.project.definitions if self.project is not None else None

    @property
    def can_undo(self) -> bool:
        defs = self._definitions()
        return bool((self.document and self.document.undo_stack) or (defs and defs.undo_stack))

    @property
    def can_redo(self) -> bool:
        defs = self._definitions()
        return bool((self.document and self.document.redo_stack) or (defs and defs.redo_stack))

    def undo(self):
        doc, defs = self.document, self._definitions()
        doc_seq = doc.undo_stack[-1].sequence if doc and doc.undo_stack and not doc.transforming else None
        def_seq = defs.undo_sequence if defs else None
        if def_seq is not None and (doc_seq is None or def_seq > doc_seq):
            entry = defs.undo()
            self.request_diagnostics()
            return entry
        return self.run("undo", doc.undo) if doc else None

    def redo(self):
        doc, defs = self.document, self._definitions()
        doc_seq = doc.redo_stack[-1].sequence if doc and doc.redo_stack else None
        def_seq = defs.redo_sequence if defs else None
        if def_seq is not None and (doc_seq is None or def_seq < doc_seq):
            entry = defs.redo()
            self.request_diagnostics()
            return entry
        return self.run("redo", doc.redo) if doc else None

    def duplicate(self):
        return self.run("duplicate", self.document.duplicate) if self.document and self.document.selection else None

    def delete(self):
        return self.run("delete", self.document.delete) if self.document and self.document.selection else None

    def copy(self) -> str | None:
        if not self.document or not self.document.selection:
            return None
        try:
            self.clipboard = self.document.copy()
        except EditError as error:
            self.log("error", f"copy: {error}")
            return None
        self.log("info", f"copied {len(self.document.selection)} object(s)")
        return self.clipboard

    def paste(self, text: str | None = None):
        text = text or self.clipboard
        if not text or not self.document:
            return None
        return self.run("paste", self.document.paste, text, self.hover_point)

    # -- gizmo --------------------------------------------------------------------------
    def gizmo_matrix(self) -> np.ndarray | None:
        document = self.document
        if not document or not document.selection:
            return None
        first = document.record(document.selection[0])
        pivot = np.mean([document.record(i)["translation"] for i in document.selection], axis=0)
        single = len(document.selection) == 1
        return g.model_matrix(pivot, first["yaw"] if single else 0, first.get("scale") if single else None)

    def gizmo_begin(self, duplicate: bool = False) -> bool:
        self._gizmo_duplicated = False
        self._gizmo_selection = list(self.document.selection)
        try:
            if duplicate:
                self.document.duplicate(offset=(0.0, 0.0, 0.0))
                self._gizmo_duplicated = True
            self.document.begin_transform()
        except EditError as error:
            if self._gizmo_duplicated:
                self._drop_gizmo_duplicate()
                self._gizmo_duplicated = False
            self.log("error", f"transform: {error}")
            return False
        self._gizmo_start = self.gizmo_matrix()
        return True

    def _drop_gizmo_duplicate(self) -> None:
        self.document.undo()
        self.document.selection = [i for i in self._gizmo_selection if self.document.scene.record(i)]

    def gizmo_update(self, matrix: np.ndarray, scale_mode: bool = False) -> None:
        """Feed the manipulated matrix: translation, yaw and (scale mode) scale relative to the drag start."""

        start = self._gizmo_start
        if start is None or not self.document.transforming:
            return  # Esc cancelled the drag; ignore the rest of this gizmo interaction
        delta = matrix[:3, 3] - start[:3, 3]
        yaw0 = math.atan2(start[0, 2], start[0, 0])
        yaw1 = math.atan2(matrix[0, 2], matrix[0, 0])
        yaw_delta = g.yaw_units(yaw1 - yaw0)
        if self.preferences.yaw_snap_degrees:
            yaw_delta = g.snap_yaw(yaw_delta, self.preferences.yaw_snap_degrees)
        if self.preferences.grid_snap:
            delta = np.array([g.snap(delta[0], self.preferences.grid_snap), delta[1], g.snap(delta[2], self.preferences.grid_snap)])
        scale = None
        if scale_mode:
            before = np.linalg.norm(start[:3, :3], axis=0)
            after = np.linalg.norm(matrix[:3, :3], axis=0)
            scale = after / np.where(before == 0, 1, before)
        try:
            self.document.update_transform(delta, yaw_delta, scale)
        except EditError as error:
            self.log("warning", str(error))

    def gizmo_end(self) -> None:
        if not self.document.transforming:  # cancelled with Esc while the gizmo was still held
            self._gizmo_duplicated = False
            return
        try:
            committed = self.document.commit_transform()
        except EditError as error:
            committed = None
            self.log("error", f"transform refused: {error}")
        if committed is None and self._gizmo_duplicated:
            self._drop_gizmo_duplicate()  # refused or zero-length Shift-drag
        self._gizmo_duplicated = False
        self.request_diagnostics()

    # -- commands -------------------------------------------------------------------------
    def execute(self, command) -> None:
        name, args = command.name, command.args
        document = self.document
        if name == "look":
            dy = -args["dy"] if self.preferences.invert_look_y else args["dy"]
            self.camera.look_sensitivity = self.preferences.look_sensitivity
            self.camera.look(args["dx"], dy)
        elif name == "move":
            self.camera.move(args["dt"], args["forward"], args["strafe"], args["vertical"], args["fast"])
        elif name == "speed":
            self.camera.adjust_speed(args["steps"])
            self.preferences.camera_speed = self.camera.speed
        elif name == "frame_selection":
            self.frame_selection()
        elif name == "frame_map":
            self.frame_map()
        elif name == "cancel":
            self.cancel()
        elif name == "click":
            self.click(args["x"], args["y"], args.get("toggle", False))
        elif name == "box_select":
            self.box_select(args["rect"], args.get("add", False))
        elif name in ("undo", "redo", "duplicate", "delete", "copy", "paste", "select_all"):
            result = getattr(self, name)()
            if name in ("undo", "redo") and self.history_listener is not None:
                self.history_listener(result)
        elif name in ("hide", "isolate", "lock", "unhide_all") and document is not None:
            getattr(document, name)()
        elif name == "save" and self.project_path:
            self.save()

    # -- diagnostics -------------------------------------------------------------------------
    def request_diagnostics(self, immediate: bool = False) -> None:
        self._diag_due = time.monotonic() + (0.0 if immediate else 0.4)

    def poll_diagnostics(self, wait: bool = False) -> bool:
        document = self.document
        changed = False
        if self._diag_future is not None and (wait or self._diag_future.done()):
            version, result = self._diag_future.result()
            self._diag_future = None
            if document is not None and version == document.scene.version:
                self.diagnostics = result + self.project_diagnostics()
                self.diagnostics_version = version
                changed = True
            else:
                self._diag_due = time.monotonic()
        if document is not None and self._diag_future is None and self._diag_due is not None and time.monotonic() >= self._diag_due:
            self._diag_due = None
            version = document.scene.version
            snapshot = _Snapshot(document)
            library = self.library
            self._diag_future = self._diag_pool.submit(lambda: (version, diag.collect(snapshot, library)))
            if wait:
                return self.poll_diagnostics(wait=True) or changed
        return changed

    def project_diagnostics(self) -> list[diag.Diagnostic]:
        if self.project is None:
            return []
        return [diag.Diagnostic("error", p["code"], p["message"], p["target"], p["field"]) for p in self.project.problems(self.library)]

    # -- sources --------------------------------------------------------------------------
    def identify_source(self, path: Path, on_done=None):
        """Identify a file or folder in the background without registering it (result: ``Source``)."""

        from .sources import identify

        def work(context):
            return identify(Path(path), lambda stage, done, total: context.progress(stage, done, total))

        return self.jobs.submit(f"Checking {Path(path).name}", work, "identify", on_done)

    def add_source(self, path: Path):
        """Register synchronously (developer and test use; the GUI uses ``add_source_async``)."""

        from .sources import SourceError

        try:
            source = self.registry.register(Path(path))
        except SourceError as error:
            self.log("error", str(error))
            return None
        self.log("info", f"registered {source.title} ({source.status})")
        return source

    def add_source_async(self, path: Path, on_done=None):
        def work(context):
            return self.registry.register(Path(path), lambda stage, done, total: context.progress(stage, done, total))

        def done(job):
            if job.state == "done":
                self.log("info", f"registered {job.result.title}")
            if on_done:
                on_done(job)

        return self.jobs.submit(f"Registering {Path(path).name}", work, "register", done)

    def verify_sources(self, on_done=None):
        def work(context):
            ids = sorted(self.registry.sources)
            for number, source_id in enumerate(ids):
                context.progress("checking", number, len(ids))
                self.registry.verify(source_id)
            return len(ids)

        return self.jobs.submit("Checking registered sources", work, "verify", on_done)

    def search_sources(self, folder: Path, on_done=None):
        """Look for missing sources under ``folder`` by content and relink those found (result: number relinked)."""

        def work(context):
            context.progress("searching", 0, None)
            found = self.registry.search(Path(folder))
            for source_id, path in found.items():
                context.check()
                self.registry.relocate(source_id, path)
            return len(found)

        return self.jobs.submit(f"Searching {Path(folder).name}", work, "search", on_done)

    def relink_source(self, source_id: str, path: Path, on_done=None):
        def work(context):
            context.progress("checking", 0, None)
            return self.registry.relocate(source_id, Path(path))

        return self.jobs.submit("Relinking source", work, "relink", on_done)

    def remove_source(self, source_id: str, delete_catalog: bool = True) -> None:
        """Forget a registration (the user's file or folder is never touched)."""

        title = self.registry.sources[source_id].title
        self.registry.remove(source_id, delete_catalog)
        self.refresh_library()
        self.log("info", f"removed {title} from Studio; your file was not changed")

    def build_catalog(self, source_id: str, wait: bool = False, on_done=None):
        """Build (or rebuild) a source's catalog in the background; a failure keeps the previous catalog."""

        def work(context):
            return self.registry.build_catalog(source_id, lambda stage, done, total: context.progress(stage, done, total),
                                               context.cancel_event)

        def done(job):
            if job.state == "done":
                self.log("info", f"catalog ready for {self.registry.sources[source_id].title}")
                self.refresh_library()
            elif job.state == "cancelled":
                self.log("warning", "catalog build cancelled; the previous catalog (if any) is unchanged")
            if on_done:
                on_done(job)

        job = self.jobs.submit(f"Building catalog for {self.registry.sources[source_id].title}", work, "catalog", done)
        job.source_id = source_id
        if wait:
            self.jobs.wait(timeout=24 * 3600)
        return job

    @property
    def builds(self) -> dict:
        return {getattr(job, "source_id", None): job for job in self.jobs.find("catalog")}

    def export_package(self, path: Path, studio_version: str = "0", on_done=None):
        """Validate and write a content package in the background (atomic; result: path)."""

        from .package import build_package

        project, library = self.project, self.library
        target = Path(path)

        def work(context):
            context.progress("validating", 0, 2)
            data = build_package(project, library, studio_version)
            context.progress("writing", 1, 2)
            atomic_write_bytes(target, data)
            return target

        return self.jobs.submit(f"Exporting {target.name}", work, "export", on_done)

    def _job_logged(self, job) -> None:
        if job.state == "failed":
            self.log("error", f"{job.label} failed: {job.error}")
            applog.write(self.logger, "error", "job_failed", job.label, kind=job.kind, detail=job.detail or "")
        else:
            applog.write(self.logger, "info", "job_" + job.state, job.label, kind=job.kind)

    def tick(self) -> None:
        self.autosaver.enabled = self.preferences.autosave_enabled
        self.autosaver.interval = self.preferences.autosave_seconds
        if self.project is not None:
            try:
                path = self.autosaver.tick(self.project)
            except Exception as error:
                self.log("error", f"autosave failed: {error}")
                path = None
            if path:
                self.log("info", "recovery copy written")
        self.jobs.poll()
        self.poll_diagnostics()

    def shutdown(self) -> None:
        self.jobs.shutdown()
        self._diag_pool.shutdown(wait=False, cancel_futures=True)
        applog.close(self.logger)


class _Snapshot:
    """An immutable view of a document for background validation."""

    def __init__(self, document: EditorDocument):
        from tools.btga_assets import overlay as overlay_module

        self.overlay = overlay_module.Overlay.from_json(document.overlay.to_json())
        self.scene = _SceneSnapshot(document.scene)


class _SceneSnapshot:
    def __init__(self, scene):
        self.base = scene.base
        self._view = {k: (f, r) for k, (f, r) in scene.view.items()}
        self._order = list(scene.order)
        self.navigation = scene.navigation

    def records(self, family=None):
        for record_id in self._order:
            entry = self._view.get(record_id)
            if entry and (family is None or entry[0] == family):
                yield entry

    def record(self, record_id):
        return self._view.get(record_id)
