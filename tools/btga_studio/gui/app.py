"""BattleTanx Studio desktop editor (Dear ImGui via imgui-bundle, OpenGL 3.3).

Layout: menu bar; left = project maps and definitions, layers and outline;
centre = 3D viewport with ImGuizmo; right = inspector and minimap;
bottom = asset browser and output/validation.  Floating windows: New
Project and New Map wizards, Project definitions, Source Manager, Controls,
and the recovery prompt.  All behaviour lives in core.controller; this
module draws and forwards input.
"""

from __future__ import annotations

import argparse
import math
import sys
import traceback
from pathlib import Path

import numpy as np
from imgui_bundle import hello_imgui, imgui, immapp, imguizmo
from imgui_bundle import portable_file_dialogs as pfd

from tools.btga_studio import __version__
from tools.btga_studio.core import geometry as g
from tools.btga_studio.core import inspector as insp
from tools.btga_studio.core import project as projects
from tools.btga_studio.core.browser import GAMES
from tools.btga_studio.core.controller import EditorController
from tools.btga_studio.core.document import EditError
from tools.btga_studio.core.settings import Settings
from tools.btga_studio.core.thumbnails import ThumbnailCache, ThumbnailService
from tools.btga_studio.gui.input import BINDABLE_KEYS, DEFAULT_BINDINGS, InputState, commands, validate_bindings

gizmo = imguizmo.im_guizmo
PROJECT_FILTER = ["BattleTanx Studio project", "*.btgaproj"]
PACKAGE_FILTER = ["BattleTanx content package", "*.btgapkg"]
SOURCE_FILTER = ["BattleTanx sources", "*.z64 *.v64 *.n64 *.cue *.bin *.iso", "All files", "*"]
STATUS_COLOURS = {"supported": (0.4, 0.9, 0.4, 1), "unknown": (0.9, 0.8, 0.3, 1), "mismatched": (1, 0.45, 0.3, 1),
                  "incomplete": (1, 0.7, 0.3, 1), "missing": (1, 0.35, 0.35, 1)}
DEFINITION_FAMILIES = ("missions", "multiplayer", "minigames", "tanks", "gangs")


def _keymap() -> dict[str, int]:
    keys = {}
    for name in BINDABLE_KEYS:
        attribute = {"Home": "home", "End": "end", "Escape": "escape", "Delete": "delete", "Insert": "insert", "PageUp": "page_up",
                     "PageDown": "page_down"}.get(name)
        if attribute is None:
            attribute = name.lower() if name.isalpha() and len(name) == 1 else (f"_{name}" if name.isdigit() else name.lower())
        key = getattr(imgui.Key, attribute, None)
        if key is not None:
            keys[name] = key
    return keys


def _vec(v) -> tuple:
    return (float(v[0]), float(v[1]))


class StudioApp:
    def __init__(self, settings: Settings, catalog: Path | None = None, project: Path | None = None, controller: EditorController | None = None):
        self.settings = settings
        self.controller = controller or EditorController(settings)
        self.renderer = None
        self.thumbnails: ThumbnailService | None = None
        self.thumb_textures: dict[str, int] = {}
        self.gizmo_operation = "translate"
        self.gizmo_was_using = False
        self.search = ""
        self.filter_platform = None
        self.filter_kind = None
        self.filter_category = None
        self.map_only = True
        self.thumbnail_mode = False
        self.browser_generation = 0
        self.browser_key = None
        self.browser_rows = []
        self.pending_values: dict[str, object] = {}
        self.field_error: str | None = None
        self.confirm_close = False
        self.pending_open: tuple | None = None
        self.outline_filter = ""
        self.startup = (catalog, project)
        self.right_drag = False
        self.left_press = None
        self.drag_rect = None
        self.keymap = _keymap()
        self.show_new_project = False
        self.show_new_map = False
        self.show_definitions = False
        self.show_sources = False
        self.show_controls = False
        self.show_recovery = False
        self.wizard = {"title": "New project", "target": 0, "map_kind": 0, "key": "map_1", "map_title": "Map 1", "size": 2048.0, "scene": 0,
                       "scene_filter": ""}
        self.definition_family = 0
        self.definition_key = "item_1"
        self.binding_error = None
        self.title = "BattleTanx Studio"
        self.drag_payload = None
        self.viewport_origin = None  # screen position of the viewport image (used by the off-screen acceptance driver)
        self._renderer_cls = None

    # -- lifecycle -----------------------------------------------------------------
    def post_init(self) -> None:
        if self._renderer_cls is None:
            from tools.btga_studio.gui.renderer import ViewportRenderer

            self._renderer_cls = ViewportRenderer
        catalog, project = self.startup
        c = self.controller
        if catalog and (Path(catalog) / "catalog.json").is_file():
            try:
                c.open_catalog(Path(catalog))
            except EditError as error:
                c.log("error", str(error))
        self.attach_library()
        if project:
            self.open_project(Path(project))
        elif c.recoveries():
            self.show_recovery = True

    def attach_library(self) -> None:
        c = self.controller
        if c.library is None:
            return
        if self.renderer is None:
            self.renderer = self._renderer_cls(c.library)
        else:
            self.renderer.unload()
            self.renderer.library = c.library
            self.renderer.textures.clear()
        prefs = self.settings.preferences
        cache = ThumbnailCache(self.settings.thumbnail_directory, prefs.thumbnail_cache_mib * 1024 * 1024)
        if self.thumbnails:
            self.thumbnails.shutdown()
        self.thumbnails = ThumbnailService(c.library.root_for_model, cache, prefs.thumbnail_workers)
        self.browser_key = None
        if c.document is not None:
            self.renderer.load_scene(c.document.scene)

    def before_exit(self) -> None:
        self.settings.save()
        if self.thumbnails:
            self.thumbnails.shutdown()
        self.controller.shutdown()

    def _load_into_renderer(self) -> None:
        if self.renderer and self.controller.document:
            self.renderer.load_scene(self.controller.document.scene)
        self.browser_key = None

    def guarded(self, label: str, action, *args) -> bool:
        try:
            action(*args)
            return True
        except (EditError, projects.ProjectError, OSError, ValueError) as error:
            self.controller.log("error", f"{label}: {error}")
            return False

    def open_project(self, path: Path) -> None:
        if self.guarded("open project", self.controller.open_project, Path(path)):
            self._load_into_renderer()

    def switch_map(self, key: str) -> None:
        if self.guarded("open map", self.controller.switch_map, key):
            self._load_into_renderer()

    # -- menus -----------------------------------------------------------------------
    def show_menus(self) -> None:
        c = self.controller
        prefs = self.settings.preferences
        has_doc = c.document is not None
        if imgui.begin_menu("File"):
            if imgui.menu_item("New project...", "Ctrl+N", False)[0]:
                self.request("new_project")
            if imgui.menu_item("Open project...", "Ctrl+O", False)[0]:
                self.request("open_project")
            if imgui.begin_menu("Recent projects", bool(prefs.recent_projects)):
                for path in list(prefs.recent_projects):
                    if imgui.menu_item(Path(path).name, "", False)[0]:
                        self.request("open_path", path)
                imgui.end_menu()
            imgui.separator()
            if imgui.menu_item("Save", "Ctrl+S", False, c.project is not None)[0]:
                self.save()
            if imgui.menu_item("Save as...", "Ctrl+Shift+S", False, c.project is not None)[0]:
                self.save(ask=True)
            imgui.separator()
            if imgui.menu_item("Export content package...", "", False, c.project is not None)[0]:
                self.export_package()
            if imgui.menu_item("Export preview package...", "", False, has_doc)[0]:
                folder = pfd.select_folder("Export preview package (inspection only)").result()
                if folder:
                    projects.export_preview(Path(folder), c.document, c.library, c.diagnostics)
                    c.log("info", f"preview package written to {Path(folder).name}")
            imgui.separator()
            if imgui.menu_item("Source manager...", "", False)[0]:
                self.show_sources = True
            if imgui.menu_item("Open catalog folder...", "", False)[0]:
                folder = pfd.select_folder("Open a neutral catalog folder").result()
                if folder and self.guarded("open catalog", c.open_catalog, Path(folder)):
                    self.attach_library()
            imgui.separator()
            if imgui.menu_item("Exit", "", False)[0] and self.runner() is not None:
                self.runner().app_shall_exit = True
            imgui.end_menu()
        if imgui.begin_menu("Edit"):
            doc = c.document
            if imgui.menu_item("Undo", "Ctrl+Z", False, bool(doc and doc.undo_stack))[0]:
                c.undo()
            if imgui.menu_item("Redo", "Ctrl+Y", False, bool(doc and doc.redo_stack))[0]:
                c.redo()
            imgui.separator()
            if imgui.menu_item("Copy", "Ctrl+C", False, bool(doc and doc.selection))[0]:
                self.copy()
            if imgui.menu_item("Paste", "Ctrl+V", False, bool(doc and c.clipboard))[0]:
                c.paste()
            if imgui.menu_item("Duplicate", "Ctrl+D", False, bool(doc and doc.selection))[0]:
                c.duplicate()
            if imgui.menu_item("Delete / disable", "Del", False, bool(doc and doc.selection))[0]:
                c.delete()
            imgui.separator()
            if imgui.menu_item("Select all", "Ctrl+A", False, has_doc)[0]:
                c.select_all()
            if imgui.menu_item("Clear selection", "", False, bool(doc and doc.selection))[0]:
                doc.select(None)
            imgui.end_menu()
        if imgui.begin_menu("View"):
            for layer in ("geometry", "collision", "markers", "navigation", "bounds", "minimap", "unresolved", "grid"):
                changed, value = imgui.menu_item(f"Show {layer}", "", prefs.layers.get(layer, True))
                if changed:
                    prefs.layers[layer] = value
            _, prefs.wireframe = imgui.menu_item("Wireframe", "", prefs.wireframe)
            imgui.separator()
            keys = validate_bindings(prefs.bindings)
            doc = c.document
            for label, action, enabled in (("Hide selection", "hide", bool(doc and doc.selection)), ("Isolate selection", "isolate", bool(doc and doc.selection)),
                                           ("Unhide all", "unhide_all", bool(doc and doc.hidden)), ("Lock selection", "lock", bool(doc and doc.selection))):
                if imgui.menu_item(label, keys[action], False, enabled)[0]:
                    getattr(doc, action)()
            if imgui.menu_item("Unlock all", "", False, bool(doc and doc.locked))[0]:
                doc.unlock_all()
            imgui.separator()
            if imgui.menu_item("Frame map", keys["frame_map"], False)[0]:
                c.frame_map()
            if imgui.menu_item("Focus selection", keys["frame_selection"], False)[0]:
                c.frame_selection()
            imgui.separator()
            _, self.show_definitions = imgui.menu_item("Project definitions", "", self.show_definitions)
            imgui.end_menu()
        if imgui.begin_menu("Create"):
            if imgui.menu_item("New map...", "", False, c.project is not None or c.library is not None)[0]:
                self.show_new_map = True
            imgui.separator()
            if c.document:
                for record_id, label in self.gameplay_templates():
                    if imgui.menu_item(label, "", False)[0]:
                        self.try_placement("marker", record_id)
            else:
                imgui.text_disabled("Open a map to place gameplay objects")
            imgui.end_menu()
        if imgui.begin_menu("Tools"):
            imgui.text("Gizmo")
            for mode in ("translate", "rotate", "scale"):
                if imgui.radio_button(mode.capitalize(), self.gizmo_operation == mode):
                    self.gizmo_operation = mode
                imgui.same_line()
            imgui.new_line()
            _, prefs.gizmo_local = imgui.checkbox("Local orientation", prefs.gizmo_local)
            imgui.separator()
            imgui.text("Snapping")
            _, prefs.surface_snap = imgui.checkbox("Snap to surface", prefs.surface_snap)
            _, prefs.grid_snap = imgui.input_float("Grid (0 = off)", prefs.grid_snap, 8.0, 64.0)
            prefs.grid_snap = max(0.0, prefs.grid_snap)
            for degrees in g.YAW_SNAP_DEGREES:
                if imgui.radio_button(f"{degrees}°##yaw" if degrees else "Yaw off", prefs.yaw_snap_degrees == degrees):
                    prefs.yaw_snap_degrees = degrees
                imgui.same_line()
            imgui.new_line()
            imgui.separator()
            if imgui.menu_item("Controls and camera...", "", False)[0]:
                self.show_controls = True
            _, prefs.autosave_enabled = imgui.checkbox("Autosave recovery copy", prefs.autosave_enabled)
            _, prefs.autosave_seconds = imgui.input_int("Autosave interval (s)", prefs.autosave_seconds)
            prefs.autosave_seconds = max(15, prefs.autosave_seconds)
            imgui.end_menu()
        if imgui.begin_menu("Help"):
            imgui.text(f"BattleTanx Studio {__version__}")
            keys = validate_bindings(prefs.bindings)
            imgui.text_disabled(f"Right mouse: look   {keys['forward']}{keys['left']}{keys['back']}{keys['right']}: move   "
                                f"{keys['down']}/{keys['up']}: down/up   Shift: faster   Wheel: speed")
            imgui.text_disabled(f"Click: select   Ctrl+click: toggle   Drag: box select   {keys['frame_selection']}: focus   {keys['frame_map']}: map")
            imgui.text_disabled(f"{keys['gizmo_translate']}/{keys['gizmo_rotate']}/{keys['gizmo_scale']}: move/rotate/scale gizmo   "
                                "Shift+drag gizmo: duplicate   Esc: cancel")
            imgui.text_disabled(f"{keys['hide']}: hide   {keys['isolate']}: isolate   {keys['unhide_all']}: unhide all   {keys['lock']}: lock")
            imgui.text_disabled("Projects are non-destructive; no ROM or disc is modified.")
            imgui.end_menu()

    def request(self, action: str, argument=None) -> None:
        """Actions that may discard unsaved work go through the unsaved-changes prompt."""

        if self.controller.dirty and action in ("new_project", "open_project", "open_path", "exit"):
            self.pending_open = (action, argument)
            self.confirm_close = True
            return
        self.perform(action, argument)

    def perform(self, action: str, argument=None) -> None:
        if action == "new_project":
            self.show_new_project = True
        elif action == "open_project":
            files = pfd.open_file("Open BattleTanx Studio project", "", PROJECT_FILTER).result()
            if files:
                self.open_project(Path(files[0]))
        elif action == "open_path":
            self.open_project(Path(argument))
        elif action == "exit":
            runner = self.runner()
            if runner is not None:
                runner.app_shall_exit = True

    def save(self, ask: bool = False) -> None:
        c = self.controller
        path = c.project_path
        if ask or path is None:
            chosen = pfd.save_file("Save BattleTanx Studio project", "untitled.btgaproj", PROJECT_FILTER).result()
            if not chosen:
                return
            path = Path(chosen if chosen.endswith(".btgaproj") else chosen + ".btgaproj")
        self.guarded("save", c.save, path)

    def export_package(self) -> None:
        from tools.btga_studio.core.package import PackageError, validate_for_export, write_package

        c = self.controller
        report = validate_for_export(c.project, c.library)
        for item in report.errors:
            c.log("error", f"export blocked: [{item['map'] or 'project'}] {item['target']}{'.' + item['field'] if item['field'] else ''}: {item['message']}")
        if not report.ok:
            return
        chosen = pfd.save_file("Export content package", "content.btgapkg", PACKAGE_FILTER).result()
        if not chosen:
            return
        path = Path(chosen if chosen.endswith(".btgapkg") else chosen + ".btgapkg")
        try:
            write_package(path, c.project, c.library, __version__)
            c.log("info", f"content package written: {path.name}")
        except (PackageError, OSError) as error:
            c.log("error", f"export failed: {error}")

    def gameplay_templates(self) -> list[tuple[str, str]]:
        index = self.controller.index
        if index is None:
            return []
        return [(e.id, f"{e.category}: {e.label}") for e in index.template_entries + index.map_entries
                if e.kind == "gameplay" and e.editable and e.source in ("source", "starter")]

    def try_placement(self, kind: str, resource_id: str) -> None:
        try:
            self.controller.begin_placement(kind, resource_id)
            self.controller.log("info", "click in the viewport to place; Esc cancels")
        except EditError as error:
            self.controller.log("error", str(error))

    def copy(self) -> None:
        text = self.controller.copy()
        if text:
            imgui.set_clipboard_text(text)

    def paste(self) -> None:
        text = imgui.get_clipboard_text() or self.controller.clipboard
        self.controller.paste(text)

    # -- wizards and floating windows ---------------------------------------------------
    def window_new_project(self) -> None:
        if not self.show_new_project:
            return
        c = self.controller
        imgui.set_next_window_size(imgui.ImVec2(520, 0), imgui.Cond_.appearing)
        opened, self.show_new_project = imgui.begin("New project", self.show_new_project)
        if opened:
            w = self.wizard
            _, w["title"] = imgui.input_text("Title", w["title"])
            targets = list(projects.TARGET_GAMES)
            _, w["target"] = imgui.combo("Target game", w["target"], [GAMES.get(t, t) if t != "studio" else "Studio (original content)" for t in targets])
            imgui.separator()
            imgui.text("First map")
            self.map_choice_widgets()
            if imgui.button("Create"):
                if self.guarded("new project", c.new_studio_project, w["title"], targets[w["target"]]) and self.create_map_from_wizard():
                    self.show_new_project = False
            imgui.same_line()
            if imgui.button("Open the starter example"):
                self.open_example()
                self.show_new_project = False
        imgui.end()

    def map_choice_widgets(self) -> None:
        w = self.wizard
        c = self.controller
        if imgui.radio_button("Blank map", w["map_kind"] == 0):
            w["map_kind"] = 0
        imgui.same_line()
        if imgui.radio_button("Import an existing map", w["map_kind"] == 1):
            w["map_kind"] = 1
        _, w["key"] = imgui.input_text("Map key (a-z, 0-9, _)", w["key"])
        _, w["map_title"] = imgui.input_text("Map title", w["map_title"])
        if w["map_kind"] == 0:
            _, w["size"] = imgui.input_float("Half-extent (units)", w["size"], 256.0, 1024.0)
        else:
            levels = [l for l in (c.library.levels() if c.library else []) if l.platform != "studio"]
            if not levels:
                imgui.text_wrapped("No game maps are loaded. Register a source in File > Source manager and build its catalog first.")
            _, w["scene_filter"] = imgui.input_text("Filter maps", w["scene_filter"])
            needle = w["scene_filter"].lower()
            shown = [l for l in levels if needle in f"{l.name} {l.platform} {l.campaign}".lower()]
            if imgui.begin_list_box("##scenes", imgui.ImVec2(-1, 160)):
                for index, level in enumerate(shown):
                    if imgui.selectable(f"[{GAMES.get(level.platform, level.platform)}] {level.name} ({level.variant})##{level.id}", w["scene"] == index)[0]:
                        w["scene"] = index
                imgui.end_list_box()
            w["_shown"] = shown

    def create_map_from_wizard(self) -> bool:
        w = self.wizard
        c = self.controller
        if w["map_kind"] == 0:
            ok = self.guarded("new map", c.add_blank_map, w["key"], w["map_title"], float(w["size"]))
        else:
            shown = w.get("_shown") or []
            if not shown:
                c.log("error", "choose a map to import")
                return False
            level = shown[min(w["scene"], len(shown) - 1)]
            ok = self.guarded("import map", c.add_imported_map, w["key"], level.id, w["map_title"] or level.name)
        if ok:
            self._load_into_renderer()
        return ok

    def open_example(self) -> None:
        from tools.btga_studio.content.examples import build_example

        c = self.controller
        c.project = build_example(c.library)
        c.active_map = None
        self.switch_map(sorted(c.project.maps)[0])

    def window_new_map(self) -> None:
        if not self.show_new_map:
            return
        opened, self.show_new_map = imgui.begin("New map", self.show_new_map)
        if opened:
            self.map_choice_widgets()
            if imgui.button("Add map"):
                if self.controller.project is None:
                    self.controller.new_studio_project(self.wizard["map_title"])
                if self.create_map_from_wizard():
                    self.show_new_map = False
        imgui.end()

    def window_definitions(self) -> None:
        if not self.show_definitions:
            return
        c = self.controller
        opened, self.show_definitions = imgui.begin("Project definitions", self.show_definitions)
        if opened and c.project is None:
            imgui.text_disabled("No project open")
        elif opened:
            project = c.project
            info = project.project
            changed, title = imgui.input_text("Project title", info["title"])
            if imgui.is_item_deactivated_after_edit():
                self.guarded("title", lambda: info.__setitem__("title", projects._text(title, "title")))
            _, info["description"] = imgui.input_text_multiline("Description", info.get("description", ""), imgui.ImVec2(-1, 60))
            imgui.text(f"Target: {info['target']['game']}  package: {info['target']['package']}")
            if imgui.collapsing_header("Campaigns"):
                for campaign in project.campaigns:
                    imgui.bullet_text(f"{campaign.get('title')} ({campaign.get('id')}): {', '.join(campaign.get('missions', []))}")
                if imgui.button("Add campaign with all missions"):
                    project.campaigns.append({"id": f"campaign_{len(project.campaigns) + 1}", "title": "New campaign", "missions": sorted(project.missions)})
            _, self.definition_family = imgui.combo("Definition type", self.definition_family, list(DEFINITION_FAMILIES))
            family = DEFINITION_FAMILIES[self.definition_family]
            table = getattr(project, family)
            for key in sorted(table):
                data = table[key]
                if imgui.tree_node(f"{key}: {data.get('title', '')}##{family}{key}"):
                    _, data["title"] = imgui.input_text(f"Title##{family}{key}", data.get("title", ""))
                    if family in ("missions", "multiplayer", "minigames"):
                        maps = sorted(project.maps)
                        current = maps.index(data["map"]) if data.get("map") in maps else 0
                        changed, current = imgui.combo(f"Map##{family}{key}", current, maps)
                        if maps:
                            data["map"] = maps[current]
                    if family == "missions":
                        imgui.text(f"Objectives: {', '.join(data.get('objectives', [])) or 'none'}")
                        if c.document and c.document.selection and imgui.button(f"Set objectives to selection##{key}"):
                            data["objectives"] = list(c.document.selection)
                    if family == "multiplayer":
                        _, data["max_players"] = imgui.slider_int(f"Players##{key}", int(data.get("max_players", 4)), 2, 4)
                    if family == "tanks":
                        imgui.text(f"Model: {data.get('model')}")
                        stats = data.setdefault("stats", {"speed": 1.0, "armor": 100, "weapon": "cannon"})
                        _, stats["speed"] = imgui.slider_float(f"Speed##{key}", float(stats.get("speed", 1.0)), 0.2, 3.0)
                        _, stats["armor"] = imgui.slider_int(f"Armor##{key}", int(stats.get("armor", 100)), 10, 400)
                    if family == "gangs":
                        imgui.text(f"Tanks: {', '.join(data.get('tanks', [])) or 'none'}")
                        for tank in sorted(project.tanks):
                            on = tank in data.get("tanks", [])
                            changed, on = imgui.checkbox(f"{tank}##{key}", on)
                            if changed:
                                data["tanks"] = sorted(set(data.get("tanks", [])) ^ {tank})
                    if imgui.button(f"Remove##{family}{key}"):
                        del table[key]
                    imgui.tree_pop()
            _, self.definition_key = imgui.input_text("New key", self.definition_key)
            imgui.same_line()
            if imgui.button(f"Add {family[:-1] if family.endswith('s') else family}"):
                default = {"title": self.definition_key.replace("_", " ").title()}
                if family in ("missions", "multiplayer", "minigames"):
                    default["map"] = c.active_map or (sorted(project.maps)[0] if project.maps else None)
                if family == "tanks":
                    starter = (c.library.starter() or {}).get("models", {})
                    default["model"] = starter.get("tank_placeholder")
                self.guarded("definition", project.define, family, self.definition_key, default)
        imgui.end()

    def window_sources(self) -> None:
        if not self.show_sources:
            return
        c = self.controller
        imgui.set_next_window_size(imgui.ImVec2(860, 320), imgui.Cond_.appearing)
        opened, self.show_sources = imgui.begin("Source manager", self.show_sources)
        if opened:
            imgui.text_wrapped("Register game files you legally own. Studio stores only a local reference and hashes, builds catalogs into "
                               "your data folder, and never copies sources into a project or package.")
            if imgui.button("Add ROM or disc image..."):
                files = pfd.open_file("Add a BattleTanx source", "", SOURCE_FILTER).result()
                for path in files or []:
                    c.add_source(Path(path))
            imgui.same_line()
            if imgui.button("Add extracted PS1 folder..."):
                folder = pfd.select_folder("Folder holding SLUS_010.44").result()
                if folder:
                    c.add_source(Path(folder))
            imgui.same_line()
            if imgui.button("Verify all"):
                for source_id in list(c.registry.sources):
                    c.registry.verify(source_id)
            imgui.same_line()
            if imgui.button("Search folder for missing..."):
                folder = pfd.select_folder("Search for moved sources").result()
                if folder:
                    for source_id, path in c.registry.search(Path(folder)).items():
                        self.guarded("relocate", c.registry.relocate, source_id, path)
            if imgui.begin_table("sources", 5, imgui.TableFlags_.row_bg | imgui.TableFlags_.borders_inner_v | imgui.TableFlags_.resizable):
                for heading in ("Source", "Status", "Detail", "Catalog", "Actions"):
                    imgui.table_setup_column(heading)
                imgui.table_headers_row()
                for source in sorted(c.registry.sources.values(), key=lambda s: s.id):
                    imgui.table_next_row()
                    imgui.table_next_column()
                    imgui.text(source.title)
                    if imgui.is_item_hovered():
                        imgui.set_tooltip(f"{source.path}\nSHA-256 {source.sha256}")
                    imgui.table_next_column()
                    imgui.text_colored(imgui.ImVec4(*STATUS_COLOURS.get(source.status, (1, 1, 1, 1))), source.status)
                    imgui.table_next_column()
                    imgui.text_wrapped(source.detail)
                    imgui.table_next_column()
                    job = c.builds.get(source.id)
                    if job:
                        imgui.progress_bar(job.fraction, imgui.ImVec2(-1, 0), f"{job.stage} {job.done}/{job.total}")
                    else:
                        imgui.text("built" if source.catalog else "not built")
                    imgui.table_next_column()
                    if job:
                        if imgui.small_button(f"Cancel##{source.id}"):
                            job.cancel.set()
                    else:
                        if imgui.small_button(f"Build catalog##{source.id}") and source.status == "supported":
                            c.build_catalog(source.id)
                        imgui.same_line()
                        if imgui.small_button(f"Relocate##{source.id}"):
                            files = pfd.open_file("Locate the moved source", "", SOURCE_FILTER).result()
                            if files:
                                self.guarded("relocate", c.registry.relocate, source.id, Path(files[0]))
                        imgui.same_line()
                        if imgui.small_button(f"Remove##{source.id}"):
                            c.registry.remove(source.id)
                            c.refresh_library()
                            self.attach_library()
                imgui.end_table()
        imgui.end()

    def window_controls(self) -> None:
        if not self.show_controls:
            return
        prefs = self.settings.preferences
        opened, self.show_controls = imgui.begin("Controls and camera", self.show_controls)
        if opened:
            _, prefs.camera_speed = imgui.slider_float("Move speed", float(prefs.camera_speed), 10.0, 20000.0, flags=imgui.SliderFlags_.logarithmic)
            self.controller.camera.speed = prefs.camera_speed
            _, prefs.look_sensitivity = imgui.slider_float("Look sensitivity", float(prefs.look_sensitivity), 0.0005, 0.01)
            _, prefs.invert_look_y = imgui.checkbox("Invert look Y", prefs.invert_look_y)
            imgui.separator()
            merged = validate_bindings(prefs.bindings) if not self.binding_error else {**DEFAULT_BINDINGS, **prefs.bindings}
            for action in DEFAULT_BINDINGS:
                current = BINDABLE_KEYS.index(merged[action]) if merged[action] in BINDABLE_KEYS else 0
                changed, current = imgui.combo(action.replace("_", " "), current, list(BINDABLE_KEYS))
                if changed:
                    candidate = {**prefs.bindings, action: BINDABLE_KEYS[current]}
                    try:
                        validate_bindings(candidate)
                        prefs.bindings = candidate
                        self.binding_error = None
                    except ValueError as error:
                        self.binding_error = str(error)
            if self.binding_error:
                imgui.text_colored(imgui.ImVec4(1, 0.4, 0.4, 1), self.binding_error)
            if imgui.button("Restore defaults"):
                prefs.bindings = {}
                self.binding_error = None
        imgui.end()

    def window_recovery(self) -> None:
        if not self.show_recovery:
            return
        imgui.open_popup("Recover unsaved work")
        if imgui.begin_popup_modal("Recover unsaved work", None, imgui.WindowFlags_.always_auto_resize)[0]:
            imgui.text("Studio found unsaved work from an earlier session:")
            for item in self.controller.recoveries():
                imgui.bullet_text(f"{item['title']}")
                imgui.same_line()
                if imgui.small_button(f"Restore##{item['path'].name}"):
                    if self.guarded("recover", self.controller.restore_recovery, item["path"]):
                        self._load_into_renderer()
                    self.show_recovery = False
                    imgui.close_current_popup()
                imgui.same_line()
                if imgui.small_button(f"Discard##{item['path'].name}"):
                    for path in (item["path"], item["path"].with_name(item["path"].name + ".json")):
                        path.unlink(missing_ok=True)
            if imgui.button("Close"):
                self.show_recovery = False
                imgui.close_current_popup()
            imgui.end_popup()

    # -- panels ---------------------------------------------------------------------------
    def panel_hierarchy(self) -> None:
        c = self.controller
        project = c.project
        if project is None:
            imgui.text_wrapped("No project open.")
            if imgui.button("New project..."):
                self.request("new_project")
            if imgui.button("Open the starter example"):
                self.open_example()
            if imgui.button("Source manager..."):
                self.show_sources = True
        else:
            imgui.text(project.project["title"] + (" *" if c.dirty else ""))
            if imgui.collapsing_header("Maps", imgui.TreeNodeFlags_.default_open):
                for key, entry in sorted(project.maps.items()):
                    label = f"{entry.title} [{entry.kind}]##{key}"
                    if imgui.selectable(label, key == c.active_map)[0] and key != c.active_map:
                        self.switch_map(key)
                if imgui.small_button("New map..."):
                    self.show_new_map = True
            if imgui.collapsing_header("Definitions"):
                for family in DEFINITION_FAMILIES:
                    imgui.text(f"{family}: {len(getattr(project, family))}")
                imgui.text(f"campaigns: {len(project.campaigns)}")
                if imgui.small_button("Edit definitions..."):
                    self.show_definitions = True
        if imgui.collapsing_header("Layers", imgui.TreeNodeFlags_.default_open):
            prefs = self.settings.preferences
            for layer in ("geometry", "collision", "markers", "navigation", "bounds", "minimap", "unresolved", "grid"):
                _, prefs.layers[layer] = imgui.checkbox(layer.capitalize(), prefs.layers.get(layer, True))
            _, prefs.wireframe = imgui.checkbox("Wireframe", prefs.wireframe)
        doc = c.document
        if doc and imgui.collapsing_header("Outline"):
            _, self.outline_filter = imgui.input_text("Filter##outline", self.outline_filter)
            needle = self.outline_filter.lower()
            rows = [(f, r) for f, r in doc.scene.records() if not needle or needle in r["id"].lower() or needle in str(r.get("category", "")).lower()]
            clipper = imgui.ListClipper()
            clipper.begin(len(rows))
            while clipper.step():
                for i in range(clipper.display_start, clipper.display_end):
                    family, record = rows[i]
                    record_id = record["id"]
                    hidden, locked = record_id in doc.hidden, record_id in doc.locked
                    if imgui.small_button(f"{'show' if hidden else 'hide'}##h{record_id}"):
                        (doc.hidden.discard if hidden else doc.hidden.add)(record_id)
                    imgui.same_line()
                    if imgui.small_button(f"{'unlock' if locked else 'lock'}##l{record_id}"):
                        (doc.locked.discard if locked else doc.locked.add)(record_id)
                    imgui.same_line()
                    label = f"{family[:4]} {record.get('category', record.get('shape', ''))} {record_id.rsplit(':', 2)[-2:]}##{record_id}"
                    if imgui.selectable(label, record_id in doc.selection)[0] and not hidden and not locked:
                        c.frame_record(record_id)

    def panel_viewport(self) -> None:
        c = self.controller
        avail = imgui.get_content_region_avail()
        width, height = max(16, int(avail.x)), max(16, int(avail.y))
        if c.document is None or self.renderer is None:
            imgui.text_wrapped("Create a project (File > New project) or open the starter example, then pick a map.")
            return
        c.viewport = (width, height)
        prefs = self.settings.preferences
        self.renderer.wireframe = prefs.wireframe
        self.renderer.layers = {**prefs.layers}
        self.renderer.user_hidden = set(c.document.hidden)
        selection = set(c.document.selection)
        texture = self.renderer.render(c.camera, width, height, selection, c.preview)
        origin = imgui.get_cursor_screen_pos()
        self.viewport_origin = (origin.x, origin.y)
        imgui.image(imgui.ImTextureRef(texture), imgui.ImVec2(width, height), imgui.ImVec2(0, 1), imgui.ImVec2(1, 0))
        hovered = imgui.is_item_hovered()
        io = imgui.get_io()
        mouse = (io.mouse_pos.x - origin.x, io.mouse_pos.y - origin.y)
        if imgui.begin_drag_drop_target():
            payload = imgui.accept_drag_drop_payload_py_id("BTGA_ASSET")
            if payload is not None and self.drag_payload:
                kind, resource = self.drag_payload
                self.try_placement(kind, resource)
                if c.placement is not None:
                    c.place_at(*mouse)
                    c.placement = None
            imgui.end_drag_drop_target()
        using_gizmo = self.draw_gizmo(origin, width, height)
        if hovered:
            c.hover(*mouse)
        if imgui.is_mouse_clicked(imgui.MouseButton_.right) and hovered:
            self.right_drag = True
        if not imgui.is_mouse_down(imgui.MouseButton_.right):
            self.right_drag = False
        if imgui.is_mouse_clicked(imgui.MouseButton_.left) and hovered and not gizmo.is_over():
            self.left_press = mouse
        clicked, box = False, None
        if self.left_press is not None and imgui.is_mouse_down(imgui.MouseButton_.left) and not using_gizmo and c.placement is None:
            if math.dist(self.left_press, mouse) >= 6:
                self.drag_rect = (self.left_press[0], self.left_press[1], mouse[0], mouse[1])
                draw = imgui.get_window_draw_list()
                a = imgui.ImVec2(origin.x + min(self.left_press[0], mouse[0]), origin.y + min(self.left_press[1], mouse[1]))
                b = imgui.ImVec2(origin.x + max(self.left_press[0], mouse[0]), origin.y + max(self.left_press[1], mouse[1]))
                draw.add_rect_filled(a, b, imgui.get_color_u32(imgui.ImVec4(0.3, 0.6, 1, 0.15)))
                draw.add_rect(a, b, imgui.get_color_u32(imgui.ImVec4(0.3, 0.6, 1, 0.9)))
        if imgui.is_mouse_released(imgui.MouseButton_.left) and self.left_press is not None:
            if self.drag_rect is not None:
                box = self.drag_rect
            else:
                clicked = not gizmo.is_over() and not using_gizmo
            self.left_press = None
            self.drag_rect = None
        keys_down = {name for name, key in self.keymap.items() if imgui.is_key_down(key)}
        keys_down |= ({"Shift"} if io.key_shift else set()) | ({"Ctrl"} if io.key_ctrl else set()) | ({"Alt"} if io.key_alt else set())
        state = InputState(
            dt=io.delta_time, viewport_hovered=hovered, viewport_focused=imgui.is_window_focused(), text_input=io.want_text_input,
            gizmo_active=using_gizmo, keys_down=keys_down, keys_pressed={name for name, key in self.keymap.items() if imgui.is_key_pressed(key, False)},
            mouse_delta=_vec(io.mouse_delta) if self.right_drag else (0.0, 0.0), wheel=io.mouse_wheel if hovered else 0.0,
            right_down=self.right_drag, left_clicked=clicked, box=box, mouse=mouse, bindings=validate_bindings(prefs.bindings))
        self.dispatch(state)
        draw = imgui.get_window_draw_list()
        status = f"{c.camera.speed:.0f} u/s   {len(c.document.scene.view)} records   {self.renderer.draw_calls} draws   gizmo: {self.gizmo_operation}"
        if c.document.hidden:
            status += f"   {len(c.document.hidden)} hidden"
        if c.placement:
            status += "   PLACING (click to place, Esc to cancel)"
            if c.preview and not c.preview["valid"]:
                status += "   outside map limits"
        draw.add_text(imgui.ImVec2(origin.x + 8, origin.y + 6), imgui.get_color_u32(imgui.ImVec4(1, 1, 1, 0.85)), status)

    def dispatch(self, state: InputState) -> None:
        c = self.controller
        for command in commands(state):
            name = command.name
            if name == "gizmo_mode":
                self.gizmo_operation = command.args["mode"]
            elif name == "save":
                self.save()
            elif name == "save_as":
                self.save(ask=True)
            elif name in ("open_project", "new_project"):
                self.request(name)
            elif name == "copy":
                self.copy()
            elif name == "paste":
                self.paste()
            else:
                c.execute(command)

    def draw_gizmo(self, origin, width: int, height: int) -> bool:
        c = self.controller
        matrix = c.gizmo_matrix()
        if matrix is None:
            if self.gizmo_was_using:
                c.gizmo_end()
                self.gizmo_was_using = False
            return False
        gizmo.set_drawlist()
        gizmo.set_rect(origin.x, origin.y, width, height)
        view = gizmo.Matrix16(list(c.camera.view().T.flatten()))
        projection = gizmo.Matrix16(list(c.camera.projection(width / height).T.flatten()))
        current = gizmo.Matrix16(list(matrix.T.flatten()))
        operation = {"translate": gizmo.OPERATION.translate, "rotate": gizmo.OPERATION.rotate_y, "scale": gizmo.OPERATION.scale}[self.gizmo_operation]
        mode = gizmo.MODE.local if self.settings.preferences.gizmo_local else gizmo.MODE.world
        changed = gizmo.manipulate(view, projection, operation, mode, current)
        using = gizmo.is_using()
        if using and not self.gizmo_was_using:
            if not c.gizmo_begin(duplicate=imgui.get_io().key_shift):
                return False
        if using and changed:
            values = np.array(current.values, float).reshape(4, 4).T
            c.gizmo_update(values, scale_mode=self.gizmo_operation == "scale")
        if not using and self.gizmo_was_using:
            c.gizmo_end()
        self.gizmo_was_using = using
        return using

    def panel_inspector(self) -> None:
        c = self.controller
        doc = c.document
        if doc is None:
            imgui.text_disabled("No map open")
            return
        entry = c.project.maps[c.active_map] if c.project else None
        if not doc.selection:
            imgui.text_disabled("Nothing selected.")
            if entry is not None and imgui.collapsing_header("Map settings", imgui.TreeNodeFlags_.default_open):
                imgui.text(f"{entry.title} [{entry.kind}]  key {entry.key}")
                bounds = doc.scene.bounds
                if bounds:
                    imgui.text(f"Limits x {bounds['x']}  z {bounds['z']}")
                if entry.kind == "blank":
                    half = float(bounds["x"][1]) if bounds else 2048.0
                    changed, half = imgui.input_float("Half-extent", half, 256.0, 1024.0)
                    if imgui.is_item_deactivated_after_edit():
                        self.guarded("map settings", c.project.set_map_settings, entry.key, half)
                    colour = [v / 255 for v in doc.scene.base["environment"][0]["clear_and_fog_colour"]]
                    changed, colour = imgui.color_edit3("Fog / background", colour)
                    if changed:
                        self.guarded("map settings", c.project.set_map_settings, entry.key, None, [int(v * 255) for v in colour])
                        if self.renderer:
                            self.renderer.background = tuple(colour)
            env = doc.scene.base.get("environment", [])
            if env and entry is not None and entry.kind == "imported" and imgui.collapsing_header("Environment / fog / background"):
                for item in env:
                    self.draw_inspector(insp.describe_environment(item))
            minimap = doc.scene.base.get("minimap") or {}
            if minimap.get("selections") and imgui.collapsing_header("Minimap alignment"):
                self.draw_inspector(insp.describe_minimap(minimap))
            if entry is not None and imgui.collapsing_header("Minimap configuration"):
                config = entry.minimap
                imgui.text(f"Mode: {config.get('mode')}")
                if entry.kind == "blank":
                    _, config["width"] = imgui.slider_int("Width (px)", int(config.get("width", 64)), 16, 72)
                    _, config["height"] = imgui.slider_int("Height (px)", int(config.get("height", 64)), 16, 75)
            navigation = doc.scene.navigation
            if navigation and navigation.get("points") and imgui.collapsing_header("Navigation"):
                imgui.text(f"{len(navigation['points'])} points, {len(navigation.get('links', []))} links")
            return
        if len(doc.selection) > 1:
            imgui.text(f"{len(doc.selection)} objects selected")
            imgui.text_disabled("Move, rotate or scale them together with the gizmo.")
            return
        family, record = doc.scene.record(doc.selection[0])
        model = insp.describe(record, family)
        self.draw_inspector(model)
        if family == "instances" and record.get("scale"):
            imgui.text(f"Scale: {record['scale']}")
        if record.get("disabled"):
            imgui.text_colored(imgui.ImVec4(1, 0.7, 0.2, 1), "Disabled in this project (source record kept).")
            if imgui.button("Enable"):
                c.run("enable", doc.enable, record["id"])

    def draw_inspector(self, model: insp.Inspector) -> None:
        c = self.controller
        imgui.text(model.title)
        if model.record_id:
            imgui.text_disabled(model.record_id)
        if model.read_only_reason:
            imgui.text_colored(imgui.ImVec4(1, 0.6, 0.3, 1), f"Read-only: {model.read_only_reason}")
        for control in model.controls:
            key = f"{model.record_id}:{control.name}"
            imgui.begin_disabled(not control.editable)
            if control.kind == "position":
                value = self.pending_values.get(key, [float(v) for v in control.value])
                changed, value = imgui.input_float3(f"{control.label}##{key}", value)
                self.pending_values[key] = value
                if imgui.is_item_deactivated_after_edit():
                    self.pending_values.pop(key, None)
                    c.run("move", c.document.set_transform, model.record_id, value, None)
            elif control.kind == "angle":
                degrees = self.pending_values.get(key, control.value * 360.0 / 65536)
                changed, degrees = imgui.input_float(f"{control.label} (deg)##{key}", degrees, 15.0, 90.0)
                self.pending_values[key] = degrees
                if imgui.is_item_deactivated_after_edit() or (changed and not imgui.is_item_active()):
                    self.pending_values.pop(key, None)
                    c.run("rotate", c.document.set_transform, model.record_id, None, int(round(degrees * 65536 / 360)) & 0xFFFF)
            elif control.kind == "bounds":
                lo, hi = control.value
                planar = lo[1] is None
                lo_v = self.pending_values.get(key + ":lo", [float(v or 0) for v in lo])
                hi_v = self.pending_values.get(key + ":hi", [float(v or 0) for v in hi])
                _, lo_v = imgui.input_float3(f"Min##{key}", lo_v)
                done = imgui.is_item_deactivated_after_edit()
                _, hi_v = imgui.input_float3(f"Max##{key}", hi_v)
                done = done or imgui.is_item_deactivated_after_edit()
                self.pending_values[key + ":lo"], self.pending_values[key + ":hi"] = lo_v, hi_v
                if done:
                    self.pending_values.pop(key + ":lo", None)
                    self.pending_values.pop(key + ":hi", None)
                    if planar:
                        lo_v[1] = hi_v[1] = None
                    c.run("collision", c.document.set_collision, model.record_id, lo_v, hi_v)
            elif control.kind == "colour":
                imgui.color_edit3(f"{control.label}##{key}", [v / 255 for v in control.value])
            elif control.kind == "enum":
                options = control.options
                current = options.get(str(control.value), str(control.value))
                if imgui.begin_combo(f"{control.label}##{key}", f"{control.value}: {current}"):
                    for option, label in options.items():
                        head = option.split(" ")[0]
                        value = int(head) if head.lstrip("-").isdigit() else None
                        if imgui.selectable(f"{option}: {label}", str(value) == str(control.value))[0] and value is not None:
                            self.commit_field(model.record_id, control, value)
                    imgui.end_combo()
            elif control.kind == "bitmask":
                imgui.text(f"{control.label}  ({control.value:#06x})")
                for bit in range(control.bits):
                    if bit % 5:
                        imgui.same_line()
                    on = bool(control.value >> bit & 1)
                    changed, on = imgui.checkbox(f"{bit}##{key}{bit}", on)
                    if changed:
                        self.commit_field(model.record_id, control, control.value ^ (1 << bit))
            elif control.kind == "bool":
                changed, value = imgui.checkbox(f"{control.label}##{key}", bool(control.value))
                if changed:
                    self.commit_field(model.record_id, control, int(value))
            elif control.kind == "record":
                imgui.text(f"{control.label}: {control.value or 'none'}")
                doc = c.document
                if control.editable and doc and len(doc.selection) == 1:
                    imgui.same_line()
                    if imgui.small_button(f"Clear##{key}"):
                        self.commit_field(model.record_id, control, None)
                if control.editable and imgui.begin_drag_drop_target():
                    imgui.end_drag_drop_target()
                if control.editable and c.document:
                    candidates = [r["id"] for f, r in c.document.scene.records() if r["id"] != model.record_id]
                    current = candidates.index(control.value) if control.value in candidates else -1
                    if imgui.begin_combo(f"Target##{key}", control.value or "choose a record"):
                        for candidate in candidates[:500]:
                            if imgui.selectable(candidate, candidate == control.value)[0]:
                                self.commit_field(model.record_id, control, candidate)
                        imgui.end_combo()
            elif control.kind in ("int", "slot", "float", "text"):
                text = self.pending_values.get(key, "" if control.value is None else str(control.value))
                _, text = imgui.input_text(f"{control.label}##{key}", text)
                self.pending_values[key] = text
                if imgui.is_item_deactivated_after_edit():
                    self.pending_values.pop(key, None)
                    self.commit_field(model.record_id, control, text)
            else:
                imgui.text(f"{control.label}: {control.value}")
            imgui.end_disabled()
            if imgui.is_item_hovered(imgui.HoveredFlags_.allow_when_disabled):
                tip = [control.meaning or control.label]
                if control.range:
                    tip.append(f"range {list(control.range)} {control.unit}".rstrip())
                if control.evidence:
                    tip.append(f"evidence: {control.evidence}")
                if not control.editable:
                    tip.append(f"not editable: {control.reason}")
                imgui.set_tooltip("\n".join(tip))
        if self.field_error:
            imgui.text_colored(imgui.ImVec4(1, 0.35, 0.35, 1), self.field_error)
        for note in model.notes:
            if note:
                imgui.text_wrapped(note)

    def commit_field(self, record_id: str, control: insp.Control, raw) -> None:
        try:
            value = insp.parse_value(control, raw)
            self.controller.document.set_field(record_id, control.name, value)
            self.field_error = None
            self.controller.request_diagnostics()
        except EditError as error:
            self.field_error = f"{control.name}: {error}"

    def panel_assets(self) -> None:
        c = self.controller
        if c.index is None:
            imgui.text_disabled("No catalog loaded")
            return
        _, self.search = imgui.input_text("Search", self.search)
        imgui.same_line()
        _, self.map_only = imgui.checkbox("This map", self.map_only)
        imgui.same_line()
        _, self.thumbnail_mode = imgui.checkbox("Thumbnails", self.thumbnail_mode)
        for label, attribute, options in (("Game", "filter_platform", [None] + sorted(GAMES)),
                                          ("Kind", "filter_kind", [None, "model", "texture", "gameplay", "environment"]),
                                          ("Category", "filter_category", [None] + c.index.categories())):
            imgui.same_line()
            imgui.set_next_item_width(150)
            current = getattr(self, attribute)
            if imgui.begin_combo(label, GAMES.get(current, current) if current else "All"):
                for option in options:
                    if imgui.selectable(GAMES.get(option, option) if option else "All", option == current)[0]:
                        setattr(self, attribute, option)
                imgui.end_combo()
        key = (self.search, self.filter_platform, self.filter_kind, self.filter_category, self.map_only,
               c.document.scene.version if c.document else None, id(c.index))
        if key != self.browser_key:
            if c.document:
                c.index.set_map(c.document.scene)
            self.browser_rows = c.index.filter(self.search, self.filter_platform, self.filter_kind, self.filter_category, self.map_only and c.document is not None)
            self.browser_key = key
            self.browser_generation += 1
            if self.thumbnails:
                self.thumbnails.cancel_older_than(self.browser_generation)
        rows = self.browser_rows
        imgui.text_disabled(f"{len(rows)} assets   click or drag into the viewport to place")
        if self.thumbnail_mode:
            self.thumbnail_grid(rows)
            return
        if imgui.begin_table("assets", 6, imgui.TableFlags_.scroll_y | imgui.TableFlags_.row_bg | imgui.TableFlags_.borders_inner_v | imgui.TableFlags_.resizable):
            for heading in ("Name", "Kind", "Category", "Game", "Origin", "ID / detail"):
                imgui.table_setup_column(heading)
            imgui.table_setup_scroll_freeze(0, 1)
            imgui.table_headers_row()
            clipper = imgui.ListClipper()
            clipper.begin(len(rows))
            while clipper.step():
                for i in range(clipper.display_start, clipper.display_end):
                    entry = rows[i]
                    imgui.table_next_row()
                    imgui.table_next_column()
                    imgui.begin_disabled(not entry.editable)
                    if imgui.selectable(f"{entry.label}##{entry.id}", False, imgui.SelectableFlags_.span_all_columns)[0]:
                        self.activate_asset(entry)
                    imgui.end_disabled()
                    self.drag_source(entry)
                    for value in (entry.kind, entry.category, GAMES.get(entry.platform, entry.platform or ""), entry.source,
                                  f"{entry.id}  {entry.detail}"):
                        imgui.table_next_column()
                        imgui.text(str(value))
            imgui.end_table()

    def drag_source(self, entry) -> None:
        if entry.editable and entry.kind in ("model", "gameplay") and imgui.begin_drag_drop_source():
            self.drag_payload = ("model", entry.model_id) if entry.kind == "model" else ("marker", entry.id)
            imgui.set_drag_drop_payload_py_id("BTGA_ASSET", 1)
            imgui.text(f"Place {entry.label}")
            imgui.end_drag_drop_source()

    def activate_asset(self, entry) -> None:
        if not entry.editable:
            self.controller.log("warning", f"{entry.label} is read-only: {entry.detail}")
        elif entry.kind == "model" and entry.source == "user":
            self.controller.frame_record(entry.id)  # a placed instance: select and frame it
        elif entry.kind == "model":
            self.try_placement("model", entry.id)
        elif entry.kind == "gameplay":
            self.try_placement("marker", entry.id)
        else:
            self.controller.log("info", f"{entry.id}: {entry.detail}")

    def thumbnail_grid(self, rows) -> None:
        size = 96
        per_row = max(1, int(imgui.get_content_region_avail().x // (size + 12)))
        models = [r for r in rows if r.kind == "model"]
        imgui.begin_child("thumbs")
        clipper = imgui.ListClipper()
        clipper.begin((len(models) + per_row - 1) // per_row, size + 24)
        while clipper.step():
            for line in range(clipper.display_start, clipper.display_end):
                for column in range(per_row):
                    index = line * per_row + column
                    if index >= len(models):
                        break
                    entry = models[index]
                    if column:
                        imgui.same_line()
                    imgui.begin_group()
                    model_id = entry.model_id
                    pixels = self.thumbnails.request(model_id, size, priority=line) if self.thumbnails and model_id else None
                    texture = self.thumbnail_texture(model_id, pixels) if model_id else None
                    if texture:
                        clicked = imgui.image_button(f"##t{entry.id}", imgui.ImTextureRef(texture), imgui.ImVec2(size, size))
                    else:
                        clicked = imgui.button(f"...##t{entry.id}", imgui.ImVec2(size + 8, size + 8))
                    if clicked:
                        self.activate_asset(entry)
                    self.drag_source(entry)
                    imgui.text_disabled(entry.label[:12])
                    imgui.end_group()
        imgui.end_child()

    def thumbnail_texture(self, model_id: str, pixels) -> int | None:
        if model_id in self.thumb_textures:
            return self.thumb_textures[model_id]
        if pixels is None:
            return None
        from OpenGL import GL as gl

        width, height, rgba = pixels
        texture = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, width, height, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, rgba)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        self.thumb_textures[model_id] = texture
        if len(self.thumb_textures) > 512:  # bounded GPU thumbnail set
            old = next(iter(self.thumb_textures))
            gl.glDeleteTextures(1, [self.thumb_textures.pop(old)])
        return texture

    def panel_output(self) -> None:
        c = self.controller
        if imgui.begin_tab_bar("output"):
            if imgui.begin_tab_item(f"Validation ({len(c.diagnostics)})###validation")[0]:
                if imgui.begin_table("diag", 4, imgui.TableFlags_.scroll_y | imgui.TableFlags_.row_bg | imgui.TableFlags_.resizable):
                    for heading in ("Severity", "Code", "Object . field", "Message"):
                        imgui.table_setup_column(heading)
                    imgui.table_headers_row()
                    for number, item in enumerate(c.diagnostics):
                        imgui.table_next_row()
                        imgui.table_next_column()
                        colour = imgui.ImVec4(1, 0.4, 0.4, 1) if item.severity == "error" else imgui.ImVec4(1, 0.8, 0.3, 1)
                        imgui.text_colored(colour, item.severity)
                        imgui.table_next_column()
                        if imgui.selectable(f"{item.code}##d{number}", False, imgui.SelectableFlags_.span_all_columns)[0] and item.target:
                            c.frame_record(item.target)
                        imgui.table_next_column()
                        imgui.text(f"{item.target or '-'}{'.' + item.field if item.field else ''}")
                        imgui.table_next_column()
                        imgui.text(item.message)
                    imgui.end_table()
                imgui.end_tab_item()
            if imgui.begin_tab_item("Output")[0]:
                for level, text in c.messages[-200:]:
                    colour = {"error": imgui.ImVec4(1, 0.4, 0.4, 1), "warning": imgui.ImVec4(1, 0.8, 0.3, 1)}.get(level, imgui.ImVec4(0.85, 0.85, 0.85, 1))
                    imgui.text_colored(colour, text)
                imgui.end_tab_item()
            imgui.end_tab_bar()

    def panel_minimap(self) -> None:
        c = self.controller
        if c.document is None:
            return
        scene = c.document.scene
        minimap = (scene.base.get("minimap") or {})
        texture = None
        if minimap.get("selections"):
            info = c.library.minimap(scene.level_id) if scene.level_id in c.library._levels else {}
            texture = info.get("texture")
        if texture is None:
            self.generated_minimap(scene)
            return
        key = f"minimap:{texture.id}"
        if key not in self.thumb_textures:
            self.thumbnail_texture(key, (texture.width, texture.height, texture.rgba))
        zoom = max(1.0, min(4.0, imgui.get_content_region_avail().x / texture.width))
        origin = imgui.get_cursor_screen_pos()
        imgui.image(imgui.ImTextureRef(self.thumb_textures[key]), imgui.ImVec2(texture.width * zoom, texture.height * zoom))
        draw = imgui.get_window_draw_list()

        def plot(x, z, colour, radius=3.0):
            point = c.library.world_to_minimap(info, x, z)
            if point:
                draw.add_circle_filled(imgui.ImVec2(origin.x + (point[0] + 0.5) * zoom, origin.y + (point[1] + 0.5) * zoom), radius, colour)

        plot(c.camera.position[0], c.camera.position[2], imgui.get_color_u32(imgui.ImVec4(1, 1, 1, 1)), 4)
        for record_id in c.document.selection:
            record = c.document.record(record_id)
            plot(record["translation"][0], record["translation"][2], imgui.get_color_u32(imgui.ImVec4(1, 0.9, 0.2, 1)))
        imgui.text_disabled(minimap.get("transform") or "")

    def generated_minimap(self, scene) -> None:
        """Overhead orthographic render for maps without a game minimap (blank maps, PS1, BattleTanx).

        Same orientation as the games' minimaps: +X to the right, +Z down.  Re-rendered only when
        the map, hidden set, selection, layers or panel size change.
        """

        bounds = scene.bounds
        if not bounds or self.renderer is None:
            imgui.text_wrapped("This map has no limits, so no overview can be drawn.")
            return
        c = self.controller
        avail = imgui.get_content_region_avail()
        side = int(max(64, min(avail.x, avail.y - 22)))
        key = (scene.version, tuple(sorted(c.document.hidden)), tuple(sorted(c.document.selection)),
               tuple(sorted(self.settings.preferences.layers.items())), side, id(scene))
        if getattr(self, "_overview_key", None) != key:
            result = self.renderer.render_top_down(side, set(c.document.selection))
            if result is None:
                return
            self._overview_texture, self._overview_camera = result
            self._overview_key = key
        camera = self._overview_camera
        origin = imgui.get_cursor_screen_pos()
        imgui.image(imgui.ImTextureRef(self._overview_texture), imgui.ImVec2(side, side), imgui.ImVec2(0, 1), imgui.ImVec2(1, 0))
        hovered = imgui.is_item_hovered()
        draw = imgui.get_window_draw_list()

        def to_screen(x, z):
            px, py = camera.to_pixel(x, z, side, side)
            return imgui.ImVec2(origin.x + px, origin.y + py)

        draw.add_rect(origin, imgui.ImVec2(origin.x + side, origin.y + side), imgui.get_color_u32(imgui.ImVec4(1, 1, 1, 0.8)))
        for record_id in c.document.selection:
            record = c.document.record(record_id)
            draw.add_circle(to_screen(record["translation"][0], record["translation"][2]), 5, imgui.get_color_u32(imgui.ImVec4(1, 0.9, 0.2, 1)), 0, 2)
        eye = c.camera.position
        forward = c.camera.forward()
        flat = np.array([forward[0], forward[2]])
        if np.linalg.norm(flat) > 1e-6:
            flat = flat / np.linalg.norm(flat)
            reach = (bounds["x"][1] - bounds["x"][0]) * 0.08
            for angle in (-0.5, 0.5):
                cos, sin = math.cos(angle), math.sin(angle)
                direction = (flat[0] * cos - flat[1] * sin, flat[0] * sin + flat[1] * cos)
                draw.add_line(to_screen(eye[0], eye[2]), to_screen(eye[0] + direction[0] * reach, eye[2] + direction[1] * reach),
                              imgui.get_color_u32(imgui.ImVec4(1, 1, 1, 0.9)), 1.5)
        draw.add_circle_filled(to_screen(eye[0], eye[2]), 4, imgui.get_color_u32(imgui.ImVec4(1, 1, 1, 1)))
        if hovered and imgui.is_mouse_clicked(imgui.MouseButton_.left):
            mouse = imgui.get_mouse_pos()
            x = camera.x0 + (mouse.x - origin.x) / side * (camera.x1 - camera.x0)
            z = camera.z0 + (mouse.y - origin.y) / side * (camera.z1 - camera.z0)
            c.camera.position = np.array([x, c.camera.position[1], z]) - np.array([forward[0], 0.0, forward[2]]) * max(c.camera.position[1], 1.0)
        imgui.text_disabled("Overhead view (no game minimap). Click to move the camera.")

    # -- frame -----------------------------------------------------------------------------
    @staticmethod
    def runner():
        """hello_imgui's runner parameters, or None when the UI is driven off-screen."""

        try:
            return hello_imgui.get_runner_params()
        except Exception:
            return None

    def show_gui(self) -> None:
        c = self.controller
        if self.thumbnails:
            self.thumbnails.pump()
        library_before = c.library
        c.tick()
        if c.library is not library_before:
            self.attach_library()
        runner = self.runner()
        if runner is not None and runner.app_shall_exit and c.dirty and not self.confirm_close:
            runner.app_shall_exit = False
            self.pending_open = ("exit", None)
            self.confirm_close = True
        title = "BattleTanx Studio"
        if c.project:
            title += f" - {c.project.project['title']}{' *' if c.dirty else ''}"
        self.title = title
        self.window_new_project()
        self.window_new_map()
        self.window_definitions()
        self.window_sources()
        self.window_controls()
        self.window_recovery()
        if self.confirm_close:
            imgui.open_popup("Unsaved changes")
        if imgui.begin_popup_modal("Unsaved changes", None, imgui.WindowFlags_.always_auto_resize)[0]:
            imgui.text("This project has unsaved changes.")
            if imgui.button("Save"):
                self.save()
                self.finish_pending(saved=not c.dirty)
                imgui.close_current_popup()
            imgui.same_line()
            if imgui.button("Discard"):
                self.finish_pending(saved=True)
                imgui.close_current_popup()
            imgui.same_line()
            if imgui.button("Cancel"):
                self.pending_open = None
                self.confirm_close = False
                imgui.close_current_popup()
            imgui.end_popup()

    def finish_pending(self, saved: bool) -> None:
        self.confirm_close = False
        pending, self.pending_open = self.pending_open, None
        if not saved or pending is None:
            return
        if self.controller.project is not None:
            self.controller.autosaver.discard(self.controller.project)
            self.controller.project.saved_text = projects.dumps(self.controller.project)  # treat the discarded state as clean
        self.perform(*pending)


def runner_params(app: StudioApp, hidden: bool = False) -> hello_imgui.RunnerParams:
    params = hello_imgui.RunnerParams()
    params.app_window_params.window_title = "BattleTanx Studio"
    params.app_window_params.window_geometry.size = (1600, 960)
    params.app_window_params.restore_previous_geometry = True
    params.app_window_params.hidden = hidden
    params.imgui_window_params.show_menu_bar = True
    params.imgui_window_params.show_status_bar = True
    params.imgui_window_params.default_imgui_window_type = hello_imgui.DefaultImGuiWindowType.provide_full_screen_dock_space
    params.imgui_window_params.enable_viewports = False
    params.ini_folder_type = hello_imgui.IniFolderType.absolute_path
    app.settings.directory.mkdir(parents=True, exist_ok=True)
    params.ini_filename = str(app.settings.layout_file)
    params.callbacks.show_menus = app.show_menus
    params.callbacks.show_gui = app.show_gui
    params.callbacks.post_init = app.post_init
    params.callbacks.before_exit = app.before_exit
    params.callbacks.show_status = lambda: imgui.text(
        f"{len(app.controller.diagnostics)} diagnostics   undo {len(app.controller.document.undo_stack) if app.controller.document else 0}")
    params.fps_idling.enable_idling = True
    splits = [
        hello_imgui.DockingSplit("MainDockSpace", "BottomSpace", imgui.Dir.down, 0.28),
        hello_imgui.DockingSplit("MainDockSpace", "LeftSpace", imgui.Dir.left, 0.18),
        hello_imgui.DockingSplit("MainDockSpace", "RightSpace", imgui.Dir.right, 0.24),
        hello_imgui.DockingSplit("RightSpace", "MinimapSpace", imgui.Dir.down, 0.35),
    ]
    windows = []
    for label, space, gui in (("Project & layers", "LeftSpace", app.panel_hierarchy), ("Viewport", "MainDockSpace", app.panel_viewport),
                              ("Inspector", "RightSpace", app.panel_inspector), ("Minimap", "MinimapSpace", app.panel_minimap),
                              ("Assets", "BottomSpace", app.panel_assets), ("Output", "BottomSpace", app.panel_output)):
        window = hello_imgui.DockableWindow()
        window.label, window.dock_space_name, window.gui_function = label, space, gui
        windows.append(window)
    params.docking_params.docking_splits = splits
    params.docking_params.dockable_windows = windows
    return params


def self_check(report: Path) -> int:
    """Exercise the packaged editor on starter content; never creates a window."""

    import json
    import platform
    import tempfile
    import time

    result = {"version": __version__, "platform": platform.platform(), "python": sys.version.split()[0], "steps": {}}
    try:
        import OpenGL.GL  # noqa: F401  (the renderer's binding must import in the frozen build)

        from tools.btga_studio.content.examples import build_example
        from tools.btga_studio.core.package import build_package, read_package
        from tools.btga_studio.gui import renderer  # noqa: F401

        with tempfile.TemporaryDirectory() as temp:
            start = time.perf_counter()
            settings = Settings(Path(temp) / "home ü")
            controller = EditorController(settings)
            project = build_example(controller.library)
            controller.project = project
            controller.switch_map("training_grounds")
            document = controller.document
            starter = controller.library.starter()
            placed = document.place_model(starter["models"]["cube"], (100.0, 0.0, 100.0))
            document.undo()
            document.redo()
            path = Path(temp) / "check ü.btgaproj"
            controller.save(path)
            reloaded = projects.load_project(path)
            package = build_package(reloaded, controller.library, __version__)
            read = read_package(package)
            controller.request_diagnostics(immediate=True)
            controller.poll_diagnostics(wait=True)
            shipped = Path(sys.executable).parent / "samples" / "starter-example.btgaproj"
            shipped_ok = None
            if getattr(sys, "frozen", False):  # the packaged example must load and equal a fresh build of it
                shipped_ok = projects.dumps(projects.load_project(shipped)) == projects.dumps(build_example(controller.library))
                if not shipped_ok:
                    raise RuntimeError("samples/starter-example.btgaproj differs from the built-in starter example")
            result["steps"] = {"maps": len(project.maps), "placed": placed, "package_bytes": len(package), "package_maps": len(read.maps),
                               "diagnostics": len(controller.diagnostics), "shipped_example": shipped_ok,
                               "seconds": round(time.perf_counter() - start, 3)}
            controller.shutdown()
        result["ok"] = True
    except Exception:
        result["ok"] = False
        result["error"] = traceback.format_exc()
    Path(report).write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="BattleTanxStudio", description="BattleTanx Studio map editor")
    parser.add_argument("--catalog", type=Path, help="extra neutral catalog folder for this session")
    parser.add_argument("--project", type=Path, help="project file to open")
    parser.add_argument("--portable", action="store_true", help="keep settings next to the program")
    parser.add_argument("--self-check", type=Path, metavar="REPORT", help="verify the installation without opening a window; write a JSON report")
    args = parser.parse_args(argv)
    if args.self_check:
        return self_check(args.self_check)
    settings = Settings.open(args.portable)
    app = StudioApp(settings, args.catalog, args.project)
    try:
        immapp.run(runner_params(app), add_ons_params=immapp.AddOnsParams(with_im_guizmo=True) if hasattr(immapp, "AddOnsParams") else None)
    except Exception:
        settings.directory.mkdir(parents=True, exist_ok=True)
        (settings.directory / "crash.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
