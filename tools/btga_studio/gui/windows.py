"""Product windows for the Studio app: welcome / Project Hub, new-project wizard, Source Manager,
gameplay editors, settings, about, help, dialogs, toolbar, status bar and context menus.

``ProductUI`` is a mixin for ``StudioApp``.  It holds no editing logic of its
own: projects go through ``core.hub`` and ``core.project``, sources through the
controller's background jobs, edits through ``EditorDocument`` (so they are
undoable and validated).  File pickers and desktop actions go through
``self.dialogs`` and ``core.desktop`` so the off-screen tests can drive every
window without real dialogs.
"""

from __future__ import annotations

import json
import re
import sys
import time
import traceback
from pathlib import Path

from imgui_bundle import imgui
from imgui_bundle import portable_file_dialogs as pfd

from tools.btga_studio import __version__
from tools.btga_studio.core import applog, desktop, hub
from tools.btga_studio.core import project as projects
from tools.btga_studio.core.document import EditError
from tools.btga_studio.core.prefabs import PrefabError, PrefabLibrary
from tools.btga_studio.core.tutorial import STEPS as TUTORIAL_STEPS
from tools.btga_studio.core.tutorial import Tutorial
from tools.btga_studio.gui.help import TOPICS

PROJECT_FILTER = ["BattleTanx Studio project", "*.btgaproj"]
PACKAGE_FILTER = ["BattleTanx content package", "*.btgapkg"]
SOURCE_FILTER = ["BattleTanx game files", "*.z64 *.v64 *.n64 *.cue *.bin *.iso *.img", "All files", "*"]
RED, AMBER, GREEN, GREY = (1.0, 0.42, 0.38, 1.0), (1.0, 0.78, 0.3, 1.0), (0.45, 0.9, 0.45, 1.0), (0.62, 0.62, 0.66, 1.0)
STATUS_COLOUR = {"supported": GREEN, "unknown": RED, "mismatched": RED, "incomplete": AMBER, "missing": AMBER}
STATUS_WORD = {"supported": "Ready", "unknown": "Not recognised", "mismatched": "Changed", "incomplete": "Incomplete", "missing": "Missing"}
GAMEPLAY_TABS = ("Map", "Spawns & teams", "Pickups & hazards", "Objectives & navigation", "Missions & campaigns", "Multiplayer",
                 "Minigames", "Tanks & gangs", "Minimap", "Prefabs")
TOOLS = (("select", "Select", "4", "Click objects to select them; drag to box-select. No handles are shown."),
         ("translate", "Move", "1", "Drag the arrows or planes to move the selection."),
         ("rotate", "Rotate", "2", "Drag the ring to turn the selection around its vertical axis."),
         ("scale", "Scale", "3", "Drag the handles to resize placed models."))


class Dialogs:
    """Native file pickers (blocking, modal).  Tests substitute an object with the same methods."""

    def open_file(self, title: str, filters: list[str]) -> Path | None:
        files = pfd.open_file(title, "", filters).result()
        return Path(files[0]) if files else None

    def select_folder(self, title: str, start: Path | None = None) -> Path | None:
        folder = pfd.select_folder(title, str(start) if start else "").result()
        return Path(folder) if folder else None

    def save_file(self, title: str, default: Path, filters: list[str]) -> Path | None:
        chosen = pfd.save_file(title, str(default), filters).result()
        return Path(chosen) if chosen else None


def _colour(rgba) -> imgui.ImVec4:
    return imgui.ImVec4(*rgba)


def tooltip(text: str) -> None:
    if imgui.is_item_hovered(imgui.HoveredFlags_.delay_short):
        imgui.set_tooltip(text)


_RECORD_ID = re.compile(r"\b(?:user|[a-z0-9_]+:[a-z]+):[0-9a-f]{6,}:(\d+)\b")


def friendly_ids(text: str) -> str:
    """Show record IDs the way the editor names objects ("object 17"); the full ID stays available in tooltips."""

    return _RECORD_ID.sub(lambda m: f"object {m.group(1)}", str(text))


def badge_position(rect, towards, size, screen):
    """Where to draw a tour step's number badge: (x, y, point where the connector ends).

    The badge sits on the target outline's corner nearest the tour window, so it covers only that corner; on large
    targets (the 3D view) it goes inside the bottom-left corner, clear of the status text at the top.  Kept on screen.
    """

    x0, y0, x1, y1 = rect
    width, height = size
    if y1 - y0 > 6 * height and x1 - x0 > 6 * width:
        return x0 + 10, y1 - height - 10, ((x0 + x1) / 2, y1 + 4)
    corners = [(x0 - 4, y0 - 4), (x1 + 4, y0 - 4), (x0 - 4, y1 + 4), (x1 + 4, y1 + 4)]
    cx, cy = min(corners, key=lambda p: (p[0] - towards[0]) ** 2 + (p[1] - towards[1]) ** 2)
    x = min(max(cx - width / 2, 4.0), screen[0] - width - 8)
    y = min(max(cy - height / 2, 4.0), screen[1] - height - 8)
    return x, y, (cx, cy)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} bytes"


class ProductUI:
    # -- state --------------------------------------------------------------------------------
    def init_product(self) -> None:
        self.dialogs = Dialogs()
        self.show_hub = False
        self.hub_entries: list[hub.RecentEntry] | None = None
        self.hub_refresh = True
        self.show_wizard = False
        self.wizard_state = {"title": "My map", "template": 0, "size": 1, "players": 4, "mode": 0, "folder": None, "error": None}
        self.show_gameplay = False
        self.gameplay_tab = 0
        self.show_settings = False
        self.show_about = False
        self.show_help = False
        self.help_topic = 0
        self.errors: list[dict] = []
        self.confirmation: dict | None = None
        self.export_result: dict | None = None
        self.identified = []  # Source objects checked but not yet registered
        self.prefab_title = "My prefab"
        self.rename_state: dict | None = None
        self.context_record: str | None = None
        self.right_press = None
        self.right_travel = 0.0
        self.prefabs = PrefabLibrary(self.settings.prefabs_directory)
        self.frame_errors = 0
        self.gameplay_error: str | None = None
        self.anchors: dict[str, tuple[float, float, float, float]] = {}
        self.previous_anchors: dict[str, tuple[float, float, float, float]] = {}
        self.tutorial = Tutorial(self.settings.preferences)
        self.save_count = 0
        self.export_count = 0
        self.environment = None  # tests set an Environment to show installed or portable mode
        self.validation_checks = 0

    # -- guided tour ---------------------------------------------------------------------------
    def tutorial_facts(self) -> dict:
        c = self.controller
        doc = c.document
        markers = [r.get("category") for _, r in doc.scene.records("markers")] if doc else []
        errors = sum(1 for d in c.diagnostics if d.severity == "error")
        camera = (tuple(round(float(v), 1) for v in c.camera.position), round(c.camera.yaw, 3), round(c.camera.pitch, 3)) if doc else None
        return {"project": c.project is not None, "camera": camera, "selection": len(doc.selection) if doc else 0,
                "transforms": sum(1 for t in doc.undo_stack if t.label in ("Transform", "Move")) if doc else 0,
                "spawns": sum(1 for m in markers if m in ("player_spawn", "team_start")), "pickups": markers.count("pickup"),
                "objectives": markers.count("objective"), "gameplay_open": bool(self.show_gameplay),
                "validated": bool(doc) and c.diagnostics_version == doc.scene.version and errors == 0,
                "checks": self.validation_checks,
                "saved": c.project is not None and c.project_path is not None and not c.dirty, "saves": self.save_count,
                "exports": self.export_count}

    def window_tutorial(self) -> None:
        tour = self.tutorial
        if not tour.active:
            return
        if tour.update(self.tutorial_facts()) and tour.finished:
            self.inform("Tour complete", "You made, checked, saved and exported a map. Help > Guided tour runs it again any time.")
            return
        step = tour.step
        if step is None:
            return
        number = self.settings.preferences.tutorial_step + 1
        viewport = imgui.get_main_viewport()
        imgui.set_next_window_pos(imgui.ImVec2(viewport.work_pos.x + viewport.work_size.x - 16, viewport.work_pos.y + viewport.work_size.y - 40),
                                  imgui.Cond_.always, imgui.ImVec2(1.0, 1.0))
        imgui.set_next_window_size(imgui.ImVec2(380, 0))
        flags = imgui.WindowFlags_.no_collapse | imgui.WindowFlags_.no_docking | imgui.WindowFlags_.no_focus_on_appearing \
            | imgui.WindowFlags_.no_saved_settings
        opened, keep = imgui.begin("Guided tour###tour", True, flags)
        if opened:
            imgui.text(f"Step {number} of {len(TUTORIAL_STEPS)}: {step.title}")
            if step.anchor:
                imgui.text_disabled(f"Look for the outlined control marked {number}.")
            imgui.push_text_wrap_pos(0.0)
            imgui.text_wrapped(step.text)
            anchors = {**self.previous_anchors, **self.anchors}
            if step.anchor and step.anchor not in anchors:
                imgui.text_colored(_colour(AMBER), "Can't see it? " + step.hidden_hint)
                if "Reset window layout" in step.hidden_hint and imgui.small_button("Reset window layout"):
                    self.reset_layout()
            imgui.pop_text_wrap_pos()
            if step.key == "open" and self.controller.project is None and imgui.button("Open the starter example"):
                self.request("open_example")
            if imgui.small_button("Back") and number > 1:
                tour.back()
            imgui.same_line()
            if imgui.small_button("Skip this step"):
                tour.skip()
            imgui.same_line()
            if imgui.small_button("Close tour"):
                tour.stop()
            box = imgui.get_window_pos(), imgui.get_window_size()
        imgui.end()
        if not keep:
            tour.stop()
            return
        target = {**self.previous_anchors, **self.anchors}.get(step.anchor) if step.anchor else None
        if target and opened:
            self.draw_pointer(target, number, box)

    def draw_pointer(self, rect, number: int, box) -> None:
        """Outline the target, connect it to the tour window and mark it with the step number (shape, line and text,
        not colour alone)."""

        draw = imgui.get_foreground_draw_list()
        yellow, dark = imgui.get_color_u32(imgui.ImVec4(1.0, 0.85, 0.2, 1.0)), imgui.get_color_u32(imgui.ImVec4(0.05, 0.05, 0.05, 0.92))
        x0, y0, x1, y1 = rect
        draw.add_rect(imgui.ImVec2(x0 - 4, y0 - 4), imgui.ImVec2(x1 + 4, y1 + 4), yellow, 4.0, 3.0)
        label = str(number)  # the full "Step N" wording is in the tour window; the badge repeats only the number
        size = imgui.calc_text_size(label)
        bx, by, bw = box[0].x, box[0].y, box[1].x
        screen = imgui.get_main_viewport().size
        lx, ly, anchor = badge_position(rect, (bx + bw / 2, by), (size.x, size.y), (screen.x, screen.y))
        draw.add_line(imgui.ImVec2(bx + bw / 2, by), imgui.ImVec2(*anchor), yellow, 2.0)
        draw.add_rect_filled(imgui.ImVec2(lx - 4, ly - 2), imgui.ImVec2(lx + size.x + 4, ly + size.y + 2), dark, 3.0)
        draw.add_rect(imgui.ImVec2(lx - 4, ly - 2), imgui.ImVec2(lx + size.x + 4, ly + size.y + 2), yellow, 3.0)
        draw.add_text(imgui.ImVec2(lx, ly), yellow, label)

    def start_tour(self) -> None:
        self.tutorial.start(from_beginning=self.tutorial.finished or not self.settings.preferences.tutorial_active)
        self.show_hub = False

    def anchor(self, name: str) -> None:
        """Remember where the last drawn control is on screen this frame (used by the guided tour to point at it)."""

        low, high = imgui.get_item_rect_min(), imgui.get_item_rect_max()
        self.anchors[name] = (low.x, low.y, high.x, high.y)

    def bring_to_front(self, name: str, visible: bool) -> None:
        """Focus a floating window on the frame it opens, so it is never hidden behind the docked panels."""

        shown = getattr(self, "_shown_windows", set())
        if visible and name not in shown:
            imgui.set_next_window_focus()
        (shown.add if visible else shown.discard)(name)
        self._shown_windows = shown

    # -- dialogs ---------------------------------------------------------------------------------
    def report_error(self, title: str, message: str, detail: str | None = None, actions: tuple = ()) -> None:
        """Show a plain-language error dialog; the technical detail goes to the log only."""

        self.errors.append({"title": title, "message": message, "actions": actions})
        applog.write(self.controller.logger, "error", "dialog", f"{title}: {message}", detail=detail or "")

    def inform(self, title: str, message: str) -> None:
        self.errors.append({"title": title, "message": message, "actions": (), "kind": "info"})

    def window_error(self) -> None:
        if not self.errors:
            return
        error = self.errors[0]
        info = error.get("kind") == "info"
        imgui.open_popup("Message###error")
        imgui.set_next_window_size(imgui.ImVec2(520, 0), imgui.Cond_.appearing)
        if imgui.begin_popup_modal("Message###error", None, imgui.WindowFlags_.always_auto_resize)[0]:
            imgui.text_colored(_colour(GREEN if info else RED), error["title"])
            imgui.push_text_wrap_pos(500)
            imgui.text_wrapped(error["message"])
            imgui.pop_text_wrap_pos()
            imgui.spacing()
            chosen = None
            for label, action in error["actions"]:
                if imgui.button(label):
                    chosen = action
                imgui.same_line()
            if not info:
                if imgui.button("Open logs folder"):
                    self.safe_desktop(desktop.open_folder, self.settings.logs_directory)
                imgui.same_line()
            if imgui.button("OK") or imgui.is_key_pressed(imgui.Key.escape) or chosen is not None:
                self.errors.pop(0)
                imgui.close_current_popup()
            imgui.end_popup()
            if chosen is not None:
                self.attempt(error["title"], chosen)

    def confirm(self, title: str, message: str, button: str, action) -> None:
        self.confirmation = {"title": title, "message": message, "button": button, "action": action}

    def window_confirm(self) -> None:
        if self.confirmation is None:
            return
        item = self.confirmation
        imgui.open_popup(f"{item['title']}###confirm")
        if imgui.begin_popup_modal(f"{item['title']}###confirm", None, imgui.WindowFlags_.always_auto_resize)[0]:
            imgui.push_text_wrap_pos(480)
            imgui.text_wrapped(item["message"])
            imgui.pop_text_wrap_pos()
            if imgui.button(item["button"]):
                self.confirmation = None
                imgui.close_current_popup()
                self.attempt(item["title"], item["action"])
            imgui.same_line()
            if imgui.button("Cancel") or imgui.is_key_pressed(imgui.Key.escape):
                self.confirmation = None
                imgui.close_current_popup()
            imgui.end_popup()

    def attempt(self, what: str, action, *args) -> object:
        """Run a user action; failures become an error dialog, never an exception out of the frame."""

        try:
            return action(*args)
        except (EditError, projects.ProjectError, hub.HubError, PrefabError, OSError, ValueError) as error:
            self.report_error(f"{what} did not work", str(error), traceback.format_exc())
        return None

    def safe_desktop(self, action, target) -> None:
        try:
            action(target)
        except (OSError, ValueError) as error:
            self.report_error("Could not open it", str(error))

    # -- Project Hub / welcome ------------------------------------------------------------------
    def open_hub(self) -> None:
        self.show_hub = True
        self.hub_refresh = True

    def window_hub(self) -> None:
        self.bring_to_front("hub", self.show_hub)
        if not self.show_hub:
            return
        c = self.controller
        prefs = self.settings.preferences
        if self.hub_refresh or self.hub_entries is None:
            self.hub_entries = hub.recent_entries(prefs, c.library)
            self.hub_refresh = False
        viewport = imgui.get_main_viewport()
        width = min(900, viewport.work_size.x - 40)
        imgui.set_next_window_pos(imgui.ImVec2(viewport.work_pos.x + viewport.work_size.x / 2, viewport.work_pos.y + viewport.work_size.y * 0.42),
                                  imgui.Cond_.appearing, imgui.ImVec2(0.5, 0.5))
        imgui.set_next_window_size_constraints(imgui.ImVec2(width, 0), imgui.ImVec2(width, viewport.work_size.y - 40))
        first = not self.hub_entries and not prefs.welcome_seen
        title = "Welcome to BattleTanx Studio###hub" if first else "Project Hub###hub"
        opened, self.show_hub = imgui.begin(title, self.show_hub, imgui.WindowFlags_.no_collapse | imgui.WindowFlags_.no_docking
                                            | imgui.WindowFlags_.always_auto_resize)
        if opened:
            if first:
                imgui.text_wrapped("Build maps, missions and multiplayer arenas for BattleTanx. You can start right away with Studio's "
                                   "own original content; no game files are needed.")
                imgui.spacing()
            width = (imgui.get_content_region_avail().x - 24) / 4
            for label, hint, action in (("Create Original Project", "Start a new project from a template using Studio's own content.",
                                         self.open_wizard),
                                        ("Import From My Games", "Use maps and models from BattleTanx games you own.", self.import_from_games),
                                        ("Open Existing Project", "Open a .btgaproj file from your computer.", self.browse_project),
                                        ("Open Example Project", "Explore a finished example made only from Studio content.", self.open_example)):
                if imgui.button(label, imgui.ImVec2(width, 56)):
                    self.attempt(label, action)
                tooltip(hint)
                imgui.same_line()
            imgui.new_line()
            imgui.text_disabled("New here? ")
            imgui.same_line()
            if imgui.small_button("Take the guided tour"):
                self.start_tour()
            tooltip("A short, skippable walk through making your first map with Studio's own content.")
            imgui.same_line()
            if imgui.small_button("Getting started"):
                self.open_help(0)
            imgui.same_line()
            if imgui.small_button("Which game files can I use?"):
                self.open_help(1)
            imgui.separator()
            self.recent_table()
            imgui.separator()
            recoveries = c.recoveries()
            if recoveries:
                imgui.text_colored(_colour(AMBER), f"Unsaved work from an earlier session: {len(recoveries)}")
                imgui.same_line()
                if imgui.small_button("Review..."):
                    self.show_recovery = True
            _, prefs.show_hub_at_start = imgui.checkbox("Show this window when Studio starts", prefs.show_hub_at_start)
        imgui.end()
        if not self.show_hub:
            prefs.welcome_seen = True

    def recent_table(self) -> None:
        prefs = self.settings.preferences
        entries = self.hub_entries or []
        if not entries:
            imgui.text_disabled("No recent projects yet. Projects you create or open will appear here.")
            return
        imgui.text("Recent projects")
        if imgui.begin_table("recent", 4, imgui.TableFlags_.row_bg | imgui.TableFlags_.borders_inner_h | imgui.TableFlags_.scroll_y,
                             imgui.ImVec2(0, min(300, 28 + 24 * len(entries)))):
            imgui.table_setup_column("Project", imgui.TableColumnFlags_.width_stretch, 3)
            imgui.table_setup_column("Status", imgui.TableColumnFlags_.width_stretch, 3)
            imgui.table_setup_column("Modified", imgui.TableColumnFlags_.width_fixed, 120)
            imgui.table_setup_column("##actions", imgui.TableColumnFlags_.width_fixed, 250)
            imgui.table_headers_row()
            for number, entry in enumerate(entries):
                imgui.table_next_row()
                imgui.table_next_column()
                label = ("[pinned] " if entry.pinned else "") + entry.title
                if imgui.selectable(f"{label}##recent{number}", False, imgui.SelectableFlags_.allow_double_click)[0] and entry.exists \
                        and not entry.error:
                    self.request("open_path", str(entry.path))
                tooltip(f"{entry.path.name}\nin {entry.path.parent.name}")
                imgui.table_next_column()
                if entry.error:
                    imgui.text_colored(_colour(RED if entry.exists else AMBER), "Cannot open" if entry.exists else "File moved or deleted")
                    tooltip(entry.error)
                elif entry.warnings:
                    imgui.text_colored(_colour(AMBER), f"Needs {len(entry.warnings)} missing item(s)")
                    tooltip("\n".join(entry.warnings) + "\nRegister the game it came from in the Source Manager.")
                else:
                    imgui.text_colored(_colour(GREEN), "Ready")
                imgui.table_next_column()
                imgui.text(time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.modified)) if entry.modified else "-")
                imgui.table_next_column()
                if not entry.exists:
                    if imgui.small_button(f"Locate...##l{number}"):
                        self.attempt("Locate project", self.locate_recent, entry)
                    imgui.same_line()
                if imgui.small_button(f"{'Unpin' if entry.pinned else 'Pin'}##p{number}"):
                    prefs.set_pinned(str(entry.path), not entry.pinned)
                    self.hub_refresh = True
                imgui.same_line()
                if imgui.small_button(f"More##m{number}"):
                    imgui.open_popup(f"recent-menu-{number}")
                if imgui.begin_popup(f"recent-menu-{number}"):
                    if imgui.menu_item("Open containing folder", "", False, entry.path.parent.is_dir())[0]:
                        self.safe_desktop(desktop.open_folder, entry.path.parent)
                    if imgui.menu_item("Duplicate", "", False, entry.exists and not entry.error)[0]:
                        copy = self.attempt("Duplicate project", hub.duplicate, entry.path)
                        if copy:
                            prefs.add_recent(str(copy))
                            self.hub_refresh = True
                    if imgui.menu_item("Rename...", "", False, entry.exists and not entry.error and not self.is_open(entry.path))[0]:
                        self.rename_state = {"path": entry.path, "title": entry.title}
                    if imgui.menu_item("Remove from this list", "", False)[0]:
                        prefs.remove_recent(str(entry.path))
                        self.hub_refresh = True
                    imgui.end_popup()
            imgui.end_table()
        self.rename_popup()

    def rename_popup(self) -> None:
        if self.rename_state is None:
            return
        imgui.open_popup("Rename project")
        if imgui.begin_popup_modal("Rename project", None, imgui.WindowFlags_.always_auto_resize)[0]:
            _, self.rename_state["title"] = imgui.input_text("New name", self.rename_state["title"])
            imgui.text_disabled("The project file is renamed to match. Nothing else changes.")
            if imgui.button("Rename"):
                old = self.rename_state["path"]
                new = self.attempt("Rename project", hub.rename, old, self.rename_state["title"])
                if new:
                    self.settings.preferences.replace_recent(str(old), str(new))
                    self.hub_refresh = True
                self.rename_state = None
                imgui.close_current_popup()
            imgui.same_line()
            if imgui.button("Cancel"):
                self.rename_state = None
                imgui.close_current_popup()
            imgui.end_popup()

    def is_open(self, path: Path) -> bool:
        current = self.controller.project_path
        return current is not None and Path(current).resolve() == Path(path).resolve()

    def locate_recent(self, entry: hub.RecentEntry) -> None:
        chosen = self.dialogs.open_file(f"Find the moved project \"{entry.title}\"", PROJECT_FILTER)
        if chosen:
            hub.relink(self.settings.preferences, entry.path, chosen)
            self.hub_refresh = True

    def browse_project(self) -> None:
        self.request("open_project")

    def import_from_games(self) -> None:
        c = self.controller
        if c.library and [level for level in c.library.levels() if level.platform != "studio"]:
            self.wizard["map_kind"] = 1
            self.show_new_project = True
        else:
            self.show_sources = True
        self.show_hub = False

    # -- new-project wizard ---------------------------------------------------------------------
    def open_wizard(self) -> None:
        self.show_wizard = True
        self.wizard_state["error"] = None
        self.show_hub = False

    def window_wizard(self) -> None:
        self.bring_to_front("wizard", self.show_wizard)
        if not self.show_wizard:
            return
        state = self.wizard_state
        imgui.set_next_window_size(imgui.ImVec2(640, 0), imgui.Cond_.appearing)
        opened, self.show_wizard = imgui.begin("Create original project###wizard", self.show_wizard,
                                               imgui.WindowFlags_.no_collapse | imgui.WindowFlags_.no_docking)
        if opened:
            imgui.text("1. Name")
            _, state["title"] = imgui.input_text("Project name", state["title"])
            imgui.text("2. Starting point")
            for number, template in enumerate(hub.TEMPLATES):
                if imgui.radio_button(f"{template.title}##t{number}", state["template"] == number):
                    state["template"] = number
                tooltip(template.description)
                if number < len(hub.TEMPLATES) - 1:
                    imgui.same_line()
            template = hub.TEMPLATES[state["template"]]
            imgui.push_text_wrap_pos(0.0)
            imgui.text_disabled(template.description)
            imgui.pop_text_wrap_pos()
            imgui.text("3. Basic rules")
            imgui.begin_disabled(template.key == "example")
            sizes = list(hub.MAP_SIZES)
            _, state["size"] = imgui.combo("Map size", state["size"], sizes)
            imgui.end_disabled()
            if template.key == "example":
                imgui.text_disabled("The example keeps its own maps and sizes.")
            if template.multiplayer:
                _, state["players"] = imgui.slider_int("Players", state["players"], 2, 4)
                _, state["mode"] = imgui.combo("Game mode", state["mode"], list(hub.MODES))
            imgui.text("4. Where to save")
            folder = Path(state["folder"]) if state["folder"] else self.settings.projects_directory
            imgui.text_wrapped(f"Folder: {folder.name}")
            tooltip(str(folder))
            imgui.same_line()
            if imgui.small_button("Change..."):
                chosen = self.dialogs.select_folder("Choose where to save the project", folder)
                if chosen:
                    state["folder"] = str(chosen)
            imgui.text_disabled(f"File: {hub.unique_path(folder, state['title'] or 'Untitled').name}")
            if state["error"]:
                imgui.text_colored(_colour(RED), state["error"])
            imgui.separator()
            if imgui.button("Create project", imgui.ImVec2(160, 0)):
                self.create_from_wizard()
            imgui.same_line()
            if imgui.button("Cancel"):
                self.show_wizard = False
        imgui.end()

    def create_from_wizard(self) -> bool:
        c = self.controller
        state = self.wizard_state
        template = hub.TEMPLATES[state["template"]]
        folder = Path(state["folder"]) if state["folder"] else self.settings.projects_directory
        try:
            path, project = hub.create(c.library, state["title"].strip(), template.key, hub.MAP_SIZES[list(hub.MAP_SIZES)[state["size"]]],
                                       state["players"], hub.MODES[state["mode"]], folder)
        except (hub.HubError, projects.ProjectError, OSError) as error:
            state["error"] = str(error)
            return False
        self.show_wizard = False
        self.open_project(path)
        return True

    # -- Source Manager -------------------------------------------------------------------------
    def window_source_manager(self) -> None:
        self.bring_to_front("sources", self.show_sources)
        if not self.show_sources:
            return
        c = self.controller
        registry = c.registry
        imgui.set_next_window_size(imgui.ImVec2(980, 560), imgui.Cond_.appearing)
        opened, self.show_sources = imgui.begin("Source Manager###sources", self.show_sources, imgui.WindowFlags_.no_collapse)
        if opened:
            imgui.text_wrapped("Use maps and models from BattleTanx games you own. Your game files stay where they are: Studio reads them, "
                               "never changes them, and never copies them into projects or packages.")
            if imgui.small_button("Which files are supported?"):
                self.open_help(1)
            imgui.separator()
            if imgui.button("Add game file..."):
                chosen = self.dialogs.open_file("Choose a BattleTanx ROM or disc image", SOURCE_FILTER)
                if chosen:
                    self.check_source(chosen)
            tooltip("Nintendo 64 ROM (.z64 .v64 .n64) or PlayStation disc image (.cue .bin .iso)")
            imgui.same_line()
            if imgui.button("Add extracted disc folder..."):
                chosen = self.dialogs.select_folder("Choose the folder holding the PlayStation disc files (SLUS_010.44)")
                if chosen:
                    self.check_source(chosen)
            imgui.same_line()
            imgui.begin_disabled(not registry.sources)
            if imgui.button("Check all"):
                self.attempt("Check sources", c.verify_sources)
            tooltip("Confirm every registered file is still present and unchanged.")
            imgui.same_line()
            if imgui.button("Find moved files..."):
                folder = self.dialogs.select_folder("Search this folder for moved game files")
                if folder:
                    self.attempt("Find moved files", c.search_sources, folder)
            imgui.end_disabled()
            self.identified_panel()
            self.jobs_panel(("identify", "register", "verify", "search", "relink"))
            imgui.separator()
            if not registry.sources:
                imgui.spacing()
                imgui.text_disabled("No games added yet. Studio works fully without them, using its own starter content.")
                imgui.text_disabled("Choose Add game file... or Add extracted disc folder... above to use your own copy.")
            else:
                self.sources_table()
            imgui.separator()
            imgui.text_disabled(f"Catalogs on disk: {human_size(registry.disk_usage())}")
        imgui.end()

    def check_source(self, path: Path) -> None:
        def done(job):
            if job.state == "done":
                self.identified = [s for s in self.identified if s.path != job.result.path] + [job.result]
            elif job.state == "failed":
                self.report_error("This file cannot be used", job.error + "\n\nChoose a BattleTanx ROM, disc image or extracted disc folder.")

        self.attempt("Check file", self.controller.identify_source, path, done)

    def identified_panel(self) -> None:
        from tools.btga_studio.core.sources import describe

        for number, source in enumerate(list(self.identified)):
            facts = describe(source)
            imgui.separator()
            imgui.text_colored(_colour(STATUS_COLOUR.get(source.status, GREY)), f"{Path(source.path).name}: {facts['status']}")
            if imgui.begin_table(f"facts{number}", 2, imgui.TableFlags_.sizing_fixed_fit):
                for label in ("game", "platform", "region", "kind", "byte_order", "revision", "hash"):
                    imgui.table_next_row()
                    imgui.table_next_column()
                    imgui.text_disabled(label.replace("_", " ").capitalize())
                    imgui.table_next_column()
                    imgui.text(facts[label])
                imgui.end_table()
            if source.detail and source.status != "supported":
                imgui.text_wrapped(source.detail)
            if source.status == "supported":
                if imgui.button(f"Add to Studio##add{number}"):
                    self.attempt("Add source", self.controller.add_source_async, Path(source.path), self._registered)
                    self.identified.remove(source)
                imgui.same_line()
            if imgui.button(f"Dismiss##dismiss{number}"):
                self.identified.remove(source)

    def _registered(self, job) -> None:
        if job.state == "failed":
            self.report_error("The game could not be added", job.error)

    def jobs_panel(self, kinds) -> None:
        for job in [j for j in self.controller.jobs.jobs.values() if j.kind in kinds and not j.finished]:
            fraction = job.fraction
            imgui.text(job.label)
            imgui.same_line()
            imgui.progress_bar(fraction if fraction is not None else (time.monotonic() % 1.0), imgui.ImVec2(260, 0),
                               f"{job.stage} {job.done}/{job.total}" if job.total else (job.stage or "working"))
            imgui.same_line()
            if imgui.small_button(f"Cancel##job{job.id}"):
                job.cancel()

    def sources_table(self) -> None:
        from tools.btga_studio.core.sources import STATUS_TEXT, describe

        c = self.controller
        registry = c.registry
        if imgui.begin_table("sources", 5, imgui.TableFlags_.row_bg | imgui.TableFlags_.borders_inner_h | imgui.TableFlags_.resizable):
            for heading, weight in (("Game", 5), ("Health", 2), ("Catalog", 2), ("Size", 1), ("Actions", 3)):
                imgui.table_setup_column(heading, imgui.TableColumnFlags_.width_stretch, weight)
            imgui.table_headers_row()
            for source in sorted(registry.sources.values(), key=lambda s: s.title):
                facts = describe(source)
                imgui.table_next_row()
                imgui.table_next_column()
                imgui.text_wrapped(f"{facts['game']} ({facts['platform']})")
                tooltip(f"{facts['kind']}: {Path(source.path).name}\n{facts['hash']}")
                imgui.table_next_column()
                imgui.text_colored(_colour(STATUS_COLOUR.get(source.status, GREY)), STATUS_WORD.get(source.status, source.status))
                tooltip(STATUS_TEXT.get(source.status, "") + (f"\n{source.detail}" if source.detail else ""))
                imgui.table_next_column()
                job = c.builds.get(source.id)
                state, size = registry.catalog_state(source)
                if job:
                    imgui.progress_bar(job.fraction or 0.0, imgui.ImVec2(-1, 0), f"{job.stage} {job.done}/{job.total or '?'}")
                else:
                    imgui.text({"built": "Ready", "not built": "Not built yet", "damaged": "Damaged; rebuild"}[state])
                imgui.table_next_column()
                imgui.text(human_size(size) if size else "-")
                imgui.table_next_column()
                if job:
                    if imgui.small_button(f"Cancel##{source.id}"):
                        job.cancel()
                    continue
                imgui.begin_disabled(source.status != "supported")
                if imgui.small_button(f"{'Rebuild' if state == 'built' else 'Build catalog'}##{source.id}"):
                    self.attempt("Build catalog", c.build_catalog, source.id, False, self._built)
                imgui.end_disabled()
                tooltip("Read the game once and store its maps and models in your local app data.")
                imgui.same_line()
                if imgui.small_button(f"Relink...##{source.id}"):
                    chosen = (self.dialogs.select_folder("Find the moved folder") if source.kind == "folder"
                              else self.dialogs.open_file("Find the moved game file", SOURCE_FILTER))
                    if chosen:
                        self.attempt("Relink", c.relink_source, source.id, chosen,
                                     lambda job: job.state == "failed" and self.report_error("That is not the same game file", job.error))
                imgui.same_line()
                if imgui.small_button(f"Remove...##{source.id}"):
                    self.confirm("Remove game from Studio",
                                 f"Remove {facts['game']} ({facts['platform']}) from Studio and delete its catalog "
                                 f"({human_size(size)})?\n\nYour game file is not deleted or changed. Projects that use this game "
                                 "will ask for it again when opened.", "Remove", lambda sid=source.id: c.remove_source(sid))
            imgui.end_table()

    def _built(self, job) -> None:
        if job.state == "failed":
            self.report_error("The catalog could not be built", job.error + "\n\nThe previous catalog, if any, was kept.",
                              actions=(("Try again", lambda: self.controller.jobs.retry(job)),))
        elif job.state == "done":
            self.attach_library()

    # -- gameplay editors ------------------------------------------------------------------------
    def window_gameplay(self) -> None:
        self.bring_to_front("gameplay", self.show_gameplay)
        if not self.show_gameplay:
            return
        c = self.controller
        imgui.set_next_window_size(imgui.ImVec2(1080, 540), imgui.Cond_.appearing)
        opened, self.show_gameplay = imgui.begin("Gameplay editor###gameplay", self.show_gameplay, imgui.WindowFlags_.no_collapse)
        if opened:
            if c.project is None:
                imgui.text_disabled("Open or create a project to edit its gameplay.")
            else:
                self.history_buttons()
                if imgui.begin_tab_bar("gameplay-tabs", imgui.TabBarFlags_.fitting_policy_scroll):
                    for number, name in enumerate(GAMEPLAY_TABS):
                        flags = imgui.TabItemFlags_.set_selected if self._select_tab == number else 0
                        if imgui.begin_tab_item(name, None, flags)[0]:
                            self.gameplay_tab = number
                            getattr(self, "tab_" + name.split()[0].lower().replace("&", ""))()
                            imgui.end_tab_item()
                    self._select_tab = None
                    imgui.end_tab_bar()
        imgui.end()

    _select_tab = None

    def open_gameplay(self, tab: str) -> None:
        self.show_gameplay = True
        self._select_tab = self.gameplay_tab = GAMEPLAY_TABS.index(tab)

    def _records(self, categories) -> list[dict]:
        doc = self.controller.document
        if doc is None:
            return []
        return [r for _, r in doc.scene.records("markers") if r.get("category") in categories and not r.get("disabled")]

    def record_list(self, title: str, categories, template_kinds, empty: str) -> None:
        """A list of one kind of gameplay object with select / focus / add; fields are edited in the inspector."""

        c = self.controller
        doc = c.document
        if doc is None:
            imgui.text_disabled("Open a map first.")
            return
        records = self._records(categories)
        imgui.text(f"{title} on this map: {len(records)}")
        for kind in template_kinds:
            imgui.same_line()
            if imgui.small_button(f"Place {kind.replace('_', ' ')}"):
                template = next((t["id"] for t in (c.library.starter() or {}).get("templates", []) if t["source_kind"] == f"studio_{kind}"), None)
                if template:
                    self.try_placement("marker", template)
            tooltip("Then click in the viewport where it should go. Esc stops placing.")
        if not records:
            imgui.text_disabled(empty)
            return
        if imgui.begin_table(f"list-{title}", 4, imgui.TableFlags_.row_bg | imgui.TableFlags_.scroll_y, imgui.ImVec2(0, 320)):
            for heading in ("Object", "Position", "Details", ""):
                imgui.table_setup_column(heading)
            imgui.table_headers_row()
            for record in records:
                imgui.table_next_row()
                imgui.table_next_column()
                label = record.get("category", "").replace("_", " ")
                if imgui.selectable(f"{label}##{record['id']}", record["id"] in doc.selection, imgui.SelectableFlags_.span_all_columns)[0]:
                    c.frame_record(record["id"])
                imgui.table_next_column()
                x, _, z = record["translation"]
                imgui.text(f"{x:.0f}, {z:.0f}")
                imgui.table_next_column()
                fields = record.get("fields", {})
                summary = ", ".join(f"{name.replace('_', ' ')} {self._value_text(spec)}" for name, spec in sorted(fields.items())
                                    if spec.get("value") not in (None, "") and not name.startswith("link_"))[:80]
                imgui.text(summary or "-")
                imgui.table_next_column()
                if imgui.small_button(f"Edit##{record['id']}"):
                    c.frame_record(record["id"])
                tooltip("Select it; its typed fields appear in the Inspector panel.")
            imgui.end_table()
        imgui.text_disabled("Select an entry to edit its fields in the Inspector. Read-only fields explain why.")

    @staticmethod
    def _value_text(spec) -> str:
        value = spec.get("value")
        labels = spec.get("labels") or spec.get("values") or {}
        if isinstance(labels, dict) and str(value) in labels:
            return str(labels[str(value)])
        return str(value)

    # -- undoable definition edits -------------------------------------------------------------
    def undo(self) -> None:
        self.after_history(self.controller.undo())

    def redo(self) -> None:
        self.after_history(self.controller.redo())

    def after_history(self, entry) -> None:
        """Bring back the editor context of an undone or redone gameplay change (tab and map)."""

        from tools.btga_studio.core.history import Entry

        if not isinstance(entry, Entry):
            return
        context = entry.context or {}
        if context.get("map") and context["map"] != self.controller.active_map and context["map"] in self.controller.project.maps:
            self.switch_map(context["map"])
        if context.get("tab") in GAMEPLAY_TABS:
            self.open_gameplay(context["tab"])
        self.controller.log("info", f"{'restored' if entry in self.defs().undo_stack else 'undid'}: {entry.label}")

    def defs(self):
        return self.controller.project.definitions

    def change(self, label: str, mutate, coalesce: str | None = None, context: dict | None = None) -> bool:
        """Apply one definition edit through the project's undo history; invalid input is shown and never recorded."""

        context = {"tab": GAMEPLAY_TABS[self.gameplay_tab], "map": self.controller.active_map, **(context or {})}
        try:
            changed = self.defs().edit(label, mutate, coalesce, context)
        except (projects.ProjectError, hub.HubError, ValueError, KeyError) as error:
            self.gameplay_error = str(error)
            return False
        if changed:
            self.gameplay_error = None
        return changed

    def seal_if_done(self) -> None:
        if imgui.is_item_deactivated():
            self.defs().seal()

    def w_text(self, label: str, data: dict, key: str, history_label: str, multiline: bool = False, validate=None,
               anchor: str | None = None) -> None:
        current = str(data.get(key, "") or "")
        if multiline:
            changed, value = imgui.input_text_multiline(label, current, imgui.ImVec2(-1, 50))
        else:
            changed, value = imgui.input_text(label, current)
        if anchor:
            self.anchor(anchor)
        if changed:
            self.change(history_label, lambda: data.__setitem__(key, validate(value) if validate else value), coalesce=f"text:{label}")
        self.seal_if_done()

    def w_slider_int(self, label: str, data: dict, key: str, low: int, high: int, default: int, history_label: str) -> None:
        changed, value = imgui.slider_int(label, int(data.get(key, default)), low, high)
        if changed:
            self.change(history_label, lambda: data.__setitem__(key, int(value)), coalesce=f"slider:{label}")
        self.seal_if_done()

    def w_slider_float(self, label: str, data: dict, key: str, low: float, high: float, default: float, history_label: str) -> None:
        changed, value = imgui.slider_float(label, float(data.get(key, default)), low, high)
        if changed:
            self.change(history_label, lambda: data.__setitem__(key, round(float(value), 3)), coalesce=f"slider:{label}")
        self.seal_if_done()

    @staticmethod
    def _nested(data: dict, key: str) -> dict:
        value = data.get(key)
        return value if isinstance(value, dict) else {}

    def w_nested_int(self, label, data, key, sub, low, high, default, history_label) -> None:
        changed, value = imgui.slider_int(label, int(self._nested(data, key).get(sub, default)), low, high)
        if changed:
            self.change(history_label, lambda: (data.__setitem__(key, self._nested(data, key)) or data[key]).__setitem__(sub, int(value)),
                        coalesce=f"slider:{label}")
        self.seal_if_done()

    def w_nested_float(self, label, data, key, sub, low, high, default, history_label) -> None:
        changed, value = imgui.slider_float(label, float(self._nested(data, key).get(sub, default)), low, high)
        if changed:
            self.change(history_label, lambda: (data.__setitem__(key, self._nested(data, key)) or data[key]).__setitem__(
                sub, round(float(value), 3)), coalesce=f"slider:{label}")
        self.seal_if_done()

    def history_buttons(self) -> None:
        defs = self.defs()
        imgui.begin_disabled(not defs.undo_stack)
        if imgui.small_button("Undo gameplay change"):
            self.undo()
        imgui.end_disabled()
        if defs.undo_stack and imgui.is_item_hovered():
            imgui.set_tooltip(f"Undo: {defs.undo_stack[-1].label} (Ctrl+Z)")
        imgui.same_line()
        imgui.begin_disabled(not defs.redo_stack)
        if imgui.small_button("Redo"):
            self.redo()
        imgui.end_disabled()
        if self.gameplay_error:
            imgui.same_line()
            imgui.text_colored(_colour(RED), self.gameplay_error)

    def tab_map(self) -> None:
        c = self.controller
        project = c.project
        doc = c.document
        info = project.project
        self.w_text("Project name", info, "title", "Rename project", validate=lambda v: projects._text(v, "title"),
                    anchor="gameplay.project-name")
        self.w_text("Description", info, "description", "Edit description", multiline=True)
        imgui.separator()
        imgui.text("Maps in this project")
        for key, entry in sorted(project.maps.items()):
            if imgui.selectable(f"{entry.title} ({'original' if entry.kind == 'blank' else 'from your game'})##map{key}", key == c.active_map)[0]:
                self.switch_map(key)
            if imgui.begin_popup_context_item(f"map-menu-{key}"):
                if imgui.menu_item("Remove map...", "", False, len(project.maps) > 1)[0]:
                    self.confirm("Remove map", f"Remove the map \"{entry.title}\" and everything on it from this project?\n\n"
                                 "This cannot be undone.", "Remove map", lambda k=key: self.remove_map(k))
                imgui.end_popup()
        if imgui.small_button("Add map..."):
            self.show_new_map = True
        if doc is None or c.active_map is None:
            return
        entry = project.maps[c.active_map]
        imgui.separator()
        imgui.text(f"Properties of {entry.title}")
        _, map_title = imgui.input_text("Map name", entry.title)
        if map_title != entry.title:
            self.change("Rename map", lambda: project.set_map_settings(entry.key, title=map_title), coalesce="text:map-title")
        self.seal_if_done()
        bounds = doc.scene.bounds
        if entry.kind == "blank" and bounds:
            half = float(bounds["x"][1])
            sizes = list(hub.MAP_SIZES.values())
            labels = list(hub.MAP_SIZES)
            current = sizes.index(half) if half in sizes else -1
            changed, choice = imgui.combo("Map size", current, labels)
            if changed and choice >= 0:
                self.change("Change map size", lambda: project.set_map_settings(entry.key, sizes[choice]))
            tooltip("The playable area. Objects outside it are refused when placed and reported by validation.")
            imgui.text_disabled(f"Playable area: {bounds['x'][1] - bounds['x'][0]:.0f} x {bounds['z'][1] - bounds['z'][0]:.0f} units. "
                                "Objects outside it are refused or reported.")
            colour = [v / 255 for v in doc.scene.base["environment"][0]["clear_and_fog_colour"]]
            changed, colour = imgui.color_edit3("Sky and fog colour", colour)
            if changed:
                self.change("Change sky colour", lambda: project.set_map_settings(entry.key, None, [int(v * 255) for v in colour]),
                            coalesce="colour:sky")
            self.seal_if_done()
        elif bounds:
            imgui.text_disabled("This map's size and environment come from your game and are shown read-only.")
            imgui.text(f"Playable area x {bounds['x']}  z {bounds['z']}")

    def remove_map(self, key: str) -> None:
        c = self.controller
        if c.active_map == key:
            c.close_map()
        c.project.remove_map(key)
        if c.project.maps and c.active_map is None:
            self.switch_map(sorted(c.project.maps)[0])

    def tab_spawns(self) -> None:
        self.record_list("Spawn points and team starts", ("player_spawn", "team_start", "unit_spawner"),
                         ("player_spawn", "team_start", "unit_generator"),
                         "No spawn points yet. Place a player spawn for missions or team starts for multiplayer.")

    def tab_pickups(self) -> None:
        self.record_list("Pickups and hazards", ("pickup", "powerup_pad", "crate", "hazard"), ("pickup", "hazard"),
                         "No pickups or hazards on this map yet.")

    def tab_objectives(self) -> None:
        self.record_list("Objectives and navigation points", ("objective", "mission_trigger", "navigation_node"), ("objective", "nav_node"),
                         "No objectives or navigation points yet.")
        doc = self.controller.document
        if doc is None:
            return
        nodes = self._records(("navigation_node",))
        if nodes:
            imgui.separator()
            imgui.text("Navigation links (who connects to whom)")
            names = ["none"] + [f"point {n + 1}" for n in range(len(nodes))]
            ids = [None] + [r["id"] for r in nodes]
            for number, node in enumerate(nodes):
                links = sorted(k for k in node.get("fields", {}) if k.startswith("link_"))
                imgui.text(f"point {number + 1}")
                for link in links:
                    imgui.same_line()
                    imgui.set_next_item_width(110)
                    value = node["fields"][link].get("value")
                    current = ids.index(value) if value in ids else 0
                    changed, current = imgui.combo(f"##{node['id']}{link}", current, names)
                    if changed:
                        self.attempt("Navigation link", doc.set_field, node["id"], link, ids[current])
                        self.controller.request_diagnostics()

    def tab_missions(self) -> None:
        c = self.controller
        project = c.project
        imgui.text("Campaigns play their missions in order.")
        for number, campaign in enumerate(project.campaigns):
            index = number
            if number == 0:
                imgui.set_next_item_open(True, imgui.Cond_.once)
            if imgui.tree_node(f"{campaign.get('title')}##camp{campaign.get('id')}"):
                self.w_text(f"Campaign name##{campaign['id']}", campaign, "title", "Rename campaign",
                            validate=lambda v: projects._text(v, "title"))
                missions = list(campaign.get("missions", []))
                for position, mission in enumerate(missions):
                    imgui.text(f"{position + 1}. {project.missions.get(mission, {}).get('title', mission)}")
                    for button, target in (("Up", position - 1), ("Down", position + 1)):
                        imgui.same_line()
                        if imgui.small_button(f"{button}##{campaign['id']}{position}") and 0 <= target < len(missions):
                            self.change("Reorder campaign", lambda i=index, a=position, b=target: self._swap(i, a, b))
                    imgui.same_line()
                    if imgui.small_button(f"Remove##{campaign['id']}{position}"):
                        self.change("Remove mission from campaign",
                                    lambda i=index, p=position: project.campaigns[i]["missions"].pop(p))
                available = [m for m in sorted(project.missions) if m not in missions]
                if available and imgui.begin_combo(f"Add mission##{campaign['id']}", "choose..."):
                    for mission in available:
                        if imgui.selectable(project.missions[mission].get("title", mission), False)[0]:
                            self.change("Add mission to campaign",
                                        lambda i=index, m=mission: project.campaigns[i].setdefault("missions", []).append(m))
                    imgui.end_combo()
                if imgui.small_button(f"Delete campaign##{campaign['id']}"):
                    self.change("Delete campaign", lambda i=index: project.campaigns.pop(i))
                imgui.tree_pop()
        if imgui.small_button("New campaign"):
            number = len(project.campaigns) + 1
            self.change("New campaign", lambda: project.campaigns.append(
                {"id": hub.slug(f"campaign {number}"), "title": f"Campaign {number}", "missions": []}))
        imgui.separator()
        imgui.text("Missions")
        objectives = {r["id"]: r for r in self._records(("objective",))}
        for number, (key, mission) in enumerate(sorted(project.missions.items())):
            if number == 0:
                imgui.set_next_item_open(True, imgui.Cond_.once)
            if imgui.tree_node(f"{mission.get('title', key)}##mis{key}"):
                self.w_text(f"Name##m{key}", mission, "title", "Rename mission", validate=lambda v: projects._text(v, "title"))
                self.map_combo(f"Map##m{key}", mission, "Change mission map")
                self.w_text(f"Briefing##m{key}", mission, "briefing", "Edit briefing", multiline=True)
                tooltip("Text shown to the player before the mission starts.")
                imgui.text("Objectives (from this map's objective markers):")
                chosen = list(mission.get("objectives", []))
                if mission.get("map") == c.active_map:
                    for record_id, record in objectives.items():
                        label = record.get("fields", {}).get("label", {}).get("value") or f"objective {record_id.rsplit(':', 1)[-1]}"
                        changed, on = imgui.checkbox(f"{label}##{key}{record_id}", record_id in chosen)
                        if changed:
                            self.change("Change mission objectives", lambda k=key, r=record_id, v=on: project.missions[k].__setitem__(
                                "objectives", [o for o in project.missions[k].get("objectives", []) if o != r] + ([r] if v else [])))
                    if not objectives:
                        imgui.text_disabled("Place an objective marker on this map first (Objectives & navigation tab).")
                else:
                    imgui.text_disabled(f"{len(chosen)} objective(s). Open the mission's map to change them.")
                if imgui.small_button(f"Delete mission##{key}"):
                    self.confirm("Delete mission", f"Delete the mission \"{mission.get('title', key)}\"? You can undo this.", "Delete",
                                 lambda k=key: self.delete_definition("missions", k))
                imgui.tree_pop()
        if imgui.small_button("New mission"):
            self.new_definition("missions", "Mission", {"map": c.active_map, "objectives": [], "briefing": ""})

    def _swap(self, campaign: int, a: int, b: int) -> None:
        missions = self.controller.project.campaigns[campaign]["missions"]
        missions[a], missions[b] = missions[b], missions[a]

    def map_combo(self, label: str, data: dict, history_label: str = "Change map") -> None:
        maps = sorted(self.controller.project.maps)
        if not maps:
            return
        titles = [self.controller.project.maps[m].title for m in maps]
        current = maps.index(data["map"]) if data.get("map") in maps else 0
        changed, current = imgui.combo(label, current, titles)
        if changed or data.get("map") not in maps:
            self.change(history_label, lambda: data.__setitem__("map", maps[current]))

    def new_definition(self, family: str, noun: str, defaults: dict) -> None:
        project = self.controller.project
        table = getattr(project, family)
        number = len(table) + 1
        while hub.slug(f"{noun} {number}") in table:
            number += 1
        key = hub.slug(f"{noun} {number}")
        self.change(f"New {noun.lower()}", lambda: project.define(family, key, {"title": f"{noun} {number}", **defaults}),
                    context={"key": key})

    def delete_definition(self, family: str, key: str) -> None:
        project = self.controller.project

        def remove():
            del getattr(project, family)[key]
            if family == "missions":
                for campaign in project.campaigns:
                    campaign["missions"] = [m for m in campaign.get("missions", []) if m != key]
            if family == "tanks":
                for gang in project.gangs.values():
                    gang["tanks"] = [t for t in gang.get("tanks", []) if t != key]

        self.change(f"Delete {family.rstrip('s')}", remove, context={"key": key})

    def tab_multiplayer(self) -> None:
        project = self.controller.project
        for key, data in sorted(project.multiplayer.items()):
            if imgui.tree_node(f"{data.get('title', key)}##mp{key}"):
                self.w_text(f"Name##mp{key}", data, "title", "Rename multiplayer setup", validate=lambda v: projects._text(v, "title"))
                self.map_combo(f"Map##mp{key}", data, "Change multiplayer map")
                self.w_slider_int(f"Players##mp{key}", data, "max_players", 2, 4, 4, "Change player count")
                modes = list(data.get("modes", []))
                for mode in hub.MODES:
                    changed, on = imgui.checkbox(f"{mode}##mp{key}{mode}", mode in modes)
                    if changed:
                        self.change("Change game modes", lambda d=data, m=mode, v=on: d.__setitem__(
                            "modes", [x for x in hub.MODES if (x in d.get("modes", [])) != (x == m) or (x == m and v)]))
                starts = len(self._records(("team_start",))) if data.get("map") == self.controller.active_map else None
                if starts is not None and starts < int(data.get("max_players", 4)):
                    imgui.text_colored(_colour(AMBER), f"This map has {starts} team start(s) for {data.get('max_players')} players.")
                if imgui.small_button(f"Delete##mp{key}"):
                    self.confirm("Delete multiplayer setup", f"Delete \"{data.get('title', key)}\"? You can undo this.", "Delete",
                                 lambda k=key: self.delete_definition("multiplayer", k))
                imgui.tree_pop()
        if imgui.small_button("New multiplayer setup"):
            self.new_definition("multiplayer", "Arena", {"map": self.controller.active_map, "modes": ["deathmatch"], "max_players": 4})

    def tab_minigames(self) -> None:
        project = self.controller.project
        for key, data in sorted(project.minigames.items()):
            if imgui.tree_node(f"{data.get('title', key)}##mg{key}"):
                self.w_text(f"Name##mg{key}", data, "title", "Rename minigame", validate=lambda v: projects._text(v, "title"))
                self.map_combo(f"Map##mg{key}", data, "Change minigame map")
                self.w_nested_int(f"Time limit (s)##mg{key}", data, "rules", "time_limit_seconds", 30, 900, 120, "Change time limit")
                self.w_nested_int(f"Targets##mg{key}", data, "rules", "targets", 1, 50, 5, "Change targets")
                imgui.text_disabled("Minigames are a Studio extension; the PC port decides how they play.")
                if imgui.small_button(f"Delete##mg{key}"):
                    self.confirm("Delete minigame", f"Delete \"{data.get('title', key)}\"? You can undo this.", "Delete",
                                 lambda k=key: self.delete_definition("minigames", k))
                imgui.tree_pop()
        if imgui.small_button("New minigame"):
            self.new_definition("minigames", "Minigame", {"map": self.controller.active_map, "rules": {"time_limit_seconds": 120, "targets": 5}})

    def tab_tanks(self) -> None:
        c = self.controller
        project = c.project
        vehicles = [e for e in (c.index.catalog_entries if c.index else []) if e.kind == "model" and "ehicle" in e.category]
        imgui.text("Tanks")
        for key, data in sorted(project.tanks.items()):
            if imgui.tree_node(f"{data.get('title', key)}##tank{key}"):
                self.w_text(f"Name##t{key}", data, "title", "Rename tank", validate=lambda v: projects._text(v, "title"))
                labels = [e.label for e in vehicles]
                ids = [e.id for e in vehicles]
                current = ids.index(data.get("model")) if data.get("model") in ids else -1
                changed, current = imgui.combo(f"Model##t{key}", current, labels)
                if changed and current >= 0:
                    self.change("Change tank model", lambda d=data, m=ids[current]: d.__setitem__("model", m))
                self.w_nested_float(f"Speed##t{key}", data, "stats", "speed", 0.2, 3.0, 1.0, "Change tank speed")
                tooltip("Relative to a standard tank (1.0).")
                self.w_nested_int(f"Armor##t{key}", data, "stats", "armor", 10, 400, 100, "Change tank armor")
                if imgui.small_button(f"Delete##t{key}"):
                    self.confirm("Delete tank", f"Delete the tank \"{data.get('title', key)}\"? Gangs using it lose it. You can undo this.",
                                 "Delete", lambda k=key: self.delete_definition("tanks", k))
                imgui.tree_pop()
        if imgui.small_button("New tank"):
            starter = (c.library.starter() or {}).get("models", {})
            self.new_definition("tanks", "Tank", {"model": starter.get("tank_placeholder"), "stats": {"speed": 1.0, "armor": 100, "weapon": "cannon"}})
        imgui.separator()
        imgui.text("Gangs")
        tooltip("A gang is a team of tanks the player or the computer can field.")
        for key, data in sorted(project.gangs.items()):
            if imgui.tree_node(f"{data.get('title', key)}##gang{key}"):
                self.w_text(f"Name##g{key}", data, "title", "Rename gang", validate=lambda v: projects._text(v, "title"))
                colour = [v / 255 for v in data.get("colour", [128, 128, 128])]
                changed, colour = imgui.color_edit3(f"Colour##g{key}", colour)
                if changed:
                    self.change("Change gang colour", lambda d=data, col=colour: d.__setitem__("colour", [int(v * 255) for v in col]),
                                coalesce=f"colour:{key}")
                self.seal_if_done()
                for tank in sorted(project.tanks):
                    changed, on = imgui.checkbox(f"{project.tanks[tank].get('title', tank)}##g{key}{tank}", tank in data.get("tanks", []))
                    if changed:
                        self.change("Change gang tanks", lambda d=data, t=tank: d.__setitem__("tanks", sorted(set(d.get("tanks", [])) ^ {t})))
                if imgui.small_button(f"Delete##g{key}"):
                    self.confirm("Delete gang", f"Delete the gang \"{data.get('title', key)}\"? You can undo this.", "Delete",
                                 lambda k=key: self.delete_definition("gangs", k))
                imgui.tree_pop()
        if imgui.small_button("New gang"):
            self.new_definition("gangs", "Gang", {"colour": [120, 120, 200], "tanks": []})

    def tab_minimap(self) -> None:
        c = self.controller
        if c.document is None or c.active_map is None:
            imgui.text_disabled("Open a map first.")
            return
        entry = c.project.maps[c.active_map]
        config = entry.minimap
        if entry.kind == "blank":
            imgui.text_wrapped("Original maps get a generated overhead minimap. Choose its size in pixels; the preview is in the "
                               "Minimap panel.")
            self.w_slider_int("Width (pixels)", config, "width", 16, 72, 64, "Change minimap width")
            self.w_slider_int("Height (pixels)", config, "height", 16, 75, 64, "Change minimap height")
            imgui.text_disabled("Studio keeps generated minimaps within 72 x 75 pixels.")
        else:
            imgui.text_wrapped("This map uses the minimap from your game. Its alignment values are shown read-only in the Inspector "
                               "(click empty space in the viewport) because writing them back is not yet supported.")
        _, c.preferences.layers["minimap"] = imgui.checkbox("Show the minimap area in the viewport", c.preferences.layers.get("minimap", False))

    def tab_prefabs(self) -> None:
        c = self.controller
        doc = c.document
        imgui.text_wrapped("A prefab is a group of objects you can reuse in any map, for example a guard post or a checkpoint.")
        _, self.prefab_title = imgui.input_text("Name", self.prefab_title)
        imgui.same_line()
        imgui.begin_disabled(not (doc and doc.selection))
        if imgui.button("Save selection as prefab"):
            self.save_prefab()
        imgui.end_disabled()
        prefabs, problems = self.prefabs.list()
        for problem in problems:
            imgui.text_colored(_colour(AMBER), problem)
        if not prefabs:
            imgui.text_disabled("No prefabs yet. Select some objects and save them here.")
        for prefab in prefabs:
            imgui.bullet_text(f"{prefab.title} ({prefab.records} objects)")
            imgui.same_line()
            imgui.begin_disabled(doc is None)
            if imgui.small_button(f"Place at view centre##{prefab.key}"):
                self.place_prefab(prefab)
            imgui.end_disabled()
            imgui.same_line()
            if imgui.small_button(f"Delete##{prefab.key}"):
                self.confirm("Delete prefab", f"Delete the prefab \"{prefab.title}\"? Maps that used it keep their objects. You can undo this.",
                             "Delete", lambda k=prefab.key: self.delete_prefab(k))

    def _prefab_text(self, key: str) -> tuple[Path, str | None]:
        path = self.prefabs.directory / f"{key}.json"
        try:
            return path, path.read_text(encoding="utf-8")
        except OSError:
            return path, None

    def save_prefab(self) -> None:
        text = self.controller.copy()
        if not text:
            return
        key = hub.slug(self.prefab_title, "prefab")
        path, before = self._prefab_text(key)
        prefab = self.attempt("Save prefab", self.prefabs.save, self.prefab_title, text)
        if prefab:
            self.defs().record_file("Save prefab", path, before, self._prefab_text(key)[1], {"tab": "Prefabs"})
            self.controller.log("info", f"prefab saved: {prefab.title}")

    def delete_prefab(self, key: str) -> None:
        path, before = self._prefab_text(key)
        self.prefabs.delete(key)
        if before is not None:
            self.defs().record_file("Delete prefab", path, before, None, {"tab": "Prefabs"})

    def place_prefab(self, prefab) -> None:
        c = self.controller
        missing = PrefabLibrary.missing_models(prefab, c.library)
        if missing:
            self.report_error("This prefab cannot be placed here",
                              f"It uses {len(missing)} model(s) from a game that is not registered. Add that game in the Source Manager.")
            return
        point = c.hover_point if c.hover_point is not None else self.view_centre_point()
        result = c.run("place prefab", c.document.paste, prefab.clipboard, point)
        if result is None:
            self.report_error("The prefab could not be placed", (c.messages[-1][1] if c.messages else "unknown reason"))

    def view_centre_point(self):
        c = self.controller
        return c.placement_point(c.viewport[0] / 2, c.viewport[1] / 2)

    # -- settings, about, help ------------------------------------------------------------------
    def window_settings(self) -> None:
        self.bring_to_front("settings", self.show_settings)
        if not self.show_settings:
            return
        prefs = self.settings.preferences
        imgui.set_next_window_size(imgui.ImVec2(720, 0), imgui.Cond_.appearing)
        opened, self.show_settings = imgui.begin("Settings###settings", self.show_settings, imgui.WindowFlags_.no_collapse)
        if opened:
            imgui.text("Where Studio keeps things")
            if imgui.begin_table("locations", 3, imgui.TableFlags_.sizing_stretch_prop):
                for label, path in self.settings.locations():
                    imgui.table_next_row()
                    imgui.table_next_column()
                    imgui.text(label)
                    imgui.table_next_column()
                    imgui.text_disabled(path.name)
                    tooltip(str(path))
                    imgui.table_next_column()
                    if imgui.small_button(f"Open##{label}"):
                        self.safe_desktop(desktop.open_folder, path)
                imgui.end_table()
            if self.settings.portable:
                imgui.text_colored(_colour(AMBER), "Portable mode: everything is stored beside the program.")
            imgui.separator()
            if imgui.button("Open logs folder"):
                self.safe_desktop(desktop.open_folder, self.settings.logs_directory)
            imgui.same_line()
            if imgui.button("Clear thumbnail cache..."):
                self.confirm("Clear thumbnail cache", "Delete all cached thumbnails? They are recreated when needed.", "Clear",
                             self.clear_thumbnails)
            imgui.same_line()
            if imgui.button("Reset window layout"):
                self.reset_layout()
            imgui.separator()
            imgui.text("Saving")
            _, prefs.autosave_enabled = imgui.checkbox("Keep a recovery copy of unsaved work", prefs.autosave_enabled)
            _, prefs.autosave_seconds = imgui.slider_int("Every (seconds)", int(prefs.autosave_seconds), 30, 900)
            if imgui.button("Default project folder..."):
                chosen = self.dialogs.select_folder("Default folder for new projects", self.settings.projects_directory)
                if chosen:
                    prefs.projects_directory = str(chosen)
            self.install_section()
            imgui.separator()
            imgui.text("Updates")
            imgui.text_disabled("Studio does not update itself. Check the Releases page for new versions.")
            if imgui.button("Open the Releases page"):
                self.safe_desktop(desktop.open_url, desktop.RELEASES_URL)
        imgui.end()

    def install_section(self) -> None:
        from tools.btga_studio.core import environment, installer

        env = environment.detect()
        imgui.separator()
        imgui.text("Installation")
        imgui.text_disabled(env.label)
        if env.mode == "installed":
            imgui.text_disabled("Remove it from Windows Settings > Apps. Your projects and settings are kept.")
            return
        if env.mode != "portable" or sys.platform != "win32":
            return
        imgui.text_disabled("You can keep using this folder, or install for your user account (Start menu entry and Apps listing).")
        if imgui.button("Install for this user..."):
            payload = installer.Payload.from_folder(env.program, env.version)
            self.confirm("Install BattleTanx Studio",
                         "Copy Studio to your AppData folder, add a Start menu entry and list it in Windows Settings > Apps? "
                         "No administrator rights are needed. Your projects and settings stay where they are.", "Install",
                         lambda: self.controller.jobs.submit(
                             "Installing BattleTanx Studio",
                             lambda context: installer.install(payload, installer.default_target(), installer.WindowsSystem(),
                                                               progress=context.progress), "install", self._installed))

    def _installed(self, job) -> None:
        if job.state == "done":
            self.inform("Installed", "BattleTanx Studio is installed. Start it from the Start menu next time; this folder can be deleted.")
        else:
            self.report_error("Installation did not finish", job.error or "cancelled")

    def clear_thumbnails(self) -> None:
        import shutil

        if self.thumbnails:
            self.thumbnails.shutdown()
        shutil.rmtree(self.settings.thumbnail_directory, ignore_errors=True)
        self.thumb_textures.clear()
        self.attach_library()
        self.controller.log("info", "thumbnail cache cleared")

    def reset_layout(self) -> None:
        runner = self.runner()
        if runner is not None:
            runner.docking_params.layout_reset = True
        self.layout_reset_requested = True
        self.controller.log("info", "window layout reset")

    def window_about(self) -> None:
        if not self.show_about:
            return
        from tools.btga_studio.core import environment

        imgui.open_popup("About BattleTanx Studio")
        imgui.set_next_window_size(imgui.ImVec2(620, 0), imgui.Cond_.appearing)
        if imgui.begin_popup_modal("About BattleTanx Studio", None, imgui.WindowFlags_.always_auto_resize)[0]:
            env = self.environment or environment.detect()
            imgui.text(f"BattleTanx Studio {env.version}")
            imgui.text_disabled(f"Build revision: {env.revision}")
            imgui.text_colored(_colour(GREEN), env.label)
            if imgui.begin_table("about-locations", 3, imgui.TableFlags_.sizing_fixed_fit):
                rows = [("Program", env.program)] + [(label, path) for label, path in self.settings.locations()
                                                       if label in ("Settings", "Local data (catalogs, caches)", "Logs", "Thumbnail cache")]
                for label, path in rows:
                    imgui.table_next_row()
                    imgui.table_next_column()
                    imgui.text(label)
                    imgui.table_next_column()
                    imgui.text_disabled(Path(path).name)
                    tooltip(str(path))
                    imgui.table_next_column()
                    if imgui.small_button(f"Open##about{label}"):
                        self.safe_desktop(desktop.open_folder, path)
                imgui.end_table()
            imgui.push_text_wrap_pos(600)
            imgui.text_wrapped(env.repair_guidance)
            imgui.text_wrapped("Studio contains no game data. It reads game files you own only on your computer.")
            imgui.text_disabled("Third-party components and licences are listed in THIRD-PARTY-NOTICES.txt beside the program.")
            imgui.pop_text_wrap_pos()
            if imgui.button("Releases page"):
                self.safe_desktop(desktop.open_url, desktop.RELEASES_URL)
            imgui.same_line()
            if imgui.button("Copy version information"):
                imgui.set_clipboard_text(json.dumps({"version": env.version, "revision": env.revision, "mode": env.mode}, sort_keys=True))
            imgui.same_line()
            if imgui.button("Close") or imgui.is_key_pressed(imgui.Key.escape):
                self.show_about = False
                imgui.close_current_popup()
            imgui.end_popup()

    def open_help(self, topic: int = 0) -> None:
        self.show_help = True
        self.help_topic = topic

    def window_help(self) -> None:
        self.bring_to_front("help", self.show_help)
        if not self.show_help:
            return
        imgui.set_next_window_size(imgui.ImVec2(780, 520), imgui.Cond_.appearing)
        opened, self.show_help = imgui.begin("Help###help", self.show_help, imgui.WindowFlags_.no_collapse)
        if opened:
            imgui.begin_child("topics", imgui.ImVec2(200, 0))
            for number, (title, _) in enumerate(TOPICS):
                if imgui.selectable(title, number == self.help_topic)[0]:
                    self.help_topic = number
            imgui.end_child()
            imgui.same_line()
            imgui.begin_child("text")
            imgui.text_wrapped(TOPICS[self.help_topic][1])
            imgui.end_child()
        imgui.end()

    def report_problem(self) -> None:
        from tools.btga_studio.core.atomic import atomic_write_text

        report = applog.problem_report(self.settings.logs_directory, __version__)
        path = self.settings.logs_directory / time.strftime("problem-report-%Y%m%d-%H%M%S.txt")
        atomic_write_text(path, report)
        self.controller.log("info", f"problem report saved as {path.name} in the logs folder")
        self.safe_desktop(desktop.open_folder, path.parent)
        self.safe_desktop(desktop.open_url, desktop.ISSUES_URL)

    # -- export ------------------------------------------------------------------------------------
    def window_export(self) -> None:
        if self.export_result is None:
            return
        imgui.open_popup("Export###export")
        if imgui.begin_popup_modal("Export###export", None, imgui.WindowFlags_.always_auto_resize)[0]:
            result = self.export_result
            imgui.text_colored(_colour(GREEN), "Content package exported")
            imgui.text(result["name"])
            imgui.text_disabled(f"{human_size(result['size'])}, {result['maps']} map(s), checked and readable")
            imgui.text_wrapped("The package holds your project and Studio's original content only. Game content is referenced, "
                               "never included, so players need their own copy of the game.")
            if imgui.button("Show in folder"):
                self.safe_desktop(desktop.open_folder, Path(result["path"]).parent)
            imgui.same_line()
            if imgui.button("Close") or imgui.is_key_pressed(imgui.Key.escape):
                self.export_result = None
                imgui.close_current_popup()
            imgui.end_popup()

    # -- toolbar, status bar and context menus --------------------------------------------------
    def toolbar(self) -> None:
        c = self.controller
        doc = c.document
        for tool, label, key, hint in TOOLS:
            active = self.gizmo_operation == tool
            if active:
                imgui.push_style_color(imgui.Col_.button, imgui.get_style_color_vec4(imgui.Col_.button_active))
            if imgui.button(label):
                self.gizmo_operation = tool
            self.anchor(f"toolbar.{tool}")
            if active:
                imgui.pop_style_color()
            tooltip(f"{hint}\nShortcut: {key}")
            imgui.same_line()
        imgui.text_disabled("|")
        imgui.same_line()
        if imgui.button("Place"):
            imgui.set_window_focus("Assets")
            self.controller.log("info", "choose an asset in the Assets panel, then click in the viewport")
        self.anchor("toolbar.place")
        tooltip("Choose something in the Assets panel, then click in the viewport. Esc stops placing.")
        for label, action, shortcut, enabled in (("Duplicate", c.duplicate, "Ctrl+D", bool(doc and doc.selection)),
                                                 ("Delete", c.delete, "Del", bool(doc and doc.selection)),
                                                 ("Undo", self.undo, "Ctrl+Z", c.can_undo),
                                                 ("Redo", self.redo, "Ctrl+Y", c.can_redo)):
            imgui.same_line()
            imgui.begin_disabled(not enabled)
            if imgui.button(label):
                action()
            imgui.end_disabled()
            tooltip(f"{label} ({shortcut})")
        imgui.same_line()
        imgui.text_disabled("|")
        imgui.same_line()
        if imgui.button("Save"):
            self.save()
        self.anchor("toolbar.save")
        tooltip("Save the project (Ctrl+S)")

    def status_text(self) -> str:
        c = self.controller
        doc = c.document
        prefs = self.settings.preferences
        tool = next((label for key, label, _, _ in TOOLS if key == self.gizmo_operation), self.gizmo_operation)
        if c.placement is not None:
            tool = "Place"
        errors = sum(1 for d in c.diagnostics if d.severity == "error")
        warnings = len(c.diagnostics) - errors
        snap = f"grid {prefs.grid_snap:g}" if prefs.grid_snap else "grid off"
        snap += f", angle {prefs.yaw_snap_degrees}°" if prefs.yaw_snap_degrees else ", angle off"
        saved = "No project" if c.project is None else ("Unsaved changes" if c.dirty else "All changes saved")
        validation = "valid" if not errors and not warnings else f"{errors} error(s), {warnings} warning(s)"
        jobs = len(c.jobs.active)
        parts = [f"Tool: {tool}", f"Selected: {len(doc.selection) if doc else 0}", f"Snap: {snap}", f"Validation: {validation}", saved]
        if jobs:
            parts.append(f"{jobs} background task(s)")
        return "   |   ".join(parts)

    def record_context_menu(self, record_id: str | None) -> None:
        """Shared actions for the viewport and outline context menus."""

        c = self.controller
        doc = c.document
        if doc is None:
            return
        if record_id and record_id not in doc.selection and doc.scene.record(record_id):
            doc.select(record_id)
        has = bool(doc.selection)
        if imgui.menu_item("Focus", "F", False, has)[0]:
            c.frame_selection()
        if imgui.menu_item("Duplicate", "Ctrl+D", False, has)[0]:
            c.duplicate()
        if imgui.menu_item("Copy", "Ctrl+C", False, has)[0]:
            self.copy()
        if imgui.menu_item("Paste here", "Ctrl+V", False, bool(c.clipboard))[0]:
            c.paste()
        imgui.separator()
        if imgui.menu_item("Hide", "H", False, has)[0]:
            doc.hide()
        if imgui.menu_item("Isolate", "I", False, has)[0]:
            doc.isolate()
        if imgui.menu_item("Lock", "L", False, has)[0]:
            doc.lock()
        if imgui.menu_item("Show all", "U", False, bool(doc.hidden))[0]:
            doc.unhide_all()
        if imgui.menu_item("Unlock all", "", False, bool(doc.locked))[0]:
            doc.unlock_all()
        imgui.separator()
        if imgui.menu_item("Save as prefab...", "", False, has)[0]:
            self.open_gameplay("Prefabs")
        if imgui.menu_item("Delete", "Del", False, has)[0]:
            c.delete()
